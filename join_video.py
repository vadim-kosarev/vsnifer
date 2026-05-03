"""
join_video.py — concatenate all video files from a work directory into one Full HD file.

Recursively scans work_dir for video files (sorted by post_id),
writes an ffmpeg concat file list, then encodes to 1920x1080 H.264/AAC.

Usage:
    python join_video.py --output result.mp4
    python join_video.py --work-dir H:\\TEMP\\vk_vsf\\babazoyka --output babazoyka_full.mp4
"""

import argparse
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

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

# Output resolution
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080


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
        # Maybe FFMPEG_HOME points directly to bin/
        candidate = Path(ffmpeg_home) / "ffmpeg.exe"
        logger.debug(f"Checking candidate: {candidate}")
        if candidate.exists():
            return candidate

    # Fall back to PATH
    logger.debug("Falling back to ffmpeg from PATH")
    return Path("ffmpeg")


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
    # Fallback: epoch so undated posts sort first
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def collect_videos(work_dir: Path) -> list[Path]:
    """
    Find video files under work_dir. Two modes:

    1. Channel mode (default): work_dir contains numeric post_id subdirectories.
       Videos are sorted by date from meta.json (oldest first).

    2. Flat mode: work_dir contains video files directly (no numeric subdirs found).
       Videos are sorted by filename.
    """
    videos: list[tuple[datetime, Path]] = []
    flat_videos: list[Path] = []

    for entry in work_dir.iterdir():
        if entry.is_dir():
            try:
                int(entry.name)  # must be a numeric post_id dir
            except ValueError:
                logger.debug(f"Skipping non-numeric dir: {entry.name}")
                continue
            for f in entry.iterdir():
                if f.suffix.lower() in VIDEO_EXTENSIONS:
                    post_date: datetime = read_post_date(entry)
                    logger.debug(f"Found video in post dir {entry.name}: {f.name} (date={post_date})")
                    videos.append((post_date, f))
                else:
                    logger.debug(f"Skipping non-video file: {f.name}")
        elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
            logger.debug(f"Found flat video: {entry.name}")
            flat_videos.append(entry)
        else:
            logger.debug(f"Skipping entry: {entry.name}")

    if videos:
        # Sort by post date ascending (oldest → newest)
        videos.sort(key=lambda x: x[0])
        logger.debug(f"Channel mode: {len(videos)} video(s) sorted by date")
        return [path for _, path in videos]

    # Flat mode: sort by filename
    flat_videos.sort(key=lambda f: f.name)
    logger.debug(f"Flat mode: {len(flat_videos)} video(s) sorted by filename")
    return flat_videos


def build_concat_list(videos: list[Path], list_file: Path) -> None:
    """Write an ffmpeg concat demuxer file list."""
    lines = []
    for video in videos:
        # Use absolute paths; escape single quotes inside path
        safe_path = str(video.resolve()).replace("'", r"\'")
        logger.debug(f"Adding to concat list: {safe_path}")
        lines.append(f"file '{safe_path}'")
    list_file.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"Concat list written: {list_file} ({len(videos)} files)")


def run_ffmpeg(ffmpeg: Path, list_file: Path, output: Path) -> None:
    """
    Concatenate and re-encode videos to 1920x1080 with blurred background.

    For non-16:9 content (vertical, square, etc.) the source is scaled to fill
    the frame and heavily blurred as background, then the properly-scaled sharp
    version is overlaid centered — no black bars.

    Filter chain (applied to the concatenated stream):
      split → two copies of the source
        [bg]  scale-to-fill 1920x1080 → crop → boxblur (blurred background)
        [fg]  scale-to-fit  1920x1080 (sharp, preserves aspect ratio)
      overlay [fg] centered on [bg]

    Codec: H.264 libx264 CRF 23, AAC 192k.
    """
    # split source into background (blurred fill) and foreground (sharp fit)
    filter_complex = (
        "[0:v]split=2[orig_bg][orig_fg];"

        # Background: scale to FILL the frame (may crop), then blur heavily
        f"[orig_bg]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
        ":force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        "boxblur=luma_radius=40:luma_power=3"
        "[bg];"

        # Foreground: scale to FIT (preserves aspect ratio, may leave space)
        f"[orig_fg]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
        ":force_original_aspect_ratio=decrease"
        "[fg];"

        # Overlay sharp foreground centered on blurred background
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[out];"

        # Audio: normalize all clips to 48kHz stereo, fix timestamp gaps/overlaps
        # aresample=async=1000 — compensates up to 1000 samples of drift per second
        "[0:a]aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "aresample=async=1000"
        "[aout]"
    )

    cmd = [
        str(ffmpeg),
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "[aout]",
        # Video: H.264 web-compatible
        "-c:v", "libx264",
        "-profile:v", "high",     # H.264 High profile — supported by all modern browsers
        "-level:v", "4.1",        # Level 4.1 — max 1080p@30fps, wide device support
        "-pix_fmt", "yuv420p",    # mandatory for browser playback (Chrome/Firefox/Safari)
        "-crf", "23",
        "-preset", "medium",
        # Audio: AAC-LC stereo 48kHz
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",           # explicit output sample rate to match filter
        # Container
        "-movflags", "+faststart",  # moov atom at front — starts playing before full download
        "-y",
        str(output),
    ]

    logger.info("Running ffmpeg:")
    logger.info("  " + " ".join(cmd))
    logger.debug(f"filter_complex:\n{filter_complex}")

    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {result.returncode}")

    logger.info(f"Output saved: {output}")


def main() -> None:
    default_work_dir: str = os.getenv("WORK_DIR", r"H:\TEMP\vk_vsf")

    parser = argparse.ArgumentParser(
        description="Concatenate downloaded channel videos into one Full HD file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python join_video.py --output result.mp4
  python join_video.py --work-dir H:\\TEMP\\vk_vsf\\babazoyka --output babazoyka_full.mp4
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
        choices=["asc", "desc"],
        default="asc",
        help="Sort order by post_id: asc = oldest first, desc = newest first (default: asc)",
    )

    args = parser.parse_args()
    logger.debug(f"Parsed args: work_dir={args.work_dir!r}, output={args.output!r}, sort={args.sort!r}")

    work_dir = Path(args.work_dir)
    output = Path(args.output)

    if not work_dir.exists():
        logger.error(f"Work directory not found: {work_dir}")
        raise SystemExit(1)

    ffmpeg = find_ffmpeg()
    logger.info(f"Using ffmpeg: {ffmpeg}")

    videos = collect_videos(work_dir)
    if args.sort == "desc":
        videos = list(reversed(videos))

    if not videos:
        logger.error(f"No video files found in: {work_dir}")
        raise SystemExit(1)

    logger.info(f"Found {len(videos)} video files")
    for v in videos:
        # Show date from meta.json if available
        meta_file = v.parent / "meta.json"
        date_str = ""
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            date_str = f"  [{meta.get('date', '')}]"
        except Exception:
            pass
        logger.info(f"  {v}{date_str}")

    # Write concat list to a temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="_concat.txt", delete=False, encoding="utf-8"
    ) as tmp:
        list_file = Path(tmp.name)

    try:
        build_concat_list(videos, list_file)
        run_ffmpeg(ffmpeg, list_file, output)
    finally:
        logger.debug(f"Removing temp concat list: {list_file}")
        list_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

