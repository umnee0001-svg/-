"""자막 생성: Whisper 자동 인식 / SRT 읽기 -> 스타일이 입혀진 ASS 파일."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fonts import family_name


@dataclass
class Cue:
    start: float
    end: float
    text: str


class SubtitleError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 입력: Whisper
# --------------------------------------------------------------------------

def transcribe(audio_path: str, *, model: str, language: str | None) -> list[Cue]:
    """faster-whisper(우선) 또는 openai-whisper 로 음성인식."""
    words = _transcribe_words(audio_path, model=model, language=language)
    if words is None:
        raise SubtitleError(
            "음성인식 라이브러리를 찾을 수 없습니다.\n"
            "  pip install faster-whisper\n"
            "자막을 직접 준비했다면 `--subs 파일.srt` 와 함께 "
            "`--set subtitle.mode=file` 을 쓰세요."
        )
    return words


def _transcribe_words(audio_path: str, *, model: str, language: str | None):
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return _transcribe_openai(audio_path, model=model, language=language)

    whisper = WhisperModel(model, device="auto", compute_type="auto")
    segments, _info = whisper.transcribe(
        audio_path, language=language, word_timestamps=True, vad_filter=True
    )
    cues: list[Cue] = []
    for seg in segments:
        if getattr(seg, "words", None):
            for word in seg.words:
                text = (word.word or "").strip()
                if text:
                    cues.append(Cue(float(word.start), float(word.end), text))
        else:
            text = (seg.text or "").strip()
            if text:
                cues.append(Cue(float(seg.start), float(seg.end), text))
    return cues


def _transcribe_openai(audio_path: str, *, model: str, language: str | None):
    try:
        import whisper  # type: ignore
    except ImportError:
        return None
    loaded = whisper.load_model(model)
    result = loaded.transcribe(audio_path, language=language, word_timestamps=True)
    cues: list[Cue] = []
    for seg in result.get("segments", []):
        words = seg.get("words") or []
        if words:
            for word in words:
                text = (word.get("word") or "").strip()
                if text:
                    cues.append(Cue(float(word["start"]), float(word["end"]), text))
        else:
            text = (seg.get("text") or "").strip()
            if text:
                cues.append(Cue(float(seg["start"]), float(seg["end"]), text))
    return cues


# --------------------------------------------------------------------------
# 입력: SRT
# --------------------------------------------------------------------------

_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_srt(path: str | Path) -> list[Cue]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        match = None
        text_start = 0
        for idx, line in enumerate(lines[:2]):
            match = _SRT_TIME.search(line)
            if match:
                text_start = idx + 1
                break
        if not match:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1.ljust(3, "0")) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2.ljust(3, "0")) / 1000
        text = " ".join(lines[text_start:]).strip()
        if text and end > start:
            cues.append(Cue(start, end, text))
    if not cues:
        raise SubtitleError(f"SRT 에서 자막을 하나도 읽지 못했습니다: {path}")
    return cues


# --------------------------------------------------------------------------
# 가공
# --------------------------------------------------------------------------

def group_cues(cues: list[Cue], *, max_chars: int, max_lines: int,
               max_gap: float = 0.7, respect_boundaries: bool = False) -> list[Cue]:
    """단어 단위 큐를 화면에 띄울 덩어리로 묶는다.

    Whisper 의 단어 단위 결과는 이어 붙여 읽기 좋은 길이로 만들고,
    이미 문장 단위인 SRT 입력은 너무 긴 큐만 쪼갠다.

    respect_boundaries=True 면 입력 큐 하나를 넘어 합치지 않는다
    (직접 만든 SRT 의 줄 나눔을 그대로 살리고 싶을 때).
    """
    if respect_boundaries:
        out: list[Cue] = []
        for cue in cues:
            out.extend(group_cues([cue], max_chars=max_chars, max_lines=max_lines,
                                  max_gap=max_gap))
        return out

    budget = max_chars * max_lines
    words: list[Cue] = []
    for cue in cues:
        pieces = cue.text.split()
        if len(pieces) <= 1:
            words.append(cue)
            continue
        # 문장 큐는 글자 수 비례로 시간을 나눠 단어 큐로 펼친다.
        total = sum(len(p) for p in pieces) or 1
        span = cue.end - cue.start
        cursor = cue.start
        for piece in pieces:
            width = span * (len(piece) / total)
            words.append(Cue(cursor, cursor + width, piece))
            cursor += width

    grouped: list[Cue] = []
    buf: list[Cue] = []

    def flush() -> None:
        if not buf:
            return
        text = " ".join(c.text for c in buf)
        grouped.append(Cue(buf[0].start, buf[-1].end, text))
        buf.clear()

    for word in words:
        if buf:
            pending = len(" ".join(c.text for c in buf)) + 1 + len(word.text)
            if pending > budget or (word.start - buf[-1].end) > max_gap:
                flush()
        buf.append(word)
        if re.search(r"[.!?。？！]$", word.text):
            flush()
    flush()
    return [c for c in grouped if c.end > c.start and c.text.strip()]


def wrap(text: str, *, max_chars: int, max_lines: int) -> str:
    """단어 경계 우선, 안 되면 글자 수로 줄바꿈. 한국어/영어 모두 동작."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    # 공백 없는 긴 토큰은 강제로 자른다.
    split_lines: list[str] = []
    for line in lines:
        while len(line) > max_chars:
            split_lines.append(line[:max_chars])
            line = line[max_chars:]
        if line:
            split_lines.append(line)

    # max_lines 를 넘더라도 글자를 버리지 않는다. 넘치는 줄은 마지막 줄에 합친다.
    if len(split_lines) > max_lines:
        head = split_lines[:max_lines - 1]
        head.append(" ".join(split_lines[max_lines - 1:]))
        split_lines = head
    return r"\N".join(split_lines)


