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
  update   Scan WORK_DIR, classify unchecked posts in batches, update meta.json.

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
"""

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests as http_requests
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

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
# Models
# ---------------------------------------------------------------------------
class PostToCheck(BaseModel):
    """
    A single post scheduled for ad classification.

    meta_path  -- absolute path to the post's meta.json (for writing results back)
    content    -- text from text.txt (sent to LLM)
    """

    id: str          # "channel/post_id"
    channel: str
    post_id: int
    meta_path: Path
    content: str

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
# Post collection
# ---------------------------------------------------------------------------
def collect_posts_to_check(
    work_dir: Path,
    channel_filter: Optional[str] = None,
    force: bool = False,
) -> list[PostToCheck]:
    """
    Scan work_dir for posts that have both meta.json and text.txt.

    Skips posts whose meta.json already contains 'ad_check' unless force=True.
    If channel_filter is given, only that channel subdirectory is scanned.
    Returns posts sorted by (channel, post_id) ascending.
    """
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

            if not force:
                try:
                    meta: dict = json.loads(meta_path.read_text(encoding="utf-8"))
                    if "ad_check" in meta:
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
                )
            )
            logger.debug(f"[{channel_name}/{post_id}] queued ({len(content)} chars)")

    logger.info(f"Found {len(posts)} post(s) to classify in {work_dir}")
    return posts


def write_ad_check_to_meta(meta_path: Path, result: AdCheckResult) -> None:
    """
    Load meta.json, add or overwrite the 'ad_check' key, and write back.
    """
    meta: dict = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["ad_check"] = {
        "ad_rate": result.ad_rate,
        "proof_of_ad": result.proof_of_ad,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


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

    try:
        raw_response: str = _call_ollama(config, prompt_system, prompt_user, think=think)
    except Exception as exc:
        logger.error(f"Ollama call failed: {exc}")
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

    prompt_before_file: Path = Path(args.prompt_before)
    prompt_after_file: Path = Path(args.prompt_after)

    for path in (prompt_before_file, prompt_after_file):
        if not path.exists():
            logger.error(f"Prompt file not found: {path}")
            raise SystemExit(1)

    config: OllamaConfig = load_ollama_config()
    if args.model:
        config = config.model_copy(update={"model": args.model})

    prompt_system: str = _render_template(prompt_before_file.read_text(encoding="utf-8"))
    prompt_after_template: str = prompt_after_file.read_text(encoding="utf-8")

    posts: list[PostToCheck] = collect_posts_to_check(
        work_dir,
        channel_filter=args.channel,
        force=args.force,
    )

    if not posts:
        logger.info("No posts to classify.")
        return

    if args.limit:
        posts = posts[: args.limit]
        logger.info(f"Limited to first {len(posts)} post(s) by --limit")

    if args.dry_run:
        logger.info(f"[DRY RUN] Would classify {len(posts)} post(s) in batches of {batch_size}")
        for p in posts:
            logger.info(f"  {p.id}")
        return

    total: int = len(posts)
    updated: int = 0
    errors: int = 0

    for batch_start in range(0, total, batch_size):
        batch: list[PostToCheck] = posts[batch_start: batch_start + batch_size]
        ids: str = ", ".join(p.id for p in batch)
        logger.info(f"Batch [{batch_start + 1}..{batch_start + len(batch)}/{total}]: {ids}")

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
                write_ad_check_to_meta(post.meta_path, result)
                logger.info(f"[{post.id}] ad_rate={result.ad_rate:.2f}  {result.proof_of_ad}")
                updated += 1
            except Exception as exc:
                logger.error(f"[{post.id}] failed to write meta.json: {exc}")
                errors += 1

    logger.info(f"Done. Updated: {updated}  Errors: {errors}  Total: {total}")


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
  update   Scan WORK_DIR, classify unchecked posts in batches, write ad_check to meta.json

Examples:
  python check_ad.py update
  python check_ad.py update --work-dir H:\\TEMP\\vk_vsf
  python check_ad.py update --batch-size 5 --model qwen3:8b
  python check_ad.py update --force
  python check_ad.py update --dry-run
  python check_ad.py update --channel babazoyka
  python check_ad.py update --limit 20
  python check_ad.py --log-level DEBUG update
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
        "--prompt-before",
        type=str,
        default=str(Path(__file__).parent / "check_ad_prompt.p1.md"),
        metavar="FILE",
        help="System prompt file (default: check_ad_prompt.p1.md)",
    )
    update_parser.add_argument(
        "--prompt-after",
        type=str,
        default=str(Path(__file__).parent / "check_ad_prompt.p2.md"),
        metavar="FILE",
        help="User prompt template with {{ content }} placeholder (default: check_ad_prompt.p2.md)",
    )
    update_parser.set_defaults(func=cmd_update)

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

    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        logger.error(f"Unexpected error: {exc}", exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

