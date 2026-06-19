# -*- coding: utf-8 -*-
"""
check_ad.py -- LLM-based ad classifier for downloaded Telegram posts.

Scans WORK_DIR for posts that do not yet have an 'ad_check' entry in meta.json,
classifies them in batches using a local Ollama LLM, and writes the result back:

    meta.json  <-- adds key:
    "ad_check": {
        "ad_rate": 0.85,
        "proof_of_ad": "Одно предложение, объясняющее решение."
    }

Commands:
  update         Scan WORK_DIR, classify unchecked posts in batches, update meta.json.
  update-nudes   Scan WORK_DIR for video posts, compute nude_rate with NudeDetector.

Usage:
    python check_ad.py
    python check_ad.py --help
    python check_ad.py update
    python check_ad.py update --work-dir H:\\TEMP\\vk_vsf
    python check_ad.py update --batch-size 5 --model qwen3:8b
    python check_ad.py update --force
    python check_ad.py update --dry-run
    python check_ad.py update --channel babazoyka
    python check_ad.py update --limit 20
    python check_ad.py --log-level DEBUG update
    python check_ad.py update-nudes
    python check_ad.py update-nudes --frames 15 --channel babazoyka
    python check_ad.py update-nudes --force --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Load .env BEFORE third-party clients (langsmith, requests) are imported so
# that HTTPS_PROXY and LANGSMITH_* vars are already in os.environ when those
# libraries create their internal HTTP sessions.
from dotenv import load_dotenv
load_dotenv()

# Propagate LANGSMITH_HTTP_PROXY → HTTPS_PROXY / HTTP_PROXY so that the
# langsmith SDK (and requests) route traffic through the configured proxy.
# Does not overwrite values already set in the environment directly.
_ls_proxy: str | None = os.getenv("LANGSMITH_HTTP_PROXY")
if _ls_proxy:
    os.environ.setdefault("HTTPS_PROXY", _ls_proxy)
    os.environ.setdefault("HTTP_PROXY", _ls_proxy)

import requests as http_requests
from langsmith import traceable
from pydantic import BaseModel

from config import ChannelConfig, load_app_config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_level: int = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
_logs_dir: Path = Path("logs")
_logs_dir.mkdir(exist_ok=True)
_log_file: Path = _logs_dir / f"{Path(__file__).stem}.log"

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangSmith auth check
# ---------------------------------------------------------------------------
def _check_langsmith_auth() -> None:
    """
    Verify that the LangSmith API key is valid before the first trace is sent.

    Performs a lightweight authenticated GET and logs a clear ERROR on 401/403
    so the problem is visible without having to dig through DEBUG output.
    Called once at startup when LANGSMITH_TRACING=true.
    """
    api_key: str | None = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
    api_url: str = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")

    if not api_key:
        logger.warning(
            "LangSmith tracing is enabled (LANGSMITH_TRACING=true) "
            "but no API key found. Set LANGSMITH_API_KEY in .env."
        )
        return

    try:
        resp = http_requests.get(
            f"{api_url}/workspaces",
            headers={"x-api-key": api_key},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("LangSmith auth OK (url=%s)", api_url)
        elif resp.status_code in (401, 403):
            logger.error(
                "LangSmith auth FAILED (%d %s). "
                "The API key is invalid or lacks write permissions. "
                "Go to https://smith.langchain.com/settings -> API Keys, "
                "create a new key with 'Service Key' role, "
                "then update LANGSMITH_API_KEY in .env.",
                resp.status_code,
                resp.reason,
            )
        else:
            logger.warning("LangSmith auth check returned unexpected status %d", resp.status_code)
    except Exception as exc:
        logger.warning("LangSmith auth check failed (connection error): %s", exc)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class PostToCheck(BaseModel):
    """
    A single post scheduled for ad classification.

    meta_path  -- absolute path to the post's meta.json (for writing results back)
    content    -- text from text.txt (sent to LLM)
    date       -- post publication date (ISO-8601) from meta.json, used for sorting
    """

    id: str          # "channel/post_id"
    channel: str
    post_id: int
    meta_path: Path
    content: str
    date: Optional[str] = None   # ISO-8601; None when meta.json has no 'date' field

    model_config = {"arbitrary_types_allowed": True}


class AdCheckResult(BaseModel):
    """Classification result for a single post as returned by the LLM."""

    id: str
    ad_rate: float
    proof_of_ad: str


class OllamaConfig(BaseModel):
    """Ollama server connection parameters."""

    base_url: str = "http://localhost:11434"
    model: str = "qwen3.5:9b"
    timeout_sec: int = 300


def load_ollama_config() -> OllamaConfig:
    """Load OllamaConfig from environment variables."""
    return OllamaConfig(
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        model=os.getenv("OLLAMA_MODEL", "qwen3.5:9b"),
        timeout_sec=int(os.getenv("OLLAMA_TIMEOUT_SEC", "300")),
    )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def _parse_date(value: str) -> date:
    """Parse YYYY-MM-DD string into a date object for argparse type=."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date: {value!r}. Use YYYY-MM-DD.")


