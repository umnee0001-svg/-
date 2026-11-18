#!/usr/bin/env python3
"""롱폼 스튜디오: 편집 → 썸네일 → 업로드를 화면에서 하는 프로그램.

실행하면 내 컴퓨터 안에서만 도는 작은 웹서버가 켜지고, 브라우저에 편집 화면이 열린다.
(인터넷에 공개되지 않는다. 127.0.0.1 에만 열림)

    python src/studio.py            # 윈도우는 '스튜디오 실행.bat' 더블클릭
    python src/studio.py --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_review  # noqa: E402
import preset as preset_mod  # noqa: E402
import thumbnail as thumb_mod  # noqa: E402
import uploader  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UI_FILE = Path(__file__).resolve().parent / "studio" / "index.html"
FOLDERS = {
    "input": ROOT / "samples",
    "output": ROOT / "output",
    "bgm": ROOT / "assets" / "bgm",
    "fonts": ROOT / "assets" / "fonts",
    "watermark": ROOT / "assets" / "watermark",
}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}
SUB_EXTS = {".srt"}
FONT_EXTS = {".ttf", ".otf", ".ttc"}
ALLOWED_UPLOAD_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS | SUB_EXTS | FONT_EXTS
UPLOAD_TARGET = {  # 끌어다 놓은 파일을 확장자에 따라 어느 폴더에 둘지
    **{e: "input" for e in VIDEO_EXTS | SUB_EXTS | IMAGE_EXTS},
    **{e: "bgm" for e in AUDIO_EXTS},
    **{e: "fonts" for e in FONT_EXTS},
}


# ─────────────────────────── 작업(렌더링·업로드) 관리 ───────────────────────────

class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.status = "running"     # running | done | failed
        self.lines: list[str] = []
        self.progress = ""          # ffmpeg 진행 줄 (\r 로 갱신되는 줄)
        self.result: dict = {}
        self.started = time.time()
        self.proc: subprocess.Popen | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "log": self.lines[-300:], "progress": self.progress,
            "result": self.result, "elapsed": round(time.time() - self.started, 1),
        }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_job(job: Job, cmd: list[str], *, cwd: Path, on_line=None, on_done=None) -> None:
    """서브프로세스를 돌리면서 출력을 job 에 쌓는다 (별도 스레드에서 실행)."""
    def worker():
        try:
            job.proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            job.lines.append(f"실행 실패: {exc}")
            job.status = "failed"
            return
        buf = b""
        assert job.proc.stdout is not None
        while True:
            chunk = job.proc.stdout.read1(4096) if hasattr(job.proc.stdout, "read1") \
                else job.proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                raw, sep, buf = buf[:m.start()], buf[m.start():m.end()], buf[m.end():]
                text = raw.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                if sep == b"\r" or text.startswith(("frame=", "size=")):
                    job.progress = text
                else:
                    job.lines.append(text)
                    if on_line:
                        on_line(text)
        if buf.strip():
            job.lines.append(buf.decode("utf-8", errors="replace").rstrip())
        code = job.proc.wait()
        job.status = "done" if code == 0 else "failed"
        if code != 0:
            job.lines.append(f"(종료 코드 {code})")
        if on_done:
            on_done(code)

    threading.Thread(target=worker, daemon=True).start()


def new_job(kind: str) -> Job:
    job = Job(kind)
    with JOBS_LOCK:
        JOBS[job.id] = job
    return job


# ─────────────────────────── 파일 다루기 ───────────────────────────

def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "file"


def resolve_in(folder_key: str, name: str | None) -> Path | None:
    """폴더 안의 파일만 허용 (../ 로 빠져나가는 것 차단)."""
    if not name:
        return None
    folder = FOLDERS[folder_key].resolve()
    path = (folder / name).resolve()
    if folder not in path.parents or not path.is_file():
        raise ValueError(f"파일을 찾을 수 없습니다: {name}")
    return path


def list_folder(key: str, exts: set[str]) -> list[dict]:
    folder = FOLDERS[key]
    if not folder.exists():
        return []
    items = []
    for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() in exts:
            items.append({
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
                "url": f"/media/{key}/{p.name}",
            })
    return items


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def list_presets() -> list[dict]:
    out = []
    for p in sorted((ROOT / "presets").glob("*.json")):
        try:
            cfg = preset_mod.load(p)
        except Exception:  # noqa: BLE001 - 깨진 프리셋은 목록에서만 뺀다
            continue
        w, h = cfg["output"]["width"], cfg["output"]["height"]
        out.append({
            "file": p.name, "name": cfg["name"], "width": w, "height": h,
            "kind": "롱폼(가로)" if w > h else "쇼츠(세로)",
            "subtitle": {k: cfg["subtitle"][k] for k in ("mode", "language", "font_size")},
            "max_duration": cfg["output"]["max_duration"],
        })
    # 롱폼 프리셋을 맨 앞에
    out.sort(key=lambda x: (x["width"] < x["height"], x["file"] != "longform.json"))
    return out


# ─────────────────────────── 기능별 처리 ───────────────────────────

def start_render(data: dict) -> Job:
    preset_file = safe_name(data.get("preset") or "longform.json")
    preset_path = ROOT / "presets" / preset_file
    if not preset_path.is_file():
        raise ValueError(f"프리셋이 없습니다: {preset_file}")
    input_path = resolve_in("input", data.get("input"))
    if not input_path:
        raise ValueError("편집할 영상을 고르세요.")

    sets: list[str] = []

    def num(key: str) -> float | None:
        val = data.get(key)
        if val in (None, ""):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 값은 숫자여야 합니다: {val}") from None

    start, end = num("trim_start"), num("trim_end")
    if start is not None:
        sets.append(f"source.trim.start={start}")
    if end is not None:
        sets.append(f"source.trim.end={end}")
    if start is not None and end is not None and end <= start:
        raise ValueError("끝 시간이 시작 시간보다 커야 합니다.")
    if num("speed") is not None:
        sets.append(f"source.speed={num('speed')}")

    cmd = [sys.executable, str(ROOT / "src" / "make.py"), "--preset", str(preset_path),
           "--input", str(input_path), "--out", str(FOLDERS["output"])]

    mode = data.get("subtitle_mode") or "auto"
    if mode == "none":
        cmd.append("--no-subs")
    elif mode == "file":
        srt = resolve_in("input", data.get("subtitle_file"))
        if not srt:
            raise ValueError("자막 파일(.srt)을 고르세요.")
        sets.append("subtitle.mode=file")
        cmd += ["--subs", str(srt)]
    else:
        sets.append("subtitle.mode=auto")
    if data.get("language"):
        sets.append(f"subtitle.language={data['language']}")
    if num("font_size") is not None:
        sets.append(f"subtitle.font_size={int(num('font_size'))}")
    font = resolve_in("fonts", data.get("font_file"))
    if font:
        sets.append(f"subtitle.font_file={font}")

    bgm = resolve_in("bgm", data.get("bgm_file"))
    if bgm:
        sets += ["bgm.enabled=true", f"bgm.file={bgm}"]
        if num("bgm_volume") is not None:
            sets.append(f"bgm.volume_db={num('bgm_volume')}")
    wm = resolve_in("watermark", data.get("watermark_file"))
    if wm:
        sets += ["watermark.enabled=true", f"watermark.file={wm}"]

    if (data.get("hook") or "").strip():
        cmd += ["--hook", data["hook"].strip()]
    if (data.get("slug") or "").strip():
        cmd += ["--slug", data["slug"].strip()]
    for s in sets:
        cmd += ["--set", s]

    # 실행 전에 프리셋 값이 올바른지 먼저 확인 (잘못되면 바로 오류를 보여줌)
    preset_mod.apply_overrides(preset_mod.load(preset_path), sets)

    job = new_job("render")

    def on_line(text: str):
        m = re.match(r"\[4/4\] 렌더링 -> (.+)$", text)
        if m:
            job.result["output"] = Path(m.group(1).strip()).name

    def on_done(code: int):
        name = job.result.get("output")
        if code == 0 and name:
            job.result["url"] = f"/media/output/{name}"

    run_job(job, cmd, cwd=ROOT, on_line=on_line, on_done=on_done)
    return job


def make_thumb(data: dict) -> dict:
    src_key = data.get("source_folder") or "output"
    if src_key not in ("input", "output"):
        raise ValueError("잘못된 폴더입니다.")
    src = resolve_in(src_key, data.get("source"))
    if not src:
        raise ValueError("썸네일을 만들 영상이나 이미지를 고르세요.")
    style = thumb_mod.ThumbStyle(
        font_file=str(resolve_in("fonts", data.get("font_file"))) if data.get("font_file") else None,
        font_size=int(data.get("font_size") or 120),
        color=data.get("color") or "#FFFFFF",
        accent_color=data.get("accent_color") or "#FFE600",
        outline=int(data.get("outline") or 10),
        position=data.get("position") or "left",
        darken=float(data.get("darken") or 0),
        box=bool(data.get("box")),
    )
    out_name = f"{src.stem}_thumb.jpg" if src_key == "output" else f"{src.stem}_thumb_{int(time.time())}.jpg"
    out = FOLDERS["output"] / safe_name(out_name)
    thumb_mod.make_thumbnail(src, out, text=data.get("text") or "",
                             time=float(data.get("time") or 0), style=style)
    return {"name": out.name, "url": f"/media/output/{out.name}?t={int(time.time() * 1000)}"}


def start_upload(data: dict) -> Job:
    video = resolve_in("output", data.get("video"))
    if not video:
        raise ValueError("올릴 영상을 고르세요.")
    thumb = resolve_in("output", data.get("thumbnail")) if data.get("thumbnail") else None
    info = uploader.UploadInfo(
        video=str(video),
        title=(data.get("title") or "").strip(),
        description=data.get("description") or "",
        tags=uploader.parse_tags(data.get("tags") or ""),
        thumbnail=str(thumb) if thumb else None,
        privacy=data.get("privacy") or "private",
        publish_at=(data.get("publish_at") or "").strip() or None,
    )
    info.validate()
    files = uploader.write_package(info)
    cfg = uploader.load_config()
    cmd = uploader.build_command(cfg["upload"].get("command") or [], info, files)
    cwd = Path(cfg["upload"].get("working_dir") or ROOT)

    job = new_job("upload")
    job.lines.append("실행: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    job.result["meta"] = rel(Path(files["meta"]))
    run_job(job, cmd, cwd=cwd)
    return job


def ai_key(cfg: dict | None = None) -> str | None:
    cfg = cfg or uploader.load_config()
    return (cfg.get("ai") or {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or None


def run_ai_review(data: dict) -> dict:
    thumb = resolve_in("output", data.get("thumbnail")) if data.get("thumbnail") else None
    return ai_review.review(
        title=(data.get("title") or "").strip(),
        description=data.get("description") or "",
        tags=uploader.parse_tags(data.get("tags") or ""),
        thumbnail=str(thumb) if thumb else None,
        notes=data.get("notes") or "",
        api_key=ai_key(),
    )


def save_ai_config(data: dict) -> dict:
    cfg = uploader.load_config()
    ai = cfg.setdefault("ai", {})
    key = (data.get("api_key") or "").strip()
    if data.get("clear"):
        ai.pop("api_key", None)
    elif key:
        if not key.startswith("sk-"):
            raise ValueError("API 키는 보통 'sk-ant-' 로 시작합니다. 복사한 값을 다시 확인해 주세요.")
        ai["api_key"] = key
    if "notes" in data:
        ai["notes"] = (data.get("notes") or "").strip()
    uploader.save_config(cfg)
    return state()["ai"]


def state() -> dict:
    cfg = uploader.load_config()
    key = ai_key(cfg)
    return {
        "presets": list_presets(),
        "inputs": list_folder("input", VIDEO_EXTS),
        "input_images": list_folder("input", IMAGE_EXTS),
        "subtitles": list_folder("input", SUB_EXTS),
        "outputs": list_folder("output", VIDEO_EXTS),
        "thumbnails": list_folder("output", IMAGE_EXTS),
        "bgm": list_folder("bgm", AUDIO_EXTS),
        "fonts": list_folder("fonts", FONT_EXTS),
        "watermarks": list_folder("watermark", IMAGE_EXTS),
        "upload": {
            "command": cfg["upload"].get("command") or [],
            "working_dir": cfg["upload"].get("working_dir") or "",
            "configured": bool(cfg["upload"].get("command")),
            "placeholders": list(uploader.PLACEHOLDERS),
        },
        "ai": {
            "configured": bool(key),
            "key_hint": (key[:7] + "…" + key[-4:]) if key else "",
            "from_env": bool(key) and not (cfg.get("ai") or {}).get("api_key"),
            "notes": (cfg.get("ai") or {}).get("notes", ""),
            "model": ai_review.MODEL,
        },
        "defaults": cfg.get("defaults", {}),
        "folders": {k: rel(v) for k, v in FOLDERS.items()},
    }


def save_upload_config(data: dict) -> dict:
    command = data.get("command")
    if isinstance(command, str):
        command = [line.strip() for line in command.splitlines() if line.strip()]
    if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
        raise ValueError("명령은 한 줄에 하나씩 적어 주세요.")
    cfg = uploader.load_config()
    cfg["upload"]["command"] = command
    cfg["upload"]["working_dir"] = (data.get("working_dir") or "").strip() or None
    uploader.save_config(cfg)
    return state()["upload"]


# ─────────────────────────── HTTP ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "LongformStudio/1.0"

    def log_message(self, fmt, *args):  # 터미널을 조용하게
        pass

    # 응답 도우미
    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status=HTTPStatus.BAD_REQUEST):
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def same_origin(self) -> bool:
        """다른 웹사이트가 이 서버에 몰래 요청을 보내지 못하게 막는다."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlparse(origin).netloc == host

    # GET
    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        try:
            if path in ("/", "/index.html"):
                body = UI_FILE.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self.send_json(state())
            elif path.startswith("/api/jobs/"):
                job = JOBS.get(path.rsplit("/", 1)[-1])
                if not job:
                    self.send_error_json("작업을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(job.to_dict())
            elif path.startswith("/media/"):
                _, _, key, name = path.split("/", 3)
                if key not in FOLDERS:
                    raise ValueError("잘못된 폴더")
                self.send_file(resolve_in(key, name))
            else:
                self.send_error_json("없는 주소입니다.", HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError) as exc:
            self.send_error_json(str(exc), HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_file(self, path: Path):
        """동영상 앞뒤로 넘기기가 되도록 Range 요청을 지원한다."""
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        status = HTTPStatus.OK
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # POST
    def do_POST(self):
        if not self.same_origin():
            self.send_error_json("허용되지 않은 요청입니다.", HTTPStatus.FORBIDDEN)
            return
        url = urlparse(self.path)
        try:
            if url.path == "/api/files":
                self.send_json(self.receive_file(parse_qs(url.query)))
            elif url.path == "/api/render":
                self.send_json(start_render(self.read_json()).to_dict())
            elif url.path == "/api/thumbnail":
                self.send_json(make_thumb(self.read_json()))
            elif url.path == "/api/upload":
                self.send_json(start_upload(self.read_json()).to_dict())
            elif url.path == "/api/ai-review":
                self.send_json(run_ai_review(self.read_json()))
            elif url.path == "/api/ai-config":
                self.send_json(save_ai_config(self.read_json()))
            elif url.path == "/api/config":
                self.send_json(save_upload_config(self.read_json()))
            elif url.path.startswith("/api/jobs/") and url.path.endswith("/cancel"):
                job = JOBS.get(url.path.split("/")[3])
                if job and job.proc and job.status == "running":
                    job.proc.terminate()
                    job.lines.append("사용자가 취소했습니다.")
                self.send_json({"ok": True})
            else:
                self.send_error_json("없는 주소입니다.", HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError, RuntimeError,
                preset_mod.PresetError, uploader.UploadError, ai_review.ReviewError) as exc:
            self.send_error_json(str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def receive_file(self, query: dict) -> dict:
        """브라우저에 끌어다 놓은 파일을 받아서 알맞은 폴더에 저장한다 (용량 큰 영상도 조금씩 저장)."""
        name = safe_name(query.get("name", ["file"])[0])
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_UPLOAD_EXTS:
            raise ValueError(f"지원하지 않는 파일 형식입니다: {ext or name}")
        key = query.get("folder", [None])[0] or UPLOAD_TARGET[ext]
        if key not in FOLDERS:
            raise ValueError("잘못된 폴더")
        folder = FOLDERS[key]
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / name
        counter = 2
        while dest.exists():
            dest = folder / f"{Path(name).stem}-{counter}{ext}"
            counter += 1
        remaining = int(self.headers.get("Content-Length") or 0)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with tmp.open("wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("파일 전송이 중간에 끊겼습니다.")
                    fh.write(chunk)
                    remaining -= len(chunk)
            tmp.replace(dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"name": dest.name, "folder": key}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studio.py", description="롱폼 스튜디오 (편집·썸네일·업로드 화면)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = parser.parse_args(argv)

    for folder in FOLDERS.values():
        folder.mkdir(parents=True, exist_ok=True)

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("빈 포트를 찾지 못했습니다. --port 로 다른 번호를 지정하세요.", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"롱폼 스튜디오가 켜졌습니다: {url}")
    print("이 창을 닫으면 프로그램이 꺼집니다. (끄기: Ctrl+C)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
