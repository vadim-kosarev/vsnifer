# -*- coding: utf-8 -*-
"""
mcp_server.py - MCP server for the vsnifer project.

Provides FastMCP tools for reading and classifying Telegram channel posts
stored in WORK_DIR. Exposes a streamable-HTTP endpoint at:
    http://<MCP_HOST>:<MCP_PORT>/mcp

Usage:
    python mcp_server.py run
    python mcp_server.py run --port 8080
    python mcp_server.py --help

Environment variables (from .env):
    WORK_DIR   - path to directory with downloaded channel posts (default: work)
    MCP_HOST   - server bind address (default: 0.0.0.0)
    MCP_PORT   - server port (default: 3100)
    LOG_LEVEL  - logging level (default: INFO)

WORK_DIR layout expected:
    WORK_DIR/
      <channel_name>/
        <post_id>/
          meta.json
          text.txt
"""

# ---------------------------------------------------------------------------
# CLI parsing — stdlib only, must come before any heavy import.
# This allows --help and invalid-arg detection without loading fastmcp,
# pydantic, dotenv, or any other non-stdlib dependency.
# ---------------------------------------------------------------------------

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. No external deps allowed here."""
    parser = argparse.ArgumentParser(
        prog="mcp_server",
        description="vsnifer MCP server — FastMCP tools for Telegram post classification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python mcp_server.py run
  python mcp_server.py run --port 8080
  python mcp_server.py run --host 127.0.0.1 --port 8080
  python mcp_server.py list-tools
        """,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    run_parser = subparsers.add_parser(
        "run",
        help="Start the MCP server (streamable-HTTP transport).",
    )
    run_parser.add_argument(
        "--host",
        default=None,
        metavar="HOST",
        help="Bind address. Overrides MCP_HOST env var (default: '0.0.0.0').",
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="Port number. Overrides MCP_PORT env var (default: 3100).",
    )
    run_parser.add_argument(
        "--mode",
        default=None,
        choices=["default", "flowise"],
        metavar="MODE",
        help=(
            "Response format mode. "
            "'default': content[0].text=summary, structuredContent=data dict. "
            "'flowise': content=auto-JSON (FastMCP), structuredContent=clean JSON object. "
            "Overrides MCP_MODE env var (default: 'default')."
        ),
    )

    subparsers.add_parser(
        "list-tools",
        help="Print all registered MCP tools with their descriptions and exit.",
    )

    return parser


# Early exit: print help and quit before loading any heavy dependency.
if __name__ == "__main__":
    _parser = _build_parser()
    _args = _parser.parse_args()
    if not _args.command:
        _parser.print_help()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Heavy imports — only reached when a real command was supplied.
# ---------------------------------------------------------------------------

import json
import logging
import os
from pathlib import Path
from typing import Annotated, Optional

import pydantic_core
import mcp.types as mcp_types
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, BeforeValidator, model_validator

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORK_DIR: Path = Path(os.getenv("WORK_DIR", "work"))
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "3100"))
MCP_MODE: str = os.getenv("MCP_MODE", "default")   # "default" | "flowise"
AD_THRESHOLD: float = 0.85

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL: int = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
LOGS_DIR: Path = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

_log_name: str = Path(__file__).stem
logger: logging.Logger = logging.getLogger(_log_name)
logger.setLevel(LOG_LEVEL)

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_file_handler = logging.FileHandler(LOGS_DIR / f"{_log_name}.log", encoding="utf-8")
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)
logger.addHandler(_console_handler)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AdCheckInfo(BaseModel):
    """LLM classification result stored in meta.json."""

    ad_rate: float
    proof_of_ad: str


class PostInfo(BaseModel):
    """Post metadata without text content."""

    id: str                       # "channel/post_id"
    channel: str
    post_id: int
    date: Optional[str]
    has_media: bool
    views: int
    reactions_total: int
    ad_check: Optional[AdCheckInfo]


class PostFull(PostInfo):
    """Full post data including text content and per-emoji reactions."""

    reactions: dict[str, int]
    text: str                     # empty string when text.txt is absent


class ChannelStats(BaseModel):
    """Aggregated statistics for a single channel."""

    channel: str
    total_posts: int
    checked: int
    unchecked: int
    ad_count: int       # checked posts with ad_rate >= AD_THRESHOLD
    clean_count: int    # checked posts with ad_rate < AD_THRESHOLD


