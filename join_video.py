"""
join_video.py — concatenate all video files from a work directory into one Full HD file.

Scans work_dir for video files (sorted by post date from meta.json),
probes each file with ffprobe, then encodes to 1920x1080 H.264/AAC using the
ffmpeg concat filter (NOT the concat demuxer).

Why concat filter instead of concat demuxer:
  The concat demuxer glues raw packets without re-synchronising audio timestamps.
  When source clips have different sample rates, channel layouts, or no audio at
  all, the result is corrupted / silent audio from the second clip onward.
  The concat filter processes each input independently, resets all timestamps, and
  properly handles missing audio streams (replaced with generated silence).

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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel

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


class VideoInfo(BaseModel):
    """Probed metadata for a single video file."""

    path: Path
    has_audio: bool
    duration: float  # seconds; used to generate silence for audio-less clips
    chapter_title: str = ""  # human-readable chapter name for the output file


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


def build_filter_complex(videos_info: list[VideoInfo]) -> str:
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
    """
    parts: list[str] = []
    concat_pads: list[str] = []

    for i, info in enumerate(videos_info):
        # --- Video: blurred background + sharp foreground overlay ---
        parts.append(f"[{i}:v]split=2[orig_bg{i}][orig_fg{i}]")
        parts.append(
            f"[orig_bg{i}]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
            f":force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
            f"boxblur=luma_radius=40:luma_power=3"
            f"[bg{i}]"
        )
        parts.append(
            f"[orig_fg{i}]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
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
    parts.append(f"{''.join(concat_pads)}concat=n={n}:v=1:a=1[vout][aout]")

    return ";".join(parts)


def run_ffmpeg(
    ffmpeg: Path, ffprobe: Path, videos: list[Path], output: Path, work_dir: Path
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

    filter_complex: str = build_filter_complex(videos_info)

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


def main() -> None:
    default_work_dir: str = os.getenv("WORK_DIR", r"H:\TEMP\vk_vsf")

    parser = argparse.ArgumentParser(
        description="Concatenate downloaded channel videos into one Full HD file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python join_video.py --output result.mp4
  python join_video.py --work-dir H:\\TEMP\\vk_vsf\\babazoyka --output babazoyka_full.mp4
  python join_video.py --output result.mp4 --start-date 2025-11-01 --end-date 2025-11-30
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
        help="Sort order: asc = oldest first, desc = newest first (default: asc)",
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

    args = parser.parse_args()
    logger.debug(
        f"Parsed args: work_dir={args.work_dir!r}, output={args.output!r}, "
        f"sort={args.sort!r}, start_date={args.start_date}, end_date={args.end_date}"
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
    if args.sort == "desc":
        videos = list(reversed(videos))

    videos = filter_videos_by_date(videos, args.start_date, args.end_date)
    if args.start_date or args.end_date:
        logger.info(
            f"Date filter: [{args.start_date or '...'} — {args.end_date or '...'}] "
            f"→ {len(videos)} video(s) after filtering"
        )

    if not videos:
        logger.error(f"No video files found in: {work_dir}")
        raise SystemExit(1)

    logger.info(f"Found {len(videos)} video file(s)")
    for v in videos:
        meta_file = v.parent / "meta.json"
        date_str = ""
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            date_str = f"  [{meta.get('date', '')}]"
        except Exception:
            pass
        logger.info(f"  {v}{date_str}")

    run_ffmpeg(ffmpeg, ffprobe, videos, output, work_dir)


if __name__ == "__main__":
    main()

