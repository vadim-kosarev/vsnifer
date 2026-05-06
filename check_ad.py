"""
check_ad.py -- tools for inspecting and filtering post content using LLM assistance.

Commands:
  build-llm-request   Collect post texts from the work directory into numbered JSON
                      files (max 30 KB each) suitable for sending to an LLM.
  check               Send a request JSON file to Ollama with an ad-detection prompt
                      and save the classified response.

Usage:
    python check_ad.py
    python check_ad.py --help
    python check_ad.py build-llm-request --output llm_request.json
    python check_ad.py build-llm-request --work-dir H:\\TEMP\\vk_vsf --output llm_request.json
    python check_ad.py check --input-file llm_request_001.json
    python check_ad.py check --input-file llm_request_001.json --prompt-file check_ad_prompt.p1.md
    python check_ad.py check --input-file llm_request_001.json --model qwen3:8b
"""

import argparse
import json
import logging
import os
import re
from datetime import datetime
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
class PostRecord(BaseModel):
    """
    A single post entry written to the LLM request JSON.

    Fields allow the post to be uniquely located back in the work directory:
      work_dir / channel / post_id /

    id          -- compound key "channel/post_id" for quick reference
    channel     -- channel folder name (e.g. "babazoyka")
    post_id     -- numeric post ID (matches the subfolder name)
    date        -- post publication date (ISO-8601) from meta.json, None if absent
    views       -- view count from meta.json, None if absent
    content     -- full text of the post (from text.txt)
    """

    id: str                          # "channel/post_id"
    channel: str
    post_id: int
    date: Optional[str] = None
    views: Optional[int] = None
    content: str


class OllamaConfig(BaseModel):
    """Ollama server connection parameters read from environment variables."""

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
# Core logic
# ---------------------------------------------------------------------------
def collect_post_records(work_dir: Path) -> list[PostRecord]:
    """
    Recursively scan work_dir for post subdirectories that contain text.txt.

    Expected layout:
        work_dir/
          <channel>/
            <post_id>/
              meta.json
              text.txt        <-- read this
              <media>

    Posts without text.txt are silently skipped.
    Posts whose post_id directory name is not a pure integer are skipped.

    Returns records sorted by (channel, post_id) ascending.
    """
    records: list[PostRecord] = []

    for channel_dir in sorted(work_dir.iterdir()):
        if not channel_dir.is_dir():
            continue
        # Skip hidden dirs and the meta folder used by some tools
        if channel_dir.name.startswith("."):
            continue

        channel_name: str = channel_dir.name

        for post_dir in sorted(channel_dir.iterdir()):
            if not post_dir.is_dir():
                continue
            if not post_dir.name.isdigit():
                continue  # channel_id.txt and similar non-post entries
            post_id: int = int(post_dir.name)

            text_file: Path = post_dir / "text.txt"
            if not text_file.exists():
                logger.debug(f"[{channel_name}/{post_id}] no text.txt, skipping")
                continue

            content: str = ""
            try:
                content = text_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning(f"[{channel_name}/{post_id}] failed to read text.txt: {exc}")
                continue

            if not content:
                logger.debug(f"[{channel_name}/{post_id}] text.txt is empty, skipping")
                continue

            # Read optional metadata
            date_str: Optional[str] = None
            views: Optional[int] = None
            try:
                meta: dict = json.loads((post_dir / "meta.json").read_text(encoding="utf-8"))
                date_str = meta.get("date")
                views = meta.get("views")
            except Exception:
                pass

            records.append(
                PostRecord(
                    id=f"{channel_name}/{post_id}",
                    channel=channel_name,
                    post_id=post_id,
                    date=date_str,
                    views=views,
                    content=content,
                )
            )
            logger.debug(f"[{channel_name}/{post_id}] collected ({len(content)} chars)")

    logger.info(f"Collected {len(records)} post(s) with text from {work_dir}")
    return records


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _split_into_chunks(records: list[PostRecord], max_bytes: int = 30 * 1024) -> list[list[PostRecord]]:
    """
    Split records into chunks so that each chunk's JSON serialization
    does not exceed max_bytes.

    Records are added one by one; as soon as adding the next record would
    exceed the limit the current chunk is closed and a new one is started.
    A single record that exceeds the limit on its own is placed in a chunk
    by itself (cannot be split further).
    """
    chunks: list[list[PostRecord]] = []
    current: list[PostRecord] = []
    current_size: int = 2  # "[]" baseline

    for record in records:
        entry_json: str = json.dumps(record.model_dump(), ensure_ascii=False)
        # Account for ", " separator between items
        separator_size: int = 2 if current else 0
        entry_size: int = len(entry_json.encode("utf-8")) + separator_size

        if current and current_size + entry_size > max_bytes:
            chunks.append(current)
            current = []
            current_size = 2

        current.append(record)
        current_size += entry_size

    if current:
        chunks.append(current)

    return chunks


