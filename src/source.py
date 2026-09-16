"""입력 소스 해석: 로컬 파일 경로 또는 URL(yt-dlp 다운로드)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve(value: str, workdir: Path, *, quiet: bool = False) -> Path:
    """소스를 로컬 파일 경로로 만들어 돌려준다."""
    if not is_url(value):
        path = Path(value).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"입력 영상을 찾을 수 없습니다: {value}")
        return path.resolve()
    return download(value, workdir, quiet=quiet)


def download(url: str, workdir: Path, *, quiet: bool = False) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError(
            "URL 입력에는 yt-dlp 가 필요합니다.  pip install yt-dlp"
        )
    workdir.mkdir(parents=True, exist_ok=True)
    template = str(workdir / "source.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ]
    if quiet:
        cmd.insert(1, "-q")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp 다운로드 실패: {url}")

    files = sorted(workdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError(f"다운로드된 파일을 찾지 못했습니다: {url}")
    return files[0].resolve()
