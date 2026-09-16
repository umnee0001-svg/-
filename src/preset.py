"""프리셋 로딩 / 병합 / 검증.

프리셋 JSON은 부분만 적어도 됩니다. 빠진 키는 DEFAULTS 값으로 채워집니다.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

DEFAULTS: dict = {
    # 프리셋 이름 (출력 파일명에 쓰임)
    "name": "shorts",

    "output": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        # 최종 영상 최대 길이(초). null 이면 소스 길이 그대로.
        "max_duration": 60,
        "video_codec": "libx264",
        "crf": 20,
        "x264_preset": "medium",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "192k",
        # {date} {time} {preset} {slug} 치환 가능
        "filename_template": "{date}_{preset}_{slug}.mp4",
    },

    "source": {
        # crop      : 꽉 채우고 넘치는 부분 잘라냄 (가장 무난)
        # pad       : 전체를 보이게 하고 위아래를 단색으로 채움
        # blur_pad  : 위아래를 같은 영상의 블러로 채움 (가로영상 -> 쇼츠 변환에 추천)
        "fit": "crop",
        "crop_anchor": "center",      # center|top|bottom|left|right
        "pad_color": "#000000",
        "blur_strength": 40,
        "trim": {"start": None, "end": None},   # 초 단위
        "speed": 1.0,                 # 0.5 ~ 2.0
        "mute_source": False,
    },

    "subtitle": {
        "enabled": True,
        # auto : Whisper 로 음성인식해서 자동 생성
        # file : --subs 로 넘긴 SRT 사용
        # none : 자막 없음
        "mode": "auto",
        "language": "ko",
        "whisper_model": "small",     # tiny|base|small|medium|large-v3
        "font_file": None,            # 예: "assets/fonts/Pretendard-Bold.ttf"
        "font_name": "Noto Sans CJK KR",   # font_file 이 없을 때 쓰는 시스템 폰트명
        "font_size": 74,
        "bold": True,
        "primary_color": "#FFFFFF",
        "outline_color": "#000000",
        "back_color": "#000000",
        "outline": 5,
        "shadow": 2,
        "border_style": 1,            # 1=외곽선, 3=박스 배경
        "alignment": 2,               # 2=하단중앙, 5=상단중앙, 8=상단, 텐키 배열
        "margin_v": 420,              # 아래(또는 위) 여백 px
        "margin_h": 80,
        "max_chars_per_line": 18,
        "max_lines": 2,
        "uppercase": False,
    },

    "bgm": {
        "enabled": False,
        "file": None,                 # 예: "assets/bgm/lofi.mp3"
        "volume_db": -20,
        "loop": True,
        "fade_in": 1.0,
        "fade_out": 1.5,
        # 말할 때 배경음을 자동으로 낮춤
        "duck": {"enabled": True, "amount_db": -10},
    },

    "watermark": {
        "enabled": False,
        "file": None,                 # PNG 권장
        "position": "top-right",      # top-left|top-right|bottom-left|bottom-right
        "margin": 48,
        "opacity": 0.85,
        "scale": 0.12,                # 영상 가로폭 대비 비율
    },

    # 첫 몇 초간 화면에 크게 박히는 훅 문구
    "hook": {
        "enabled": False,
        "text": "",
        "start": 0.0,
        "duration": 3.0,
        "font_file": None,
        "font_size": 96,
        "color": "#FFFFFF",
        "outline_color": "#000000",
        "outline": 6,
        "box": False,
        "box_color": "#000000",
        "box_opacity": 0.6,
        "position_y": 0.18,           # 화면 높이 대비 비율
    },
}

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class PresetError(ValueError):
    """프리셋 내용이 잘못됐을 때."""


def _deep_merge(base: dict, override: dict, path: str = "") -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        here = f"{path}.{key}" if path else key
        if key not in out:
            raise PresetError(
                f"알 수 없는 프리셋 항목입니다: '{here}'. "
                f"사용 가능한 항목: {', '.join(sorted(out)) or '(없음)'}"
            )
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value, here)
        else:
            out[key] = value
    return out


def load(path: str | Path) -> dict:
    """프리셋 파일을 읽어 기본값과 병합한 뒤 검증해서 돌려준다."""
    p = Path(path)
    if not p.is_file():
        raise PresetError(f"프리셋 파일을 찾을 수 없습니다: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PresetError(f"프리셋 JSON 문법 오류 ({p}): {exc}") from exc
    if not isinstance(raw, dict):
        raise PresetError(f"프리셋 최상위는 객체여야 합니다: {p}")

    raw.pop("$schema", None)   # 에디터 자동완성용 키는 무시
    preset = _deep_merge(DEFAULTS, raw)
    validate(preset)
    return preset


def apply_overrides(preset: dict, overrides: list[str]) -> dict:
    """CLI 의 --set a.b=c 형태 오버라이드를 적용한다."""
    out = copy.deepcopy(preset)
    for item in overrides:
        if "=" not in item:
            raise PresetError(f"--set 은 '경로=값' 형태여야 합니다: {item!r}")
        dotted, _, rawval = item.partition("=")
        keys = dotted.strip().split(".")
        node = out
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node:
                raise PresetError(f"--set 경로가 존재하지 않습니다: {dotted}")
            node = node[key]
        last = keys[-1]
        if not isinstance(node, dict) or last not in node:
            raise PresetError(f"--set 경로가 존재하지 않습니다: {dotted}")
        node[last] = _coerce(rawval)
    validate(out)
    return out


def _coerce(text: str):
    low = text.strip().lower()
    if low in ("null", "none", ""):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise PresetError(message)


def _check_color(value, label: str) -> None:
    _require(
        isinstance(value, str) and bool(_HEX_RE.match(value)),
        f"{label} 는 '#RRGGBB' 형태의 색상이어야 합니다 (받은 값: {value!r})",
    )


def validate(preset: dict) -> None:
    out = preset["output"]
    _require(isinstance(out["width"], int) and out["width"] > 0, "output.width 는 양의 정수여야 합니다")
    _require(isinstance(out["height"], int) and out["height"] > 0, "output.height 는 양의 정수여야 합니다")
    _require(out["width"] % 2 == 0 and out["height"] % 2 == 0,
             "output.width/height 는 짝수여야 합니다 (H.264 제약)")
    _require(isinstance(out["fps"], (int, float)) and out["fps"] > 0, "output.fps 는 0보다 커야 합니다")
    _require(out["max_duration"] is None or out["max_duration"] > 0,
             "output.max_duration 은 null 이거나 0보다 커야 합니다")
    _require(isinstance(out["crf"], int) and 0 <= out["crf"] <= 51, "output.crf 는 0~51 사이 정수여야 합니다")

    src = preset["source"]
    _require(src["fit"] in ("crop", "pad", "blur_pad"),
             "source.fit 은 crop / pad / blur_pad 중 하나여야 합니다")
    _require(src["crop_anchor"] in ("center", "top", "bottom", "left", "right"),
             "source.crop_anchor 는 center/top/bottom/left/right 중 하나여야 합니다")
    _check_color(src["pad_color"], "source.pad_color")
    _require(0.25 <= src["speed"] <= 4.0, "source.speed 는 0.25 ~ 4.0 사이여야 합니다")
    start, end = src["trim"]["start"], src["trim"]["end"]
    _require(start is None or start >= 0, "source.trim.start 는 0 이상이어야 합니다")
    if start is not None and end is not None:
        _require(end > start, "source.trim.end 는 start 보다 커야 합니다")

    sub = preset["subtitle"]
    _require(sub["mode"] in ("auto", "file", "none"),
             "subtitle.mode 는 auto / file / none 중 하나여야 합니다")
    _require(sub["border_style"] in (1, 3), "subtitle.border_style 은 1 또는 3 이어야 합니다")
    _require(1 <= sub["alignment"] <= 9, "subtitle.alignment 는 1~9 사이여야 합니다")
    _require(sub["max_chars_per_line"] >= 4, "subtitle.max_chars_per_line 은 4 이상이어야 합니다")
    _require(sub["max_lines"] >= 1, "subtitle.max_lines 는 1 이상이어야 합니다")
    _require(sub["font_size"] > 0, "subtitle.font_size 는 0보다 커야 합니다")
    for key in ("primary_color", "outline_color", "back_color"):
        _check_color(sub[key], f"subtitle.{key}")
    if sub["enabled"] and sub["font_file"]:
        _require(Path(sub["font_file"]).is_file(),
                 f"subtitle.font_file 을 찾을 수 없습니다: {sub['font_file']}")

    bgm = preset["bgm"]
    if bgm["enabled"]:
        _require(bool(bgm["file"]), "bgm.enabled 가 true 면 bgm.file 이 필요합니다")
        _require(Path(bgm["file"]).is_file(), f"bgm.file 을 찾을 수 없습니다: {bgm['file']}")
        _require(bgm["fade_in"] >= 0 and bgm["fade_out"] >= 0, "bgm 페이드 값은 0 이상이어야 합니다")

    wm = preset["watermark"]
    if wm["enabled"]:
        _require(bool(wm["file"]), "watermark.enabled 가 true 면 watermark.file 이 필요합니다")
        _require(Path(wm["file"]).is_file(), f"watermark.file 을 찾을 수 없습니다: {wm['file']}")
        _require(wm["position"] in ("top-left", "top-right", "bottom-left", "bottom-right"),
                 "watermark.position 은 top-left/top-right/bottom-left/bottom-right 중 하나여야 합니다")
        _require(0 < wm["scale"] <= 1, "watermark.scale 은 0 초과 1 이하여야 합니다")
        _require(0 <= wm["opacity"] <= 1, "watermark.opacity 는 0~1 사이여야 합니다")

    hook = preset["hook"]
    if hook["enabled"]:
        _require(bool(hook["text"]), "hook.enabled 가 true 면 hook.text 가 필요합니다")
        _require(hook["duration"] > 0, "hook.duration 은 0보다 커야 합니다")
        _require(0 <= hook["position_y"] <= 1, "hook.position_y 는 0~1 사이여야 합니다")
        _check_color(hook["color"], "hook.color")
        _check_color(hook["outline_color"], "hook.outline_color")
        if hook["font_file"]:
            _require(Path(hook["font_file"]).is_file(),
                     f"hook.font_file 을 찾을 수 없습니다: {hook['font_file']}")