def _numbered_output_path(base: Path, index: int, total: int) -> Path:
    """
    Build a numbered output path: base.stem + "_NNN" + base.suffix.
    Width of the numeric part matches the number of digits in total.
    """
    width: int = max(3, len(str(total)))
    suffix: str = f"_{index:0{width}d}"
    return base.with_name(base.stem + suffix + base.suffix)


def cmd_build_llm_request(args: argparse.Namespace) -> None:
    """
    Build JSON files containing all post texts from the work directory.

    Output is split into multiple files of at most MAX_CHUNK_KB kilobytes each.
    Each file is numbered: llm_request_001.json, llm_request_002.json, ...

    Each entry in the JSON array is a PostRecord with channel, post_id, date,
    views, and content fields -- enough to locate the post in the work directory
    and to send to an LLM for classification.
    """
    work_dir: Path = Path(args.work_dir)
    output_base: Path = Path(args.output)
    max_kb: int = args.max_chunk_kb
    max_bytes: int = max_kb * 1024

    if not work_dir.exists():
        logger.error(f"Work directory not found: {work_dir}")
        raise SystemExit(1)

    logger.info(f"Scanning work directory: {work_dir}")
    records: list[PostRecord] = collect_post_records(work_dir)

    if not records:
        logger.warning("No posts with text found -- output file will not be written")
        return

    chunks: list[list[PostRecord]] = _split_into_chunks(records, max_bytes=max_bytes)
    logger.info(f"Splitting {len(records)} record(s) into {len(chunks)} file(s) (max {max_kb} KB each)")

    output_base.parent.mkdir(parents=True, exist_ok=True)

    for i, chunk in enumerate(chunks, start=1):
        out_path: Path = _numbered_output_path(output_base, i, len(chunks))
        payload: list[dict] = [r.model_dump() for r in chunk]
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        size_kb: float = out_path.stat().st_size / 1024
        logger.info(f"  [{i}/{len(chunks)}] {out_path.name}  ({len(chunk)} records, {size_kb:.1f} KB)")

    logger.info(f"Done. {len(chunks)} file(s) written to: {output_base.parent}")


def _render_template(template: str, extra: Optional[dict[str, str]] = None) -> str:
    """
    Replace {{ PLACEHOLDER }} tokens in a template string.

    Resolution order for each token:
      1. extra dict (e.g. {"content": "<json data>"})
      2. os.environ

    Unknown placeholders that are not found in either source are left as-is
    and a warning is logged so the user notices missing variables.
    """
    def _replace(match: re.Match) -> str:
        key: str = match.group(1).strip()
        if extra and key in extra:
            return extra[key]
        env_val: Optional[str] = os.environ.get(key)
        if env_val is not None:
            return env_val
        logger.warning(f"Template placeholder '{{{{ {key} }}}}' not resolved (no env var or extra value)")
        return match.group(0)  # leave unchanged

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace, template)



