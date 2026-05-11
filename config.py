# -*- coding: utf-8 -*-
"""
config.py — Application configuration and proxy management.

Extended settings (proxy list, drop-detection thresholds) are loaded from
.env.json.  Credentials (API_ID, API_HASH, PHONE, etc.) stay in .env.

Proxy rotation flow:
  ProxyManager   — holds the list, tracks the active index, builds kwargs.
  ConnectionDropMonitor  — logging.Handler attached to the Telethon connection
                           logger; counts "Server closed the connection" events.
  ProxySwitchNeeded      — exception raised by the watchdog when the threshold
                           is exceeded; caught in cmd_download to reconnect.

Usage:
    from config import load_app_config, ProxyManager, ConnectionDropMonitor, ProxySwitchNeeded

    config = load_app_config()
    proxy_manager = ProxyManager(config.proxies)
    monitor = ConnectionDropMonitor(
        max_drops=config.proxy_switch_max_drops,
        window_secs=config.proxy_switch_window_secs,
    )
    monitor.attach()
"""

import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

import socks
import TelethonFakeTLS
from pydantic import BaseModel
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

ENV_JSON_PATH: Path = Path(".env.json")

_logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ProxyConfig(BaseModel):
    """Single proxy server configuration."""

    type: str           # "mtproto" | "socks5" | "socks4"
    host: str
    port: int
    secret: str = ""    # MTProxy secret (hex); empty for SOCKS proxies.

    model_config = {"extra": "forbid"}


class AppConfig(BaseModel):
    """Extended application configuration loaded from .env.json."""

    proxies: list[ProxyConfig] = []
    # How many connection drops in the rolling window before we switch proxy.
    proxy_switch_max_drops: int = 3
    # Rolling window duration in seconds.
    proxy_switch_window_secs: float = 300.0
    # Ad / spam filter rules.
    ad_filter: "AdFilterConfig" = None  # type: ignore[assignment]

    model_config = {"extra": "allow"}

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        if self.ad_filter is None:
            object.__setattr__(self, "ad_filter", AdFilterConfig())