def _parse_last_days(value: str) -> int:
    """Parse --last-days value: accept '7' or '7d', return int."""
    v = value.strip().lower().rstrip("d")
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid value: {value!r}. Use a number like 7 or 7d.")
    if n < 1:
        raise argparse.ArgumentTypeError(f"Value must be >= 1, got {n}")
    return n


def _resolve_date_range(
    start_date: Optional[date],
    end_date: Optional[date],
    last_days: Optional[int],
) -> tuple[Optional[date], Optional[date]]:
    """
    Compute the effective (start_date, end_date) pair.

    --last-days N overrides --start-date when both are given.
    Returns (None, None) when no date filter is active.
    """
    if last_days is not None:
        return date.today() - timedelta(days=last_days - 1), end_date
    return start_date, end_date


# ---------------------------------------------------------------------------
# Post collection
# ---------------------------------------------------------------------------
def collect_posts_to_check(
    work_dir: Path,
    channel_filter: Optional[str] = None,
    force: bool = False,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[PostToCheck]:
    """
    Scan work_dir for posts that have both meta.json and text.txt.

    Skips posts whose meta.json already contains 'ad_check' unless force=True.
    If channel_filter is given, only that channel subdirectory is scanned.
    If start_date or end_date is set, posts outside the range are skipped;
    posts with no 'date' field in meta.json are also skipped when a date filter
    is active (a warning is logged for each such post).
    Returns posts sorted by date descending (newest first), post_id descending as tiebreaker.
    """
    date_filter_active: bool = start_date is not None or end_date is not None
    posts: list[PostToCheck] = []

    if not work_dir.exists():
        logger.error(f"Work directory not found: {work_dir}")
        return posts

    for channel_dir in sorted(work_dir.iterdir()):
        if not channel_dir.is_dir() or channel_dir.name.startswith("."):
            continue
        if channel_filter and channel_dir.name != channel_filter:
            continue

        channel_name: str = channel_dir.name

        for post_dir in sorted(channel_dir.iterdir()):
            if not post_dir.is_dir() or not post_dir.name.isdigit():
                continue

            post_id: int = int(post_dir.name)
            meta_path: Path = post_dir / "meta.json"
            text_path: Path = post_dir / "text.txt"

            if not meta_path.exists():
                logger.debug(f"[{channel_name}/{post_id}] no meta.json, skipping")
                continue
            if not text_path.exists():
                logger.debug(f"[{channel_name}/{post_id}] no text.txt, skipping")
                continue

            date_str: Optional[str] = None
            try:
                meta: dict = json.loads(meta_path.read_text(encoding="utf-8"))
                date_str = meta.get("date")

                # --- date range filter ---
                if date_filter_active:
                    if not date_str:
                        logger.warning(
                            f"[{channel_name}/{post_id}] no 'date' in meta.json, "
                            f"skipping (date filter active)"
                        )
                        continue
                    try:
                        post_date: date = date.fromisoformat(date_str[:10])
                    except ValueError:
                        logger.warning(
                            f"[{channel_name}/{post_id}] cannot parse date {date_str!r}, skipping"
                        )
                        continue
                    if start_date and post_date < start_date:
                        logger.debug(
                            f"[{channel_name}/{post_id}] before start_date ({start_date}), skipping"
                        )
                        continue
                    if end_date and post_date > end_date:
                        logger.debug(
                            f"[{channel_name}/{post_id}] after end_date ({end_date}), skipping"
                        )
                        continue

                if not force and "ad_check" in meta:
                    logger.debug(f"[{channel_name}/{post_id}] already checked, skipping")
                    continue
            except Exception as exc:
                logger.warning(f"[{channel_name}/{post_id}] failed to read meta.json: {exc}")
                continue

            try:
                content: str = text_path.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning(f"[{channel_name}/{post_id}] failed to read text.txt: {exc}")
                continue

            if not content:
                logger.debug(f"[{channel_name}/{post_id}] text.txt is empty, skipping")
                continue

            posts.append(
                PostToCheck(
                    id=f"{channel_name}/{post_id}",
                    channel=channel_name,
                    post_id=post_id,
                    meta_path=meta_path,
                    content=content,
                    date=date_str,
                )
            )
            logger.debug(f"[{channel_name}/{post_id}] queued ({len(content)} chars)")

    # Newest first: sort by date desc, then post_id desc as tiebreaker
    posts.sort(
        key=lambda p: (p.date or "", p.post_id),
        reverse=True,
    )

    logger.info(f"Found {len(posts)} post(s) to classify in {work_dir}")
    return posts


def write_ad_check_to_meta(meta_path: Path, result: AdCheckResult) -> Optional[dict]:
    """
    Load meta.json, add or overwrite the 'ad_check' key, and write back.
    Returns the previous ad_check dict, or None if it did not exist.
    """
    meta: dict = json.loads(meta_path.read_text(encoding="utf-8"))
    old: Optional[dict] = meta.get("ad_check")
    meta["ad_check"] = {
        "ad_rate": result.ad_rate,
        "proof_of_ad": result.proof_of_ad,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return old


# ---------------------------------------------------------------------------
# Whitelist pre-filter
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def _parse_whitelist_ids(channels: list[ChannelConfig]) -> set[str]:
    """
    Extract a set of lowercase t.me path identifiers from a list of ChannelConfig.

    https://t.me/username  -> 'username'
    https://t.me/+HASH     -> '+hash'
    """
    ids: set[str] = set()
    for ch in channels:
        m = re.search(r"t\.me/([^\s,/?#]+)", ch.url)
        if m:
            ids.add(m.group(1).rstrip("/").lower())
    return ids


def _format_channels_json(channels: list[ChannelConfig]) -> str:
    """Serialize channels as a compact JSON array for LLM prompt injection."""
    return json.dumps(
        [{"url": c.url, "name": c.name} for c in channels],
        ensure_ascii=False,
    )


def _has_only_whitelisted_links(content: str, whitelist_ids: set[str]) -> bool:
    """
    Return True if every URL in *content* is either a whitelisted t.me channel
    or a safe max.ru link (channel's own page like max.ru/CHANNELNAME).

    max.ru/join/* links are invite links to arbitrary channels and are NOT safe.
    Posts with no URLs return False so the LLM still evaluates text-only ad signals.
    """
    urls = _URL_RE.findall(content)
    if not urls:
        return False
    for url in urls:
        if "max.ru" in url:
            if "/join/" not in url:
                continue
            return False
        m = re.match(r"https?://t\.me/([^\s/?#)\]>\"']+)", url)
        if m:
            ident = m.group(1).rstrip("/").lower()
            if ident in whitelist_ids:
                continue
        return False
    return True


# ---------------------------------------------------------------------------
# LLM utilities
# ---------------------------------------------------------------------------
def _render_template(template: str, extra: Optional[dict[str, str]] = None) -> str:
    """
    Replace {{ PLACEHOLDER }} tokens in a template string.

    Resolution order for each token:
      1. extra dict (e.g. {"content": "<json>"})
      2. os.environ

    Unresolved placeholders are left as-is and a WARNING is logged.
    """
    def _replace(match: re.Match) -> str:
        key: str = match.group(1).strip()
        if extra and key in extra:
            return extra[key]
        env_val: Optional[str] = os.environ.get(key)
        if env_val is not None:
            return env_val
        logger.warning(f"Template placeholder '{{{{ {key} }}}}' not resolved")
        return match.group(0)

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace, template)


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks emitted by reasoning models (qwen3, deepseek-r1)."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json_array(text: str) -> str:
    """
    Extract a JSON array from LLM response text.

    Handles:
      1. ```json [ ... ] ```  -- markdown code block with language tag
      2. ``` [ ... ] ```       -- plain markdown code block
      3. Raw text containing [ ... ]
    """
    m = re.search(r"```(?:json)?\s*(\[.*?])\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\[.*])", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


@traceable(
    run_type="llm",
    name="ollama_chat",
    project_name=os.getenv("LANGSMITH_PROJECT", "vsnifer"),
)
def _call_ollama(
    config: OllamaConfig,
    prompt_system: str,
    prompt_user: str,
    think: bool = False,
) -> str:
    """
    POST to Ollama /api/chat with system + user roles and streaming.

    Using /api/chat ensures instruction-tuned models (qwen3, llama3, etc.) handle
    the system prompt and user request as intended -- they are trained on this role
    separation and follow instructions more reliably than raw completion mode.

    think=False disables chain-of-thought for models that support it.
    Streamed tokens are printed to stdout immediately.
    Returns the full accumulated response text.
    """
    url: str = f"{config.base_url.rstrip('/')}/api/chat"
    payload: dict = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user",   "content": prompt_user},
        ],
        "think": think,
        "stream": True,
    }
    think_label: str = "thinking=ON" if think else "thinking=OFF"
    logger.info(
        f"Ollama: model={config.model}  {think_label}  "
        f"system={len(prompt_system)}c  user={len(prompt_user)}c"
    )
    logger.debug("--- system prompt (%d chars) ---\n%s\n--- end system prompt ---", len(prompt_system), prompt_system)
    logger.debug("--- user prompt (%d chars) ---\n%s\n--- end user prompt ---", len(prompt_user), prompt_user)
    print(f"\n--- Ollama ({config.model}, {think_label}) ---", flush=True)

    full_response: list[str] = []

    with http_requests.post(url, json=payload, timeout=config.timeout_sec, stream=True) as resp:
        resp.raise_for_status()
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                chunk: dict = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            token: str = chunk.get("message", {}).get("content", "")
            if token:
                print(token, end="", flush=True)
                full_response.append(token)
            if chunk.get("done"):
                break

    print("\n--- end ---\n", flush=True)
    return "".join(full_response)


