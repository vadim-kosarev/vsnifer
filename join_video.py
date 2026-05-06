"""
join_video.py — concatenate all video files from a work directory into one Full HD file.

Scans work_dir for video files (sorted by post date or interest score from meta.json),
probes each file with ffprobe, then encodes to 1920x1080 H.264/AAC using the
ffmpeg concat filter (NOT the concat demuxer).

Why concat filter instead of concat demuxer:
  The concat demuxer glues raw packets without re-synchronising audio timestamps.
  When source clips have different sample rates, channel layouts, or no audio at
  all, the result is corrupted / silent audio from the second clip onward.
  The concat filter processes each input independently, resets all timestamps, and
  properly handles missing audio streams (replaced with generated silence).

Interest scoring (--sort interest-asc / interest-desc):
  Raw score = (reactions * w_r + forwards * w_f + replies * w_rep) / max(views, 1)
  Dividing by views normalises for channel audience size, so videos from small
  channels can outrank videos from large channels if they drove higher engagement.
  Scores are then globally min-max normalised to the [0, 1] range.
  Weights are read from .env: INTEREST_W_REACTIONS, INTEREST_W_FORWARDS,
  INTEREST_W_REPLIES (defaults: 10, 5, 2).

Usage:
    python join_video.py --output result.mp4
    python join_video.py --work-dir H:\\TEMP\\vk_vsf\\babazoyka --output babazoyka_full.mp4
    python join_video.py --output result.mp4 --sort interest-asc
"""

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
from pydantic import BaseModel

from config import AdFilterConfig, load_app_config

load_dotenv()

_log_level: int = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
_logs_dir: Path = Path("logs")
_logs_dir.mkdir(exist_ok=True)
_log_file: Path = _logs_dir / f"{Path(__file__).stem}.log"

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_log_file),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logger.debug(f"Logging level: {logging.getLevelName(_log_level)}, log file: {_log_file}")

# Video file extensions to collect
VIDEO_EXTENSIONS: set[str] = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts"}

# Output resolution — horizontal (landscape) default
OUTPUT_WIDTH_H = 1920
OUTPUT_HEIGHT_H = 1080

# Output resolution — vertical (portrait)
OUTPUT_WIDTH_V = 1080
OUTPUT_HEIGHT_V = 1920


def output_dimensions(orientation: str) -> tuple[int, int]:
    """Return (width, height) for the given orientation ('horizontal' or 'vertical')."""
    if orientation == "vertical":
        return OUTPUT_WIDTH_V, OUTPUT_HEIGHT_V
    return OUTPUT_WIDTH_H, OUTPUT_HEIGHT_H


class VideoInfo(BaseModel):
    """Probed metadata for a single video file."""

    path: Path
    has_audio: bool
    duration: float       # seconds; used to generate silence for audio-less clips
    chapter_title: str = ""    # human-readable chapter name for the output file
    interest_score: float = 0.0  # globally normalised engagement score [0, 1]


class InterestWeights(BaseModel):
    """
    Weights for computing the raw engagement rate from post metadata.

    Read from .env variables:
      INTEREST_W_REACTIONS (default 10)
      INTEREST_W_FORWARDS  (default 5)
      INTEREST_W_REPLIES   (default 2)
    """

    reactions: float = 10.0
    forwards: float = 5.0
    replies: float = 2.0


def load_interest_weights() -> InterestWeights:
    """Load InterestWeights from environment variables."""
    return InterestWeights(
        reactions=float(os.getenv("INTEREST_W_REACTIONS", "10")),
        forwards=float(os.getenv("INTEREST_W_FORWARDS", "5")),
        replies=float(os.getenv("INTEREST_W_REPLIES", "2")),
    )


def find_ffmpeg() -> Path:
    """
    Locate ffmpeg binary.
    Checks FFMPEG_HOME env var first, then falls back to PATH.
    """
    ffmpeg_home: str = os.getenv("FFMPEG_HOME", "")
    logger.debug(f"FFMPEG_HOME env: '{ffmpeg_home}'")
    if ffmpeg_home:
        candidate: Path = Path(ffmpeg_home) / "bin" / "ffmpeg.exe"
        logger.debug(f"Checking candidate: {candidate}")
        if candidate.exists():
            return candidate
        candidate = Path(ffmpeg_home) / "ffmpeg.exe"
        logger.debug(f"Checking candidate: {candidate}")
        if candidate.exists():
            return candidate
    logger.debug("Falling back to ffmpeg from PATH")
    return Path("ffmpeg")


def find_ffprobe() -> Path:
    """
    Locate ffprobe binary (same directory as ffmpeg).
    Checks FFMPEG_HOME env var first, then falls back to PATH.
    """
    ffmpeg_home: str = os.getenv("FFMPEG_HOME", "")
    if ffmpeg_home:
        for subdir in ("bin", ""):
            candidate: Path = Path(ffmpeg_home) / subdir / "ffprobe.exe"
            logger.debug(f"Checking ffprobe candidate: {candidate}")
            if candidate.exists():
                return candidate
    logger.debug("Falling back to ffprobe from PATH")
    return Path("ffprobe")


