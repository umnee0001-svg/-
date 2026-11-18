#!/usr/bin/env python3
"""유튜브 썸네일(1280x720 JPG) 만들기.

영상의 한 장면(또는 이미지 파일) 위에 큰 글씨를 얹는다. ffmpeg 만 있으면 된다.

사용 예:
    python src/thumbnail.py --input 영상.mp4 --time 12.5 --text "이거 모르면 손해\\n!3분 정리"
    python src/thumbnail.py --input 배경.jpg --text "제목" --position center --out output/thumb.jpg

글자 규칙:
    - 줄바꿈은 줄마다 따로 적거나 \\n 으로 구분
    - 줄 맨 앞에 ! 를 붙이면 그 줄은 강조색(accent_color)으로 칠해진다
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fonts  # noqa: E402
from filters import esc  # noqa: E402

WIDTH, HEIGHT = 1280, 720
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
# 유튜브 썸네일 용량 제한은 2MB
MAX_BYTES = 2 * 1024 * 1024


@dataclass
class ThumbStyle:
    font_file: str | None = None
    font_name: str = "Noto Sans CJK KR"
    language: str = "ko"
    font_size: int = 120
    color: str = "#FFFFFF"
    accent_color: str = "#FFE600"
    outline_color: str = "#000000"
    outline: int = 10
    # left | center | bottom
    position: str = "left"
    # 0 이면 원본 밝기, 0.5 면 절반쯤 어둡게 (글씨가 잘 보이도록)
    darken: float = 0.25
    box: bool = False
    box_color: str = "#000000"
    box_opacity: float = 0.55
    line_spacing: float = 1.15


def parse_lines(text: str) -> list[tuple[str, bool]]:
    """'첫 줄\\n!강조 줄' -> [('첫 줄', False), ('강조 줄', True)]. 빈 줄은 버린다."""
    text = text.replace("\\n", "\n")
    out: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("!") and len(line) > 1:
            out.append((line[1:].strip(), True))
        else:
            out.append((line, False))
    return out


def estimate_width(text: str, size: int) -> float:
    """글자 폭 대략 계산 (한글·한자·가나 같은 전각 글자는 1em, 나머지는 0.6em)."""
    import unicodedata

    return sum(size * (1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.6)
               for ch in text)


def fit_font_size(lines: list[tuple[str, bool]], size: int, max_width: int) -> int:
    """가장 긴 줄이 화면 밖으로 나가지 않도록 글자 크기를 줄인다."""
    widest = max((estimate_width(t, size) for t, _ in lines), default=0)
    if widest <= max_width:
        return size
    return max(36, int(size * max_width / widest))


def _hex(color: str) -> str:
    return color.replace("#", "0x")


def build_filter(lines: list[tuple[str, bool]], style: ThumbStyle,
                 textfiles: list[str]) -> str:
    """배경 맞추기 + 어둡게 + 줄마다 drawtext 를 이어 붙인 필터 체인."""
    parts = [
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={WIDTH}:{HEIGHT}",
        "setsar=1",
    ]
    if style.darken > 0:
        alpha = max(0.0, min(style.darken, 0.9))
        parts.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{alpha:.2f}:t=fill")

    font_arg = fonts.drawtext_font_args(style.font_file, style.font_name, style.language or None)
    key, _, value = font_arg.partition("=")
    font_part = f"{key}={esc(value)}"

    margin = 70
    size = fit_font_size(lines, style.font_size, WIDTH - 2 * margin)
    step = int(size * style.line_spacing)
    block_h = step * (len(lines) - 1) + size

    if style.position == "center":
        top = (HEIGHT - block_h) // 2
        x_expr = "(w-text_w)/2"
    elif style.position == "bottom":
        top = HEIGHT - margin - block_h
        x_expr = "(w-text_w)/2"
    else:  # left
        top = (HEIGHT - block_h) // 2
        x_expr = str(margin)

    for i, ((_text, accent), textfile) in enumerate(zip(lines, textfiles)):
        args = [
            font_part,
            f"textfile={esc(textfile)}",
            "reload=0",
            f"fontsize={size}",
            f"fontcolor={_hex(style.accent_color if accent else style.color)}",
            f"borderw={style.outline}",
            f"bordercolor={_hex(style.outline_color)}",
            f"x={esc(x_expr)}",
            f"y={top + i * step}",
        ]
        if style.box:
            args += [
                "box=1",
                f"boxcolor={_hex(style.box_color)}@{style.box_opacity}",
                "boxborderw=18",
            ]
        parts.append("drawtext=" + ":".join(args))
    return ",".join(parts)


def make_thumbnail(input_path: str | Path, out_path: str | Path, *, text: str,
                   time: float = 0.0, style: ThumbStyle | None = None,
                   dry_run: bool = False) -> Path | list[str]:
    """썸네일을 만들고 경로를 돌려준다. dry_run 이면 ffmpeg 명령만 돌려준다."""
    style = style or ThumbStyle()
    input_path = Path(input_path)
    out_path = Path(out_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"입력 파일이 없습니다: {input_path}")
    if style.position not in ("left", "center", "bottom"):
        raise ValueError("position 은 left / center / bottom 중 하나여야 합니다")

    lines = parse_lines(text)
    is_image = input_path.suffix.lower() in IMAGE_EXTS

    with tempfile.TemporaryDirectory(prefix="thumb-") as tmpdir:
        textfiles = []
        for i, (line, _accent) in enumerate(lines):
            tf = Path(tmpdir) / f"line{i}.txt"
            tf.write_text(line, encoding="utf-8")
            textfiles.append(str(tf))

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if not is_image and time > 0:
            cmd += ["-ss", f"{time:.3f}"]
        cmd += ["-i", str(input_path), "-frames:v", "1",
                "-vf", build_filter(lines, style, textfiles)]

        if dry_run:
            return cmd + ["-q:v", "2", str(out_path)]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            out_path.unlink()
        # 2MB 를 넘으면 JPG 화질을 조금씩 낮춰서 다시 저장
        qualities = [None] if out_path.suffix.lower() == ".png" else [2, 4, 6, 9, 13]
        for quality in qualities:
            run = cmd + (["-q:v", str(quality)] if quality else []) + [str(out_path)]
            proc = subprocess.run(run, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode != 0 or not out_path.exists():
                msg = proc.stderr.strip() or "출력 파일이 만들어지지 않았습니다"
                if not is_image and time > 0:
                    msg += f"\n(영상 길이보다 큰 시간({time}s)을 넣은 건 아닌지 확인하세요)"
                raise RuntimeError(f"썸네일 생성 실패:\n{msg}")
            if out_path.stat().st_size <= MAX_BYTES:
                break
    return out_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="thumbnail.py", description="유튜브 썸네일 만들기 (1280x720)")
    p.add_argument("--input", required=True, help="영상 또는 이미지 파일")
    p.add_argument("--text", default="", help="썸네일 글자. \\n 으로 줄바꿈, 줄 앞 ! = 강조색")
    p.add_argument("--time", type=float, default=0.0, help="영상에서 뽑을 장면 (초)")
    p.add_argument("--out", help="저장 경로 (기본: output/<입력이름>_thumb.jpg)")
    p.add_argument("--font-file", help=".ttf/.otf 경로")
    p.add_argument("--font-size", type=int, default=120)
    p.add_argument("--color", default="#FFFFFF")
    p.add_argument("--accent-color", default="#FFE600")
    p.add_argument("--outline", type=int, default=10)
    p.add_argument("--position", default="left", choices=["left", "center", "bottom"])
    p.add_argument("--darken", type=float, default=0.25, help="0~0.9, 배경 어둡게")
    p.add_argument("--box", action="store_true", help="글씨 뒤에 반투명 박스")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    out = Path(args.out) if args.out else Path("output") / f"{Path(args.input).stem}_thumb.jpg"
    style = ThumbStyle(
        font_file=args.font_file, font_size=args.font_size, color=args.color,
        accent_color=args.accent_color, outline=args.outline,
        position=args.position, darken=args.darken, box=args.box,
    )
    result = make_thumbnail(args.input, out, text=args.text, time=args.time,
                            style=style, dry_run=args.dry_run)
    if args.dry_run:
        print(" ".join(result))
    else:
        print(f"완료: {result}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)