def classify_batch(
    batch: list[PostToCheck],
    config: OllamaConfig,
    prompt_system: str,
    prompt_after_template: str,
    think: bool = False,
) -> list[AdCheckResult]:
    """
    Send one batch of posts to Ollama for ad classification.

    Input to LLM: JSON array of {"id": ..., "content": ...} objects.
    Expected output: JSON array of AdCheckResult objects (same count).

    Returns a list of AdCheckResult on success.
    Returns an empty list on any error (call failure, bad JSON, count mismatch, parse error).
    """
    input_data: list[dict] = [{"id": p.id, "content": p.content} for p in batch]
    input_json: str = json.dumps(input_data, ensure_ascii=False, indent=2)

    if "{{ content }}" not in prompt_after_template:
        logger.warning(
            "Placeholder '{{ content }}' not found in user prompt template -- "
            "input data will NOT be sent"
        )
        prompt_user: str = _render_template(prompt_after_template)
    else:
        prompt_user = _render_template(prompt_after_template, extra={"content": input_json})

    raw_response: str = ""
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            raw_response = _call_ollama(config, prompt_system, prompt_user, think=think)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logger.warning(f"Ollama call failed (attempt {attempt + 1}/3): {exc}. Retrying in 5s...")
                time.sleep(5)
    if last_exc is not None:
        logger.error(f"Ollama call failed after 3 attempts: {last_exc}")
        return []

    logger.debug("Raw response (%d chars):\n%s", len(raw_response), raw_response)

    cleaned: str = _strip_think_tags(raw_response)
    json_text: str = _extract_json_array(cleaned)

    try:
        raw_list: list[dict] = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.error(f"LLM response is not valid JSON: {exc}\nRaw snippet: {json_text[:500]}")
        return []

    if len(raw_list) != len(batch):
        logger.error(
            f"LLM returned {len(raw_list)} result(s) for {len(batch)} post(s) -- batch skipped"
        )
        return []

    results: list[AdCheckResult] = []
    for item in raw_list:
        try:
            results.append(AdCheckResult(**item))
        except Exception as exc:
            logger.error(f"Failed to parse AdCheckResult from {item}: {exc}")
            return []

    return results


