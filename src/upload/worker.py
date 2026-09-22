"""큐를 돌면서 실제로 업로드하는 일꾼.

예약 공개 시각(publish_at)보다 lead_minutes 만큼 앞서 업로드를 시작합니다.
업로드 자체에 시간이 걸리고, 유튜브 처리에도 시간이 걸리기 때문입니다.

  python -m upload.worker --once    # 지금 올릴 게 있으면 한 번만 처리
  python -m upload.worker           # 계속 돌면서 감시
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from upload import channels as channels_mod      # noqa: E402
from upload import multilogin as ml_mod          # noqa: E402
from upload import queue as queue_mod            # noqa: E402
from upload import scheduler as scheduler_mod    # noqa: E402
from upload import settings as settings_mod      # noqa: E402
from upload import studio as studio_mod          # noqa: E402


class Worker:
    """UI 서버와 같은 프로세스에서 백그라운드 스레드로도, 단독으로도 돕니다."""

    def __init__(self, settings: dict, *, on_log=None):
        self.settings = settings
        self.on_log = on_log or (lambda message, level: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.current_job: int | None = None
        self.last_error = ""

    # ------------------------------------------------------------ 로그

    def _log(self, conn, job_id: int | None, message: str, level: str = "info") -> None:
        queue_mod.log(conn, job_id, message, level)
        prefix = {"info": "  ", "warn": "! ", "error": "X "}.get(level, "  ")
        print(f"{prefix}{message}", flush=True)
        self.on_log(message, level)

    # ------------------------------------------------------------ 한 건 처리

    def _upload_one(self, conn, job: dict, channel: dict) -> None:
        def log(message: str, level: str = "info") -> None:
            self._log(conn, job["id"], message, level)

        session = ml_mod.launch(channel, self.settings)
        try:
            from playwright.sync_api import sync_playwright  # 지연 import
        except ImportError as exc:
            session.close()
            raise RuntimeError(
                "playwright 가 설치돼 있지 않습니다.\n"
                "  pip install playwright   (브라우저는 MultiLogin 걸 쓰므로 "
                "playwright install 은 안 해도 됩니다)"
            ) from exc

        slow_mo = int(self.settings["studio"].get("slow_mo_ms", 0))
        with session:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(
                    session.cdp_url, slow_mo=slow_mo or None, timeout=60000
                )
                try:
                    context = browser.contexts[0] if browser.contexts \
                        else browser.new_context()
                    page = context.pages[0] if context.pages else context.new_page()
                    url = studio_mod.upload(page, job, channel, self.settings, log)
                    queue_mod.update_job(conn, job["id"], status="done",
                                         video_url=url, last_error="")
                finally:
                    browser.close()

    def _handle(self, conn, config: dict, job: dict) -> None:
        self.current_job = job["id"]
        attempts = job["attempts"] + 1
        queue_mod.update_job(conn, job["id"], status="uploading", attempts=attempts)
        try:
            channel = channels_mod.find(config, job["channel_id"])
            self._upload_one(conn, job, channel)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 큐는 살아 있어야 합니다
            reason = f"{type(exc).__name__}: {exc}"
            self.last_error = reason
            max_attempts = self.settings["worker"]["max_attempts"]
            if attempts < max_attempts:
                delay = self.settings["worker"]["retry_after_minutes"]
                queue_mod.update_job(conn, job["id"], status="scheduled",
                                     last_error=reason)
                self._log(conn, job["id"],
                          f"실패({attempts}/{max_attempts}). {delay}분 뒤 다시 "
                          f"시도합니다. 사유: {reason}", "warn")
            else:
                queue_mod.update_job(conn, job["id"], status="failed",
                                     last_error=reason)
                self._log(conn, job["id"],
                          f"{max_attempts}번 실패해서 포기합니다. 사유: {reason}",
                          "error")
        finally:
            self.current_job = None

    # ------------------------------------------------------------ 한 바퀴

    def tick(self, conn, config: dict) -> int:
        """지금 올려야 할 작업을 처리합니다. 처리한 개수를 돌려줍니다."""
        scheduler_mod.assign(conn, config)

        tz = ZoneInfo(config["timezone"])
        lead = timedelta(minutes=self.settings["worker"]["lead_minutes"])
        cutoff = (datetime.now(tz) + lead).isoformat(timespec="seconds")

        jobs = queue_mod.due_jobs(conn, before=cutoff)
        done = 0
        for job in jobs:
            if self._stop.is_set():
                break
            # 재시도 대기 중인 건 건너뜁니다.
            if job["attempts"] > 0 and job["last_error"]:
                waited = datetime.now(tz) - datetime.fromisoformat(
                    job["updated_at"]).astimezone(tz)
                if waited < timedelta(
                        minutes=self.settings["worker"]["retry_after_minutes"]):
                    continue
            self._handle(conn, config, job)
            done += 1
        return done

    # ------------------------------------------------------------ 루프

    def run_forever(self) -> None:
        self.running = True
        conn = queue_mod.connect(self.settings["db_file"])
        recovered = queue_mod.recover_stuck(conn)
        if recovered:
            self._log(conn, None,
                      f"중단됐던 작업 {recovered}건을 다시 대기열에 넣었습니다.", "warn")
        poll = self.settings["worker"]["poll_seconds"]
        try:
            while not self._stop.is_set():
                try:
                    config = channels_mod.load(self.settings["channels_file"])
                    self.tick(conn, config)
                except channels_mod.ConfigError as exc:
                    self._log(conn, None, f"채널 설정 오류: {exc}", "error")
                except Exception as exc:  # noqa: BLE001
                    self._log(conn, None,
                              f"일꾼 루프 오류: {type(exc).__name__}: {exc}", "error")
                self._stop.wait(poll)
        finally:
            self.running = False
            conn.close()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever,
                                        name="uploader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="유튜브 예약 업로드 일꾼")
    parser.add_argument("--settings", default="settings.json")
    parser.add_argument("--once", action="store_true",
                        help="한 바퀴만 돌고 끝냅니다")
    args = parser.parse_args(argv)

    try:
        settings = settings_mod.load(args.settings)
        config = channels_mod.load(settings["channels_file"])
    except (settings_mod.SettingsError, channels_mod.ConfigError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    worker = Worker(settings)
    if args.once:
        conn = queue_mod.connect(settings["db_file"])
        queue_mod.recover_stuck(conn)
        count = worker.tick(conn, config)
        print(f"{count}건 처리했습니다.")
        conn.close()
        return 0

    print("일꾼을 시작합니다. Ctrl+C 로 멈춥니다.")
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
        print("\n멈췄습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
