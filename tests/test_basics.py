"""표준 라이브러리만 쓰는 단위 테스트.  실행: python3 tests/test_basics.py"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import filters  # noqa: E402
import preset as preset_mod  # noqa: E402
import subtitles as sub  # noqa: E402


class TestPreset(unittest.TestCase):
    def test_all_shipped_presets_load(self):
        found = sorted((ROOT / "presets").glob("*.json"))
        self.assertTrue(found, "presets/ 에 프리셋이 없습니다")
        for path in found:
            with self.subTest(preset=path.name):
                cfg = preset_mod.load(path)
                self.assertEqual(cfg["output"]["width"], 1080)
                self.assertEqual(cfg["output"]["height"], 1920)

    def test_partial_preset_is_filled_with_defaults(self):
        cfg = preset_mod._deep_merge(preset_mod.DEFAULTS, {"subtitle": {"font_size": 99}})
        self.assertEqual(cfg["subtitle"]["font_size"], 99)
        self.assertEqual(cfg["subtitle"]["max_lines"],
                         preset_mod.DEFAULTS["subtitle"]["max_lines"])

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(preset_mod.PresetError):
            preset_mod._deep_merge(preset_mod.DEFAULTS, {"subtitle": {"fontsize": 99}})

    def test_override_parsing(self):
        cfg = preset_mod.apply_overrides(
            preset_mod.load(ROOT / "presets/default.json"),
            ["subtitle.font_size=90", "subtitle.bold=false", "source.speed=1.5",
             "source.trim.start=null"],
        )
        self.assertEqual(cfg["subtitle"]["font_size"], 90)
        self.assertIs(cfg["subtitle"]["bold"], False)
        self.assertEqual(cfg["source"]["speed"], 1.5)
        self.assertIsNone(cfg["source"]["trim"]["start"])

    def test_override_rejects_unknown_path(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        with self.assertRaises(preset_mod.PresetError):
            preset_mod.apply_overrides(cfg, ["subtitle.nope=1"])

    def test_validation_catches_odd_dimensions(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        cfg["output"]["width"] = 1081
        with self.assertRaises(preset_mod.PresetError):
            preset_mod.validate(cfg)


class TestSchema(unittest.TestCase):
    """presets/schema.json 이 DEFAULTS 와 어긋나지 않게 잡아둔다."""

    def _walk(self, defaults: dict, schema: dict, path: str = ""):
        props = schema.get("properties", {})
        for key, value in defaults.items():
            if key.startswith("_"):
                continue
            here = f"{path}.{key}" if path else key
            self.assertIn(key, props, f"schema.json 에 '{here}' 가 없습니다")
            if isinstance(value, dict):
                self._walk(value, props[key], here)
        for key in props:
            if key == "$schema":
                continue
            here = f"{path}.{key}" if path else key
            self.assertIn(key, defaults, f"schema.json 의 '{here}' 는 DEFAULTS 에 없습니다")

    def test_schema_matches_defaults(self):
        import json
        schema = json.loads((ROOT / "preset.schema.json").read_text(encoding="utf-8"))
        self._walk(preset_mod.DEFAULTS, schema)


class TestSubtitles(unittest.TestCase):
    def test_srt_parsing(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:02,500\n첫 줄\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n두 번째\n줄\n"
        )
        path = ROOT / "tests" / "_tmp.srt"
        path.write_text(srt, encoding="utf-8")
        try:
            cues = sub.parse_srt(path)
        finally:
            path.unlink()
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[0].end, 2.5)
        self.assertEqual(cues[1].text, "두 번째 줄")

    def test_wrap_never_drops_text(self):
        text = "이것은 아주 긴 한국어 문장이고 줄바꿈이 여러 번 필요합니다"
        wrapped = sub.wrap(text, max_chars=10, max_lines=2)
        flat = wrapped.replace(r"\N", " ")
        self.assertEqual(sorted(flat.split()), sorted(text.split()))

    def test_wrap_respects_max_chars(self):
        wrapped = sub.wrap("abcdefghij klmnop qrs", max_chars=10, max_lines=5)
        for line in wrapped.split(r"\N"):
            self.assertLessEqual(len(line), 10)

    def test_wrap_breaks_long_token_without_spaces(self):
        wrapped = sub.wrap("가" * 25, max_chars=10, max_lines=5)
        self.assertEqual(wrapped.count(r"\N"), 2)

    def test_group_respects_boundaries(self):
        cues = [sub.Cue(0.0, 1.0, "짧은 하나"), sub.Cue(1.1, 2.0, "짧은 둘")]
        merged = sub.group_cues(cues, max_chars=20, max_lines=2)
        kept = sub.group_cues(cues, max_chars=20, max_lines=2, respect_boundaries=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(kept), 2)

    def test_group_splits_overlong_cue(self):
        long_cue = [sub.Cue(0.0, 6.0, " ".join(["단어"] * 30))]
        out = sub.group_cues(long_cue, max_chars=10, max_lines=2, respect_boundaries=True)
        self.assertGreater(len(out), 1)
        self.assertTrue(all(c.end > c.start for c in out))

    def test_clip_cues_trims_to_duration(self):
        cues = [sub.Cue(0.0, 2.0, "a"), sub.Cue(4.0, 6.0, "b")]
        out = sub.clip_cues(cues, offset=1.0, duration=4.0)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0].start, 0.0)
        self.assertAlmostEqual(out[1].end, 4.0)

    def test_scale_cues(self):
        out = sub.scale_cues([sub.Cue(2.0, 4.0, "x")], 2.0)
        self.assertAlmostEqual(out[0].start, 1.0)
        self.assertAlmostEqual(out[0].end, 2.0)

    def test_ass_color_is_bgr(self):
        self.assertEqual(sub.ass_color("#FF0000"), "&H000000FF")
        self.assertEqual(sub.ass_color("#00FF00"), "&H0000FF00")
        self.assertEqual(sub.ass_color("#123456", 128), "&H80563412")

    def test_ass_time_format(self):
        self.assertEqual(sub._ass_time(0), "0:00:00.00")
        self.assertEqual(sub._ass_time(3661.5), "1:01:01.50")


class TestFilters(unittest.TestCase):
    def test_atempo_chain_splits_out_of_range(self):
        self.assertEqual(filters.atempo_chain(1.0), [])
        self.assertEqual(filters.atempo_chain(2.0), ["atempo=2.000000"])
        self.assertEqual(filters.atempo_chain(4.0), ["atempo=2.0", "atempo=2.000000"])
        self.assertEqual(len(filters.atempo_chain(0.25)), 2)

    def test_escaping(self):
        self.assertEqual(filters.esc("/a/b:c"), "/a/b\\:c")
        self.assertEqual(filters.esc("x,y[z]"), "x\\,y\\[z\\]")

    def test_video_chain_labels_are_connected(self):
        cfg = preset_mod.load(ROOT / "presets/landscape-to-shorts.json")
        cfg["hook"]["enabled"] = True
        cfg["hook"]["text"] = "훅"
        parts = filters.build_video_chain(
            cfg, ass_path="/tmp/x.ass", watermark_label="1:v", hook_textfile="/tmp/h.txt"
        )
        graph = ";".join(parts)
        self.assertTrue(graph.endswith("[vout]"))
        self.assertIn("gblur", graph)
        self.assertIn("subtitles=", graph)
        self.assertIn("drawtext=", graph)
        self.assertIn("overlay=", graph)

    def test_audio_chain_without_any_source(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        parts, label = filters.build_audio_chain(cfg, source_audio=False, bgm_label=None)
        self.assertEqual(parts, [])
        self.assertIsNone(label)

    def test_audio_chain_ducks_when_both_present(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        cfg["bgm"]["enabled"] = True
        parts, label = filters.build_audio_chain(cfg, source_audio=True, bgm_label="2:a")
        graph = ";".join(parts)
        self.assertEqual(label, "aout")
        self.assertIn("sidechaincompress", graph)
        self.assertIn("amix=inputs=2", graph)

    def test_bgm_fades_in_and_out(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        cfg["bgm"]["enabled"] = True
        cfg["bgm"]["fade_in"] = 1.0
        cfg["bgm"]["fade_out"] = 2.0
        parts, _ = filters.build_audio_chain(
            cfg, source_audio=False, bgm_label="1:a", duration=10.0
        )
        graph = ";".join(parts)
        self.assertIn("afade=t=in:st=0:d=1.0", graph)
        self.assertIn("afade=t=out:st=8.000:d=2.000", graph)

    def test_bgm_fade_out_clamped_to_short_clip(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        cfg["bgm"]["enabled"] = True
        cfg["bgm"]["fade_out"] = 5.0
        parts, _ = filters.build_audio_chain(
            cfg, source_audio=False, bgm_label="1:a", duration=3.0
        )
        self.assertIn("afade=t=out:st=0.000:d=3.000", ";".join(parts))

    def test_command_maps_audio_out_when_silent(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        cmd = filters.build_command(
            cfg, input_path="in.mp4", output_path="out.mp4", ass_path=None,
            hook_textfile=None, duration=10.0, start=0.0, has_source_audio=False,
        )
        self.assertIn("-an", cmd)
        self.assertIn("[vout]", cmd)

    def test_command_applies_trim_and_speed_to_input_length(self):
        cfg = preset_mod.load(ROOT / "presets/default.json")
        cfg["source"]["speed"] = 2.0
        cmd = filters.build_command(
            cfg, input_path="in.mp4", output_path="out.mp4", ass_path=None,
            hook_textfile=None, duration=5.0, start=3.0, has_source_audio=True,
        )
        self.assertEqual(cmd[cmd.index("-ss") + 1], "3.000")
        # 입력에서는 5s * 2.0 = 10s 를 읽어야 2배속 후 5s 가 된다.
        self.assertIn("10.000", cmd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
