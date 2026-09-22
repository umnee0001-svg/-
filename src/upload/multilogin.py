"""MultiLogin 프로필을 띄우고 Playwright 가 붙을 CDP 주소를 받아옵니다.

MultiLogin 로컬 API 는 버전마다 경로와 응답 형태가 다르고, 업데이트되면서
또 바뀝니다. 그래서 경로를 settings.json 으로 빼두었습니다. 연결이 안 되면
코드가 아니라 settings.json 의 multilogin 항목을 고치세요.

  MultiLogin X  : /api/v2/profile/f/{folder_id}/p/{profile_id}/start?automation=true
  MultiLogin 6  : /api/v1/profile/start?automation=puppeteer&profileId={profile_id}

둘 다 MultiLogin 앱이 실행 중이고 로그인돼 있어야 동작합니다.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

# 로컬 API 기본 주소. MultiLogin 앱이 이 포트로 듣습니다.
DEFAULT_BASE = "http://127.0.0.1:35000"

DEFAULT_ENDPOINTS = {
    "x": {
        "start": "/api/v2/profile/f/{folder_id}/p/{profile_id}/start"
                 "?automation=true&headless_mode=false",
        "stop": "/api/v1/profile/stop/p/{profile_id}",
    },
    "v6": {
        "start": "/api/v1/profile/start?automation=puppeteer&profileId={profile_id}",
        "stop": "/api/v1/profile/stop?profileId={profile_id}",
    },
}

WS_RE = re.compile(r"(ws://[^\s\"']+)")
PORT_RE = re.compile(r"\b(\d{2,5})\b")


class MultiLoginError(RuntimeError):
    """MultiLogin 로컬 API 와 얘기가 안 될 때."""


def _request(url: str, token: str = "", timeout: float = 120.0) -> str:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise MultiLoginError(
            f"MultiLogin 로컬 API 가 {exc.code} 를 돌려줬습니다.\n"
            f"  요청: {url}\n  응답: {body}\n"
            f"  → MultiLogin 앱이 켜져 있고 로그인돼 있는지, profile_id/folder_id 가 "
            f"맞는지 확인하세요."
        ) from exc
    except urllib.error.URLError as exc:
        raise MultiLoginError(
            f"MultiLogin 로컬 API 에 연결하지 못했습니다: {url}\n"
            f"  사유: {exc.reason}\n"
            f"  → MultiLogin 앱을 실행하세요. 포트가 다르면 "
            f"settings.json 의 multilogin.base_url 을 고치세요."
        ) from exc


def _extract_cdp(body: str, base: str) -> str:
    """응답에서 CDP 주소를 뽑아냅니다. 버전마다 모양이 달라 순서대로 시도합니다."""
    body = body.strip()

    # 1) 본문 어딘가에 ws:// 가 그대로 들어있는 경우 (MultiLogin 6 puppeteer 모드)
    match = WS_RE.search(body)
    if match:
        return match.group(1)

    # 2) JSON 안에 포트가 들어있는 경우 (MultiLogin X)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        for holder in (data.get("data"), data, data.get("value")):
            if not isinstance(holder, dict):
                continue
            for key in ("ws", "wsUrl", "webSocketDebuggerUrl", "cdp_url"):
                value = holder.get(key)
                if isinstance(value, str) and value.startswith("ws"):
                    return value
            for key in ("port", "debug_port", "debugPort"):
                value = holder.get(key)
                if value:
                    host = base.split("//", 1)[-1].split(":", 1)[0]
                    return f"http://{host}:{value}"
        # 실패 응답이면 이유를 그대로 보여줍니다.
        status = data.get("status") or {}
        message = (status.get("message") if isinstance(status, dict) else None) \
            or data.get("message") or ""
        if message:
            raise MultiLoginError(f"MultiLogin 이 프로필 시작을 거절했습니다: {message}")

    # 3) 본문이 그냥 포트 숫자거나 http 주소인 경우
    if body.startswith("http"):
        return body
    if body.isdigit():
        host = base.split("//", 1)[-1].split(":", 1)[0]
        return f"http://{host}:{body}"

    raise MultiLoginError(
        f"MultiLogin 응답에서 CDP 주소를 찾지 못했습니다.\n"
        f"  응답: {body[:500]}\n"
        f"  → MultiLogin 버전이 바뀌었을 수 있습니다. settings.json 의 "
        f"multilogin.endpoints 를 현재 버전 문서에 맞게 고치세요."
    )


class Session:
    """띄워둔 프로필 하나. with 문으로 쓰면 끝날 때 알아서 닫습니다."""

    def __init__(self, cdp_url: str, *, profile_id: str, stop_url: str, token: str):
        self.cdp_url = cdp_url
        self.profile_id = profile_id
        self._stop_url = stop_url
        self._token = token
        self._closed = False

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _request(self._stop_url, self._token, timeout=30)
        except MultiLoginError:
            # 이미 닫혔거나 앱이 내려간 경우. 업로드 결과까지 실패로 만들 이유는 없습니다.
            pass


def launch(channel: dict, settings: dict) -> Session:
    """채널에 묶인 MultiLogin 프로필을 띄우고 Session 을 돌려줍니다."""
    ml_cfg = settings.get("multilogin", {})
    base = (ml_cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
    token = ml_cfg.get("token", "")
    endpoints = ml_cfg.get("endpoints") or {}

    ml = channel["multilogin"]
    version = ml["version"]
    paths = {**DEFAULT_ENDPOINTS[version], **endpoints.get(version, {})}

    fields = {"profile_id": ml["profile_id"], "folder_id": ml["folder_id"]}
    start_url = base + paths["start"].format(**fields)
    stop_url = base + paths["stop"].format(**fields)

    body = _request(start_url, token,
                    timeout=float(ml_cfg.get("start_timeout", 180)))
    cdp_url = _extract_cdp(body, base)

    # 브라우저가 실제로 포트를 열 때까지 잠깐 여유를 줍니다.
    time.sleep(float(ml_cfg.get("warmup_seconds", 3)))
    return Session(cdp_url, profile_id=ml["profile_id"],
                   stop_url=stop_url, token=token)
