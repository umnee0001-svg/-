"""채널 설정 로딩 / 병합 / 검증.

channels.json 에는 채널을 몇 개든 적을 수 있습니다. 채널 하나가
MultiLogin 프로필 하나에 대응합니다. 빠진 키는 CHANNEL_DEFAULTS 로 채워집니다.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 채널 하나의 기본값. channels.json 에는 바꾸고 싶은 항목만 적으면 됩니다.
CHANNEL_DEFAULTS: dict = {
    "id": "",                    # 내부 식별자 (영문/숫자/하이픈)
    "name": "",                  # UI 에 보일 이름
    "enabled": True,

    "multilogin": {
        # "x" = MultiLogin X, "v6" = MultiLogin 6 (구버전)
        "version": "x",
        "profile_id": "",
        "folder_id": "",         # X 에서만 사용
    },

    "schedule": {
        # 이 채널이 올리는 시각. "HH:MM" 을 원하는 만큼.
        "slots": ["09:00", "18:00"],
        # 0=월 ... 6=일
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        # 같은 채널 업로드 사이 최소 간격(분). 슬롯이 촘촘해도 이 간격은 지킵니다.
        "min_gap_minutes": 120,
        # 하루 상한. null 이면 슬롯 개수만큼.
        "max_per_day": None,
    },

    "defaults": {
        # public | unlisted | private | scheduled
        # scheduled 면 publish_at 시각에 자동 공개됩니다.
        "visibility": "scheduled",
        "made_for_kids": False,
        # {title} {description} {hashtags} {date} 치환 가능
        "title_template": "{title}",
        "description_template": "{description}\n\n{hashtags}",
        # 영상마다 따로 안 적으면 이 해시태그가 붙습니다.
        "hashtags": [],
        "playlist": None,
        "shopping": {
            "enabled": False,
            # Studio 쇼핑 탭에서 검색할 제품 키워드 또는 제품 URL
            "products": [],
        },
    },
}

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

VISIBILITIES = {"public", "unlisted", "private", "scheduled"}
ML_VERSIONS = {"x", "v6"}


class ConfigError(ValueError):
    """설정 파일이 잘못됐을 때. 메시지에 어느 항목인지 적습니다."""


def _merge(base: dict, override: dict, path: str = "") -> dict:
    """override 에 있는 키만 base 위에 덮어씁니다(깊은 병합)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        where = f"{path}.{key}" if path else key
        if key not in out:
            raise ConfigError(f"알 수 없는 설정 항목입니다: {where}")
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value, where)
        else:
            out[key] = value
    return out


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def validate_channel(ch: dict) -> None:
    where = ch.get("id") or "(id 없음)"

    _require(bool(ch["id"]), "채널의 id 가 비어 있습니다.")
    _require(bool(ID_RE.match(ch["id"])),
             f"채널 id 는 영문/숫자로 시작하고 -_ 만 쓸 수 있습니다: {ch['id']}")
    _require(bool(ch["name"]), f"[{where}] name 이 비어 있습니다.")

    ml = ch["multilogin"]
    _require(ml["version"] in ML_VERSIONS,
             f"[{where}] multilogin.version 은 {sorted(ML_VERSIONS)} 중 하나여야 합니다.")
    _require(bool(ml["profile_id"]),
             f"[{where}] multilogin.profile_id 가 비어 있습니다. "
             f"MultiLogin 앱에서 프로필 ID 를 복사해 넣으세요.")
    if ml["version"] == "x":
        _require(bool(ml["folder_id"]),
                 f"[{where}] MultiLogin X 는 folder_id 도 필요합니다.")

    sc = ch["schedule"]
    _require(isinstance(sc["slots"], list) and sc["slots"],
             f"[{where}] schedule.slots 에 시각을 최소 하나 적어야 합니다. 예: [\"09:00\"]")
    for slot in sc["slots"]:
        _require(isinstance(slot, str) and bool(TIME_RE.match(slot)),
                 f"[{where}] 시각 형식이 잘못됐습니다(HH:MM 24시간): {slot!r}")
    _require(len(set(sc["slots"])) == len(sc["slots"]),
             f"[{where}] schedule.slots 에 같은 시각이 중복됩니다.")
    _require(isinstance(sc["weekdays"], list) and sc["weekdays"],
             f"[{where}] schedule.weekdays 가 비어 있습니다. 0=월 ... 6=일")
    for day in sc["weekdays"]:
        _require(isinstance(day, int) and 0 <= day <= 6,
                 f"[{where}] weekdays 는 0~6 사이 정수여야 합니다: {day!r}")
    _require(isinstance(sc["min_gap_minutes"], int) and sc["min_gap_minutes"] >= 0,
             f"[{where}] min_gap_minutes 는 0 이상 정수여야 합니다.")
    if sc["max_per_day"] is not None:
        _require(isinstance(sc["max_per_day"], int) and sc["max_per_day"] > 0,
                 f"[{where}] max_per_day 는 1 이상 정수이거나 null 이어야 합니다.")

    de = ch["defaults"]
    _require(de["visibility"] in VISIBILITIES,
             f"[{where}] visibility 는 {sorted(VISIBILITIES)} 중 하나여야 합니다.")
    _require(isinstance(de["hashtags"], list),
             f"[{where}] hashtags 는 리스트여야 합니다.")
    for tag in de["hashtags"]:
        _require(isinstance(tag, str) and tag.startswith("#"),
                 f"[{where}] 해시태그는 # 로 시작해야 합니다: {tag!r}")
    shop = de["shopping"]
    _require(isinstance(shop["products"], list),
             f"[{where}] shopping.products 는 리스트여야 합니다.")
    if shop["enabled"]:
        _require(bool(shop["products"]),
                 f"[{where}] shopping.enabled 가 켜져 있는데 products 가 비어 있습니다.")