def clip_cues(cues: list[Cue], *, offset: float = 0.0,
              duration: float | None = None) -> list[Cue]:
    """트림/속도 조절 후 타임라인에 맞춰 큐를 이동·절단한다."""
    out: list[Cue] = []
    for cue in cues:
        start = cue.start - offset
        end = cue.end - offset
        if duration is not None:
            if start >= duration:
                continue
            end = min(end, duration)
        if end <= 0:
            continue
        out.append(Cue(max(start, 0.0), end, cue.text))
    return [c for c in out if c.end > c.start]


def scale_cues(cues: list[Cue], factor: float) -> list[Cue]:
    if factor == 1.0:
        return cues
    return [Cue(c.start / factor, c.end / factor, c.text) for c in cues]


# --------------------------------------------------------------------------
# 출력: ASS
# --------------------------------------------------------------------------

def ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> ASS 의 '&HAABBGGRR' (alpha 0 = 불투명)."""
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{b.upper()}{g.upper()}{r.upper()}"


def _ass_time(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:
        centis = 99
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def write_ass(cues: list[Cue], out_path: str | Path, *, style: dict,
              width: int, height: int) -> Path:
    font = style.get("font_name") or "Sans"
    if style.get("font_file"):
        font = family_name(style["font_file"]) or font

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{style['font_size']},{ass_color(style['primary_color'])},{ass_color(style['primary_color'])},{ass_color(style['outline_color'])},{ass_color(style['back_color'], 96)},{-1 if style.get('bold') else 0},0,0,0,100,100,0,0,{style['border_style']},{style['outline']},{style['shadow']},{style['alignment']},{style['margin_h']},{style['margin_h']},{style['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    for cue in cues:
        text = cue.text.upper() if style.get("uppercase") else cue.text
        body = wrap(text, max_chars=style["max_chars_per_line"],
                    max_lines=style["max_lines"])
        body = body.replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Main,,0,0,0,,{body}"
        )

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