class AdFilterConfig(BaseModel):
    """
    Configuration for the ad/spam filter applied in join_video.py.

    All text lists are case-insensitive.  An empty list or zero value disables
    the corresponding rule group.

    Fields:
      ban_text_contains       — ban if post text contains any of these substrings.
      ban_text_regex          — ban if post text matches any of these regexes.
      ban_channel_mentions    — ban if post text mentions any of these @channels
                                or t.me/<channel> links.
      ban_min_views           — ban posts with fewer views than this value (0 = off).
      ban_min_duration_sec    — ban clips shorter than this many seconds (0 = off).
      ban_max_duration_sec    — ban clips longer than this many seconds (0 = off).
      ban_max_file_size_mb    — ban clips larger than this many megabytes (0 = off).
      ban_require_text        — ban posts that have no text at all (text.txt absent
                                or empty). false = off.
      ban_llm_ad_rate_threshold  — ban posts whose ad_check.ad_rate >= threshold
                                   (0.0 = disabled). Default 0.85.
      ban_nude_rate_threshold — ban video posts whose nude_check.nude_rate >= threshold
                                (0.0 = disabled). Default 0.5. Override via .env
                                NUDE_RATE_THRESHOLD or CLI --nude-rate-threshold.
    """

    ban_text_contains: list[str] = []
    ban_text_regex: list[str] = []
    ban_channel_mentions: list[str] = []
    ban_min_views: int = 0
    ban_min_duration_sec: float = 0.0
    ban_max_duration_sec: float = 0.0
    ban_max_file_size_mb: float = 0.0
    ban_require_text: bool = False
    # Ban posts whose LLM ad_check.ad_rate >= this threshold (0.0 = disabled).
    # Populated by check_ad.py update; posts without ad_check are never banned
    # by this rule regardless of the threshold.
    ban_llm_ad_rate_threshold: float = 0.85
    # Ban video posts whose nude_check.nude_rate >= this threshold (0.0 = disabled).
    # Populated by check_ad.py update-nudes; posts without nude_check are never
    # banned by this rule.  Override via .env NUDE_RATE_THRESHOLD or
    # CLI --nude-rate-threshold.
    ban_nude_rate_threshold: float = 0.5

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_app_config(path: Path = ENV_JSON_PATH) -> AppConfig:
    """
    Load AppConfig from .env.json.

    Returns an AppConfig with default values when the file does not exist or
    cannot be parsed, so the application starts without .env.json.
    """
    if not path.exists():
        _logger.debug(f"{path} not found — using default AppConfig")
        return AppConfig()
    try:
        data: dict = json.loads(path.read_text(encoding="utf-8"))
        config = AppConfig(**data)
        _logger.info(
            f"Loaded {path}: {len(config.proxies)} proxy(ies), "
            f"drop threshold {config.proxy_switch_max_drops} in "
            f"{config.proxy_switch_window_secs}s"
        )
        return config
    except Exception as exc:
        _logger.error(f"Failed to parse {path}: {exc} — using defaults")
        return AppConfig()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_kwargs_for_proxy(proxy: ProxyConfig) -> dict:
    """
    Build TelegramClient extra kwargs for a single ProxyConfig.

      MTProxy, secret starts with 'ee' → FakeTLS (TelethonFakeTLS).
      MTProxy, any other secret        → RandomizedIntermediate.
      SOCKS5 / SOCKS4                  → PySocks proxy tuple.
    """
    ptype: str = proxy.type.lower().strip()
    if ptype == "socks5":
        return {"proxy": (socks.SOCKS5, proxy.host, proxy.port)}
    if ptype == "socks4":
        return {"proxy": (socks.SOCKS4, proxy.host, proxy.port)}
    if ptype == "mtproto":
        if proxy.secret.lower().startswith("ee"):
            return {
                "connection": TelethonFakeTLS.ConnectionTcpMTProxyFakeTLS,
                "proxy": (proxy.host, proxy.port, proxy.secret[2:]),
            }
        return {
            "connection": ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (proxy.host, proxy.port, proxy.secret),
        }
    raise ValueError(
        f"Unsupported proxy type: '{ptype}'. Supported values: mtproto, socks5, socks4"
    )


def build_client_kwargs_from_env() -> dict:
    """
    Build TelegramClient extra kwargs from .env single-proxy settings.

    Used as fallback when no proxies are defined in .env.json.
    Returns an empty dict when PROXY_TYPE is not set.
    """
    proxy_type: str = os.getenv("PROXY_TYPE", "").lower().strip()
    if not proxy_type:
        return {}
    host: Optional[str] = os.getenv("PROXY_HOST")
    port_str: Optional[str] = os.getenv("PROXY_PORT")
    if not host or not port_str:
        raise ValueError("PROXY_HOST and PROXY_PORT must be set when PROXY_TYPE is defined")
    proxy = ProxyConfig(
        type=proxy_type,
        host=host,
        port=int(port_str),
        secret=os.getenv("PROXY_SECRET", ""),
    )
    return _build_kwargs_for_proxy(proxy)


# ---------------------------------------------------------------------------
# Proxy switch signal
# ---------------------------------------------------------------------------

class ProxySwitchNeeded(Exception):
    """
    Raised by the proxy watchdog when connection drops exceed the configured
    threshold.  Signals cmd_download to disconnect, rotate the proxy, and
    recreate the TelegramClient.
    """


# ---------------------------------------------------------------------------
# ProxyManager
# ---------------------------------------------------------------------------