# ---------------------------------------------------------------------------
# Command: update
# ---------------------------------------------------------------------------
def cmd_update(args: argparse.Namespace) -> None:
    """
    Scan WORK_DIR for posts without ad_check, send them to LLM in batches,
    and write the classification result back to each post's meta.json.

    Each meta.json receives a new key:
        "ad_check": {
            "ad_rate": 0.85,
            "proof_of_ad": "One sentence in Russian explaining the decision."
        }

    Already-classified posts are skipped unless --force is set.
    Use --dry-run to preview what would be processed without making changes.
    """
    work_dir: Path = Path(args.work_dir)
    batch_size: int = args.batch_size

    if not work_dir.exists():
        logger.error(f"Work directory not found: {work_dir}")
        raise SystemExit(1)

    prompt_system_file: Path = Path(args.prompt_system)
    prompt_user_file: Path = Path(args.prompt_user)

    for path in (prompt_system_file, prompt_user_file):
        if not path.exists():
            logger.error(f"Prompt file not found: {path}")
            raise SystemExit(1)

    config: OllamaConfig = load_ollama_config()
    if args.model:
        config = config.model_copy(update={"model": args.model})

    app_config = load_app_config()
    channels = app_config.channels
    if not channels:
        logger.warning("No channels defined in .env.json — whitelist pre-filter disabled, CHANNELS placeholder will be empty")
    channels_json: str = _format_channels_json(channels)
    logger.info("Loaded %d channel(s) from .env.json", len(channels))

    channel_extra = {"CHANNELS": channels_json}
    prompt_system: str = _render_template(prompt_system_file.read_text(encoding="utf-8"), extra=channel_extra)
    prompt_after_template: str = prompt_user_file.read_text(encoding="utf-8")

    effective_start, effective_end = _resolve_date_range(
        args.start_date, args.end_date, args.last_days
    )
    if effective_start or effective_end:
        logger.info(
            f"Date filter: [{effective_start or '...'} — {effective_end or '...'}]"
        )

    posts: list[PostToCheck] = collect_posts_to_check(
        work_dir,
        channel_filter=args.channel,
        force=args.force,
        start_date=effective_start,
        end_date=effective_end,
    )

    if not posts:
        logger.info("No posts to classify.")
        return

    # Pre-filter: posts whose only links are whitelisted channels need no LLM call.
    whitelist_ids = _parse_whitelist_ids(channels)
    auto_clear: list[PostToCheck] = []
    to_classify: list[PostToCheck] = posts
    if whitelist_ids:
        auto_clear = [p for p in posts if _has_only_whitelisted_links(p.content, whitelist_ids)]
        to_classify = [p for p in posts if not _has_only_whitelisted_links(p.content, whitelist_ids)]
        if auto_clear:
            logger.info(
                "Pre-filter: %d post(s) have only whitelisted links → ad_rate=0.0 (skipping LLM)",
                len(auto_clear),
            )

    if args.limit:
        to_classify = to_classify[: args.limit]
        logger.info(f"Limited to first {len(to_classify)} post(s) for LLM by --limit")

    if args.dry_run:
        logger.info(
            f"[DRY RUN] Pre-filter (ad_rate=0.0, no LLM): {len(auto_clear)} post(s)"
        )
        for p in auto_clear:
            logger.info(f"  {p.id}")
        logger.info(
            f"[DRY RUN] Would classify with LLM: {len(to_classify)} post(s) in batches of {batch_size}"
        )
        for p in to_classify:
            logger.info(f"  {p.id}")
        return

    total: int = len(auto_clear) + len(to_classify)
    updated: int = 0
    errors: int = 0

    # Write pre-filtered results directly without calling LLM.
    for post in auto_clear:
        result = AdCheckResult(
            id=post.id,
            ad_rate=0.0,
            proof_of_ad="Все ссылки из белого списка, реклама не обнаружена.",
        )
        try:
            old = write_ad_check_to_meta(post.meta_path, result)
            if old is not None:
                logger.info(f"[{post.id}] pre-filter: ad_rate {old['ad_rate']:.2f} -> 0.0")
            else:
                logger.info(f"[{post.id}] pre-filter: ad_rate=0.0")
            updated += 1
        except Exception as exc:
            logger.error(f"[{post.id}] failed to write meta.json: {exc}")
            errors += 1

    llm_total: int = len(to_classify)
    for batch_start in range(0, llm_total, batch_size):
        batch: list[PostToCheck] = to_classify[batch_start: batch_start + batch_size]
        ids: str = ", ".join(p.id for p in batch)
        logger.info(f"Batch [{batch_start + 1}..{batch_start + len(batch)}/{llm_total}]: {ids}")

        results: list[AdCheckResult] = classify_batch(
            batch, config, prompt_system, prompt_after_template, think=args.think
        )

        if not results:
            logger.error(f"Batch failed -- skipping {len(batch)} post(s)")
            errors += len(batch)
            continue

        result_map: dict[str, AdCheckResult] = {r.id: r for r in results}

        for post in batch:
            result: Optional[AdCheckResult] = result_map.get(post.id)
            if result is None:
                logger.error(f"[{post.id}] missing from LLM response")
                errors += 1
                continue
            try:
                old = write_ad_check_to_meta(post.meta_path, result)
                if old is not None:
                    logger.info(
                        f"[{post.id}] ad_rate: {old['ad_rate']:.2f} -> {result.ad_rate:.2f}"
                        f"  proof: {old['proof_of_ad']!r} -> {result.proof_of_ad!r}"
                    )
                else:
                    logger.info(f"[{post.id}] ad_rate={result.ad_rate:.2f}  {result.proof_of_ad}")
                updated += 1
            except Exception as exc:
                logger.error(f"[{post.id}] failed to write meta.json: {exc}")
                errors += 1

    logger.info(f"Done. Updated: {updated}  Errors: {errors}  Total: {total}")