def read_post_date(post_dir: Path) -> datetime:
    """
    Read post date from meta.json next to the video file.
    Returns a timezone-aware datetime, or epoch (1970-01-01) if meta.json is missing/invalid.
    """
    meta_file: Path = post_dir / "meta.json"
    logger.debug(f"Reading post date from: {meta_file}")
    try:
        meta: dict = json.loads(meta_file.read_text(encoding="utf-8"))
        date_str: str = meta.get("date") or ""
        if date_str:
            parsed: datetime = datetime.fromisoformat(date_str)
            logger.debug(f"Post date parsed: {parsed} (from '{date_str}')")
            return parsed
        logger.debug(f"No 'date' field in meta.json: {meta_file}")
    except Exception as exc:
        logger.debug(f"Failed to read meta.json ({meta_file}): {exc}")
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def get_chapter_title(video_path: Path, work_dir: Path) -> str:
    """
    Build a human-readable chapter title for a video clip.

    Tries to read the post date from meta.json in the clip's parent dir.
    Derives channel name and post id from the path relative to work_dir.

    Examples:
      work_dir/babazoyka/12345/clip.mp4  → "2025-11-03 babazoyka/12345"
      work_dir/12345/clip.mp4            → "2025-11-03 12345"
      work_dir/clip.mp4                  → "clip"
    """
    post_dir: Path = video_path.parent
    meta_file: Path = post_dir / "meta.json"

    date_prefix: str = ""
    try:
        meta: dict = json.loads(meta_file.read_text(encoding="utf-8"))
        date_str: str = meta.get("date", "")
        if date_str:
            dt: datetime = datetime.fromisoformat(date_str)
            date_prefix = dt.strftime("%Y-%m-%d") + " "
    except Exception:
        pass

    try:
        rel_parts = video_path.relative_to(work_dir).parts
    except ValueError:
        return video_path.stem

    if len(rel_parts) >= 3:
        # multi-channel: channel / post_id / video
        label = f"{rel_parts[0]}/{rel_parts[1]}"
    elif len(rel_parts) == 2:
        # single-channel: post_id / video
        label = rel_parts[0]
    else:
        label = video_path.stem

    return f"{date_prefix}{label}"


def build_chapters_metadata(videos_info: list["VideoInfo"]) -> str:
    """
    Build an ffmetadata string with chapter markers.

    Each chapter covers the duration of one input clip.
    Timestamps are in milliseconds (TIMEBASE=1/1000).
    """
    lines: list[str] = [";FFMETADATA1"]
    offset_ms: int = 0
    for info in videos_info:
        start_ms: int = offset_ms
        end_ms: int = offset_ms + max(1, int(info.duration * 1000))
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        # escape '=' and ';' which are special in ffmetadata values
        safe_title: str = info.chapter_title.replace("=", "\\=").replace(";", "\\;")
        lines.append(f"title={safe_title}")
        offset_ms = end_ms
    return "\n".join(lines) + "\n"