class ProxyManager:
    """
    Round-robin manager for a list of proxy servers.

    Falls back to .env single-proxy settings (PROXY_HOST / PROXY_PORT /
    PROXY_SECRET) when the proxies list is empty.
    """

    def __init__(self, proxies: list[ProxyConfig]) -> None:
        self._proxies: list[ProxyConfig] = list(proxies)
        self._index: int = 0

    @property
    def count(self) -> int:
        """Number of configured proxies."""
        return len(self._proxies)

    @property
    def current(self) -> Optional[ProxyConfig]:
        """Currently active proxy, or None when the list is empty."""
        if not self._proxies:
            return None
        return self._proxies[self._index]

    def advance(self) -> Optional[ProxyConfig]:
        """
        Rotate to the next proxy (circular).

        Returns the new current proxy, or None when the list is empty.
        Logs the rotation so it is traceable in the log file.
        """
        if not self._proxies:
            return None
        prev: ProxyConfig = self._proxies[self._index]
        self._index = (self._index + 1) % len(self._proxies)
        current: ProxyConfig = self._proxies[self._index]
        _logger.info(
            f"Proxy rotated: {prev.host}:{prev.port} -> {current.host}:{current.port}"
        )
        return current

    def build_client_kwargs(self) -> dict:
        """Build TelegramClient kwargs for the currently active proxy."""
        if self.current is None:
            return build_client_kwargs_from_env()
        return _build_kwargs_for_proxy(self.current)

    def describe_current(self) -> str:
        """Human-readable identifier for the current proxy."""
        if self.current is None:
            return "env proxy (no .env.json list)"
        return f"{self.current.type}://{self.current.host}:{self.current.port}"


# ---------------------------------------------------------------------------
# ConnectionDropMonitor
# ---------------------------------------------------------------------------

class ConnectionDropMonitor(logging.Handler):
    """
    Logging handler that counts 'Server closed the connection' events emitted
    by the Telethon connection layer.

    Attach it to the Telethon connection logger via attach().  The handler
    records timestamps of matching log records and exposes should_switch() to
    check whether the rolling-window threshold has been reached.

    Example:
        monitor = ConnectionDropMonitor(max_drops=3, window_secs=300)
        monitor.attach()
        ...
        if monitor.should_switch():
            raise ProxySwitchNeeded()
        monitor.reset()   # after switching proxy
        monitor.detach()  # on teardown
    """

    _TELETHON_LOGGER: str = "telethon.network.connection.connection"
    _DROP_PATTERN: str = "Server closed the connection"

    def __init__(self, max_drops: int = 3, window_secs: float = 300.0) -> None:
        super().__init__(level=logging.WARNING)
        self._max_drops: int = max_drops
        self._window_secs: float = window_secs
        self._timestamps: deque[float] = deque()

    # -- logging.Handler interface ------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        if self._DROP_PATTERN in record.getMessage():
            now: float = time.monotonic()
            self._timestamps.append(now)
            self._evict_old(now)
            _logger.debug(
                f"Connection drop #{len(self._timestamps)} recorded "
                f"(threshold: {self._max_drops} per {self._window_secs}s)"
            )

    # -- Internal -----------------------------------------------------------

    def _evict_old(self, now: float) -> None:
        cutoff: float = now - self._window_secs
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    # -- Public API ---------------------------------------------------------

    @property
    def drop_count(self) -> int:
        """Number of drops within the current rolling window."""
        self._evict_old(time.monotonic())
        return len(self._timestamps)

    def should_switch(self) -> bool:
        """Return True when the drop count has reached the configured maximum."""
        return self.drop_count >= self._max_drops

    def reset(self) -> None:
        """Clear all recorded drops.  Call immediately after switching proxy."""
        self._timestamps.clear()
        _logger.debug("ConnectionDropMonitor: counter reset")

    def attach(self) -> None:
        """Start intercepting Telethon connection-level log records."""
        logging.getLogger(self._TELETHON_LOGGER).addHandler(self)
        _logger.debug(f"ConnectionDropMonitor attached to '{self._TELETHON_LOGGER}'")

    def detach(self) -> None:
        """Stop intercepting log records."""
        logging.getLogger(self._TELETHON_LOGGER).removeHandler(self)
        _logger.debug("ConnectionDropMonitor detached")

