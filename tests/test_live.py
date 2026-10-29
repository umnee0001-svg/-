"""24시간 라이브 송출기 테스트.  실행: python3 tests/test_live.py

ffmpeg 가 있으면 실제 변환 → 반복 송출(로컬 파일로)까지 확인한다.
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import live_app  # noqa: E402
import live_core as lc  # noqa: E402
from probe import MediaInfo  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def wait_for(cond, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


class TestCommands(unittest.TestCase):
    def test_convert_video_with_audio(self):
        info = MediaInfo(duration=12.5, width=1080, height=1920, fps=30, has_audio=True, has_video=True)
        cmd = lc.build_convert_cmd("in.mp4", "out.mp4", info, "1080p")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease", graph)
        self.assertIn("loudnorm", graph)
        self.assertEqual(cmd[cmd.index("-g") + 1], "60")          # 2초 키프레임
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "4500k")
        self.assertEqual(cmd[cmd.index("-t") + 1], "12.500")
        self.assertNotIn("lavfi", cmd)

    def test_convert_video_without_audio_adds_silence(self):
        info = MediaInfo(duration=5, width=640, height=360, fps=25, has_audio=False, has_video=True)
        cmd = lc.build_convert_cmd("in.mp4", "out.mp4", info, "720p")
        self.assertIn("anullsrc=r=48000:cl=stereo", cmd)
        self.assertNotIn("loudnorm", cmd[cmd.index("-filter_complex") + 1])

    def test_convert_audio_only_uses_cover_or_black(self):
        info = MediaInfo(duration=200, width=0, height=0, fps=0, has_audio=True, has_video=False)
        black = lc.build_convert_cmd("a.mp3", "out.mp4", info, "vertical")
        self.assertIn("color=c=black:s=1080x1920:r=30", black)
        cover = lc.build_convert_cmd("a.mp3", "out.mp4", info, "1080p", cover="c.png")
        self.assertEqual(cover[cover.index("-loop") + 1], "1")
        self.assertIn("c.png", cover)

    def test_stream_loops_forever_without_reencode(self):
        cmd = lc.build_stream_cmd("x.mp4", "rtmp://host/app/KEY")
        self.assertEqual(cmd[cmd.index("-stream_loop") + 1], "-1")
        self.assertIn("-re", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertEqual(cmd[-1], "rtmp://host/app/KEY")

    def test_helpers(self):
        self.assertEqual(lc.ingest_url(lc.YOUTUBE_RTMP + "/", " abc "), lc.YOUTUBE_RTMP + "/abc")
        self.assertEqual(lc.mask("failed rtmp://x/abc-123", "abc-123"), "failed rtmp://x/****")
        self.assertEqual(lc.safe_stem('C:/a/b/나의:영상?.mp4'), "나의_영상_")
        self.assertEqual(lc.safe_stem("....mp4"), "video")


class TestManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = lc.Manager(self.tmp.name)
        self.m.ffmpeg_ok = True

    def tearDown(self):
        self.m.shutdown()
        self.tmp.cleanup()

    def _ready_file(self, name="a.mp4"):
        path = Path(self.tmp.name) / name
        path.write_bytes(b"x")
        rec = {"id": lc.new_id(), "name": name, "path": str(path), "status": "ready",
               "progress": 1.0, "error": "", "duration": 10.0, "resolution": "1080p",
               "added_at": time.time()}
        self.m.files[rec["id"]] = rec
        return rec

    def test_channel_validation(self):
        with self.assertRaises(ValueError):
            self.m.add_channel("", "key")
        with self.assertRaises(ValueError):
            self.m.add_channel("a", "key", "http://nope")
        self.m.add_channel("a", "abcd-efgh-1234")
        with self.assertRaises(ValueError):
            self.m.add_channel("b", "abcd-efgh-1234")
        snap = self.m.snapshot()
        self.assertEqual(snap["channels"][0]["key_hint"], "…1234")
        self.assertNotIn("abcd-efgh", json.dumps(snap))   # 화면에는 키 전체를 보내지 않는다

    def test_rejects_unsupported_upload(self):
        with self.assertRaises(ValueError):
            self.m.receive_upload("doc.pdf", io.BytesIO(b"x"), 1)

    def test_broadcast_rules(self):
        self.m.broadcast_records["busy"] = {"id": "busy", "channel_id": "c1", "file_id": "f", "mode": "keep"}
        ch = self.m.add_channel("a", "k")
        self.m.channels["c1"] = {"id": "c1", "name": "busy", "key": "k2", "server": lc.YOUTUBE_RTMP}
        f = self._ready_file()
        with self.assertRaises(ValueError):           # 같은 채널로 두 방송 불가
            self.m.start_broadcast("c1", f["id"], "keep")
        with self.assertRaises(ValueError):           # 방식 오류
            self.m.start_broadcast(ch["id"], f["id"], "weird")
        f["status"] = "converting"
        with self.assertRaises(ValueError):           # 변환 안 끝난 파일
            self.m.start_broadcast(ch["id"], f["id"], "keep")
        f["status"] = "ready"
        self.m.settings["max_live"] = 1
        with self.assertRaises(ValueError):           # 동시 송출 한도
            self.m.start_broadcast(ch["id"], f["id"], "keep")
        self.m.broadcast_records["busy"]["file_id"] = f["id"]
        with self.assertRaises(ValueError):           # 방송 중인 채널/파일은 삭제 불가
            self.m.delete_channel("c1")
        with self.assertRaises(ValueError):
            self.m.delete_file(f["id"])

    def test_state_survives_restart(self):
        self.m.add_channel("a", "k")
        f = self._ready_file()
        converting = dict(f, id="conv", status="converting")
        self.m.files["conv"] = converting
        self.m.update_settings({"resolution": "720p", "max_live": 5})
        again = lc.Manager(self.tmp.name)
        self.assertEqual(again.settings["resolution"], "720p")
        self.assertEqual(again.settings["max_live"], 5)
        self.assertEqual(len(again.channels), 1)
        self.assertEqual(again.files[f["id"]]["status"], "ready")
        self.assertEqual(again.files["conv"]["status"], "error")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg 없음")
class TestEndToEnd(unittest.TestCase):
    """실제 ffmpeg 로 변환 → 로컬 파일로 반복 송출 → 중지 → 재실행 시 이어 가기."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "짧은 영상.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc=s=640x360:r=25",
                        "-f", "lavfi", "-i", "sine=f=440",
                        "-t", "3", "-shortest", str(self.src)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_convert_and_loop_stream(self):
        m = lc.Manager(self.dir / "data")
        with open(self.src, "rb") as fh:
            rec = m.receive_upload(self.src.name, fh, self.src.stat().st_size)
        self.assertTrue(wait_for(lambda: rec["status"] != "converting"), "변환이 끝나지 않음")
        self.assertEqual(rec["status"], "ready", rec["error"])
        out = lc.probe(rec["path"])
        self.assertEqual((out.width, out.height), (1920, 1080))
        self.assertTrue(out.has_audio)
        self.assertEqual(list((self.dir / "data" / "uploads").iterdir()), [])  # 원본 임시파일 정리

        sink = self.dir / "sink"
        sink.mkdir()
        ch = m.add_channel("로컬", "out.flv", str(sink))
        b = m.start_broadcast(ch["id"], rec["id"], "keep")
        self.assertTrue(wait_for(lambda: m.live[b["id"]].status == "live", 15))
        time.sleep(4)   # 3초 영상이 최소 한 번은 반복되도록
        snap = m.snapshot()["broadcasts"][0]
        self.assertEqual(snap["reconnects"], 0, snap["last_error"])

        m.shutdown()    # 프로그램 종료: 방송 기록은 남는다
        streamed = lc.probe(str(sink / "out.flv")).duration
        self.assertGreater(streamed, 6.0)

        again = lc.Manager(self.dir / "data")
        self.assertIn(b["id"], again.broadcast_records)
        again.resume()
        self.assertTrue(wait_for(lambda: again.live[b["id"]].status in ("starting", "live"), 5))
        again.stop_broadcast(b["id"])
        self.assertEqual(again.broadcast_records, {})
        self.assertEqual(lc.Manager(self.dir / "data").broadcast_records, {})

    def test_broken_file_reports_error(self):
        bad = self.dir / "bad.mp4"
        bad.write_bytes(b"not a video")
        m = lc.Manager(self.dir / "data")
        with open(bad, "rb") as fh:
            rec = m.receive_upload("bad.mp4", fh, bad.stat().st_size)
        self.assertTrue(wait_for(lambda: rec["status"] != "converting"))
        self.assertEqual(rec["status"], "error")
        self.assertTrue(rec["error"])


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = lc.Manager(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), live_app.make_handler(self.m, 0))
        port = self.server.server_address[1]
        self.server.RequestHandlerClass = live_app.make_handler(self.m, port)
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def test_page_and_state(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("방송 시작".encode(), body)
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("files", json.loads(body))

    def test_writes_need_app_header(self):
        status, _ = self.request("POST", "/api/channels", {"name": "a", "key": "k"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/channels", {"name": "a", "key": "k"}, {"X-LiveLoop": "1"})
        self.assertEqual(status, 200)
        status, body = self.request("POST", "/api/channels", {"name": "b", "key": "k"}, {"X-LiveLoop": "1"})
        self.assertEqual(status, 400)
        self.assertIn("이미 등록된", json.loads(body)["error"])

    def test_foreign_host_is_rejected(self):
        status, _ = self.request("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
