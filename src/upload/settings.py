"""앱 전역 설정 (settings.json). 없으면 기본값으로 동작합니다."""
from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULTS: dict = {
    # 파일 경로 (앱 실행 위치 기준 상대경로 허용)
    "channels_file": "channels.json",
    "db_file": "upload/queue.db",

    "server": {
        "host": "127.0.0.1",
        "port": 8777,
        "open_browser": True,
    },

    "multilogin": {
        "base_url": "http://127.0.0.1:35000",
        "token": "",
        "start_timeout": 180,
        "warmup_seconds": 3,
        # 버전이 바뀌어 경로가 달라지면 여기에 덮어쓰세요.
        # 예: {"x": {"start": "/api/v3/..."}}
        "endpoints": {},
    },

    "studio": {
        "selectors_path": None,          # null 이면 upload/selectors.json
        # 채널 계정 언어에 맞춘 날짜/시간 표기 (한국어 계정 기준)
        "date_format": "%Y. %m. %d.",
        "time_format": "%H:%M",
        "processing_timeout_seconds": 900,
        # 브라우저를 눈으로 보면서 확인하고 싶을 때 느리게
        "slow_mo_ms": 0,
    },

    "worker": {
        # 예약 공개 시각보다 몇 분 앞서 업로드를 시작할지.
        # 처리 시간이 있으니 넉넉히 두는 편이 안전합니다.
        "lead_minutes": 45,
        # 큐를 몇 초마다 확인할지
        "poll_seconds": 60,
        # 한 작업을 몇 번까지 다시 시도할지
        "max_attempts": 3,
        # 재시도 전에 몇 분 쉴지
        "retry_after_minutes": 20,
        # 한 번에 여러 채널을 동시에 올리지 않습니다(프로필 격리 유지).
        "sequential": True,
    },
}


class SettingsError(ValueError):
    pass


def _merge(base: dict, override: dict, path: str = "") -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        where = f"{path}.{key}" if path else key
        if key not in out:
            raise SettingsError(f"알 수 없는 설정 항목입니다: {where}")
        # endpoints 는 자유 형식이라 통째로 받습니다.
        if isinstance(out[key], dict) and isinstance(value, dict) and key != "endpoints":
            out[key] = _merge(out[key], value, where)
        else:
            out[key] = value
    return out


def load(path: str | Path = "settings.json") -> dict:
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path} 의 JSON 형식이 잘못됐습니다: {exc}") from exc
    return _merge(DEFAULTS, raw)