class AdCheckInput(BaseModel):
    """Input record for set_ad_check_batch.

    Accepts two equivalent formats:
      - {channel, post_id, ad_rate, proof_of_ad}   — canonical
      - {id, ad_rate, proof_of_ad}                 — composite id like "babazoyka/4610"
      - {post_id, ad_rate, proof_of_ad}            — bare post_id (channel supplied by set_ad_check_batch)
    proof_of_ad is coerced to str (agent may send bool/None).
    """

    channel: Optional[str] = None
    post_id: int
    ad_rate: float
    proof_of_ad: str

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        d = dict(data)

        # Split composite "id" → channel + post_id
        if "id" in d and ("channel" not in d or "post_id" not in d):
            raw_id = str(d.pop("id"))
            parts = raw_id.split("/", 1)
            if len(parts) == 2 and parts[1].isdigit():
                d.setdefault("channel", parts[0])
                d.setdefault("post_id", int(parts[1]))
            else:
                d["id"] = raw_id  # leave as-is so Pydantic reports the error

        # Coerce proof_of_ad to str (agent may send bool, None, int, …)
        if "proof_of_ad" in d and not isinstance(d["proof_of_ad"], str):
            v = d["proof_of_ad"]
            d["proof_of_ad"] = "" if v is None or v is False else str(v)

        return d


class SetAdCheckResult(BaseModel):
    """Result of a single ad-check write operation."""

    ok: bool
    id: str
    error: Optional[str] = None


class BatchResult(BaseModel):
    """Aggregated result of set_ad_check_batch."""

    written: int
    errors: list[str]
    ids: list[str]


class ChannelInfo(BaseModel):
    """Channel entry from the CHANNELS whitelist."""

    name: str   # human-readable identifier (username or invite hash)
    url: str    # canonical t.me URL


class ChannelListResult(BaseModel):
    """Response model for tools returning a list of channels."""

    channels: list[ChannelInfo]
    total: int


class StatsResult(BaseModel):
    """Response model for get_stats."""

    stats: list[ChannelStats]
    total_channels: int


class PostListResult(BaseModel):
    """Response model for tools returning a list of posts."""

    posts: list[PostFull]
    total: int
    channel: Optional[str] = None


# ---------------------------------------------------------------------------
# Serializers: produce human-readable content[0].text for MCP clients (Flowise etc.)
# structuredContent is filled automatically by FastMCP from the return type.
# ---------------------------------------------------------------------------

def _make_tool_result(data: BaseModel, summary: str) -> ToolResult:
    """
    Build a ToolResult respecting MCP_MODE:

      default — content[0].text = human-readable summary,
                structuredContent = typed data dict (for programmatic clients).

      flowise — content=None (FastMCP auto-generates content[0].text = compact JSON),
                structuredContent = clean JSON object (no escaping, proper dict).
                Flowise reads structuredContent as a native JSON object.
    """
    data_dict = pydantic_core.to_jsonable_python(data)
    if MCP_MODE == "flowise":
        return ToolResult(
            content=None,
            structured_content=data_dict,
        )
    return ToolResult(
        content=[mcp_types.TextContent(type="text", text=summary)],
        structured_content=data_dict,
    )


def _coerce_json_list(v: object, *, parse_items: bool = False) -> object:
    """
    Robustly coerce an LLM-supplied value into a Python list.

    Handles:
      - JSON-encoded string  → list  (e.g. '["a","b"]')
      - list of JSON strings → list of dicts/values  (when parse_items=True)
      - native list          → returned as-is (items optionally decoded)
    """
    # Outer value may be a JSON-encoded string
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                v = parsed
            else:
                return v          # not a list — let Pydantic report the error
        except Exception:
            return v

    # Optionally decode each item (for list[SomeModel] where agent sends list[str])
    if parse_items and isinstance(v, list):
        result = []
        for item in v:
            if isinstance(item, str):
                try:
                    result.append(json.loads(item))
                    continue
                except Exception:
                    pass
            result.append(item)
        return result

    return v


