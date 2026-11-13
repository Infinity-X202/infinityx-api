import json
import shutil
import subprocess
from pathlib import Path

from .errors import AppError

AUDIO_ARGS = {
    "mp3": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
    "wav": ["-vn", "-c:a", "pcm_s16le"],
    "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
}
VIDEO_ARGS = {
    "mp4": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    "webm": ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4", "-crf", "33", "-b:v", "0", "-c:a", "libopus", "-b:a", "128k"],
}
TARGET_SUFFIX = {
    "mp3": ".mp3",
    "wav": ".wav",
    "m4a": ".m4a",
    "mp4": ".mp4",
    "webm": ".webm",
}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def convert_media(src: Path, dest: Path, target: str, root: Path) -> Path:
    if target not in TARGET_SUFFIX:
        raise AppError("That conversion format is not available.")
    if not ffmpeg_available():
        raise AppError("FFmpeg is not installed, so conversion cannot run.")
    source = src.resolve()
    output = dest.resolve()
    base = root.resolve()
    if not source.is_relative_to(base) or not output.is_relative_to(base):
        raise AppError("Media paths must stay inside the InfinityX data folder.")
    if output.suffix.lower() != TARGET_SUFFIX[target]:
        raise AppError("The output file type does not match the conversion.")
    if not source.is_file():
        raise AppError("The source file is missing.")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = AUDIO_ARGS[target] if target in AUDIO_ARGS else VIDEO_ARGS[target]
    command = ["ffmpeg", "-y", "-i", str(source), *args, str(output)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=30 * 60,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError("Conversion exceeded the time limit.") from exc
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise AppError("FFmpeg could not convert this file.")
    return output


def probe_media(path: Path) -> dict:
    if shutil.which("ffprobe") is None or not path.is_file():
        return {}
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        timeout=40,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    try:
        data = json.loads(completed.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    duration = fmt.get("duration")
    size = fmt.get("size")
    return {
        "duration": float(duration) if duration else None,
        "size": int(size) if size else path.stat().st_size,
        "format": fmt.get("format_name"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
    }
