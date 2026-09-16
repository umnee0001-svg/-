"""TTF/OTF 파일에서 폰트 패밀리 이름을 직접 읽어온다.

libass 는 ASS 스타일의 Fontname 으로 폰트를 찾기 때문에, 사용자가 프리셋에
`font_file` 만 적어도 되도록 파일에서 이름을 꺼내 쓴다. 외부 의존성 없음.
"""
from __future__ import annotations

import struct
from pathlib import Path

_NAME_FAMILY = 1
_NAME_TYPOGRAPHIC_FAMILY = 16


def family_name(path: str | Path) -> str | None:
    """폰트 파일의 패밀리 이름. 읽지 못하면 None."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    try:
        return _parse_name_table(data)
    except (struct.error, IndexError, ValueError, UnicodeDecodeError):
        return None


def _parse_name_table(data: bytes) -> str | None:
    tag = data[:4]
    if tag == b"ttcf":
        # TrueType Collection: 첫 번째 폰트의 오프셋으로 점프
        (offset,) = struct.unpack_from(">I", data, 12)
    elif tag in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
        offset = 0
    else:
        return None

    (num_tables,) = struct.unpack_from(">H", data, offset + 4)
    name_offset = name_length = None
    for i in range(num_tables):
        rec = offset + 12 + i * 16
        table_tag = data[rec:rec + 4]
        if table_tag == b"name":
            name_offset, name_length = struct.unpack_from(">II", data, rec + 8)
            break
    if name_offset is None:
        return None

    fmt, count, string_offset = struct.unpack_from(">HHH", data, name_offset)
    storage = name_offset + string_offset
    # (우선순위, 이름) 중 가장 우선순위가 높은 것을 고른다.
    best: tuple[int, str] | None = None
    for i in range(count):
        rec = name_offset + 6 + i * 12
        platform_id, encoding_id, _lang_id, name_id, length, str_off = struct.unpack_from(
            ">HHHHHH", data, rec
        )
        if name_id not in (_NAME_FAMILY, _NAME_TYPOGRAPHIC_FAMILY):
            continue
        raw = data[storage + str_off:storage + str_off + length]
        if not raw:
            continue
        if platform_id == 3 or (platform_id == 0):
            try:
                text = raw.decode("utf-16-be")
            except UnicodeDecodeError:
                continue
        elif platform_id == 1 and encoding_id == 0:
            text = raw.decode("mac-roman", errors="replace")
        else:
            continue
        text = text.strip("\x00").strip()
        if not text:
            continue
        # 타이포그래픽 패밀리(16) > 패밀리(1), 윈도우 플랫폼(3) 선호
        score = (10 if name_id == _NAME_TYPOGRAPHIC_FAMILY else 0) + (5 if platform_id == 3 else 0)
        if best is None or score > best[0]:
            best = (score, text)
    return best[1] if best else None


def match_file(font_name: str | None, lang: str | None = None) -> str | None:
    """fontconfig(fc-match)로 실제 폰트 파일 경로를 찾는다.

    drawtext 는 libass 와 달리 글자별 폰트 대체를 하지 않아서, 한글이 없는
    폰트로 매칭되면 네모(□)로 나온다. `:lang=ko` 를 붙여 해당 언어를
    실제로 지원하는 파일을 고르게 한다. fc-match 가 없으면 None.
    """
    import shutil
    import subprocess

    if not shutil.which("fc-match"):
        return None
    pattern = font_name or ""
    if lang:
        pattern = f"{pattern}:lang={lang}"
    if not pattern:
        return None
    try:
        proc = subprocess.run(
            ["fc-match", pattern, "-f", "%{file}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = proc.stdout.strip()
    return path if proc.returncode == 0 and path and Path(path).is_file() else None


def drawtext_font_args(font_file: str | None, font_name: str | None,
                       lang: str | None) -> str:
    """drawtext 에 넘길 폰트 인자 한 조각 ('fontfile=...' 또는 'font=...')."""
    resolved = font_file or match_file(font_name, lang)
    if resolved:
        return "fontfile=" + str(Path(resolved).resolve())
    return "font=" + (font_name or "Sans")
