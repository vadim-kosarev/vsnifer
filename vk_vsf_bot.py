"""
Telegram bot for collecting and managing video content from channels.

This bot connects to specified Telegram channels and allows viewing,
downloading, and managing video posts.
"""

import asyncio
import logging
import os
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

# Configure logging
logs_dir: Path = Path("logs")
logs_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(logs_dir / "bot.log"),
        logging.StreamHandler(),
    ],
)
logger: logging.Logger = logging.getLogger(__name__)


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
            result: list[ChannelPost] = []
            async for message in client.iter_messages(channel_id, limit=count):
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


async def main() -> None:
    """Main entry point for the bot."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Telegram bot for collecting video content from channels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python vk_vsf_bot.py
  python vk_vsf_bot.py --help
  python vk_vsf_bot.py view-recent
  python vk_vsf_bot.py view-recent --count 20
  python vk_vsf_bot.py view-recent --channel "+otRtx2aMM0ZlMTVi" --count 5
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