def build_timestamps_text(videos_info: list["VideoInfo"]) -> str:
    """
    Build a plain-text timestamps file suitable for a YouTube description.

    Format:
      0:00 babazoyka/12345
      1:34 babazoyka/12350
      ...
    """
    lines: list[str] = []
    offset: float = 0.0
    for info in videos_info:
        h: int = int(offset // 3600)
        m: int = int((offset % 3600) // 60)
        s: int = int(offset % 60)
        ts: str = f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"
        lines.append(f"{ts} {info.chapter_title}")
        offset += info.duration
    return "\n".join(lines) + "\n"


def filter_videos_by_date(
    videos: list[Path],
    start_date: Optional[date],
    end_date: Optional[date],
) -> list[Path]:
    """
    Retain only videos whose post date (from meta.json) falls within
    [start_date, end_date] (both bounds inclusive, both optional).

    Videos without a readable meta.json are kept only when no filter is active;
    if either bound is set they are excluded with a warning.
    """
    if start_date is None and end_date is None:
        return videos

    result: list[Path] = []
    for video in videos:
        post_date: datetime = read_post_date(video.parent)
        if post_date.year == 1970:
            # Could not read date — skip when a filter is active
            logger.warning(f"No date in meta.json, skipping: {video}")
            continue
        pd: date = post_date.date()
        if start_date and pd < start_date:
            logger.debug(f"Before start-date ({start_date}), skipping: {video.name}")
            continue
        if end_date and pd > end_date:
            logger.debug(f"After end-date ({end_date}), skipping: {video.name}")
            continue
        result.append(video)
    return result


def _compute_raw_interest(meta: dict, weights: InterestWeights) -> float:
    """
    Compute the raw engagement rate for a single post.

    Formula:
        (reactions * w_r + forwards * w_f + replies * w_rep) / max(views, 1)

    Dividing all engagement signals by views removes the dependency on the
    channel's absolute audience size, making scores comparable across channels
    of very different popularity.
    """
    views: int = max(meta.get("views") or 0, 1)
    reactions: int = meta.get("reactions_total") or 0
    forwards: int = meta.get("forwards") or 0
    replies: int = meta.get("replies") or 0
    return (
        reactions * weights.reactions
        + forwards * weights.forwards
        + replies * weights.replies
    ) / views


def compute_interest_scores(
    videos: list[Path],
    weights: InterestWeights,
) -> dict[Path, float]:
    """
    Compute normalised interest scores in [0, 1] for every video in the list.

    Steps:
      1. Read meta.json from each video's post directory.
      2. Compute raw engagement rate (weighted signals / views).
      3. Apply global min-max normalisation so the most engaging video gets 1.0
         and the least engaging gets 0.0.

    Videos whose meta.json is missing or unreadable receive a raw score of 0.0.
    When all raw scores are identical (including all-zero), every video is
    assigned 0.5 so the sort order is stable but semantically neutral.
    """
    raw: dict[Path, float] = {}
    for video in videos:
        meta_file: Path = video.parent / "meta.json"
        try:
            meta: dict = json.loads(meta_file.read_text(encoding="utf-8"))
            raw[video] = _compute_raw_interest(meta, weights)
        except Exception as exc:
            logger.debug(f"No interest score for {video.name}: {exc}")
            raw[video] = 0.0

    values: list[float] = list(raw.values())
    min_v: float = min(values) if values else 0.0
    max_v: float = max(values) if values else 0.0
    span: float = max_v - min_v

    if span == 0.0:
        return {v: 0.5 for v in videos}

    return {v: (s - min_v) / span for v, s in raw.items()}


# ---------------------------------------------------------------------------
# Ad / spam filter
# ---------------------------------------------------------------------------

# A BanRule receives the full PostContext and returns:
#   None        — post passes (not banned by this rule)
#   str         — post is banned; the string is the human-readable reason
BanRule = Callable[["PostContext"], Optional[str]]


class PostContext(BaseModel):
    """
    All available data about a single post, passed to every BanRule.

    Loaded from the post directory that contains the video:
      meta.json    → views, reactions, forwards, replies, date, media_type …
      text.txt     → full post text (empty string when absent)
      stat()       → file_size_bytes (always available)
      ffprobe      → duration_seconds (populated when ffprobe is passed to
                     _load_post_context; None otherwise)
    The channel name is derived from the path relative to work_dir.
    """

    video_path: Path
    post_dir: Path
    channel_name: str             # folder name, e.g. "babazoyka"
    post_id: Optional[int] = None
    text: str = ""                # full post text; empty when no text.txt
    date: Optional[datetime] = None
    views: Optional[int] = None
    forwards: Optional[int] = None
    replies: Optional[int] = None
    reactions_total: Optional[int] = None
    reactions: Optional[dict[str, int]] = None
    has_media: bool = False
    media_type: Optional[str] = None
    # Media properties
    file_size_bytes: Optional[int] = None   # from stat(), always filled
    duration_seconds: Optional[float] = None  # from ffprobe, None if not probed
    has_text: bool = False                  # True when text.txt exists and is non-empty

    model_config = {"arbitrary_types_allowed": True}

    @property
    def file_size_mb(self) -> Optional[float]:
        """File size in megabytes, or None when unavailable."""
        return self.file_size_bytes / (1024 * 1024) if self.file_size_bytes is not None else None


def _load_post_context(
    video_path: Path,
    work_dir: Path,
    ffprobe: Optional[Path] = None,
) -> PostContext:
    """
    Build a PostContext for video_path by reading meta.json and text.txt
    from the post directory.  Missing files are silently ignored.

    When ffprobe is provided, a lightweight format-level probe is performed
    to populate duration_seconds (~0.05 s per file).
    file_size_bytes is always populated from the filesystem stat.
    """
    post_dir: Path = video_path.parent

    # Derive channel name from relative path depth
    try:
        rel_parts = video_path.relative_to(work_dir).parts
        # work_dir/channel/post_id/video  → rel_parts[0] is channel
        # work_dir/post_id/video          → use work_dir name as channel
        channel_name = rel_parts[0] if len(rel_parts) >= 3 else work_dir.name
    except ValueError:
        channel_name = post_dir.parent.name

    meta: dict = {}
    try:
        meta = json.loads((post_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    text: str = ""
    try:
        text = (post_dir / "text.txt").read_text(encoding="utf-8")
    except Exception:
        pass
    has_text: bool = bool(text.strip())

    post_date: Optional[datetime] = None
    date_str: str = meta.get("date", "")
    if date_str:
        try:
            post_date = datetime.fromisoformat(date_str)
        except Exception:
            pass

    # File size — always available from the filesystem
    file_size_bytes: Optional[int] = None
    try:
        file_size_bytes = video_path.stat().st_size
    except Exception:
        pass

    # Duration — lightweight ffprobe format probe (no stream decoding)
    duration_seconds: Optional[float] = None
    if ffprobe is not None:
        try:
            cmd: list[str] = [
                str(ffprobe), "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(video_path),
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", timeout=15
            )
            if result.returncode == 0:
                fmt: dict = json.loads(result.stdout).get("format", {})
                if fmt.get("duration"):
                    duration_seconds = float(fmt["duration"])
        except Exception as exc:
            logger.debug(f"ffprobe duration probe failed for {video_path.name}: {exc}")

    return PostContext(
        video_path=video_path,
        post_dir=post_dir,
        channel_name=channel_name,
        post_id=meta.get("post_id"),
        text=text,
        has_text=has_text,
        date=post_date,
        views=meta.get("views"),
        forwards=meta.get("forwards"),
        replies=meta.get("replies"),
        reactions_total=meta.get("reactions_total"),
        reactions=meta.get("reactions"),
        has_media=meta.get("has_media", False),
        media_type=meta.get("media_type"),
        file_size_bytes=file_size_bytes,
        duration_seconds=duration_seconds,
    )


def build_ban_rules(cfg: AdFilterConfig) -> list[BanRule]:
    """
    Build the active list of ban rules from AdFilterConfig (loaded from .env.json).

    Each rule is a callable (PostContext) -> Optional[str]:
      returns None   → video passes this rule
      returns str    → video is banned; the string is logged as the reason

    -----------------------------------------------------------------------
    Config-driven rules are built from cfg (ad_filter section in .env.json).
    Custom code-level rules can be appended at the bottom of this function
    without touching .env.json.
    -----------------------------------------------------------------------
    """
    rules: list[BanRule] = []

    # ------------------------------------------------------------------
    # Rule: text contains banned substrings (case-insensitive)
    # .env.json → ad_filter.ban_text_contains
    # ------------------------------------------------------------------
    if cfg.ban_text_contains:
        _patterns: list[str] = [p.lower() for p in cfg.ban_text_contains]

        def _rule_text_contains(
            ctx: PostContext, patterns: list[str] = _patterns
        ) -> Optional[str]:
            text_lower = ctx.text.lower()
            for p in patterns:
                if p in text_lower:
                    return f"text contains {p!r}"
            return None

        rules.append(_rule_text_contains)

    # ------------------------------------------------------------------
    # Rule: text matches banned regular expressions (case-insensitive)
    # .env.json → ad_filter.ban_text_regex
    # ------------------------------------------------------------------
    if cfg.ban_text_regex:
        _compiled: list[tuple[str, re.Pattern]] = [
            (p, re.compile(p, re.IGNORECASE | re.DOTALL))
            for p in cfg.ban_text_regex
        ]

        def _rule_text_regex(
            ctx: PostContext, compiled: list[tuple[str, re.Pattern]] = _compiled
        ) -> Optional[str]:
            for pattern_str, rx in compiled:
                if rx.search(ctx.text):
                    return f"text matches regex {pattern_str!r}"
            return None

        rules.append(_rule_text_regex)

    # ------------------------------------------------------------------
    # Rule: text mentions a banned channel (@name or t.me/name)
    # .env.json → ad_filter.ban_channel_mentions
    # ------------------------------------------------------------------
    if cfg.ban_channel_mentions:
        _channels: list[str] = [c.lower().lstrip("@") for c in cfg.ban_channel_mentions]

        def _rule_channel_mention(
            ctx: PostContext, channels: list[str] = _channels
        ) -> Optional[str]:
            text_lower = ctx.text.lower()
            for ch in channels:
                if f"@{ch}" in text_lower or f"t.me/{ch}" in text_lower:
                    return f"text mentions banned channel @{ch}"
            return None

        rules.append(_rule_channel_mention)

    # ------------------------------------------------------------------
    # Rule: post has fewer views than the minimum threshold
    # .env.json → ad_filter.ban_min_views  (0 = disabled)
    # ------------------------------------------------------------------
    if cfg.ban_min_views and cfg.ban_min_views > 0:
        _min_views: int = cfg.ban_min_views

        def _rule_min_views(
            ctx: PostContext, min_v: int = _min_views
        ) -> Optional[str]:
            if ctx.views is not None and ctx.views < min_v:
                return f"views {ctx.views} < minimum {min_v}"
            return None

        rules.append(_rule_min_views)

    # ------------------------------------------------------------------
    # Rule: clip is shorter than the minimum duration
    # .env.json → ad_filter.ban_min_duration_sec  (0 = disabled)
    # Requires ffprobe to be passed to filter_videos_by_rules.
    # ------------------------------------------------------------------
    if cfg.ban_min_duration_sec and cfg.ban_min_duration_sec > 0:
        _min_dur: float = cfg.ban_min_duration_sec

        def _rule_min_duration(
            ctx: PostContext, min_d: float = _min_dur
        ) -> Optional[str]:
            if ctx.duration_seconds is not None and ctx.duration_seconds < min_d:
                return f"duration {ctx.duration_seconds:.1f}s < minimum {min_d}s"
            return None

        rules.append(_rule_min_duration)

    # ------------------------------------------------------------------
    # Rule: clip is longer than the maximum duration
    # .env.json → ad_filter.ban_max_duration_sec  (0 = disabled)
    # ------------------------------------------------------------------
    if cfg.ban_max_duration_sec and cfg.ban_max_duration_sec > 0:
        _max_dur: float = cfg.ban_max_duration_sec

        def _rule_max_duration(
            ctx: PostContext, max_d: float = _max_dur
        ) -> Optional[str]:
            if ctx.duration_seconds is not None and ctx.duration_seconds > max_d:
                return f"duration {ctx.duration_seconds:.1f}s > maximum {max_d}s"
            return None

        rules.append(_rule_max_duration)

    # ------------------------------------------------------------------
    # Rule: file is larger than the maximum size
    # .env.json → ad_filter.ban_max_file_size_mb  (0 = disabled)
    # ------------------------------------------------------------------
    if cfg.ban_max_file_size_mb and cfg.ban_max_file_size_mb > 0:
        _max_mb: float = cfg.ban_max_file_size_mb

        def _rule_max_size(
            ctx: PostContext, max_mb: float = _max_mb
        ) -> Optional[str]:
            if ctx.file_size_mb is not None and ctx.file_size_mb > max_mb:
                return f"file size {ctx.file_size_mb:.1f} MB > maximum {max_mb} MB"
            return None

        rules.append(_rule_max_size)

    # ------------------------------------------------------------------
    # Rule: post has no text at all (text.txt missing or empty)
    # .env.json → ad_filter.ban_require_text  (false = disabled)
    # ------------------------------------------------------------------
    if cfg.ban_require_text:

        def _rule_require_text(ctx: PostContext) -> Optional[str]:
            if not ctx.has_text:
                return "post has no text (text.txt absent or empty)"
            return None

        rules.append(_rule_require_text)

    # ==================================================================
    # Custom code-level rules — add your own below this line.
    # Each rule must match the signature:
    #   (ctx: PostContext) -> Optional[str]
    # Return None to pass, return a string reason to ban.
    #
    # Available ctx fields:
    #   text, has_text, channel_name, post_id, date
    #   views, forwards, replies, reactions_total, reactions
    #   has_media, media_type
    #   file_size_bytes, file_size_mb   (always populated)
    #   duration_seconds                (populated when ffprobe passed)
    # ==================================================================

    # Example: ban very short clips (likely teasers / reposts).
    #
    # def _rule_too_short(ctx: PostContext) -> Optional[str]:
    #     if ctx.duration_seconds is not None and ctx.duration_seconds < 3:
    #         return f"clip too short ({ctx.duration_seconds:.1f}s)"
    #     return None
    # rules.append(_rule_too_short)

    # Example: ban posts that contain external (non-Telegram) URLs.
    #
    # _external_url_rx = re.compile(r"https?://(?!t\.me)\S+", re.IGNORECASE)
    # def _rule_external_url(ctx: PostContext) -> Optional[str]:
    #     if _external_url_rx.search(ctx.text):
    #         return "text contains external URL"
    #     return None
    # rules.append(_rule_external_url)

    # Example: ban posts from a specific channel regardless of text.
    #
    # def _rule_ban_channel(ctx: PostContext) -> Optional[str]:
    #     if ctx.channel_name == "some_channel_folder_name":
    #         return "channel banned"
    #     return None
    # rules.append(_rule_ban_channel)

    # Example: ban posts with a suspicious reactions-to-views ratio
    # (bots often produce many reactions with few views).
    #
    # def _rule_reaction_ratio(ctx: PostContext) -> Optional[str]:
    #     if ctx.views and ctx.reactions_total:
    #         ratio = ctx.reactions_total / ctx.views
    #         if ratio > 0.5:
    #             return f"suspicious reaction ratio {ratio:.2f}"
    #     return None
    # rules.append(_rule_reaction_ratio)

    return rules


def filter_videos_by_rules(
    videos: list[Path],
    work_dir: Path,
    rules: list[BanRule],
    ffprobe: Optional[Path] = None,
) -> list[Path]:
    """
    Apply ban rules to every video and return only those that pass all rules.

    For each video the first matching rule wins (short-circuit evaluation).
    Banned videos are logged at INFO level with the triggering rule reason.

    ffprobe: when provided, duration_seconds is populated in PostContext via a
    lightweight format probe.  Required by ban_min_duration_sec /
    ban_max_duration_sec rules.  When None, duration_seconds is always None.
    """
    if not rules:
        return videos

    needs_probe = ffprobe is not None
    if needs_probe:
        logger.debug(f"Ad filter: ffprobe duration probing enabled ({ffprobe})")

    passed: list[Path] = []
    banned_count: int = 0

    for video in videos:
        ctx: PostContext = _load_post_context(video, work_dir, ffprobe=ffprobe)
        ban_reason: Optional[str] = None

        for rule in rules:
            reason = rule(ctx)
            if reason:
                ban_reason = reason
                break

        if ban_reason:
            size_str = f"  [{ctx.file_size_mb:.1f} MB]" if ctx.file_size_mb is not None else ""
            dur_str = f"  [{ctx.duration_seconds:.1f}s]" if ctx.duration_seconds is not None else ""
            logger.info(
                f"FILTERED: [{ctx.channel_name}] post {ctx.post_id} "
                f"({video.name}){size_str}{dur_str} — {ban_reason}"
            )
            banned_count += 1
        else:
            passed.append(video)

    if banned_count:
        logger.info(
            f"Ad filter: {banned_count} video(s) excluded, "
            f"{len(passed)} video(s) passed"
        )

    return passed


def _scan_post_dirs(channel_dir: Path, videos: list[tuple[datetime, Path]]) -> None:
    """
    Scan a single channel directory for numeric post_id subdirs and collect video files.
    Appends (post_date, video_path) tuples to the provided list.
    """
    for entry in channel_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            int(entry.name)  # must be a numeric post_id dir
        except ValueError:
            logger.debug(f"Skipping non-numeric dir in {channel_dir.name}: {entry.name}")
            continue
        for f in entry.iterdir():
            if f.suffix.lower() in VIDEO_EXTENSIONS:
                post_date: datetime = read_post_date(entry)
                logger.debug(f"Found video in {channel_dir.name}/{entry.name}: {f.name} (date={post_date})")
                videos.append((post_date, f))
            else:
                logger.debug(f"Skipping non-video file: {f.name}")


def collect_videos(work_dir: Path) -> list[Path]:
    """
    Find video files under work_dir. Three modes (auto-detected):

    1. Channel mode: work_dir contains numeric post_id subdirs directly.
       → work_dir/<post_id>/<video>

    2. Multi-channel mode: work_dir contains named channel subdirs,
       each with numeric post_id subdirs.
       → work_dir/<channel>/<post_id>/<video>

    3. Flat mode: work_dir contains video files directly.
       → work_dir/<video>

    Modes 1 and 2 sort by date from meta.json (oldest first).
    Mode 3 sorts by filename.
    """
    videos: list[tuple[datetime, Path]] = []
    flat_videos: list[Path] = []
    channel_dirs: list[Path] = []

    for entry in work_dir.iterdir():
        if entry.is_dir():
            try:
                int(entry.name)  # numeric → post_id dir (channel mode)
                for f in entry.iterdir():
                    if f.suffix.lower() in VIDEO_EXTENSIONS:
                        post_date: datetime = read_post_date(entry)
                        logger.debug(f"Found video in post dir {entry.name}: {f.name} (date={post_date})")
                        videos.append((post_date, f))
                    else:
                        logger.debug(f"Skipping non-video file: {f.name}")
            except ValueError:
                # Non-numeric dir — treat as a channel subdir (multi-channel mode)
                logger.debug(f"Non-numeric dir (possible channel): {entry.name}")
                channel_dirs.append(entry)
        elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
            logger.debug(f"Found flat video: {entry.name}")
            flat_videos.append(entry)
        else:
            logger.debug(f"Skipping entry: {entry.name}")

    # Multi-channel mode: no numeric post_id dirs found directly,
    # but there are named subdirs — scan each one level deeper
    if not videos and channel_dirs:
        logger.debug(f"Multi-channel mode: scanning {len(channel_dirs)} channel dir(s)")
        for channel_dir in channel_dirs:
            _scan_post_dirs(channel_dir, videos)

    if videos:
        videos.sort(key=lambda x: x[0])
        logger.debug(f"Found {len(videos)} video(s), sorted by date")
        return [path for _, path in videos]

    # Flat mode
    flat_videos.sort(key=lambda f: f.name)
    logger.debug(f"Flat mode: {len(flat_videos)} video(s) sorted by filename")
    return flat_videos


def probe_video(ffprobe: Path, video: Path) -> Optional["VideoInfo"]:
    """
    Probe a video file with ffprobe to detect audio presence and duration.

    Returns None if the file is unreadable or has no video stream
    (corrupted file, incomplete download, missing moov atom, etc.).
    Such files are logged as warnings and skipped by the caller.

    Duration is read from the video stream first, then from the container
    format as a fallback (needed for silence-fill when a clip has no audio).
    """
    cmd: list[str] = [
        str(ffprobe),
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(video),
    ]
    logger.debug(f"Probing: {video.name}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        logger.warning(f"Skipping {video} — ffprobe failed (corrupted or incomplete file)")
        return None

    try:
        data: dict = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning(f"Skipping {video} — ffprobe returned invalid JSON: {exc}")
        return None

    streams: list[dict] = data.get("streams", [])
    fmt: dict = data.get("format", {})

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        logger.warning(f"Skipping {video} — no video stream found")
        return None

    has_audio: bool = any(s.get("codec_type") == "audio" for s in streams)

    duration: float = 0.0
    for s in video_streams:
        try:
            duration = float(s["duration"])
            break
        except (KeyError, ValueError, TypeError):
            pass

    if duration == 0.0:
        try:
            duration = float(fmt.get("duration", 0.0))
        except (ValueError, TypeError):
            pass

    logger.debug(f"  {video.name}: has_audio={has_audio}, duration={duration:.2f}s")
    return VideoInfo(path=video, has_audio=has_audio, duration=duration)


def build_filter_complex(
    videos_info: list[VideoInfo],
    audio_delay_ms: int = 0,
    out_width: int = OUTPUT_WIDTH_H,
    out_height: int = OUTPUT_HEIGHT_H,
) -> str:
    """
    Build a filter_complex string using the concat filter.

    Each input gets its own video normalization chain (blurred background +
    sharp foreground overlay) and audio normalization. Clips without an audio
    stream receive a generated silence track of matching duration.

    The concat filter resets all timestamps between segments, which eliminates
    the audio corruption that occurs with the concat demuxer when source clips
    have mismatched sample rates, channel layouts, or missing audio.

    Filter topology per input i:
      Video: [i:v] → split → blurred background + sharp foreground overlay → [vi]
      Audio: [i:a] → resample → format → asetpts reset → [ai]
          or anullsrc → atrim(duration) → asetpts reset → [ai]  (no audio)
    Final: [v0][a0][v1][a1]... concat=n=N:v=1:a=1 → [vout][aout]

    Audio sync correction (audio_delay_ms):
      > 0 : audio is late  → trim first N ms from audio output (advance audio).
      < 0 : audio is early → prepend N ms of silence to audio output (delay audio).
      = 0 : no correction applied.
    """
    parts: list[str] = []
    concat_pads: list[str] = []

    for i, info in enumerate(videos_info):
        # --- Video: blurred background + sharp foreground overlay ---
        parts.append(f"[{i}:v]split=2[orig_bg{i}][orig_fg{i}]")
        parts.append(
            f"[orig_bg{i}]scale={out_width}:{out_height}"
            f":force_original_aspect_ratio=increase,"
            f"crop={out_width}:{out_height},"
            f"boxblur=luma_radius=40:luma_power=3"
            f"[bg{i}]"
        )
        parts.append(
            f"[orig_fg{i}]scale={out_width}:{out_height}"
            f":force_original_aspect_ratio=decrease"
            f"[fg{i}]"
        )
        parts.append(f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{i}]")

        # --- Audio: normalize or generate silence ---
        if info.has_audio:
            # Normalize to 48kHz stereo fltp; asetpts resets timestamps from
            # sample count — eliminates any gaps/overlaps from the source stream
            parts.append(
                f"[{i}:a]aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"asetpts=N/SR/TB"
                f"[a{i}]"
            )
        else:
            # No audio stream — generate silence for the exact clip duration
            logger.debug(f"Input {i} ({info.path.name}): no audio, generating silence ({info.duration:.2f}s)")
            parts.append(
                f"anullsrc=r=48000:cl=stereo,"
                f"atrim=duration={info.duration:.6f},"
                f"asetpts=N/SR/TB"
                f"[a{i}]"
            )

        concat_pads.append(f"[v{i}][a{i}]")

    n: int = len(videos_info)

    if audio_delay_ms != 0:
        # Concat outputs to a raw pad, then apply sync correction
        parts.append(f"{''.join(concat_pads)}concat=n={n}:v=1:a=1[vout][aout_raw]")
        delay_s: float = abs(audio_delay_ms) / 1000.0
        if audio_delay_ms > 0:
            # Audio is late: advance audio by trimming its beginning
            logger.debug(f"Audio sync: advancing audio by {audio_delay_ms}ms (trim start)")
            parts.append(
                f"[aout_raw]atrim=start={delay_s:.6f},"
                f"asetpts=N/SR/TB"
                f"[aout]"
            )
        else:
            # Audio is early: delay audio by prepending silence
            delay_ms_str: str = f"{abs(audio_delay_ms)}"
            logger.debug(f"Audio sync: delaying audio by {abs(audio_delay_ms)}ms (adelay)")
            parts.append(f"[aout_raw]adelay={delay_ms_str}|{delay_ms_str}[aout]")
    else:
        parts.append(f"{''.join(concat_pads)}concat=n={n}:v=1:a=1[vout][aout]")

    return ";".join(parts)


def run_ffmpeg(
    ffmpeg: Path, ffprobe: Path, videos: list[Path], output: Path, work_dir: Path,
    audio_delay_ms: int = 0,
    out_width: int = OUTPUT_WIDTH_H,
    out_height: int = OUTPUT_HEIGHT_H,
) -> None:
    """
    Probe all videos, build a concat-filter command, and encode to Full HD.

    Uses concat filter (not concat demuxer) for reliable audio across clips
    with different formats, sample rates, or missing audio streams.

    The filter graph is written to a temporary file and passed via
    -filter_script to keep the command line short (avoids Windows 32 767-char
    CreateProcess limit when encoding hundreds of clips).

    After encoding:
      - Chapter markers are embedded into the MP4 via an ffmetadata sidecar.
      - A plain-text timestamps file (<output>.timestamps.txt) is written
        next to the output for use in YouTube descriptions.

    audio_delay_ms:
      Positive: audio is late  → advance audio (trim audio start).
      Negative: audio is early → delay audio (prepend silence).
      Zero:     no sync correction.
    """
    logger.info(f"Probing {len(videos)} video file(s) with ffprobe...")
    probed: list[Optional[VideoInfo]] = [probe_video(ffprobe, v) for v in videos]

    videos_info: list[VideoInfo] = [info for info in probed if info is not None]
    skipped_count: int = len(probed) - len(videos_info)
    if skipped_count:
        logger.warning(f"Skipped {skipped_count} corrupted/unreadable file(s)")
    if not videos_info:
        raise RuntimeError("No valid video files to process after probing")

    # Populate chapter titles from file paths / meta.json
    for info in videos_info:
        info.chapter_title = get_chapter_title(info.path, work_dir)

    total_seconds: float = sum(info.duration for info in videos_info)
    total_h = int(total_seconds // 3600)
    total_m = int((total_seconds % 3600) // 60)
    total_s = int(total_seconds % 60)
    logger.info(
        f"Encoding {len(videos_info)} valid file(s), "
        f"total duration: {total_h:02d}:{total_m:02d}:{total_s:02d} "
        f"({total_seconds:.1f}s)"
    )
    if audio_delay_ms != 0:
        direction = "advance" if audio_delay_ms > 0 else "delay"
        logger.info(f"Audio sync correction: {direction} audio by {abs(audio_delay_ms)}ms")

    filter_complex: str = build_filter_complex(
        videos_info, audio_delay_ms=audio_delay_ms,
        out_width=out_width, out_height=out_height,
    )

    # Each video is a separate -i input
    inputs: list[str] = []
    for info in videos_info:
        inputs.extend(["-i", str(info.path)])

    filter_script: Optional[Path] = None
    metadata_script: Optional[Path] = None
    try:
        # Write filter graph to a temp file (-filter_complex_script) so the
        # command line stays short regardless of the number of input clips
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_filter.txt", delete=False, encoding="utf-8"
        ) as tmp:
            filter_script = Path(tmp.name)
            tmp.write(filter_complex)
        logger.debug(f"Filter script written: {filter_script}")
        logger.debug(f"filter_complex:\n{filter_complex}")

        # Write ffmetadata with chapter markers
        chapters_content: str = build_chapters_metadata(videos_info)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_meta.txt", delete=False, encoding="utf-8"
        ) as tmp:
            metadata_script = Path(tmp.name)
            tmp.write(chapters_content)
        logger.debug(f"Chapters metadata written: {metadata_script}")
        logger.debug(f"chapters:\n{chapters_content}")

        metadata_input_index: int = len(videos_info)  # 0-based index of metadata -i

        cmd: list[str] = [
            str(ffmpeg),
            *inputs,
            "-i", str(metadata_script),          # metadata input (no video/audio)
            "-filter_complex_script", str(filter_script),
            "-map", "[vout]",
            "-map", "[aout]",
            "-map_metadata", str(metadata_input_index),  # embed chapters
            # Video: H.264 web-compatible
            "-c:v", "libx264",
            "-profile:v", "high",
            "-level:v", "4.1",
            "-pix_fmt", "yuv420p",
            "-crf", "23",
            "-preset", "medium",
            # Audio: AAC-LC stereo 48kHz
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            # Container
            "-movflags", "+faststart",
            "-y",
            str(output),
        ]

        logger.info("Running ffmpeg:")
        logger.info("  " + " ".join(cmd))

        result = subprocess.run(cmd, capture_output=False)
    finally:
        if filter_script:
            logger.debug(f"Removing filter script: {filter_script}")
            filter_script.unlink(missing_ok=True)
        if metadata_script:
            logger.debug(f"Removing metadata script: {metadata_script}")
            metadata_script.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {result.returncode}")

    logger.info(f"Output saved: {output}")

    # Save plain-text timestamps for YouTube description
    timestamps_path: Path = output.with_name(output.stem + ".timestamps.txt")
    timestamps_path.write_text(build_timestamps_text(videos_info), encoding="utf-8")
    logger.info(f"Timestamps saved: {timestamps_path}")


def _parse_last_days(value: str) -> int:
    """Parse --last-days value: accept '7' or '7d', return int."""
    v = value.strip().lower().rstrip("d")
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid --last-days value: {value!r}. Use a number like 7 or 7d.")
    if n < 1:
        raise argparse.ArgumentTypeError(f"--last-days must be >= 1, got {n}")
    return n


def main() -> None:
    default_work_dir: str = os.getenv("WORK_DIR", r"H:\TEMP\vk_vsf")
    default_audio_delay_ms: int = int(os.getenv("AUDIO_DELAY_MS", "0"))

    parser = argparse.ArgumentParser(
        description="Concatenate downloaded channel videos into one Full HD file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python join_video.py --output result.mp4
  python join_video.py --work-dir H:\\TEMP\\vk_vsf\\babazoyka --output babazoyka_full.mp4
  python join_video.py --output result.mp4 --start-date 2025-11-01 --end-date 2025-11-30
  python join_video.py --output result.mp4 --last-days 7
  python join_video.py --output result.mp4 --last-days 7d
  python join_video.py --output result.mp4 --audio-delay-ms 200
  python join_video.py --output result.mp4 --sort interest-asc
  python join_video.py --output result.mp4 --sort interest-desc --last-days 30
  python join_video.py --output result.mp4 --no-ad-filter
        """,
    )
    parser.add_argument(
        "--work-dir",
        type=str,
        default=default_work_dir,
        help=f"Work directory containing post subfolders (default from .env: {default_work_dir})",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output video file path (e.g. result.mp4)",
    )
    parser.add_argument(
        "--sort",
        choices=["asc", "desc", "interest-asc", "interest-desc"],
        default="asc",
        help=(
            "Sort order. "
            "asc / desc = by date (oldest/newest first). "
            "interest-asc = least interesting first (builds to climax). "
            "interest-desc = most interesting first. "
            "Default: asc"
        ),
    )
    parser.add_argument(
        "--start-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=None,
        metavar="YYYY-MM-DD",
        help="Include only videos posted on or after this date (inclusive)",
    )
    parser.add_argument(
        "--end-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=None,
        metavar="YYYY-MM-DD",
        help="Include only videos posted on or before this date (inclusive)",
    )
    parser.add_argument(
        "--last-days",
        type=_parse_last_days,
        default=None,
        metavar="N",
        help=(
            "Include only videos from the last N days (including today). "
            "Accepts a plain integer or integer with 'd' suffix: 7 or 7d. "
            "Equivalent to --start-date <today minus N-1 days>. "
            "Overrides --start-date if both are given."
        ),
    )
    parser.add_argument(
        "--audio-delay-ms",
        type=int,
        default=default_audio_delay_ms,
        metavar="MS",
        help=(
            "Audio sync correction in milliseconds. "
            "Positive: audio is late, advance audio (trim start). "
            "Negative: audio is early, delay audio (add silence). "
            f"Default from .env AUDIO_DELAY_MS: {default_audio_delay_ms}"
        ),
    )
    parser.add_argument(
        "--no-ad-filter",
        action="store_true",
        default=False,
        help=(
            "Disable the ad/spam filter even when rules are configured in .env.json "
            "(ad_filter section). Useful for debugging to see the full unfiltered list."
        ),
    )
    parser.add_argument(
        "--orientation",
        choices=["horizontal", "vertical"],
        default="horizontal",
        help=(
            "Output video orientation. "
            "horizontal (default): 1920×1080 (landscape, YouTube/TV). "
            "vertical: 1080×1920 (portrait, Reels/Shorts/TikTok)."
        ),
    )

    args = parser.parse_args()
    logger.debug(
        f"Parsed args: work_dir={args.work_dir!r}, output={args.output!r}, "
        f"sort={args.sort!r}, start_date={args.start_date}, end_date={args.end_date}, "
        f"last_days={args.last_days}, orientation={args.orientation}, "
        f"audio_delay_ms={args.audio_delay_ms}, no_ad_filter={args.no_ad_filter}"
    )

    work_dir = Path(args.work_dir)
    output = Path(args.output)

    if not work_dir.exists():
        logger.error(f"Work directory not found: {work_dir}")
        raise SystemExit(1)

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    logger.info(f"Using ffmpeg:  {ffmpeg}")
    logger.info(f"Using ffprobe: {ffprobe}")

    videos = collect_videos(work_dir)

    # Date-based sort (default collect_videos already sorts by date asc)
    if args.sort == "desc":
        videos = list(reversed(videos))

    # --last-days overrides --start-date
    effective_start_date = args.start_date
    if args.last_days is not None:
        effective_start_date = date.today() - timedelta(days=args.last_days - 1)
        logger.info(f"--last-days {args.last_days}: start date set to {effective_start_date}")

    videos = filter_videos_by_date(videos, effective_start_date, args.end_date)
    if effective_start_date or args.end_date:
        logger.info(
            f"Date filter: [{effective_start_date or '...'} — {args.end_date or '...'}] "
            f"→ {len(videos)} video(s) after filtering"
        )

    if not videos:
        logger.error(f"No video files found in: {work_dir}")
        raise SystemExit(1)

    # Ad / spam filter
    if not args.no_ad_filter:
        app_config = load_app_config()
        ban_rules = build_ban_rules(app_config.ad_filter)
        if ban_rules:
            needs_duration = (
                app_config.ad_filter.ban_min_duration_sec > 0
                or app_config.ad_filter.ban_max_duration_sec > 0
            )
            logger.info(
                f"Ad filter: {len(ban_rules)} rule(s) active"
                + (" (ffprobe duration probing enabled)" if needs_duration else "")
            )
            videos = filter_videos_by_rules(
                videos, work_dir, ban_rules,
                ffprobe=ffprobe if needs_duration else None,
            )
            if not videos:
                logger.error("All videos were excluded by the ad filter. Use --no-ad-filter to bypass.")
                raise SystemExit(1)
        else:
            logger.debug("Ad filter: no rules configured, skipping")

    # Interest-based sort
    interest_scores: dict[Path, float] = {}
    if args.sort in ("interest-asc", "interest-desc"):
        weights = load_interest_weights()
        logger.info(
            f"Computing interest scores "
            f"(weights: reactions={weights.reactions}, "
            f"forwards={weights.forwards}, replies={weights.replies})"
        )
        interest_scores = compute_interest_scores(videos, weights)
        reverse_order = args.sort == "interest-desc"
        videos = sorted(videos, key=lambda v: interest_scores[v], reverse=reverse_order)
        logger.info(
            f"Sorted {len(videos)} video(s) by interest "
            f"({'desc' if reverse_order else 'asc'})"
        )

    logger.info(f"Found {len(videos)} video file(s)")
    for v in videos:
        meta_file = v.parent / "meta.json"
        date_str = ""
        score_str = ""
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            date_str = f"  [{meta.get('date', '')}]"
            views = meta.get("views")
            reactions = meta.get("reactions_total")
            if views is not None:
                date_str += f"  views={views}"
            if reactions is not None:
                date_str += f"  reactions={reactions}"
        except Exception:
            pass
        if v in interest_scores:
            score_str = f"  interest={interest_scores[v]:.4f}"
        logger.info(f"  {v}{date_str}{score_str}")

    out_width, out_height = output_dimensions(args.orientation)
    logger.info(f"Output orientation: {args.orientation} ({out_width}×{out_height})")

    run_ffmpeg(
        ffmpeg, ffprobe, videos, output, work_dir,
        audio_delay_ms=args.audio_delay_ms,
        out_width=out_width,
        out_height=out_height,
    )


if __name__ == "__main__":
    main()

