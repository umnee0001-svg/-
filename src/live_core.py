"""24시간 라이브 송출 엔진: 보관함(자동 변환) · 채널(스트림 키) · 송출(ffmpeg 감시/재접속).

화면(live_app.py) 과 분리되어 있어서 이 모듈만으로도 동작한다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from probe import MediaInfo, probe

YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# 이름: (가로, 세로, 영상 비트레이트)  — YouTube 권장값 기준
RESOLUTIONS = {
    "1080p": (1920, 1080, 4500),
    "720p": (1280, 720, 2500),
    "vertical": (1080, 1920, 4500),
}
FPS = 30

MODES = ("keep", "restart12")
# YouTube 는 12시간이 넘는 방송의 다시보기를 남기지 않으므로 그보다 조금 일찍 끊는다.
RESTART_EVERY = 12 * 3600 - 5 * 60
# 끊은 뒤 YouTube 가 이전 방송을 종료 처리할 시간을 준다.
RESTART_GAP = 120

DEFAULT_SETTINGS = {"resolution": "1080p", "max_live": 3, "cover": None}

def new_id() -> str:
    return uuid.uuid4().hex[:10]


def safe_stem(filename: str, limit: int = 80) -> str:
    stem = Path(filename).stem
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" .")
    return stem[:limit] or "video"


def ingest_url(server: str, key: str) -> str:
    return server.rstrip("/") + "/" + key.strip()


def mask(text: str, secret: str) -> str:
    """ffmpeg 오류에 섞여 나오는 스트림 키를 가린다."""
    return text.replace(secret, "****") if secret else text


def build_convert_cmd(src: str, dest: str, info: MediaInfo, resolution: str,
                      cover: str | None = None) -> list[str]:
    """어떤 소스든 송출용 규격(H.264 CBR, 2초 키프레임, AAC, -14 LUFS)으로 맞춘다.

    규격을 미리 통일해 두면 송출할 때는 재인코딩 없이(-c copy) 내보낼 수 있어
    PC 부담이 거의 없다.
    """
    width, height, kbps = RESOLUTIONS[resolution]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-nostats", "-progress", "pipe:1"]

    if info.has_video:
        cmd += ["-i", src]
        video_in = "0:v:0"
        if info.has_audio:
            audio_in = "0:a:0"
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_in = "1:a:0"
    else:
        # 음원 파일: 커버 이미지(없으면 검은 화면)를 영상으로 깐다.
        if cover:
            cmd += ["-loop", "1", "-framerate", str(FPS), "-i", cover]
        else:
            cmd += ["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={FPS}"]
        cmd += ["-i", src]
        video_in = "0:v:0"
        audio_in = "1:a:0"

    video = (f"[{video_in}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
             f"fps={FPS},format=yuv420p[v]")
    loudnorm = "loudnorm=I=-14:TP=-1.5:LRA=11," if info.has_audio else ""
    audio = (f"[{audio_in}]{loudnorm}aresample=48000,"
             f"aformat=sample_fmts=fltp:channel_layouts=stereo[a]")

    cmd += [
        "-filter_complex", f"{video};{audio}",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
        "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-shortest",
    ]
    if info.duration > 0:
        cmd += ["-t", f"{info.duration:.3f}"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", dest]
    return cmd


def build_stream_cmd(path: str, url: str) -> list[str]:
    """변환된 파일 하나를 실시간 속도로 끝없이 반복해서 내보낸다."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-re", "-stream_loop", "-1", "-i", path,
        "-c", "copy", "-f", "flv", "-flvflags", "no_duration_filesize", url,
    ]


def _popen(cmd: list[str], **kwargs) -> subprocess.Popen:
    # 일부러 CREATE_NO_WINDOW 를 쓰지 않는다: ffmpeg 가 프로그램의 콘솔 창에 묶여 있어야
    # 사용자가 창을 닫을 때 같이 종료된다. (떠돌이 ffmpeg 가 남으면 재실행 시 이중 송출)
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", **kwargs)