def _coerce_str_list(v: object) -> object:
    """Coerce to list[str]: decode outer JSON string, extract str from dict items."""
    v = _coerce_json_list(v, parse_items=False)
    if isinstance(v, list):
        result = []
        for item in v:
            if isinstance(item, dict):
                # Agent may send {"id": "channel/post_id"} or {"post_id": "..."} etc.
                for key in ("id", "post_id", "name", "value"):
                    if key in item:
                        result.append(str(item[key]))
                        break
                else:
                    # fallback: first value in the dict
                    result.append(str(next(iter(item.values()))))
            else:
                result.append(item)
        return result
    return v


def _coerce_obj_list(v: object) -> object:
    """Coerce to list[Model]: decode outer JSON string AND inner JSON strings."""
    return _coerce_json_list(v, parse_items=True)


# list[str]  — ids, tags, etc.: outer JSON string tolerated
JsonStrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]

# list[Model] — set_ad_check_batch results etc.: outer + inner JSON strings tolerated
JsonAdCheckInputList = Annotated[list[AdCheckInput], BeforeValidator(_coerce_obj_list)]


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "vsnifer",
    instructions="""
You analyze posts from Telegram channels to detect advertising content.
Use get_unchecked_batch to retrieve posts without classification, analyze each one,
then call set_ad_check_batch to persist results.
ad_rate: 0.0 = definitely not an ad, 1.0 = definitely an ad.
Repeat the get_unchecked_batch -> analyze -> set_ad_check_batch loop until unchecked == 0.
""",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _channel_dirs() -> list[Path]:
    """Return sorted list of channel directories inside WORK_DIR."""
    if not WORK_DIR.exists():
        return []
    return sorted(p for p in WORK_DIR.iterdir() if p.is_dir())


def _post_dirs(channel_dir: Path) -> list[Path]:
    """Return sorted list of numeric post directories inside a channel directory."""
    return sorted(
        p for p in channel_dir.iterdir() if p.is_dir() and p.name.isdigit()
    )


def _read_meta(post_dir: Path) -> Optional[dict]:
    """
    Read and parse meta.json from a post directory.

    Returns None when the file is absent or cannot be parsed.
    """
    meta_path = post_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read %s: %s", meta_path, exc)
        return None


def _read_text(post_dir: Path) -> str:
    """Read text.txt from a post directory. Returns empty string when absent."""
    text_path = post_dir / "text.txt"
    if not text_path.exists():
        return ""
    try:
        return text_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read %s: %s", text_path, exc)
        return ""


def _meta_to_post_info(channel: str, post_id: int, meta: dict) -> PostInfo:
    """Build a PostInfo from raw meta.json content."""
    ad_check_raw = meta.get("ad_check")
    ad_check = AdCheckInfo(**ad_check_raw) if ad_check_raw else None
    return PostInfo(
        id=f"{channel}/{post_id}",
        channel=channel,
        post_id=post_id,
        date=meta.get("date"),
        has_media=meta.get("has_media") or False,
        views=meta.get("views") or 0,
        reactions_total=meta.get("reactions_total") or 0,
        ad_check=ad_check,
    )


def _meta_to_post_full(channel: str, post_id: int, meta: dict, text: str) -> PostFull:
    """Build a PostFull from raw meta.json content and text file content."""
    ad_check_raw = meta.get("ad_check")
    ad_check = AdCheckInfo(**ad_check_raw) if ad_check_raw else None
    return PostFull(
        id=f"{channel}/{post_id}",
        channel=channel,
        post_id=post_id,
        date=meta.get("date"),
        has_media=meta.get("has_media") or False,
        views=meta.get("views") or 0,
        reactions_total=meta.get("reactions_total") or 0,
        ad_check=ad_check,
        reactions=meta.get("reactions") or {},
        text=text,
    )


def _write_ad_check(
    channel: str,
    post_id: int,
    ad_rate: float,
    proof_of_ad: str,
) -> SetAdCheckResult:
    """Persist ad_check fields to meta.json for a single post."""
    post_dir = WORK_DIR / channel / str(post_id)
    meta_path = post_dir / "meta.json"
    post_uid = f"{channel}/{post_id}"

    if not post_dir.exists():
        msg = f"Post directory not found: {post_dir}"
        logger.error(msg)
        return SetAdCheckResult(ok=False, id=post_uid, error=msg)

    if not meta_path.exists():
        msg = f"meta.json not found: {meta_path}"
        logger.error(msg)
        return SetAdCheckResult(ok=False, id=post_uid, error=msg)

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["ad_check"] = {"ad_rate": ad_rate, "proof_of_ad": proof_of_ad}
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Wrote ad_check for %s: ad_rate=%.2f", post_uid, ad_rate)
        return SetAdCheckResult(ok=True, id=post_uid)
    except Exception as exc:
        msg = str(exc)
        logger.error("Failed to write ad_check for %s: %s", post_uid, msg)
        return SetAdCheckResult(ok=False, id=post_uid, error=msg)