def load(path: str | Path) -> dict:
    """channels.json 을 읽어 기본값을 채운 설정 딕셔너리를 돌려줍니다."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"채널 설정 파일이 없습니다: {path}\n"
            f"channels.example.json 을 복사해서 만드세요."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 의 JSON 형식이 잘못됐습니다: {exc}") from exc

    _require(isinstance(raw, dict), f"{path} 의 최상위는 객체여야 합니다.")
    unknown = set(raw) - {"timezone", "channels"}
    _require(not unknown, f"알 수 없는 최상위 항목입니다: {sorted(unknown)}")

    tz_name = raw.get("timezone", "Asia/Seoul")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone 을 알 수 없습니다: {tz_name!r} ({exc})") from exc

    channels_raw = raw.get("channels", [])
    _require(isinstance(channels_raw, list), "channels 는 리스트여야 합니다.")

    channels = []
    seen_ids: set[str] = set()
    seen_profiles: dict[str, str] = {}
    for item in channels_raw:
        _require(isinstance(item, dict), "channels 의 각 항목은 객체여야 합니다.")
        ch = _merge(CHANNEL_DEFAULTS, item)
        validate_channel(ch)
        _require(ch["id"] not in seen_ids, f"채널 id 가 중복됩니다: {ch['id']}")
        seen_ids.add(ch["id"])
        # 프로필 하나를 두 채널이 공유하면 격리 의미가 사라지므로 막습니다.
        profile = ch["multilogin"]["profile_id"]
        _require(profile not in seen_profiles,
                 f"MultiLogin 프로필 {profile} 을 채널 "
                 f"{seen_profiles.get(profile)} 와 {ch['id']} 가 함께 쓰고 있습니다. "
                 f"채널마다 프로필을 따로 두세요.")
        seen_profiles[profile] = ch["id"]
        channels.append(ch)

    return {"timezone": tz_name, "channels": channels}


def save(config: dict, path: str | Path) -> None:
    """UI 에서 채널을 추가/수정했을 때 되쓰기. 쓰기 전에 검증합니다."""
    for ch in config["channels"]:
        validate_channel(ch)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def new_channel(**overrides) -> dict:
    """UI 의 '채널 추가' 가 쓰는 빈 채널. 채널 개수 제한은 없습니다."""
    return _merge(CHANNEL_DEFAULTS, overrides)


def find(config: dict, channel_id: str) -> dict:
    for ch in config["channels"]:
        if ch["id"] == channel_id:
            return ch
    raise ConfigError(f"그런 채널이 없습니다: {channel_id}")
