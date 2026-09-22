"""로컬 웹 UI 서버 (표준 라이브러리만 사용).

브라우저에서 채널을 추가하고, 영상을 큐에 넣고, 예약 현황을 보고,
일꾼을 켜고 끄는 화면입니다. 외부에 열지 않고 127.0.0.1 로만 듣습니다.
"""
from __future__ import annotations

import json
import mimetypes
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from upload import channels as channels_mod
from upload import queue as queue_mod
from upload import scheduler as scheduler_mod
from upload import worker as worker_mod

STATIC_DIR = Path(__file__).parent / "static"

# 큐에 넣을 수 있는 영상 확장자
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


class AppError(Exception):
    """UI 에 그대로 보여줄 오류."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class App:
    """UI 가 부르는 동작들. HTTP 와 분리해두면 테스트하기 쉽습니다."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.channels_path = Path(settings["channels_file"])
        self.conn = queue_mod.connect(settings["db_file"])
        self.lock = threading.Lock()
        self.worker = worker_mod.Worker(settings)
        queue_mod.recover_stuck(self.conn)

    # ------------------------------------------------------------ 설정

    def config(self) -> dict:
        if not self.channels_path.exists():
            # 처음 실행이면 빈 설정으로 시작합니다. UI 에서 채널을 추가하면 됩니다.
            return {"timezone": "Asia/Seoul", "channels": []}
        return channels_mod.load(self.channels_path)

    def save_config(self, config: dict) -> None:
        channels_mod.save(config, self.channels_path)

    # ------------------------------------------------------------ 상태

    def state(self) -> dict:
        try:
            config = self.config()
            config_error = ""
        except channels_mod.ConfigError as exc:
            config = {"timezone": "Asia/Seoul", "channels": []}
            config_error = str(exc)

        with self.lock:
            jobs = queue_mod.list_jobs(self.conn)
            logs = queue_mod.list_logs(self.conn, limit=120)

        by_channel = {ch["id"]: ch["name"] for ch in config["channels"]}
        for job in jobs:
            job["channel_name"] = by_channel.get(job["channel_id"], job["channel_id"])

        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1

        return {
            "timezone": config["timezone"],
            "channels": config["channels"],
            "jobs": jobs,
            "logs": logs,
            "counts": counts,
            "config_error": config_error,
            "worker": {
                "running": self.worker.running,
                "current_job": self.worker.current_job,
                "last_error": self.worker.last_error,
            },
            "now": datetime.now(ZoneInfo(config["timezone"])).isoformat(
                timespec="seconds"),
        }

    # ------------------------------------------------------------ 채널

    def save_channel(self, payload: dict) -> dict:
        config = self.config()
        incoming = channels_mod.new_channel(**payload)
        channels_mod.validate_channel(incoming)

        for index, existing in enumerate(config["channels"]):
            if existing["id"] == incoming["id"]:
                config["channels"][index] = incoming
                break
        else:
            config["channels"].append(incoming)

        try:
            self.save_config(config)
        except channels_mod.ConfigError as exc:
            raise AppError(str(exc)) from exc
        return {"ok": True, "channel": incoming}

    def delete_channel(self, channel_id: str) -> dict:
        config = self.config()
        remaining = [c for c in config["channels"] if c["id"] != channel_id]
        if len(remaining) == len(config["channels"]):
            raise AppError(f"그런 채널이 없습니다: {channel_id}", 404)
        with self.lock:
            pending = queue_mod.list_jobs(self.conn, channel_id=channel_id)
            live = [j for j in pending if j["status"] in ("queued", "scheduled")]
        if live:
            raise AppError(
                f"이 채널에 아직 올리지 않은 작업이 {len(live)}건 있습니다. "
                f"먼저 취소하거나 다른 채널로 옮기세요."
            )
        config["channels"] = remaining
        self.save_config(config)
        return {"ok": True}

    # ------------------------------------------------------------ 작업

    def add_jobs(self, payload: dict) -> dict:
        config = self.config()
        channel_id = payload.get("channel_id", "")
        channels_mod.find(config, channel_id)   # 없으면 여기서 오류

        videos = payload.get("videos") or []
        if not videos:
            raise AppError("영상 파일을 하나 이상 지정하세요.")

        publish_at = payload.get("publish_at") or None
        if publish_at:
            try:
                parsed = datetime.fromisoformat(publish_at)
            except ValueError as exc:
                raise AppError(
                    f"예약 시각 형식이 잘못됐습니다: {publish_at!r} "
                    f"(예: 2026-09-23T09:00)"
                ) from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(config["timezone"]))
            publish_at = parsed.isoformat(timespec="seconds")

        created = []
        with self.lock:
            for item in videos:
                path = Path(item.get("path", "")).expanduser()
                if not path.exists():
                    raise AppError(f"영상 파일이 없습니다: {path}")
                job_id = queue_mod.add_job(
                    self.conn,
                    channel_id=channel_id,
                    video_path=str(path.resolve()),
                    title=item.get("title", "") or payload.get("title", ""),
                    description=item.get("description", "")
                    or payload.get("description", ""),
                    hashtags=item.get("hashtags") or payload.get("hashtags") or [],
                    products=item.get("products") or payload.get("products") or [],
                    visibility=payload.get("visibility", "scheduled"),
                    # 여러 개를 한꺼번에 넣을 때 시각을 주면 전부 같은 시각이 되므로,
                    # 첫 건에만 붙이고 나머지는 스케줄러가 슬롯을 나눠 갖게 둡니다.
                    publish_at=publish_at if len(videos) == 1 else None,
                )
                created.append(job_id)
            scheduler_mod.assign(self.conn, config)
        return {"ok": True, "job_ids": created}

    def update_job(self, job_id: int, payload: dict) -> dict:
        with self.lock:
            job = queue_mod.get_job(self.conn, job_id)
            if not job:
                raise AppError(f"그런 작업이 없습니다: {job_id}", 404)
            if job["status"] == "uploading":
                raise AppError("지금 올리는 중인 작업은 수정할 수 없습니다.")
            fields = {k: v for k, v in payload.items()
                      if k in ("title", "description", "hashtags", "products",
                               "visibility", "publish_at", "channel_id", "status")}
            if fields.get("publish_at"):
                config = self.config()
                parsed = datetime.fromisoformat(fields["publish_at"])
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo(config["timezone"]))
                fields["publish_at"] = parsed.isoformat(timespec="seconds")
                fields["status"] = "scheduled"
            queue_mod.update_job(self.conn, job_id, **fields)
            return {"ok": True, "job": queue_mod.get_job(self.conn, job_id)}

    def cancel_job(self, job_id: int) -> dict:
        return self.update_job(job_id, {"status": "canceled"})

    def retry_job(self, job_id: int) -> dict:
        with self.lock:
            job = queue_mod.get_job(self.conn, job_id)
            if not job:
                raise AppError(f"그런 작업이 없습니다: {job_id}", 404)
            queue_mod.update_job(self.conn, job_id, status="scheduled",
                                 attempts=0, last_error="")
            queue_mod.log(self.conn, job_id, "수동 재시도 요청")
        return {"ok": True}

    def delete_job(self, job_id: int) -> dict:
        with self.lock:
            self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return {"ok": True}

    # ------------------------------------------------------------ 미리보기 / 탐색

    def preview(self, payload: dict) -> dict:
        """실제로 입력될 제목/설명을 미리 보여줍니다."""
        from upload import studio as studio_mod

        config = self.config()
        channel = channels_mod.find(config, payload.get("channel_id", ""))
        job = {
            "title": payload.get("title", ""),
            "description": payload.get("description", ""),
            "hashtags": payload.get("hashtags") or [],
            "products": [],
            "visibility": "scheduled",
            "publish_at": None,
        }
        return studio_mod.render(job, channel)

    def browse(self, directory: str) -> dict:
        """영상 고르기용 폴더 목록. 앱이 도는 컴퓨터의 로컬 경로만 봅니다."""
        path = Path(directory or ".").expanduser()
        if not path.is_dir():
            raise AppError(f"폴더가 아닙니다: {path}", 404)
        path = path.resolve()
        dirs, files = [], []
        try:
            for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() in VIDEO_SUFFIXES:
                    files.append({
                        "name": entry.name,
                        "path": str(entry),
                        "size_mb": round(entry.stat().st_size / 1024 / 1024, 1),
                    })
        except PermissionError as exc:
            raise AppError(f"폴더를 읽을 권한이 없습니다: {path}", 403) from exc
        return {"cwd": str(path), "parent": str(path.parent),
                "dirs": dirs, "files": files}

    # ------------------------------------------------------------ 일꾼

    def worker_start(self) -> dict:
        config = self.config()
        if not config["channels"]:
            raise AppError("채널을 먼저 하나 이상 추가하세요.")
        self.worker.start()
        return {"ok": True}

    def worker_stop(self) -> dict:
        self.worker.stop()
        return {"ok": True}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    app: App = None            # 서버 만들 때 주입
    server_version = "ShortsUploader/1.0"

    def log_message(self, fmt, *args) -> None:
        pass                    # 요청 로그로 콘솔을 덮지 않습니다.

    # -------------------------------------------------------- 도우미

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError(f"요청 본문이 JSON 이 아닙니다: {exc}") from exc

    def _send_static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        # 정적 폴더 밖으로 나가는 경로는 막습니다.
        if not str(target).startswith(str(STATIC_DIR.resolve())) \
                or not target.is_file():
            self.send_error(404, "not found")
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        app = self.app

        if method == "GET":
            if path == "/":
                return ("static", "index.html")
            if path == "/api/state":
                return ("json", app.state())
            if path == "/api/browse":
                return ("json", app.browse((query.get("dir") or ["."])[0]))
            if path.startswith("/static/"):
                return ("static", path[len("/static/"):])
            return None

        body = self._read_json()

        if method == "POST":
            if path == "/api/channels":
                return ("json", app.save_channel(body))
            if path == "/api/jobs":
                return ("json", app.add_jobs(body))
            if path == "/api/preview":
                return ("json", app.preview(body))
            if path == "/api/worker/start":
                return ("json", app.worker_start())
            if path == "/api/worker/stop":
                return ("json", app.worker_stop())
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "retry":
                return ("json", app.retry_job(int(parts[2])))
            if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "cancel":
                return ("json", app.cancel_job(int(parts[2])))
            return None

        parts = path.strip("/").split("/")
        if method == "PATCH" and len(parts) == 3 and parts[1] == "jobs":
            return ("json", app.update_job(int(parts[2]), body))
        if method == "DELETE" and len(parts) == 3:
            if parts[1] == "jobs":
                return ("json", app.delete_job(int(parts[2])))
            if parts[1] == "channels":
                return ("json", app.delete_channel(parts[2]))
        return None

    def _handle(self, method: str) -> None:
        try:
            result = self._dispatch(method)
        except AppError as exc:
            self._send_json({"error": str(exc)}, exc.status)
            return
        except (channels_mod.ConfigError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except Exception as exc:  # noqa: BLE001 - UI 가 이유를 볼 수 있어야 합니다
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if result is None:
            self.send_error(404, "not found")
            return
        kind, payload = result
        if kind == "static":
            self._send_static(payload)
        else:
            self._send_json(payload)

    def do_GET(self) -> None:      # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:    # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:   # noqa: N802
        self._handle("DELETE")


def serve(settings: dict) -> None:
    app = App(settings)
    handler = type("BoundHandler", (Handler,), {"app": app})
    host = settings["server"]["host"]
    port = settings["server"]["port"]
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"

    print(f"업로드 앱이 켜졌습니다 → {url}")
    print("Ctrl+C 로 종료합니다.")
    if settings["server"]["open_browser"]:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료 중...")
        app.worker.stop()
        httpd.shutdown()