def _parse_channel_entry(raw: str) -> ChannelInfo:
    """
    Parse a single channel entry from the CHANNELS env var into a ChannelInfo.

    Supported formats:
      @username              -> name="username",  url="https://t.me/username"
      https://t.me/username  -> name="username",  url="https://t.me/username"
      https://t.me/+hash     -> name="+hash",     url="https://t.me/+hash"
      +hash                  -> name="+hash",     url="https://t.me/+hash"
      numeric id             -> name="<id>",      url="https://t.me/<id>"
    """
    entry = raw.strip()
    if entry.startswith("https://t.me/") or entry.startswith("http://t.me/"):
        name = entry.split("t.me/", 1)[1].lstrip("/")
        url = f"https://t.me/{name}"
        return ChannelInfo(name=name, url=url)
    if entry.startswith("@"):
        name = entry[1:]
        return ChannelInfo(name=name, url=f"https://t.me/{name}")
    if entry.startswith("+"):
        return ChannelInfo(name=entry, url=f"https://t.me/{entry}")
    # Numeric ID or bare username.
    return ChannelInfo(name=entry, url=f"https://t.me/{entry}")


def _parse_channels_env() -> list[ChannelInfo]:
    """Parse the CHANNELS env var and return a list of ChannelInfo."""
    raw = os.getenv("CHANNELS", "")
    if not raw.strip():
        return []
    return [_parse_channel_entry(e) for e in raw.split(",") if e.strip()]


def _iter_all_posts(
    channel_filter: Optional[str] = None,
) -> list[tuple[str, int, dict]]:
    """
    Collect (channel_name, post_id, meta_dict) tuples for all posts in WORK_DIR.

    Args:
        channel_filter: when given, only posts from this channel are included.
    """
    result: list[tuple[str, int, dict]] = []
    for ch_dir in _channel_dirs():
        if channel_filter and ch_dir.name != channel_filter:
            continue
        for post_dir in _post_dirs(ch_dir):
            meta = _read_meta(post_dir)
            if meta is None:
                continue
            result.append((ch_dir.name, int(post_dir.name), meta))
    return result


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_channels() -> ToolResult:
    """List all downloaded Telegram channels available in WORK_DIR."""
    channels = [
        ChannelInfo(name=d.name, url=f"https://t.me/{d.name}")
        for d in _channel_dirs()
    ]
    logger.info("list_channels: %d channel(s)", len(channels))
    result = ChannelListResult(channels=channels, total=len(channels))
    return _make_tool_result(result, f"Found {result.total} channel(s).")


@mcp.tool()
def get_white_list() -> ToolResult:
    """
    Return the whitelist of Telegram channels configured in CHANNELS env var.

    Each entry contains:
      name - channel username or invite hash (matches WORK_DIR folder names)
      url  - canonical https://t.me/ link
    """
    channels = _parse_channels_env()
    logger.info("get_white_list: %d channel(s)", len(channels))
    result = ChannelListResult(channels=channels, total=len(channels))
    return _make_tool_result(result, f"Found {result.total} channel(s).")


