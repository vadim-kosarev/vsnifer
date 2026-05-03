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
from pathlib import Path
from typing import Optional

import argparse
import socks
import TelethonFakeTLS
from dotenv import load_dotenv
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.tl.types import Message

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

    class Config:
        """Pydantic config."""

        extra = "forbid"


def build_client_kwargs() -> dict:
    """
    Build TelegramClient extra kwargs (proxy / connection) from environment variables.

    Supported PROXY_TYPE values: socks5, socks4, mtproto.
    Returns empty dict if PROXY_TYPE is not set.
    """
    proxy_type: Optional[str] = os.getenv("PROXY_TYPE", "").lower().strip()
    if not proxy_type:
        return {}

    proxy_host: Optional[str] = os.getenv("PROXY_HOST")
    proxy_port_str: Optional[str] = os.getenv("PROXY_PORT")

    if not proxy_host or not proxy_port_str:
        raise ValueError("PROXY_HOST and PROXY_PORT must be set when PROXY_TYPE is defined")

    proxy_port: int = int(proxy_port_str)

    if proxy_type == "socks5":
        return {"proxy": (socks.SOCKS5, proxy_host, proxy_port)}
    elif proxy_type == "socks4":
        return {"proxy": (socks.SOCKS4, proxy_host, proxy_port)}
    elif proxy_type == "mtproto":
        proxy_secret: str = os.getenv("PROXY_SECRET", "")
        # ee-prefix = FakeTLS: TelethonFakeTLS prepends 'ee' itself, so strip it before passing
        # dd-prefix or plain = randomized intermediate
        if proxy_secret.lower().startswith("ee"):
            secret_for_faketls: str = proxy_secret[2:]  # strip 'ee' — library adds it back internally
            return {
                "connection": TelethonFakeTLS.ConnectionTcpMTProxyFakeTLS,
                "proxy": (proxy_host, proxy_port, secret_for_faketls),
            }
        return {
            "connection": ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (proxy_host, proxy_port, proxy_secret),
        }
    else:
        raise ValueError(f"Unsupported PROXY_TYPE: '{proxy_type}'. Use: socks5, socks4, mtproto")


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
                post: ChannelPost = ChannelPost(
                    post_id=message.id,
                    text=message.text,
                    has_media=message.media is not None,
                    media_type=type(message.media).__name__ if message.media else None,
                    date=message.date.isoformat() if message.date else "Unknown",
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
        channel_id.startswith("http")      # full URL
        or channel_id.startswith("@")      # already @username
        or channel_id.startswith("+")      # private invite hash
        or channel_id.lstrip("-").isdigit()  # numeric ID
    ):
        return channel_id
    return f"@{channel_id}"


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
) -> None:
    """
    Download recent posts from a channel into work/<channel>/<post_id>/.

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
    logger.info(f"Saving posts to: {channel_dir}")

    downloaded: int = 0
    skipped: int = 0

    async for message in client.iter_messages(channel, limit=count):
        post_dir: Path = channel_dir / str(message.id)

        # Skip already downloaded posts
        if (post_dir / "meta.json").exists():
            logger.debug(f"Post {message.id} already downloaded, skipping")
            skipped += 1
            continue

        post_dir.mkdir(exist_ok=True)

        # Save metadata
        meta: dict = {
            "post_id": message.id,
            "date": message.date.isoformat() if message.date else None,
            "has_media": message.media is not None,
            "media_type": type(message.media).__name__ if message.media else None,
            "views": getattr(message, "views", None),
            "forwards": getattr(message, "forwards", None),
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
                    logger.info(f"Post {message.id}: saved media → {Path(media_path).name}")
                else:
                    logger.warning(f"Post {message.id}: media download returned None")
            except Exception as e:
                logger.error(f"Post {message.id}: media download failed: {e}")
            # Longer pause after media download
            await _human_delay(short=False)
        else:
            logger.info(f"Post {message.id}: text only")
            # Short pause between posts
            await _human_delay(short=True)

        downloaded += 1

    logger.info(f"Done: {downloaded} downloaded, {skipped} already existed")


async def cmd_download(args: argparse.Namespace) -> None:
    """
    Handle 'download' command — fetch posts with media into work/<channel>/.
    """
    channel_id: str = args.channel or os.getenv("TARGET_CHANNEL", "")
    channel_id = normalize_channel_id(channel_id)
    count: int = args.count
    work_dir: Path = Path(args.work_dir)

    if not channel_id:
        logger.error("Channel not specified. Use --channel or set TARGET_CHANNEL in .env")
        raise ValueError("Channel not specified")

    logger.info(f"Downloading {count} posts from {channel_id} → work/{channel_to_folder_name(channel_id)}/")

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

    try:
        if credentials.phone:
            await asyncio.wait_for(client.start(phone=credentials.phone), timeout=120)
        else:
            await asyncio.wait_for(
                client.start(bot_token=credentials.bot_token), timeout=30
            )

        await asyncio.wait_for(
            download_channel_posts(client, channel_id, count, work_dir),
            timeout=3600,  # 1 hour max for large downloads
        )
    except Exception as e:
        logger.error(f"Download error: {e}")
        raise
    finally:
        await client.disconnect()


async def main() -> None:
    """Main entry point for the bot."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Telegram bot for collecting video content from channels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python vk_vsf_bot.py view-recent
  python vk_vsf_bot.py view-recent --channel @babazoyka --count 20
  python vk_vsf_bot.py download
  python vk_vsf_bot.py download --channel @babazoyka --count 50
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
        default=os.getenv("TARGET_CHANNEL", ""),
        help="Channel ID or username (default from .env)",
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

