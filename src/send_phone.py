#!/usr/bin/env python3
"""완성된 영상 + 제목 + 설명을 텔레그램으로 여러 폰에 보낸다.

폰마다 메시지 3개가 도착한다.
    1) 영상 파일        → 원본 그대로(재압축 없음). 폰에 저장해서 업로드
    2) 제목             → 탭 한 번에 복사
    3) 설명             → 블록 우측 상단 '복사' 버튼

사용 예:
    python src/send_phone.py --video output/a.mp4 --title "제목" --desc "설명"
    python src/send_phone.py --title "제목" --desc-file desc.txt --to 폰1,폰3
    python src/send_phone.py --find-chats      # 채팅 ID 확인 (최초 1회)

설정은 phones.json (phones.example.json 참고) 또는 환경변수
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS(쉼표 구분) 로 준다.
"""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "phones.json"
HOME_CONFIG = Path.home() / ".shorts" / "phones.json"   # 여러 폴더(터미널)가 같이 쓰는 위치
API = "https://api.telegram.org/bot{token}/{method}"
VIDEO_LIMIT = 50 * 1024 * 1024   # 봇 API 업로드 한도
TEXT_LIMIT = 4096                # 메시지 1개 글자 수 한도


class SendError(RuntimeError):
    pass


def find_config() -> Path | None:
    """--config 가 없을 때: 환경변수 → 이 폴더의 phones.json → ~/.shorts/phones.json"""
    env = os.environ.get("SHORTS_PHONES_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (DEFAULT_CONFIG, HOME_CONFIG):
        if candidate.exists():
            return candidate
    return None


def load_config(path: Path | None = None) -> tuple[str, dict[str, str]]:
    """(봇 토큰, {이름: chat_id}) 를 돌려준다. 환경변수가 파일보다 우선."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chats: dict[str, str] = {}

    path = path or find_config()
    if path and path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        token = token or data.get("bot_token", "")
        chats = {str(k): str(v) for k, v in data.get("chats", {}).items()}

    env_ids = os.environ.get("TELEGRAM_CHAT_IDS", "")
    if env_ids:
        chats = {f"chat{i + 1}": cid.strip()
                 for i, cid in enumerate(env_ids.split(",")) if cid.strip()}

    if not token or token.startswith("여기에"):
        raise SendError(
            "텔레그램 봇 토큰이 없습니다. phones.example.json 을 phones.json 으로 복사해 "
            f"bot_token 을 채우세요 ({DEFAULT_CONFIG} 또는 {HOME_CONFIG}). "
            "TELEGRAM_BOT_TOKEN 환경변수도 됩니다."
        )
    return token, chats


def select_chats(chats: dict[str, str], only: str | None) -> dict[str, str]:
    if not only:
        return chats
    names = [n.strip() for n in only.split(",") if n.strip()]
    missing = [n for n in names if n not in chats]
    if missing:
        raise SendError(f"phones.json 에 없는 이름: {', '.join(missing)} "
                        f"(있는 이름: {', '.join(chats) or '없음'})")
    return {n: chats[n] for n in names}


def title_message(title: str) -> str:
    # <code> 는 텔레그램에서 탭하면 바로 복사된다
    return f"<code>{html.escape(title)}</code>"


def desc_messages(desc: str) -> list[str]:
    """<pre> 블록에는 '복사' 버튼이 붙는다. 길면 여러 개로 나눈다."""
    room = TEXT_LIMIT - len("<pre></pre>") - 200   # 이스케이프 여유분
    chunks, current = [], ""
    for line in desc.splitlines(keepends=True):
        while len(line) > room:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:room])
            line = line[room:]
        if len(current) + len(line) > room:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [f"<pre>{html.escape(c.rstrip(chr(10)))}</pre>" for c in chunks]


def _call(token: str, method: str, *, fields: dict, file_field: str | None = None,
          file_path: Path | None = None, timeout: float = 30) -> dict:
    url = API.format(token=token, method=method)
    if file_path is None:
        body = json.dumps(fields).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8"))
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8"))
        parts.append(file_path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            raise SendError(f"텔레그램 오류 HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise SendError(f"텔레그램에 연결하지 못했습니다: {exc.reason}") from None
    if not data.get("ok"):
        raise SendError(f"텔레그램 오류: {data.get('description', data)}")
    return data["result"]


def send(*, title: str = "", desc: str = "", video: Path | None = None, label: str = "",
         only: str | None = None, config: Path | None = None) -> int:
    """설정된 모든(또는 지정한) 폰에 보낸다. 실패한 폰 수를 돌려준다."""
    token, chats = load_config(config)
    chats = select_chats(chats, only)
    if not chats:
        raise SendError("보낼 채팅이 없습니다. --find-chats 로 ID 를 확인해 phones.json 에 적으세요.")
    if not (title or desc or video):
        raise SendError("보낼 내용이 없습니다 (--title / --desc / --video).")

    send_video = video is not None
    if video is not None:
        if not video.exists():
            raise SendError(f"영상 파일이 없습니다: {video}")
        size = video.stat().st_size
        if size > VIDEO_LIMIT:
            print(f"  ! 영상이 {size / 1024 / 1024:.0f}MB 라 봇 한도(50MB)를 넘습니다. "
                  "제목·설명만 보냅니다.", file=sys.stderr)
            send_video = False

    failed = 0
    for name, chat_id in chats.items():
        try:
            if label:
                # 여러 터미널이 같은 채팅으로 보낼 때 어느 묶음인지 구분하는 머리글
                _call(token, "sendMessage", fields={"chat_id": chat_id,
                      "text": f"<b>━━ {html.escape(label)} ━━</b>", "parse_mode": "HTML"})
            if send_video:
                # sendVideo 가 아니라 '파일'로 보내야 화질·용량이 원본 그대로 유지된다
                _call(token, "sendDocument", fields={"chat_id": chat_id,
                      "disable_content_type_detection": "true"},
                      file_field="document", file_path=video, timeout=300)
            if title:
                _call(token, "sendMessage", fields={"chat_id": chat_id,
                      "text": title_message(title), "parse_mode": "HTML"})
            for msg in desc_messages(desc) if desc else []:
                _call(token, "sendMessage", fields={"chat_id": chat_id,
                      "text": msg, "parse_mode": "HTML"})
            print(f"  ✓ {name}")
        except SendError as exc:
            failed += 1
            print(f"  ✗ {name}: {exc}", file=sys.stderr)
    return failed


def find_chats(config: Path | None = None) -> None:
    token, _ = load_config(config)
    updates = _call(token, "getUpdates", fields={})
    seen: dict[str, str] = {}
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat")
        if not chat:
            continue
        label = chat.get("title") or " ".join(
            filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
        seen[str(chat["id"])] = f"{label} ({chat.get('type')})"
    if not seen:
        print("받은 메시지가 없습니다. 각 폰에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        return
    print("chat_id 목록 — phones.json 의 chats 에 이름과 함께 적으세요:")
    for cid, label in seen.items():
        print(f"  {cid:>16}  {label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="send_phone.py",
                                     description="영상·제목·설명을 텔레그램으로 여러 폰에 전송")
    parser.add_argument("--video", type=Path, help="보낼 영상 파일 (50MB 이하)")
    parser.add_argument("--title", default="", help="제목")
    parser.add_argument("--desc", default="", help="설명 (줄바꿈은 \\n 대신 --desc-file 권장)")
    parser.add_argument("--desc-file", type=Path, help="설명이 담긴 텍스트 파일")
    parser.add_argument("--to", help="일부 폰에만 보내기 (예: 폰1,폰3)")
    parser.add_argument("--label", default="", help="묶음 머리글 (예: 폰2, 요리채널)")
    parser.add_argument("--config", type=Path,
                        help=f"설정 파일 (기본: {DEFAULT_CONFIG.name} → ~/.shorts/phones.json)")
    parser.add_argument("--find-chats", action="store_true", help="봇에 말을 건 채팅의 ID 출력")
    args = parser.parse_args(argv)

    if args.find_chats:
        find_chats(args.config)
        return 0

    desc = args.desc_file.read_text(encoding="utf-8") if args.desc_file else args.desc
    failed = send(title=args.title, desc=desc.strip(), video=args.video,
                  label=args.label, only=args.to, config=args.config)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SendError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)