@mcp.tool()
def get_stats(channel: Optional[str] = None) -> ToolResult:
    """
    Return per-channel statistics.

    Args:
        channel: when given, return stats only for this channel.
    """
    counters: dict[str, dict] = {}

    for ch_name, _post_id, meta in _iter_all_posts(channel_filter=channel):
        if ch_name not in counters:
            counters[ch_name] = {
                "total_posts": 0,
                "checked": 0,
                "unchecked": 0,
                "ad_count": 0,
                "clean_count": 0,
            }
        c = counters[ch_name]
        ad_check = meta.get("ad_check")
        c["total_posts"] += 1
        if ad_check is not None:
            c["checked"] += 1
            if ad_check.get("ad_rate", 0.0) >= AD_THRESHOLD:
                c["ad_count"] += 1
            else:
                c["clean_count"] += 1
        else:
            c["unchecked"] += 1

    stats = sorted(
        [ChannelStats(channel=ch, **counts) for ch, counts in counters.items()],
        key=lambda s: s.channel,
    )
    logger.info("get_stats: %d channel(s)", len(stats))
    result = StatsResult(stats=stats, total_channels=len(stats))
    lines = [f"Stats for {result.total_channels} channel(s):"]
    for s in stats:
        lines.append(
            f"  {s.channel}: {s.total_posts} posts, "
            f"{s.unchecked} unchecked, {s.ad_count} ads, {s.clean_count} clean"
        )
    return _make_tool_result(result, "\n".join(lines))


@mcp.tool()
def list_posts(
    channel: Optional[str] = None,
    has_ad_check: Optional[bool] = None,
    min_ad_rate: Optional[float] = None,
    max_ad_rate: Optional[float] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort: str = "date_desc",
    limit: int = 50,
    offset: int = 0,
) -> ToolResult:
    """
    List posts with optional filters, including text content.

    Default sort order is descending by importance:
      - date_desc   newest first (default)
      - date_asc    oldest first
      - ad_rate_desc  highest ad_rate first (most likely ads on top)
      - ad_rate_asc   lowest ad_rate first (cleanest posts on top)
      - views_desc  most viewed first
      - views_asc   least viewed first

    Args:
        channel:      filter by channel name.
        has_ad_check: None=all posts, True=only classified, False=only unclassified.
        min_ad_rate:  include only posts with ad_rate >= this value.
        max_ad_rate:  include only posts with ad_rate <= this value.
        date_from:    include only posts published on or after this date (YYYY-MM-DD).
        date_to:      include only posts published on or before this date (YYYY-MM-DD).
        sort:         date_desc | date_asc | ad_rate_desc | ad_rate_asc | views_desc | views_asc.
        limit:        maximum number of results to return.
        offset:       skip this many results before returning.
    """
    posts: list[PostFull] = []

    for ch_name, post_id, meta in _iter_all_posts(channel_filter=channel):
        post_dir = WORK_DIR / ch_name / str(post_id)
        text = _read_text(post_dir)
        info = _meta_to_post_full(ch_name, post_id, meta, text)

        if has_ad_check is True and info.ad_check is None:
            continue
        if has_ad_check is False and info.ad_check is not None:
            continue

        if min_ad_rate is not None:
            if info.ad_check is None or info.ad_check.ad_rate < min_ad_rate:
                continue
        if max_ad_rate is not None:
            if info.ad_check is None or info.ad_check.ad_rate > max_ad_rate:
                continue

        # Lexicographic date comparison works for ISO-8601 prefixes.
        if date_from and info.date and info.date[:10] < date_from:
            continue
        if date_to and info.date and info.date[:10] > date_to:
            continue

        posts.append(info)

    _SORT_KEY = {
        "date_desc":     (lambda p: (p.date or ""),                          True),
        "date_asc":      (lambda p: (p.date or ""),                          False),
        "ad_rate_desc":  (lambda p: (p.ad_check.ad_rate if p.ad_check else -1.0), True),
        "ad_rate_asc":   (lambda p: (p.ad_check.ad_rate if p.ad_check else 2.0),  False),
        "views_desc":    (lambda p: p.views,                                 True),
        "views_asc":     (lambda p: p.views,                                 False),
    }
    key_fn, reverse = _SORT_KEY.get(sort, _SORT_KEY["date_desc"])
    posts.sort(key=key_fn, reverse=reverse)

    result = posts[offset: offset + limit]
    logger.info(
        "list_posts: returning %d post(s) (total matched: %d, sort=%s)", len(result), len(posts), sort
    )
    paged = PostListResult(posts=result, total=len(result), channel=channel)
    ch = f" in '{channel}'" if channel else ""
    return _make_tool_result(paged, f"Found {paged.total} post(s){ch}.")



