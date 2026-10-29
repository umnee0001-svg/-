#!/usr/bin/env python3
"""24시간 라이브 송출기 — 브라우저 창으로 여는 로컬 앱.

사용 예:
    python src/live_app.py                      # 창이 브라우저로 열림
    python src/live_app.py --data D:/LiveLoop   # 보관함·설정 저장 위치 지정
    python src/live_app.py --port 8725 --no-browser
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_core import Manager  # noqa: E402

UI_PATH = Path(__file__).resolve().parent / "live_ui.html"
DEFAULT_PORT = 8724
# 다른 웹사이트가 이 앱으로 요청을 보내지 못하게, 화면이 붙이는 헤더가 있어야만 변경을 받는다.
CSRF_HEADER = "X-LiveLoop"


def make_handler(manager: Manager, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "LiveLoop"

        def log_message(self, fmt, *args):  # 요청마다 찍히는 로그는 생략
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, message: str, status: int = 400) -> None:
            self._json({"error": message}, status)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _guard(self, write: bool) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._error("허용되지 않은 주소입니다.", HTTPStatus.FORBIDDEN)
                return False
            if write and self.headers.get(CSRF_HEADER) != "1":
                self._error("허용되지 않은 요청입니다.", HTTPStatus.FORBIDDEN)
                return False
            return True

        def do_GET(self):
            if not self._guard(write=False):
                return
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(manager.snapshot())
            else:
                self._error("없는 주소입니다.", HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if not self._guard(write=True):
                return
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            try:
                if url.path in ("/api/upload", "/api/cover"):
                    name = unquote(parse_qs(url.query).get("name", [""])[0])
                    length = int(self.headers.get("Content-Length") or 0)
                    if not name or not length:
                        return self._error("파일이 비어 있습니다.")
                    if url.path == "/api/upload":
                        return self._json(manager.receive_upload(name, self.rfile, length))
                    manager.set_cover(name, self.rfile, length)
                    return self._json({"ok": True})
                body = self._body()
                if url.path == "/api/channels":
                    ch = manager.add_channel(body.get("name", ""), body.get("key", ""),
                                             body.get("server", ""))
                    return self._json({"id": ch["id"]})
                if url.path == "/api/broadcasts":
                    return self._json(manager.start_broadcast(
                        body.get("channel_id", ""), body.get("file_id", ""), body.get("mode", "")))
                if parts[:2] == ["api", "broadcasts"] and len(parts) == 4 and parts[3] == "stop":
                    manager.stop_broadcast(parts[2])
                    return self._json({"ok": True})
                if url.path == "/api/settings":
                    manager.update_settings(body)
                    return self._json({"ok": True})
                self._error("없는 주소입니다.", HTTPStatus.NOT_FOUND)
            except (ValueError, KeyError, OSError) as exc:
                self._error(exc.args[0] if exc.args else str(exc))

        def do_DELETE(self):
            if not self._guard(write=True):
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            try:
                if parts[:2] == ["api", "files"] and len(parts) == 3:
                    manager.delete_file(parts[2])
                elif parts[:2] == ["api", "channels"] and len(parts) == 3:
                    manager.delete_channel(parts[2])
                else:
                    return self._error("없는 주소입니다.", HTTPStatus.NOT_FOUND)
                self._json({"ok": True})
            except (ValueError, KeyError) as exc:
                self._error(exc.args[0] if exc.args else str(exc))

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live_app.py", description="24시간 라이브 송출기")
    parser.add_argument("--data", default=str(Path.home() / "LiveLoop"),
                        help="보관함·채널·설정을 저장할 폴더 (기본: 사용자폴더/LiveLoop)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="브라우저 창을 자동으로 열지 않음")
    args = parser.parse_args(argv)

    url = f"http://127.0.0.1:{args.port}/"
    try:
        manager = Manager(args.data)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(manager, args.port))
    except OSError as exc:
        # 이미 켜져 있으면 창만 다시 연다.
        print(f"포트 {args.port} 를 쓸 수 없습니다 ({exc}). 이미 실행 중이라면 창만 엽니다.")
        if not args.no_browser:
            webbrowser.open(url)
        return 1

    print("=" * 56)
    print(" 24시간 라이브 송출기")
    print(f" 창 주소   : {url}")
    print(f" 저장 위치 : {manager.data_dir}")
    print(" 이 검은 창을 닫으면 방송도 멈춥니다. (다시 켜면 이어서 송출)")
    print("=" * 56)
    if not manager.ffmpeg_ok:
        print("!! ffmpeg 를 찾을 수 없습니다.  winget install Gyan.FFmpeg  후 다시 켜 주세요.")

    manager.resume()
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("종료 중… 송출을 정리합니다.")
        server.server_close()
        manager.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
