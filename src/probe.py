"""ffprobe / ffmpeg 래퍼."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


class ToolMissing(RuntimeError):
    pass


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolMissing(
            f"'{name}' 를 찾을 수 없습니다. 설치 후 다시 실행해 주세요.\n"
            f"  macOS : brew install ffmpeg\n"
            f"  Ubuntu: sudo apt install ffmpeg"
        )
    return path


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    has_video: bool


def probe(path: str) -> MediaInfo:
    require_tool("ffprobe")
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 실패 ({path}):\n{proc.stderr.strip()}")
    data = json.loads(proc.stdout)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"),
                      (video or {}).get("duration"),
                      (audio or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    fps = 0.0
    if video:
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        try:
            num, _, den = rate.partition("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

    return MediaInfo(
        duration=duration,
        width=int(video["width"]) if video and video.get("width") else 0,
        height=int(video["height"]) if video and video.get("height") else 0,
        fps=fps,
        has_audio=audio is not None,
        has_video=video is not None,
    )