@mcp.tool()
def get_posts(channel: Optional[str] = None, ids: Optional[JsonStrList] = None) -> ToolResult:
    """
    Get full data for multiple posts at once, including text content.

    Args:
        channel: channel name. If ids is omitted, returns all posts of this channel.
                 Also used as default channel for bare numeric ids.
        ids:     optional list of post identifiers. Accepts:
                   - composite "channel/post_id"  (e.g. "babazoyka/23609")
                   - bare numeric id              (e.g. "23609") — requires channel
                 If omitted, all posts of channel are returned.
    """
    logger.info("get_posts called: channel=%r, ids=%r", channel, ids)

    # No ids supplied — return all posts for the channel
    if not ids:
        if not channel:
            logger.warning("get_posts: neither ids nor channel provided")
            empty = PostListResult(posts=[], total=0, channel=None)
            return _make_tool_result(empty, "No channel or ids provided.")
        results: list[PostFull] = []
        for ch_name, post_id, meta in _iter_all_posts(channel_filter=channel):
            post_dir = WORK_DIR / ch_name / str(post_id)
            text = _read_text(post_dir)
            results.append(_meta_to_post_full(ch_name, post_id, meta, text))
        results.sort(key=lambda p: (p.date or ""), reverse=True)
        logger.info("get_posts: returned %d post(s) for channel %r", len(results), channel)
        paged = PostListResult(posts=results, total=len(results), channel=channel)
        return _make_tool_result(paged, f"Found {paged.total} post(s) in '{channel}'.")

    results = []
    for raw_id in ids:
        # Normalize separators: some agents use ":" instead of "/"
        sid = str(raw_id).replace(":", "/", 1)
        if "/" not in sid and channel:
            sid = f"{channel}/{sid}"
        parts = sid.split("/", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            logger.warning("get_posts: skipping invalid id %r (raw: %r)", sid, raw_id)
            continue
        ch, pid = parts[0], int(parts[1])
        post_dir = WORK_DIR / ch / str(pid)
        meta = _read_meta(post_dir)
        if meta is None:
            logger.warning("get_posts: post not found %r", sid)
            continue
        text = _read_text(post_dir)
        results.append(_meta_to_post_full(ch, pid, meta, text))
    logger.info("get_posts: returned %d/%d post(s)", len(results), len(ids))
    paged = PostListResult(posts=results, total=len(results), channel=channel)
    return _make_tool_result(paged, f"Found {paged.total} post(s).")


@mcp.tool()
def get_unchecked_batch(
    channel: Optional[str] = None,
    batch_size: int = 5,
) -> ToolResult:
    """
    Return the next batch of posts without ad_check, newest first.

    Use set_ad_check_batch to write classification results back.

    Args:
        channel:    filter by channel name; None means all channels.
        batch_size: maximum number of posts to return.
    """
    logger.info("get_unchecked_batch called: channel=%r, batch_size=%r", channel, batch_size)
    posts: list[PostFull] = []

    for ch_name, post_id, meta in _iter_all_posts(channel_filter=channel):
        if meta.get("ad_check") is not None:
            continue
        post_dir = WORK_DIR / ch_name / str(post_id)
        text = _read_text(post_dir)
        posts.append(_meta_to_post_full(ch_name, post_id, meta, text))

    posts.sort(key=lambda p: (p.date or ""), reverse=True)
    batch = posts[:batch_size]
    logger.info(
        "get_unchecked_batch: returning %d post(s) (total unchecked: %d)",
        len(batch),
        len(posts),
    )
    result = PostListResult(posts=batch, total=len(batch), channel=channel)
    ch = f" in '{channel}'" if channel else ""
    return _make_tool_result(result, f"Found {len(batch)} unchecked post(s){ch} (total unchecked: {len(posts)}).")



@mcp.tool()
def clear_ad_check(id: str) -> SetAdCheckResult:
    """
    Remove ad_check classification from meta.json for a single post.

    Use this to reset a post back to unchecked state so it will appear
    again in get_unchecked_batch.

    Args:
        id: composite post identifier in "channel/post_id" format.
            Example: "babazoyka/23609"
    """
    logger.info("clear_ad_check called: id=%r", id)
    parts = id.split("/", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return SetAdCheckResult(
            ok=False, id=id,
            error=f"Invalid id format: {id!r}. Expected 'channel/post_id'.",
        )
    channel, post_id = parts[0], int(parts[1])
    post_dir = WORK_DIR / channel / str(post_id)
    meta_path = post_dir / "meta.json"
    post_uid = f"{channel}/{post_id}"

    if not meta_path.exists():
        msg = f"meta.json not found: {meta_path}"
        logger.error(msg)
        return SetAdCheckResult(ok=False, id=post_uid, error=msg)

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if "ad_check" not in meta:
            logger.info("clear_ad_check: %s — already unchecked", post_uid)
            return SetAdCheckResult(ok=True, id=post_uid)
        del meta["ad_check"]
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("clear_ad_check: removed ad_check for %s", post_uid)
        return SetAdCheckResult(ok=True, id=post_uid)
    except Exception as exc:
        msg = str(exc)
        logger.error("clear_ad_check failed for %s: %s", post_uid, msg)
        return SetAdCheckResult(ok=False, id=post_uid, error=msg)


@mcp.tool()
def set_ad_check_batch(
    results: JsonAdCheckInputList,
    channel: Optional[str] = None,
) -> BatchResult:
    """
    Write ad classification results for multiple posts at once.

    Intended to be called after get_unchecked_batch once all posts have been analyzed.

    Args:
        results: list of AdCheckInput with post_id, ad_rate, proof_of_ad.
                 Each item may include channel explicitly, or use composite id format.
                 If channel is omitted from an item, the top-level channel param is used.
        channel: default channel name applied to items that don't specify one.
    """
    logger.info("set_ad_check_batch called: channel=%r, results_count=%d, results=%r", channel, len(results), results)
    written = 0
    errors: list[str] = []
    ids: list[str] = []

    for item in results:
        effective_channel = item.channel or channel
        if not effective_channel:
            errors.append(f"post_id={item.post_id}: channel not specified")
            continue
        outcome = _write_ad_check(effective_channel, item.post_id, item.ad_rate, item.proof_of_ad)
        if outcome.ok:
            written += 1
            ids.append(outcome.id)
        else:
            errors.append(f"{outcome.id}: {outcome.error}")

    logger.info("set_ad_check_batch: written=%d errors=%d", written, len(errors))
    return BatchResult(written=written, errors=errors, ids=ids)


# ---------------------------------------------------------------------------
# Entry point — _args was parsed in the early block above
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _args.command == "list-tools":
        import asyncio

        def _schema_type(prop: dict) -> str:
            """Convert a JSON-schema property dict to a compact type string."""
            if "anyOf" in prop:
                parts = []
                for s in prop["anyOf"]:
                    if s.get("type") == "null":
                        parts.append("None")
                    elif "type" in s:
                        parts.append(s["type"])
                    else:
                        parts.append("any")
                return " | ".join(parts)
            t = prop.get("type", "any")
            if t == "array":
                items = prop.get("items", {})
                return f"list[{_schema_type(items)}]"
            return t

        def _fmt_return(return_type: object) -> str:
            """Shorten return_type string by stripping module/class noise."""
            import re
            s = str(return_type)
            # "<class 'fastmcp.tools.base.ToolResult'>" → "ToolResult"
            s = re.sub(r"<class '[\w.]+\.(\w+)'>", r"\1", s)
            # remaining module prefixes
            s = s.replace("__main__.", "").replace("mcp_server.", "")
            return s

        async def _print_tools() -> None:
            tools = await mcp.list_tools()
            print(f"Registered MCP tools ({len(tools)}):\n")
            for t in tools:
                first_line = (t.description or "").strip().splitlines()[0] if t.description else ""
                ret = _fmt_return(t.return_type)
                props: dict = (t.parameters or {}).get("properties", {})
                params_str = f"({', '.join(props.keys())})" if props else "()"
                print(f"  {t.name}  {params_str}  ->  {ret}    - {first_line}")

        asyncio.run(_print_tools())

    elif _args.command == "run":
        _host: str = _args.host or MCP_HOST
        _port: int = _args.port or MCP_PORT
        # CLI --mode overrides env var MCP_MODE
        if _args.mode:
            MCP_MODE = _args.mode
        logger.info(
            "Starting vsnifer MCP server on %s:%d (stateless, json_response, mode=%s), WORK_DIR=%s",
            _host,
            _port,
            MCP_MODE,
            WORK_DIR.resolve(),
        )
        mcp.run(
            transport="streamable-http",
            host=_host,
            port=_port,
            stateless_http=True,
            json_response=True,
        )