def _drain(stream, sink: deque) -> None:
    with stream:
        for line in stream:
            line = line.strip()
            if line:
                sink.append(line)


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Broadcast:
    """방송 하나 = ffmpeg 하나 + 그것을 지켜보다 끊기면 다시 붙이는 스레드."""

    def __init__(self, manager: "Manager", record: dict):
        self.manager = manager
        self.record = record
        self.status = "starting"
        self.started_at = time.time()
        self.segment_started_at = self.started_at
        self.last_error = ""
        self.reconnects = 0
        self.next_retry_at: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def id(self) -> str:
        return self.record["id"]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(10)

    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            channel = self.manager.channels.get(self.record["channel_id"])
            file = self.manager.files.get(self.record["file_id"])
            if not channel or not file or file["status"] != "ready":
                self.status = "stopped"
                self.last_error = "채널이나 파일이 없어져 방송을 멈췄습니다."
                return

            key = channel["key"]
            cmd = build_stream_cmd(file["path"], ingest_url(channel["server"], key))
            errors: deque = deque(maxlen=20)
            self.status = "starting"
            self.next_retry_at = None
            self.segment_started_at = time.time()
            try:
                proc = _popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError as exc:
                self.status = "stopped"
                self.last_error = f"ffmpeg 실행 실패: {exc}"
                return
            reader = threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True)
            reader.start()

            deadline = None
            if self.record["mode"] == "restart12":
                deadline = self.segment_started_at + RESTART_EVERY
            planned = False
            while proc.poll() is None:
                if self._stop.wait(1):
                    break
                if self.status == "starting" and time.time() - self.segment_started_at > 5:
                    self.status = "live"
                if deadline and time.time() >= deadline:
                    planned = True
                    break
            _stop_proc(proc)
            reader.join(2)

            if self._stop.is_set():
                break
            if planned:
                self.status = "restarting"
                self.next_retry_at = time.time() + RESTART_GAP
                self.manager.log(f"[{channel['name']}] 12시간 재시작: {RESTART_GAP}초 쉬고 새 방송으로 다시 송출")
                if self._stop.wait(RESTART_GAP):
                    break
                continue

            # 예상하지 못한 종료 → 잠깐 쉬고 다시 붙는다 (5초, 10초, 20초 … 최대 60초)
            if time.time() - self.segment_started_at > 60:
                backoff = 5
            self.reconnects += 1
            self.last_error = mask(" / ".join(list(errors)[-3:]) or "연결이 끊겼습니다.", key)
            self.status = "reconnecting"
            self.next_retry_at = time.time() + backoff
            self.manager.log(f"[{channel['name']}] 송출 끊김 → {backoff}초 뒤 재접속: {self.last_error}")
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60)
        self.status = "stopped"