# ---------------------------------------------------------------------------
# Nudity detection constants
# ---------------------------------------------------------------------------

#: nudenet 3.x: classes considered explicitly nude (high-confidence nudity signal)
_NUDE_EXPLICIT_CLASSES: frozenset[str] = frozenset({
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
})

#: Video file extensions to scan in post directories
_VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv",
})


# ---------------------------------------------------------------------------
# Nudity detection helpers
# ---------------------------------------------------------------------------

def _find_video_file(post_dir: Path) -> Optional[Path]:
    """Return the first video file found in a post directory, or None."""
    for f in post_dir.iterdir():
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS:
            return f
    return None


def _extract_frames(video_path: Path, max_frames: int = 10) -> list:
    """
    Extract up to *max_frames* evenly-spaced frames from a video.

    Returns a list of numpy arrays in RGBA format (required by nudenet's _read_image).
    Returns an empty list if the video cannot be opened or has no decodable frames.
    """
    import cv2  # lazy import — not needed for the 'update' command

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.warning("Cannot open video: %s", video_path)
        return []

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        logger.warning("Video reports 0 frames, trying sequential read: %s", video_path)
        # fallback: read first max_frames sequentially
        frames = []
        while len(frames) < max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
            frames.append(rgba)
        cap.release()
        return frames

    n = min(max_frames, total)
    indices = [int(i * total / n) for i in range(n)]
    frames = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret:
            # nudenet _read_image does COLOR_RGBA2BGR internally → pass RGBA
            rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
            frames.append(rgba)
    cap.release()
    logger.debug("Extracted %d frame(s) from %s (total_frames=%d)", len(frames), video_path.name, total)
    return frames


