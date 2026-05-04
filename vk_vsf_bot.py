"""
Telegram bot for collecting and managing video content from channels.

This bot connects to specified Telegram channels and allows viewing,
downloading, and managing video posts.
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import argparse
from dotenv import load_dotenv
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.tl.types import Message

from config import (
    ConnectionDropMonitor,
    ProxyManager,
    ProxySwitchNeeded,
    load_app_config,
)

# Load environment variables
load_dotenv()

# Configure logging — log file named after the script: logs/<script_name>.log
_log_level: int = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
_logs_dir: Path = Path("logs")
_logs_dir.mkdir(exist_ok=True)
_log_file: Path = _logs_dir / f"{Path(__file__).stem}.log"

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_log_file),
        logging.StreamHandler(),
    ],
)
logger: logging.Logger = logging.getLogger(__name__)
logger.debug(f"Logging level: {logging.getLevelName(_log_level)}, log file: {_log_file}")


class TelegramCredentials(BaseModel):
    """Telegram API credentials."""

    api_id: int
    api_hash: str
    phone: Optional[str] = None  # User account phone (international format, e.g. +79001234567)
    bot_token: Optional[str] = None  # Bot token (alternative to phone auth)

    class Config:
        """Pydantic config."""

        extra = "forbid"


class ChannelPost(BaseModel):
    """Represents a post from a Telegram channel."""

    post_id: int
    text: Optional[str] = None
    has_media: bool = False
    media_type: Optional[str] = None
    date: str
    views: Optional[int] = None
    forwards: Optional[int] = None
    replies: Optional[int] = None
    reactions_total: Optional[int] = None
    reactions: Optional[dict[str, int]] = None  # {"👍": 12, "❤": 5, ...}

    class Config:
        """Pydantic config."""

        extra = "forbid"


def build_client_kwargs() -> dict:
    """
    Build TelegramClient extra kwargs from the proxy list in .env.json,
    falling back to the single-proxy settings in .env.

    Kept for backward compatibility with fetch_recent_posts.
    For download commands with rotation use ProxyManager directly.
    """
    return ProxyManager(load_app_config().proxies).build_client_kwargs()


def parse_reactions(message) -> tuple[Optional[int], Optional[dict[str, int]]]:
    """
    Extract reaction counts from a Telethon message.

    Returns:
        (reactions_total, reactions_breakdown) where breakdown maps
        emoji string → count. Custom emoji are keyed as "custom:<doc_id>".
        Both values are None if the message has no reactions.
    """
    msg_reactions = getattr(message, "reactions", None)
    if not msg_reactions or not getattr(msg_reactions, "results", None):
        return None, None

    breakdown: dict[str, int] = {}
    total: int = 0
    for r in msg_reactions.results:
        count: int = r.count
        total += count
        emoticon: Optional[str] = getattr(r.reaction, "emoticon", None)
        if emoticon:
            breakdown[emoticon] = count
        else:
            doc_id = getattr(r.reaction, "document_id", None)
            if doc_id:
                breakdown[f"custom:{doc_id}"] = count

    return total, breakdown or None


async def get_credentials() -> TelegramCredentials:
    """
    Load Telegram credentials from environment variables.

    Returns:
        TelegramCredentials: Loaded credentials.

    Raises:
        ValueError: If required environment variables are missing or invalid.
    """
    api_id: Optional[str] = os.getenv("API_ID")
    api_hash: Optional[str] = os.getenv("API_HASH")
    bot_token: Optional[str] = os.getenv("BOT_TOKEN")
    phone: Optional[str] = os.getenv("PHONE")

    if not all([api_id, api_hash]):
        raise ValueError("Missing required environment variables: API_ID, API_HASH")

    if not phone and not bot_token:
        raise ValueError("Missing auth method: set PHONE (user session) or BOT_TOKEN in .env")

    # Check for placeholder values
    placeholder_values: set[str] = {"YOUR_API_ID", "YOUR_API_HASH", "your_api_id_here", "your_api_hash_here"}
    if api_id in placeholder_values or api_hash in placeholder_values:
        raise ValueError("Placeholder values in .env")

    try:
        return TelegramCredentials(
            api_id=int(api_id),
            api_hash=api_hash,
            phone=phone,
            bot_token=bot_token,
        )
    except ValueError as e:
        raise ValueError(f"Invalid API_ID format. Must be a number, got: {api_id}") from e



async def fetch_recent_posts(
    channel_id: str, count: int = 10
) -> list[ChannelPost]:
    """
    Fetch recent posts from a Telegram channel.

    Args:
        channel_id: Channel identifier (e.g., '+otRtx2aMM0ZlMTVi').
        count: Number of recent posts to fetch.

    Returns:
        List of ChannelPost objects.
    """
    try:
        credentials: TelegramCredentials = await get_credentials()
    except ValueError as e:
        logger.error(f"Credential error: {e}")
        raise

    client: TelegramClient = TelegramClient(
        "vsnifer_session",
        credentials.api_id,
        credentials.api_hash,
        **build_client_kwargs(),
    )

    posts: list[ChannelPost] = []

    try:
        # Prefer user session (phone) over bot token — bots can't read arbitrary channels
        if credentials.phone:
            logger.info("Using user session auth (phone)")
            await asyncio.wait_for(
                client.start(phone=credentials.phone),
                timeout=120,  # Extra time for manual code entry on first run
            )
        else:
            logger.info("Using bot token auth")
            await asyncio.wait_for(
                client.start(bot_token=credentials.bot_token),
                timeout=30,
            )
        logger.info(f"Authenticated, fetching recent {count} posts from channel: {channel_id}")

        async def _collect_posts() -> list[ChannelPost]:
            # Resolve channel entity (uses cache if available)
            _work_dir = Path(os.getenv("WORK_DIR", "H:\\TEMP\\vk_vsf"))
            channel = await resolve_channel(client, channel_id, _work_dir / channel_to_folder_name(channel_id))
            result: list[ChannelPost] = []
            async for message in client.iter_messages(channel, limit=count):
                reactions_total, reactions = parse_reactions(message)
                post: ChannelPost = ChannelPost(
                    post_id=message.id,
                    text=message.text,
                    has_media=message.media is not None,
                    media_type=type(message.media).__name__ if message.media else None,
                    date=message.date.isoformat() if message.date else "Unknown",
                    views=getattr(message, "views", None),
                    forwards=getattr(message, "forwards", None),
                    replies=getattr(message.replies, "replies", None) if getattr(message, "replies", None) else None,
                    reactions_total=reactions_total,
                    reactions=reactions,
                )
                result.append(post)
                logger.debug(f"Fetched post {post.post_id}: {post.text[:50] if post.text else 'No text'}...")
            return result

        posts = await asyncio.wait_for(_collect_posts(), timeout=60)

        logger.info(f"Successfully fetched {len(posts)} posts")

    except Exception as e:
        logger.error(f"Error fetching posts: {e}")
        raise
    finally:
        await client.disconnect()

    return posts


async def display_posts(posts: list[ChannelPost]) -> None:
    """
    Display fetched posts in formatted output.

    Args:
        posts: List of ChannelPost objects to display.
    """
    if not posts:
        logger.info("No posts found")
        return

    logger.info(f"\n{'=' * 80}")
    logger.info(f"Found {len(posts)} posts")
    logger.info(f"{'=' * 80}\n")

    for i, post in enumerate(posts, 1):
        logger.info(f"Post #{i} (ID: {post.post_id})")
        logger.info(f"Date: {post.date}")
        logger.info(f"Has media: {post.has_media}")
        if post.media_type:
            logger.info(f"Media type: {post.media_type}")
        if post.views is not None:
            logger.info(f"Views: {post.views}")
        if post.forwards is not None:
            logger.info(f"Forwards: {post.forwards}")
        if post.replies is not None:
            logger.info(f"Replies: {post.replies}")
        if post.reactions_total is not None:
            breakdown: str = ""
            if post.reactions:
                breakdown = "  " + "  ".join(f"{e}×{c}" for e, c in post.reactions.items())
            logger.info(f"Reactions: {post.reactions_total}{breakdown}")
        if post.text:
            text_preview: str = post.text[:200]
            logger.info(f"Text: {text_preview}{'...' if len(post.text) > 200 else ''}")
        logger.info("-" * 80)


async def cmd_view_recent(args: argparse.Namespace) -> None:
    """
    Handle 'view-recent' command to show recent channel posts.

    Args:
        args: Parsed command arguments.
    """
    channel_id: str = args.channel or os.getenv("TARGET_CHANNEL", "+otRtx2aMM0ZlMTVi")
    channel_id = normalize_channel_id(channel_id)
    count: int = args.count

    try:
        logger.info(f"Fetching {count} recent posts from channel: {channel_id}")
        posts: list[ChannelPost] = await fetch_recent_posts(channel_id, count)
        await display_posts(posts)
    except ValueError as e:
        error_msg: str = str(e)
        if "Placeholder" in error_msg:
            logger.error(
                "Configuration error: Please update .env file with actual Telegram credentials.\n"
                "1. Go to https://my.telegram.org/apps\n"
                "2. Create application and get API_ID and API_HASH\n"
                "3. Update .env file with real values"
            )
        elif "Missing required" in error_msg:
            logger.error(f"Configuration error: {error_msg}")
        else:
            logger.error(f"Error: {error_msg}")
        raise


async def _human_delay(short: bool = False) -> None:
    """
    Sleep for a random human-like interval.

    short=True  →  0.3–1.2s  (between reading posts)
    short=False →  1.5–4.5s  (after downloading media)
    """
    delay: float = random.uniform(0.3, 1.2) if short else random.uniform(1.5, 4.5)
    logger.debug(f"Sleeping {delay:.1f}s (human delay)")
    await asyncio.sleep(delay)


def normalize_channel_id(channel_id: str) -> str:
    """
    Normalize channel identifier — only adds '@' for bare usernames.

    Leaves untouched:
      - https://t.me/... URLs
      - @username
      - +<invite_hash>
      - numeric IDs
    """
    channel_id = channel_id.strip()
    if (
        channel_id.startswith("http")        # full URL
        or channel_id.startswith("@")        # already @username
        or channel_id.startswith("+")        # private invite hash
        or channel_id.lstrip("-").isdigit()  # numeric ID
    ):
        return channel_id
    return f"@{channel_id}"


def parse_channels_env() -> list[str]:
    """
    Parse CHANNELS env var (comma-separated list) into normalized channel IDs.

    Falls back to TARGET_CHANNEL if CHANNELS is not set.
    Returns an empty list if neither is configured.

    Examples of valid .env entries:
      CHANNELS=@babazoyka
      CHANNELS=@babazoyka, https://t.me/+5wnJFWU8yLZjNTdi, +otRtx2aMM0ZlMTVi
    """
    raw: str = os.getenv("CHANNELS", "").strip()
    if raw:
        channels: list[str] = [
            normalize_channel_id(c)
            for c in raw.split(",")
            if c.strip()
        ]
        logger.debug(f"Parsed CHANNELS env: {channels}")
        return channels

    # Fallback: single TARGET_CHANNEL
    target: str = os.getenv("TARGET_CHANNEL", "").strip()
    if target:
        logger.debug(f"CHANNELS not set, falling back to TARGET_CHANNEL: {target}")
        return [normalize_channel_id(target)]

    return []


def channel_to_folder_name(channel_id: str) -> str:
    """Convert any channel identifier to a safe folder name."""
    name: str = channel_id
    # Strip URL prefix: https://t.me/ or t.me/
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    # Strip leading punctuation
    name = name.lstrip("@").lstrip("+")
    # Replace characters unsafe for folder names
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name or "unknown_channel"


async def resolve_channel(client: TelegramClient, channel_id: str, channel_dir: Path):
    """
    Resolve channel_id to a Telethon entity, caching the numeric ID.

    On first call: calls get_entity(channel_id), saves entity.id to
    channel_dir/channel_id.txt so future calls use the stable numeric ID directly.

    Returns the resolved entity object.
    """
    cache_file: Path = channel_dir / "channel_id.txt"

    if cache_file.exists():
        cached_id: int = int(cache_file.read_text(encoding="utf-8").strip())
        logger.debug(f"Using cached channel ID: {cached_id}")
        entity = await client.get_entity(cached_id)
    else:
        logger.info(f"Resolving channel: {channel_id}")
        entity = await client.get_entity(channel_id)
        channel_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(str(entity.id), encoding="utf-8")
        logger.info(f"Resolved channel ID: {entity.id} → cached to {cache_file}")

    return entity


async def download_channel_posts(
    client: TelegramClient,
    channel_id: str,
    count: int,
    work_dir: Path,
    since: Optional[datetime] = None,
) -> None:
    """
    Download recent posts from a channel into work/<channel>/<post_id>/.

    Args:
        since: If set, skip posts older than this datetime and stop iterating
               once the boundary is crossed (posts are returned newest→oldest).

    Each post folder contains:
      meta.json       — post metadata (id, date, media type, views, forwards)
      text.txt        — post text (if any)
      channel_id.txt  — cached numeric channel ID (channel root folder)
      <media>         — photo/video/document files (if any)
    """
    channel_dir: Path = work_dir / channel_to_folder_name(channel_id)
    channel_dir.mkdir(parents=True, exist_ok=True)

    # Resolve once → get stable entity, cache numeric ID
    channel = await resolve_channel(client, channel_id, channel_dir)
    if since:
        logger.info(f"Saving posts to: {channel_dir} (since {since.isoformat()})")
    else:
        logger.info(f"Saving posts to: {channel_dir}")

    downloaded: int = 0
    skipped: int = 0
    total: int = 0  # incremented as we iterate (actual messages received)
    ch: str = channel_dir.name  # short channel label for log messages

    async for message in client.iter_messages(channel, limit=count):
        total += 1

        # iter_messages returns newest→oldest; once we go past the since boundary, stop
        if since and message.date and message.date < since:
            logger.info(f"[{ch}][{total}] Post {message.id} ({message.date}) is before --since, stopping")
            break

        post_dir: Path = channel_dir / str(message.id)

        # Skip already downloaded posts
        if (post_dir / "meta.json").exists():
            logger.info(f"[{ch}][{total}/{count}] Post {message.id}: already downloaded, skipping")
            skipped += 1
            continue

        logger.info(f"[{ch}][{total}/{count}] Post {message.id} ({message.date})")

        post_dir.mkdir(exist_ok=True)

        # Save metadata
        reactions_total, reactions = parse_reactions(message)
        meta: dict = {
            "post_id": message.id,
            "date": message.date.isoformat() if message.date else None,
            "has_media": message.media is not None,
            "media_type": type(message.media).__name__ if message.media else None,
            "views": getattr(message, "views", None),
            "forwards": getattr(message, "forwards", None),
            "replies": getattr(message.replies, "replies", None) if getattr(message, "replies", None) else None,
            "reactions_total": reactions_total,
            "reactions": reactions,
        }
        (post_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Save text
        if message.text:
            (post_dir / "text.txt").write_text(message.text, encoding="utf-8")

        # Download media
        if message.media:
            try:
                media_path: Optional[Path] = await client.download_media(
                    message, file=str(post_dir) + "/"
                )
                if media_path:
                    logger.info(f"[{ch}][{total}/{count}] Post {message.id}: saved media → {Path(media_path).name}")
                else:
                    logger.warning(f"[{ch}][{total}/{count}] Post {message.id}: media download returned None")
            except Exception as e:
                logger.error(f"[{ch}][{total}/{count}] Post {message.id}: media download failed: {e}")
            await _human_delay(short=False)
        else:
            logger.info(f"[{ch}][{total}/{count}] Post {message.id}: text only")
            await _human_delay(short=True)

        downloaded += 1

    logger.info(f"Done: {downloaded} downloaded, {skipped} already existed (total seen: {total})")


async def _proxy_watchdog(monitor: ConnectionDropMonitor, check_interval: float = 15.0) -> None:
    """
    Periodically check the connection drop counter.
    Raises ProxySwitchNeeded when the threshold is exceeded.
    """
    while True:
        await asyncio.sleep(check_interval)
        if monitor.should_switch():
            raise ProxySwitchNeeded(
                f"Connection dropped {monitor.drop_count} times — proxy switch required"
            )


async def _download_with_watchdog(
    client: TelegramClient,
    channel_id: str,
    count: int,
    work_dir: Path,
    since: Optional[datetime],
    monitor: ConnectionDropMonitor,
) -> bool:
    """
    Run download_channel_posts concurrently with a proxy watchdog task.

    Returns:
        True  — the watchdog triggered a proxy switch (download was interrupted).
        False — the download completed normally.

    Raises any exception coming from the download task (non-proxy errors).
    """
    download_task = asyncio.create_task(
        download_channel_posts(client, channel_id, count, work_dir, since)
    )
    watchdog_task = asyncio.create_task(_proxy_watchdog(monitor))

    done, pending = await asyncio.wait(
        {download_task, watchdog_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    if watchdog_task in done and not watchdog_task.cancelled():
        exc = watchdog_task.exception()
        if isinstance(exc, ProxySwitchNeeded):
            logger.warning(str(exc))
            return True
        if exc is not None:
            raise exc

    if download_task in done and not download_task.cancelled():
        exc = download_task.exception()
        if exc is not None:
            raise exc

    return False


async def cmd_download(args: argparse.Namespace) -> None:
    """
    Handle 'download' command — fetch posts with media into work/<channel>/.

    Channel resolution:
      --channel @foo      -> single channel
      --all-channels      -> all channels from CHANNELS env var
      (neither)           -> TARGET_CHANNEL env var (single fallback)

    Proxy rotation:
      Proxies are read from .env.json (proxies list).  A ConnectionDropMonitor
      watches the Telethon connection logger for "Server closed the connection"
      events.  When proxy_switch_max_drops drops occur within
      proxy_switch_window_secs, the download is interrupted, the proxy is
      advanced to the next one in the list, a new TelegramClient is created,
      and the download resumes (already-downloaded posts are skipped).
    """
    work_dir: Path = Path(args.work_dir)

    # Determine channel list
    if args.all_channels:
        channels: list[str] = parse_channels_env()
        if not channels:
            logger.error("--all-channels: CHANNELS is not set in .env")
            raise ValueError("CHANNELS not configured")
    elif args.channel:
        channels = [normalize_channel_id(args.channel)]
    else:
        target: str = os.getenv("TARGET_CHANNEL", "").strip()
        if not target:
            logger.error("No channel specified. Use --channel, --all-channels, or set TARGET_CHANNEL in .env")
            raise ValueError("No channel specified")
        channels = [normalize_channel_id(target)]

    if not channels:
        logger.error("No channels configured. Use --channel or set CHANNELS in .env")
        raise ValueError("No channels configured")

    count: int = args.count

    # Parse --since date filter
    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
            # Make timezone-aware (UTC) if naive — Telegram dates are always UTC
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            logger.info(f"Filtering posts since: {since.isoformat()}")
        except ValueError:
            logger.error(
                f"Invalid --since value: {args.since!r}. "
                "Use ISO format: 2026-04-01 or 2026-04-01T10:00:00"
            )
            raise ValueError(f"Invalid --since: {args.since!r}")

    logger.info(f"Downloading {count} posts per channel from {len(channels)} channel(s): {channels}")

    try:
        credentials: TelegramCredentials = await get_credentials()
    except ValueError as e:
        logger.error(f"Credential error: {e}")
        raise

    # --- Proxy manager + connection drop monitor --------------------------
    app_config = load_app_config()
    proxy_manager = ProxyManager(app_config.proxies)
    monitor = ConnectionDropMonitor(
        max_drops=app_config.proxy_switch_max_drops,
        window_secs=app_config.proxy_switch_window_secs,
    )
    monitor.attach()

    completed_channels: set[str] = set()
    # At least one attempt; at most one attempt per proxy in the list.
    max_attempts: int = max(proxy_manager.count, 1)

    for attempt in range(max_attempts):
        remaining: list[str] = [ch for ch in channels if ch not in completed_channels]
        if not remaining:
            break

        if attempt > 0:
            proxy_manager.advance()

        logger.info(
            f"Proxy attempt {attempt + 1}/{max_attempts}: {proxy_manager.describe_current()}"
        )

        client: TelegramClient = TelegramClient(
            "vsnifer_session",
            credentials.api_id,
            credentials.api_hash,
            **proxy_manager.build_client_kwargs(),
        )

        switch_triggered: bool = False
        try:
            if credentials.phone:
                await asyncio.wait_for(client.start(phone=credentials.phone), timeout=120)
            else:
                await asyncio.wait_for(
                    client.start(bot_token=credentials.bot_token), timeout=30
                )

            for channel_id in remaining:
                logger.info(f"--- Channel: {channel_id} ---")
                try:
                    switch_triggered = await _download_with_watchdog(
                        client, channel_id, count, work_dir, since, monitor
                    )
                except Exception as e:
                    logger.error(f"Failed to download from {channel_id}: {e}")
                    switch_triggered = False

                if switch_triggered:
                    logger.warning(
                        f"Proxy switch triggered during '{channel_id}' — "
                        f"will reconnect and resume"
                    )
                    monitor.reset()
                    break  # exit channel loop; outer loop reconnects with next proxy

                completed_channels.add(channel_id)

        except Exception as e:
            logger.error(f"Client error on attempt {attempt + 1}: {e}")
        finally:
            await client.disconnect()

    monitor.detach()
    logger.info(
        f"Download finished. "
        f"Completed {len(completed_channels)}/{len(channels)} channel(s)."
    )


async def main() -> None:
    """Main entry point for the bot."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Telegram bot for collecting video content from channels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python vk_vsf_bot.py view-recent
  python vk_vsf_bot.py view-recent --channel @babazoyka --count 20
  python vk_vsf_bot.py download --channel @babazoyka --count 50
  python vk_vsf_bot.py download --all-channels --count 20
  python vk_vsf_bot.py download --all-channels --since 2026-04-01
  python vk_vsf_bot.py download --all-channels --since 2026-04-01T10:00:00 --count 100
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # view-recent command
    view_recent_parser = subparsers.add_parser(
        "view-recent",
        help="View recent posts from a channel",
    )
    view_recent_parser.add_argument(
        "--channel",
        type=str,
        default=os.getenv("TARGET_CHANNEL", "+otRtx2aMM0ZlMTVi"),
        help="Channel ID or username (default from .env)",
    )
    view_recent_parser.add_argument(
        "--count",
        type=int,
        default=int(os.getenv("RECENT_POSTS_COUNT", "10")),
        help="Number of recent posts to fetch (default: 10)",
    )
    view_recent_parser.set_defaults(func=cmd_view_recent)

    # download command
    download_parser = subparsers.add_parser(
        "download",
        help="Download recent posts with media into work/<channel>/",
    )
    download_parser.add_argument(
        "--channel",
        type=str,
        default="",
        help="Single channel to download from (overrides --all-channels and TARGET_CHANNEL)",
    )
    download_parser.add_argument(
        "--all-channels",
        action="store_true",
        default=False,
        help="Download from all channels listed in CHANNELS env var",
    )
    download_parser.add_argument(
        "--count",
        type=int,
        default=int(os.getenv("RECENT_POSTS_COUNT", "10")),
        help="Number of recent posts to download (default: 10)",
    )
    download_parser.add_argument(
        "--work-dir",
        type=str,
        default=os.getenv("WORK_DIR", r"work"),
        help=r"Directory to save downloaded posts (default from .env: H:\TEMP\vk_vsf)",
    )
    download_parser.add_argument(
        "--since",
        type=str,
        default=None,
        metavar="DATE",
        help=(
            "Only download posts published on or after this date. "
            "Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (e.g. 2026-04-01 or 2026-04-01T10:00:00). "
            "Stops iteration as soon as an older post is encountered."
        ),
    )
    download_parser.set_defaults(func=cmd_download)

    args: argparse.Namespace = parser.parse_args()

    # Show help if no command provided
    if not args.command:
        parser.print_help()
        return

    # Execute command
    try:
        if hasattr(args, "func"):
            await args.func(args)
        else:
            parser.print_help()
    except ValueError:
        # ValueError already logged, just exit cleanly
        exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        exit(1)


if __name__ == "__main__":
    asyncio.run(main())