class Manager:
    """보관함 · 채널 · 방송 상태를 들고 data_dir/state.json 에 저장한다."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.library_dir = self.data_dir / "library"
        self.upload_dir = self.data_dir / "uploads"
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"

        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.files: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self.broadcast_records: dict[str, dict] = {}
        self.live: dict[str, Broadcast] = {}
        self.events: deque = deque(maxlen=50)

        self._lock = threading.RLock()
        self._convert_slot = threading.Semaphore(1)   # 변환은 한 번에 하나씩
        self.ffmpeg_ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        self._load()

    # ---- 저장/불러오기 -------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.settings.update(data.get("settings", {}))
        self.channels = {c["id"]: c for c in data.get("channels", [])}
        for f in data.get("files", []):
            if f["status"] == "converting":   # 변환 도중 꺼졌던 파일
                f["status"], f["error"] = "error", "변환 도중 프로그램이 종료됐습니다. 다시 올려 주세요."
            if f["status"] == "ready" and not Path(f["path"]).is_file():
                f["status"], f["error"] = "error", "변환된 파일이 사라졌습니다."
            self.files[f["id"]] = f
        self.broadcast_records = {b["id"]: b for b in data.get("broadcasts", [])}

    def save(self) -> None:
        with self._lock:
            data = {
                "settings": self.settings,
                "channels": list(self.channels.values()),
                "files": list(self.files.values()),
                "broadcasts": list(self.broadcast_records.values()),
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)

    def log(self, message: str) -> None:
        self.events.append({"t": time.time(), "msg": message})
        print(time.strftime("%H:%M:%S"), message, flush=True)

    def resume(self) -> None:
        """프로그램을 껐다 켜면, 끄기 전에 송출 중이던 방송을 이어서 다시 시작한다."""
        for record in list(self.broadcast_records.values()):
            ok = (record["channel_id"] in self.channels
                  and self.files.get(record["file_id"], {}).get("status") == "ready")
            if not ok:
                del self.broadcast_records[record["id"]]
                continue
            self.log(f"이전 방송 이어서 시작: {self.channels[record['channel_id']]['name']}")
            self._launch(record)
        self.save()

    def shutdown(self) -> None:
        """프로그램 종료: ffmpeg 는 멈추되 방송 목록은 남겨서 다음 실행 때 이어 간다."""
        for bc in list(self.live.values()):
            bc.stop()

    # ---- 설정 ---------------------------------------------------------------

    def update_settings(self, values: dict) -> None:
        with self._lock:
            if "resolution" in values:
                if values["resolution"] not in RESOLUTIONS:
                    raise ValueError("알 수 없는 해상도입니다.")
                self.settings["resolution"] = values["resolution"]
            if "max_live" in values:
                n = int(values["max_live"])
                if not 1 <= n <= 10:
                    raise ValueError("동시 송출 수는 1~10 사이로 정해 주세요.")
                self.settings["max_live"] = n
            self.save()

    def set_cover(self, filename: str, stream, length: int) -> None:
        ext = Path(filename).suffix.lower()
        if ext not in IMAGE_EXT:
            raise ValueError("커버는 png · jpg · webp 이미지만 됩니다.")
        dest = self.data_dir / f"cover{ext}"
        _copy_stream(stream, dest, length)
        with self._lock:
            old = self.settings.get("cover")
            if old and old != str(dest):
                Path(old).unlink(missing_ok=True)
            self.settings["cover"] = str(dest)
            self.save()

    # ---- 보관함 -------------------------------------------------------------

    def receive_upload(self, filename: str, stream, length: int) -> dict:
        ext = Path(filename).suffix.lower()
        if ext not in VIDEO_EXT | AUDIO_EXT:
            raise ValueError(f"지원하지 않는 형식입니다: {ext or filename}")
        file_id = new_id()
        upload_path = self.upload_dir / f"{file_id}{ext}"
        _copy_stream(stream, upload_path, length)

        stem = safe_stem(filename)
        dest = self.library_dir / f"live_{stem}.mp4"
        n = 2
        while dest.exists() or any(f.get("path") == str(dest) for f in self.files.values()):
            dest = self.library_dir / f"live_{stem}-{n}.mp4"
            n += 1

        record = {
            "id": file_id, "name": filename, "path": str(dest), "status": "converting",
            "progress": 0.0, "error": "", "duration": 0.0,
            "resolution": self.settings["resolution"], "added_at": time.time(),
        }
        with self._lock:
            self.files[file_id] = record
            self.save()
        threading.Thread(target=self._convert, args=(record, upload_path), daemon=True).start()
        return record

    def _convert(self, record: dict, upload_path: Path) -> None:
        with self._convert_slot:
            tmp = Path(record["path"] + ".part")
            try:
                info = probe(str(upload_path))
                if not info.has_audio and not info.has_video:
                    raise RuntimeError("영상도 소리도 없는 파일입니다.")
                record["duration"] = info.duration
                cover = self.settings.get("cover")
                cover = cover if cover and Path(cover).is_file() else None
                cmd = build_convert_cmd(str(upload_path), str(tmp), info,
                                        record["resolution"], cover)
                self.log(f"변환 시작: {record['name']}")
                proc = _popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                errors: deque = deque(maxlen=20)
                reader = threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True)
                reader.start()
                with proc.stdout:
                    for line in proc.stdout:
                        key, _, value = line.strip().partition("=")
                        if key == "out_time_us" and value.isdigit() and info.duration > 0:
                            record["progress"] = min(0.99, int(value) / 1e6 / info.duration)
                proc.wait()
                reader.join(2)
                if proc.returncode != 0:
                    raise RuntimeError(" / ".join(list(errors)[-3:]) or "ffmpeg 변환 실패")
                os.replace(tmp, record["path"])
                record.update(status="ready", progress=1.0, error="")
                self.log(f"변환 완료: {record['name']}")
            except Exception as exc:  # noqa: BLE001 — 어떤 실패든 화면에 보여준다
                tmp.unlink(missing_ok=True)
                record.update(status="error", error=str(exc))
                self.log(f"변환 실패: {record['name']}: {exc}")
            finally:
                upload_path.unlink(missing_ok=True)
                self.save()

    def delete_file(self, file_id: str) -> None:
        with self._lock:
            record = self.files.get(file_id)
            if not record:
                raise KeyError("파일이 없습니다.")
            if record["status"] == "converting":
                raise ValueError("변환 중인 파일은 지울 수 없습니다.")
            if any(b["file_id"] == file_id for b in self.broadcast_records.values()):
                raise ValueError("방송 중인 파일입니다. 먼저 방송을 중지해 주세요.")
            Path(record["path"]).unlink(missing_ok=True)
            del self.files[file_id]
            self.save()

    # ---- 채널 ---------------------------------------------------------------

    def add_channel(self, name: str, key: str, server: str = YOUTUBE_RTMP) -> dict:
        name, key, server = name.strip(), key.strip(), (server or YOUTUBE_RTMP).strip()
        if not name or not key:
            raise ValueError("채널 이름과 스트림 키를 모두 적어 주세요.")
        if not re.match(r"^rtmps?://", server) and not Path(server).is_dir():
            raise ValueError("서버 주소는 rtmp:// 또는 rtmps:// 로 시작해야 합니다.")
        with self._lock:
            if any(c["key"] == key for c in self.channels.values()):
                raise ValueError("이미 등록된 스트림 키입니다.")
            record = {"id": new_id(), "name": name, "key": key, "server": server}
            self.channels[record["id"]] = record
            self.save()
            return record

    def delete_channel(self, channel_id: str) -> None:
        with self._lock:
            if channel_id not in self.channels:
                raise KeyError("채널이 없습니다.")
            if any(b["channel_id"] == channel_id for b in self.broadcast_records.values()):
                raise ValueError("방송 중인 채널입니다. 먼저 방송을 중지해 주세요.")
            del self.channels[channel_id]
            self.save()

    # ---- 방송 ---------------------------------------------------------------

    def start_broadcast(self, channel_id: str, file_id: str, mode: str) -> dict:
        with self._lock:
            if not self.ffmpeg_ok:
                raise ValueError("ffmpeg 를 찾을 수 없습니다. 설치 후 프로그램을 다시 켜 주세요.")
            channel = self.channels.get(channel_id)
            file = self.files.get(file_id)
            if not channel:
                raise ValueError("채널을 골라 주세요.")
            if not file:
                raise ValueError("방송할 파일을 보관함에서 골라 주세요.")
            if file["status"] != "ready":
                raise ValueError("아직 변환이 끝나지 않은 파일입니다.")
            if mode not in MODES:
                raise ValueError("방식을 골라 주세요.")
            if any(b["channel_id"] == channel_id for b in self.broadcast_records.values()):
                raise ValueError(f"'{channel['name']}' 채널은 이미 방송 중입니다. "
                                 "스트림 키 하나에는 방송 하나만 보낼 수 있어요.")
            if len(self.broadcast_records) >= self.settings["max_live"]:
                raise ValueError(f"동시 송출은 최대 {self.settings['max_live']}개입니다.")
            record = {"id": new_id(), "channel_id": channel_id, "file_id": file_id, "mode": mode}
            self.broadcast_records[record["id"]] = record
            self.save()
            self.log(f"방송 시작: {channel['name']} ← {file['name']}")
            self._launch(record)
            return record

    def _launch(self, record: dict) -> None:
        bc = Broadcast(self, record)
        self.live[record["id"]] = bc
        bc.start()

    def stop_broadcast(self, broadcast_id: str) -> None:
        with self._lock:
            record = self.broadcast_records.pop(broadcast_id, None)
            bc = self.live.pop(broadcast_id, None)
            if record is None:
                raise KeyError("방송이 없습니다.")
            self.save()
        if bc:
            bc.stop()
        self.log(f"방송 중지: {self.channels.get(record['channel_id'], {}).get('name', '?')}")

    # ---- 화면용 스냅샷 -------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            broadcasts = []
            for record in self.broadcast_records.values():
                bc = self.live.get(record["id"])
                channel = self.channels.get(record["channel_id"], {})
                file = self.files.get(record["file_id"], {})
                broadcasts.append({
                    "id": record["id"],
                    "channel_id": record["channel_id"],
                    "channel": channel.get("name", "?"),
                    "file_id": record["file_id"],
                    "file": file.get("name", "?"),
                    "mode": record["mode"],
                    "status": bc.status if bc else "stopped",
                    "uptime": now - bc.started_at if bc else 0,
                    "segment_uptime": now - bc.segment_started_at if bc else 0,
                    "reconnects": bc.reconnects if bc else 0,
                    "last_error": bc.last_error if bc else "",
                    "retry_in": max(0, bc.next_retry_at - now) if bc and bc.next_retry_at else None,
                })
            return {
                "ffmpeg_ok": self.ffmpeg_ok,
                "data_dir": str(self.data_dir),
                "settings": {**self.settings, "cover": bool(self.settings.get("cover"))},
                "resolutions": list(RESOLUTIONS),
                "files": sorted(self.files.values(), key=lambda f: f["added_at"], reverse=True),
                "channels": [
                    {"id": c["id"], "name": c["name"], "server": c["server"],
                     "key_hint": "…" + c["key"][-4:]}
                    for c in self.channels.values()
                ],
                "broadcasts": broadcasts,
                "events": list(self.events)[-10:],
            }


def _copy_stream(stream, dest: Path, length: int, chunk: int = 1 << 20) -> None:
    remaining = length
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(tmp, "wb") as out:
            while remaining > 0:
                data = stream.read(min(chunk, remaining))
                if not data:
                    raise IOError("업로드가 중간에 끊겼습니다.")
                out.write(data)
                remaining -= len(data)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