def compute_nude_rate(video_path: Path, max_frames: int = 10) -> float:
    """
    Sample *max_frames* frames from the video and run NudeDetector on each.

    Returns the maximum detection score across all explicit-class detections
    in all sampled frames.  0.0 means nothing was detected; 1.0 means high
    confidence of explicit nudity.
    """
    from nudenet import NudeDetector  # lazy import

    frames = _extract_frames(video_path, max_frames=max_frames)
    if not frames:
        logger.warning("No frames extracted from %s — nude_rate=0.0", video_path.name)
        return 0.0

    detector = NudeDetector()
    max_score = 0.0

    try:
        all_results = detector.detect_batch(frames)
        for frame_results in all_results:
            for det in frame_results:
                if det.get("class") in _NUDE_EXPLICIT_CLASSES:
                    score = float(det.get("score", 0.0))
                    if score > max_score:
                        max_score = score
    except Exception as exc:
        logger.warning("detect_batch failed (%s), trying frame-by-frame: %s", video_path.name, exc)
        for frame in frames:
            try:
                for det in detector.detect(frame):
                    if det.get("class") in _NUDE_EXPLICIT_CLASSES:
                        score = float(det.get("score", 0.0))
                        if score > max_score:
                            max_score = score
            except Exception as exc2:
                logger.debug("detect failed on one frame: %s", exc2)

    return round(float(max_score), 4)


# ---------------------------------------------------------------------------
# Command: update-nudes
# ---------------------------------------------------------------------------