def _strip_think_tags(text: str) -> str:
    """
    Remove <think>...</think> blocks emitted by reasoning models (e.g. qwen3).
    The model thinks aloud before producing the actual answer.
    """
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json_array(text: str) -> str:
    """
    Extract a JSON array from LLM response text.

    Handles three common patterns:
      1. ```json [ ... ] ```  -- markdown code block with language tag
      2. ``` [ ... ] ```       -- markdown code block without language tag
      3. Raw text starting with '['
    Falls back to returning the full text if nothing matches.
    """
    # Markdown code block (with or without 'json' tag)
    m = re.search(r"```(?:json)?\s*(\[.*?])\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Bare JSON array anywhere in the text
    m = re.search(r"(\[.*])", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _call_ollama(config: OllamaConfig, prompt_system: str, prompt_user: str, think: bool = False) -> str:
    """
    POST to Ollama /api/chat with system + user roles and streaming enabled.

    Using /api/chat (instead of /api/generate) ensures that instruction-tuned
    models (qwen3, llama3, etc.) handle the system prompt and user request
    as intended -- they are trained on this role separation and follow
    instructions much more reliably than in raw completion mode.

    think=False passes {"think": false} to Ollama, disabling chain-of-thought
    for models that support it (qwen3, deepseek-r1, etc.). This makes responses
    faster and prevents the model from wandering off-task.

    Streamed tokens are printed to stdout immediately.
    The full response text is accumulated and returned when the stream ends.
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
    logger.info(f"Ollama request: POST {url}  model={config.model}  {think_label}")
    logger.debug(f"System prompt: {len(prompt_system)} chars  |  User prompt: {len(prompt_user)} chars")
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
            # /api/chat puts the token at chunk["message"]["content"]
            token: str = chunk.get("message", {}).get("content", "")
            if token:
                print(token, end="", flush=True)
                full_response.append(token)
            if chunk.get("done"):
                break

    print("\n--- end ---\n", flush=True)
    return "".join(full_response)


def _response_output_path(input_path: Path) -> Path:
    """
    Derive the response output path from the input path.
    llm_request_001.json  ->  llm_request_001_response.json
    """
    return input_path.with_name(input_path.stem + "_response" + input_path.suffix)


def cmd_check(args: argparse.Namespace) -> None:
    """
    Send an LLM request file to Ollama for ad classification via /api/chat.

    Uses system/user role separation so instruction-tuned models follow the
    prompt reliably:
      SYSTEM  = --prompt-before (p1): expert persona + ad rules + output format
      USER    = input JSON data + --prompt-after (p2): strict JSON-only reminder

    Steps:
      1. Read p1 (system) and p2 (user suffix) prompt files.
      2. Read the input JSON file.
      3. POST to Ollama /api/chat with stream=True; tokens printed live.
      4. Strip <think> blocks and extract the JSON array from the response.
      5. Save parsed JSON to <input_stem>_response.json.
      6. On parse failure, save raw response to <input_stem>_response.raw.txt.
    """
    input_file: Path = Path(args.input_file)
    prompt_before_file: Path = Path(args.prompt_before)
    prompt_after_file: Path = Path(args.prompt_after)

    if not input_file.exists():
        logger.error(f"Input file not found: {input_file}")
        raise SystemExit(1)
    if not prompt_before_file.exists():
        logger.error(f"Prompt-before file not found: {prompt_before_file}")
        raise SystemExit(1)
    if not prompt_after_file.exists():
        logger.error(f"Prompt-after file not found: {prompt_after_file}")
        raise SystemExit(1)

    config: OllamaConfig = load_ollama_config()
    if args.model:
        config = config.model_copy(update={"model": args.model})

    prompt_system_raw: str = prompt_before_file.read_text(encoding="utf-8")
    prompt_template: str = prompt_after_file.read_text(encoding="utf-8")
    input_text: str = input_file.read_text(encoding="utf-8")

    # Render env-var placeholders in system prompt (no {{ content }} there)
    prompt_system: str = _render_template(prompt_system_raw)

    # Render user template: {{ content }} → input data, rest → env vars
    if "{{ content }}" not in prompt_template:
        logger.warning(
            f"Placeholder '{{{{ content }}}}' not found in {prompt_after_file} -- "
            "input data will NOT be sent"
        )
        prompt_user: str = _render_template(prompt_template)
    else:
        prompt_user = _render_template(prompt_template, extra={"content": input_text})

    logger.info(f"Input file:     {input_file}  ({input_file.stat().st_size / 1024:.1f} KB)")
    logger.info(f"System prompt:  {prompt_before_file}  ({len(prompt_system)} chars)")
    logger.info(f"User template:  {prompt_after_file}  ({len(prompt_template)} chars)")
    logger.info(f"Total user msg: {len(prompt_user.encode('utf-8')) / 1024:.1f} KB")

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("=== SYSTEM PROMPT ===\n%s\n=== END SYSTEM PROMPT ===", prompt_system)
        logger.debug("=== USER MESSAGE ===\n%s\n=== END USER MESSAGE ===", prompt_user)

    raw_response: str = _call_ollama(config, prompt_system, prompt_user, think=args.think)
    logger.debug(f"Raw response ({len(raw_response)} chars):\n%s", raw_response)

    cleaned: str = _strip_think_tags(raw_response)
    json_text: str = _extract_json_array(cleaned)

    output_file: Path = _response_output_path(input_file)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.error(f"LLM response is not valid JSON: {exc}")
        raw_out: Path = output_file.with_suffix(".raw.txt")
        raw_out.write_text(raw_response, encoding="utf-8")
        logger.info(f"Raw response saved for inspection: {raw_out}")
        raise SystemExit(1)

    output_file.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Response saved: {output_file}  ({output_file.stat().st_size / 1024:.1f} KB)")
    if isinstance(parsed, list):
        logger.info(f"Classified {len(parsed)} post(s)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Main entry point."""
    default_work_dir: str = os.getenv("WORK_DIR", r"H:\TEMP\vk_vsf")
    default_output: str = "llm_request.json"

    parser = argparse.ArgumentParser(
        description="Tools for inspecting and filtering post content with LLM assistance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  build-llm-request   Collect all post texts into numbered JSON files (max 30 KB each)
  check               Send a request file to Ollama and save the classified response

Examples:
  python check_ad.py build-llm-request
  python check_ad.py build-llm-request --output my_request.json
  python check_ad.py build-llm-request --work-dir H:\\TEMP\\vk_vsf --output llm_request.json
  python check_ad.py build-llm-request --max-chunk-kb 50
  python check_ad.py check --input-file llm_request_001.json
  python check_ad.py check --input-file llm_request_001.json --prompt-before check_ad_prompt.p1.md --prompt-after check_ad_prompt.p2.md
  python check_ad.py check --input-file llm_request_001.json --model qwen3:8b
  python check_ad.py --log-level DEBUG check --input-file llm_request_001.json
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

    # --- build-llm-request ---
    build_parser = subparsers.add_parser(
        "build-llm-request",
        help="Collect all post texts from work_dir into a single JSON file",
    )
    build_parser.add_argument(
        "--work-dir",
        type=str,
        default=default_work_dir,
        metavar="PATH",
        help=f"Work directory with downloaded posts (default from .env WORK_DIR: {default_work_dir})",
    )
    build_parser.add_argument(
        "--output",
        type=str,
        default=default_output,
        metavar="FILE",
        help=f"Base output JSON file path. Files are numbered automatically: llm_request_001.json, ... (default: {default_output})",
    )
    build_parser.add_argument(
        "--max-chunk-kb",
        type=int,
        default=30,
        metavar="KB",
        help="Maximum size of each output file in kilobytes (default: 30)",
    )
    build_parser.set_defaults(func=cmd_build_llm_request)

    # --- check ---
    check_parser = subparsers.add_parser(
        "check",
        help="Send a request JSON file to Ollama and save the classified response",
    )
    check_parser.add_argument(
        "--input-file",
        type=str,
        required=True,
        metavar="FILE",
        help="Path to the request JSON file (e.g. llm_request_001.json)",
    )
    check_parser.add_argument(
        "--prompt-before",
        type=str,
        default=str(Path(__file__).parent / "check_ad_prompt.p1.md"),
        metavar="FILE",
        help="Prompt file inserted BEFORE the input data (default: check_ad_prompt.p1.md)",
    )
    check_parser.add_argument(
        "--prompt-after",
        type=str,
        default=str(Path(__file__).parent / "check_ad_prompt.p2.md"),
        metavar="FILE",
        help="Prompt file inserted AFTER the input data (default: check_ad_prompt.p2.md)",
    )
    check_parser.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="MODEL",
        help="Ollama model name (default from .env OLLAMA_MODEL)",
    )
    check_parser.add_argument(
        "--think",
        action="store_true",
        default=False,
        help="Enable chain-of-thought reasoning in the model (default: off). "
             "Useful for debugging; makes responses slower and less predictable.",
    )
    check_parser.set_defaults(func=cmd_check)

    args = parser.parse_args()

    # Apply log level: CLI arg overrides .env
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