def cmd_update_nudes(args: argparse.Namespace) -> None:
    """
    Scan WORK_DIR for posts that contain video files, compute nude_rate for each
    using NudeDetector, and write the result to meta.json:

        "nude_check": {
            "nude_rate": 0.0,      # 0.0 = clean, 1.0 = explicit
            "video_file": "name.mp4",
            "frames_sampled": 10
        }

    Posts that already have nude_check are skipped unless --force is set.
    """
    work_dir: Path = Path(args.work_dir)

    if not work_dir.exists():
        logger.error("Work directory not found: %s", work_dir)
        raise SystemExit(1)

    effective_start, effective_end = _resolve_date_range(
        args.start_date, args.end_date, args.last_days
    )
    if effective_start or effective_end:
        logger.info(
            "Date filter: [%s — %s]", effective_start or "...", effective_end or "..."
        )
    date_filter_active: bool = effective_start is not None or effective_end is not None

    # ---- collect posts with video files ----
    posts_with_video: list[dict] = []

    for channel_dir in sorted(work_dir.iterdir()):
        if not channel_dir.is_dir() or channel_dir.name.startswith("."):
            continue
        if args.channel and channel_dir.name != args.channel:
            continue

        channel_name: str = channel_dir.name

        for post_dir in sorted(channel_dir.iterdir()):
            if not post_dir.is_dir() or not post_dir.name.isdigit():
                continue

            meta_path: Path = post_dir / "meta.json"
            if not meta_path.exists():
                continue

            video_file = _find_video_file(post_dir)
            if video_file is None:
                logger.debug("[%s/%s] no video file, skipping", channel_name, post_dir.name)
                continue

            try:
                meta: dict = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("[%s/%s] failed to read meta.json: %s", channel_name, post_dir.name, exc)
                continue

            # --- date range filter ---
            if date_filter_active:
                date_str: Optional[str] = meta.get("date")
                if not date_str:
                    logger.warning(
                        "[%s/%s] no 'date' in meta.json, skipping (date filter active)",
                        channel_name, post_dir.name,
                    )
                    continue
                try:
                    post_date: date = date.fromisoformat(date_str[:10])
                except ValueError:
                    logger.warning(
                        "[%s/%s] cannot parse date %r, skipping", channel_name, post_dir.name, date_str
                    )
                    continue
                if effective_start and post_date < effective_start:
                    logger.debug(
                        "[%s/%s] before start_date (%s), skipping", channel_name, post_dir.name, effective_start
                    )
                    continue
                if effective_end and post_date > effective_end:
                    logger.debug(
                        "[%s/%s] after end_date (%s), skipping", channel_name, post_dir.name, effective_end
                    )
                    continue

            if not args.force and "nude_check" in meta:
                logger.debug("[%s/%s] already has nude_check, skipping", channel_name, post_dir.name)
                continue

            posts_with_video.append({
                "id": f"{channel_name}/{post_dir.name}",
                "meta_path": meta_path,
                "video_file": video_file,
            })

    if not posts_with_video:
        logger.info("No posts with video files to process.")
        return

    if args.limit:
        posts_with_video = posts_with_video[: args.limit]
        logger.info("Limited to first %d post(s) by --limit", len(posts_with_video))

    logger.info(
        "Found %d post(s) with video files to analyze (frames_per_video=%d)",
        len(posts_with_video), args.frames,
    )

    if args.dry_run:
        logger.info("[DRY RUN] Would analyze:")
        for p in posts_with_video:
            logger.info("  %s  (%s)", p["id"], p["video_file"].name)
        return

    total: int = len(posts_with_video)
    updated: int = 0
    errors: int = 0

    for i, post_info in enumerate(posts_with_video, 1):
        post_uid: str = post_info["id"]
        video_file: Path = post_info["video_file"]
        meta_path: Path = post_info["meta_path"]

        logger.info("[%d/%d] %s: analyzing %s", i, total, post_uid, video_file.name)

        try:
            nude_rate = compute_nude_rate(video_file, max_frames=args.frames)
        except Exception as exc:
            logger.error("[%s] compute_nude_rate failed: %s", post_uid, exc, exc_info=True)
            errors += 1
            continue

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["nude_check"] = {
                "nude_rate": nude_rate,
                "video_file": video_file.name,
                "frames_sampled": args.frames,
            }
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("[%s] nude_rate=%.4f  (%s)", post_uid, nude_rate, video_file.name)
            updated += 1
        except Exception as exc:
            logger.error("[%s] failed to write meta.json: %s", post_uid, exc)
            errors += 1

    logger.info("Done. Updated: %d  Errors: %d  Total: %d", updated, errors, total)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Main entry point."""
    default_work_dir: str = os.getenv("WORK_DIR", r"H:\TEMP\vk_vsf")

    parser = argparse.ArgumentParser(
        description="LLM-based ad classifier for downloaded Telegram posts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  update         Scan WORK_DIR, classify unchecked posts in batches, write ad_check to meta.json
  update-nudes   Scan WORK_DIR for video posts, compute nude_rate with NudeDetector, write nude_check

Examples:
  python check_ad.py update
  python check_ad.py update --work-dir H:\\TEMP\\vk_vsf
  python check_ad.py update --batch-size 5 --model qwen3:8b
  python check_ad.py update --force
  python check_ad.py update --dry-run
  python check_ad.py update --channel babazoyka
  python check_ad.py update --limit 20
  python check_ad.py update --think
  python check_ad.py update --start-date 2026-05-01
  python check_ad.py update --last-days 7
  python check_ad.py update --force --last-days 7
  python check_ad.py update --start-date 2026-05-01 --end-date 2026-05-31
  python check_ad.py update --prompt-system llm/check_ad_prompt.p1.md --prompt-user llm/check_ad_prompt.p2.md
  python check_ad.py --log-level DEBUG update

  python check_ad.py update-nudes
  python check_ad.py update-nudes --frames 15
  python check_ad.py update-nudes --channel babazoyka
  python check_ad.py update-nudes --force --dry-run
  python check_ad.py update-nudes --limit 20
  python check_ad.py update-nudes --last-days 7
  python check_ad.py update-nudes --force --start-date 2026-05-01 --end-date 2026-05-31
        """,
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        metavar="LEVEL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "debug", "info", "warning", "error"],
        help="Logging level: DEBUG, INFO, WARNING, ERROR. Overrides LOG_LEVEL from .env.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- update ---
    update_parser = subparsers.add_parser(
        "update",
        help="Classify unchecked posts and write ad_check to meta.json",
    )
    update_parser.add_argument(
        "--work-dir",
        type=str,
        default=default_work_dir,
        metavar="PATH",
        help=f"Work directory with downloaded posts (default from .env WORK_DIR: {default_work_dir})",
    )
    update_parser.add_argument(
        "--channel",
        type=str,
        default=None,
        metavar="NAME",
        help="Process only this channel subdirectory (folder name, e.g. babazoyka)",
    )
    update_parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        metavar="N",
        help="Number of posts per LLM request (default: 5)",
    )
    update_parser.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="MODEL",
        help="Ollama model name (default from .env OLLAMA_MODEL)",
    )
    update_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-classify posts that already have ad_check in meta.json",
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be classified without making any changes",
    )
    update_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N posts (useful for testing)",
    )
    update_parser.add_argument(
        "--think",
        action="store_true",
        default=False,
        help="Enable chain-of-thought reasoning in the model (slower, useful for debugging)",
    )
    update_parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Process only posts published on or after this date",
    )
    update_parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Process only posts published on or before this date",
    )
    update_parser.add_argument(
        "--last-days",
        type=_parse_last_days,
        default=None,
        metavar="N",
        help=(
            "Process only posts from the last N days (including today). "
            "Accepts 7 or 7d. Overrides --start-date."
        ),
    )
    update_parser.add_argument(
        "--prompt-system",
        type=str,
        default=str(Path(__file__).parent / "llm" / "check_ad_prompt.p1.md"),
        metavar="FILE",
        help="System prompt file (default: llm/check_ad_prompt.p1.md)",
    )
    update_parser.add_argument(
        "--prompt-user",
        type=str,
        default=str(Path(__file__).parent / "llm" / "check_ad_prompt.p2.md"),
        metavar="FILE",
        help="User prompt template with {{ content }} placeholder (default: llm/check_ad_prompt.p2.md)",
    )
    update_parser.set_defaults(func=cmd_update)

    # --- update-nudes ---
    update_nudes_parser = subparsers.add_parser(
        "update-nudes",
        help="Compute nude_rate for video posts and write to meta.json",
    )
    update_nudes_parser.add_argument(
        "--work-dir",
        type=str,
        default=default_work_dir,
        metavar="PATH",
        help=f"Work directory with downloaded posts (default from .env WORK_DIR: {default_work_dir})",
    )
    update_nudes_parser.add_argument(
        "--channel",
        type=str,
        default=None,
        metavar="NAME",
        help="Process only this channel subdirectory (folder name, e.g. babazoyka)",
    )
    update_nudes_parser.add_argument(
        "--frames",
        type=int,
        default=10,
        metavar="N",
        help="Number of frames to sample from each video (default: 10)",
    )
    update_nudes_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-analyze posts that already have nude_check in meta.json",
    )
    update_nudes_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be analyzed without making any changes",
    )
    update_nudes_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N posts (useful for testing)",
    )
    update_nudes_parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Process only posts published on or after this date",
    )
    update_nudes_parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Process only posts published on or before this date",
    )
    update_nudes_parser.add_argument(
        "--last-days",
        type=_parse_last_days,
        default=None,
        metavar="N",
        help=(
            "Process only posts from the last N days (including today). "
            "Accepts 7 or 7d. Overrides --start-date."
        ),
    )
    update_nudes_parser.set_defaults(func=cmd_update_nudes)

    # Support 'help [command]' syntax per CLAUDE.python.md conventions.
    # Rewrite: help update  ->  update --help
    #          help         ->  --help
    if len(sys.argv) >= 2 and sys.argv[1] == "help":
        if len(sys.argv) >= 3:
            sys.argv = [sys.argv[0], sys.argv[2], "--help"]
        else:
            sys.argv = [sys.argv[0], "--help"]

    args = parser.parse_args()

    # CLI --log-level overrides .env LOG_LEVEL
    if args.log_level:
        new_level: int = getattr(logging, args.log_level.upper())
        logging.getLogger().setLevel(new_level)
        for handler in logging.getLogger().handlers:
            handler.setLevel(new_level)
        logger.debug(f"Log level overridden by CLI: {args.log_level.upper()}")

    if not args.command:
        parser.print_help()
        return

    # Validate LangSmith credentials early so auth errors are immediately visible.
    if os.getenv("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes"):
        _check_langsmith_auth()

    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        logger.error(f"Unexpected error: {exc}", exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

