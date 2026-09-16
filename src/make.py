#!/usr/bin/env python3
"""프리셋 기반 쇼츠 자동 생성기.

사용 예:
    python src/make.py --preset presets/default.json --input my.mp4
    python src/make.py --preset presets/blur-pad.json --input "https://..." --hook "이거 모르면 손해"
    python src/make.py --preset presets/default.json --input my.mp4 --set subtitle.font_size=90
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import filters  # noqa: E402
import preset as preset_mod  # noqa: E402
import source as source_mod  # noqa: E402
import subtitles as sub_mod  # noqa: E402
from probe import probe, require_tool  # noqa: E402


def slugify(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^\w가-힣-]+", "-", text.strip(), flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text[:limit] or "clip")


def build_output_path(preset: dict, out_dir: Path, slug: str) -> Path:
    now = datetime.now()
    name = preset["output"]["filename_template"].format(
        date=now.strftime("%Y%m%d"),
        time=now.strftime("%H%M%S"),
        preset=slugify(preset["name"], 24),
        slug=slug,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    if path.exists():
        stem, suffix = path.stem, path.suffix
        counter = 2
        while path.exists():
            path = out_dir / f"{stem}-{counter}{suffix}"
            counter += 1
    return path


def extract_audio(input_path: Path, dest: Path, *, start: float,
                  duration: float | None, speed: float) -> Path:
    """음성인식용 16kHz 모노 wav 를 최종 타임라인 기준으로 뽑는다."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration * speed:.3f}"]
    cmd += ["-i", str(input_path), "-vn"]
    chain = filters.atempo_chain(speed)
    if chain:
        cmd += ["-af", ",".join(chain)]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"오디오 추출 실패:\n{proc.stderr.strip()}")
    return dest


def prepare_cues(cfg: dict, args, input_path: Path, tmp: Path, *,
                 start: float, duration: float | None, speed: float,
                 has_audio: bool) -> list[sub_mod.Cue]:
    sub = cfg["subtitle"]
    mode = sub["mode"]

    respect_boundaries = False
    if mode == "file":
        if not args.subs:
            raise SystemExit("subtitle.mode 가 'file' 입니다. --subs 로 SRT 파일을 지정하세요.")
        cues = sub_mod.parse_srt(args.subs)
        cues = sub_mod.clip_cues(cues, offset=start)
        cues = sub_mod.scale_cues(cues, speed)
        respect_boundaries = True   # 직접 만든 SRT 의 줄 나눔을 존중
    else:
        if not has_audio:
            print("  ! 소스에 오디오가 없어 자동 자막을 건너뜁니다.", file=sys.stderr)
            return []
        print("  · 음성 인식 중 (처음 실행 시 모델 다운로드로 시간이 걸립니다)...")
        wav = extract_audio(input_path, tmp / "speech.wav",
                            start=start, duration=duration, speed=speed)
        cues = sub_mod.transcribe(
            str(wav), model=sub["whisper_model"], language=sub["language"] or None
        )

    cues = sub_mod.group_cues(
        cues, max_chars=sub["max_chars_per_line"], max_lines=sub["max_lines"],
        respect_boundaries=respect_boundaries,
    )
    return sub_mod.clip_cues(cues, duration=duration)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make.py",
        description="프리셋 기반 세로 쇼츠 자동 생성기",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--preset", required=True, help="프리셋 JSON 경로")
    parser.add_argument("--input", required=True, help="영상 파일 경로 또는 URL")
    parser.add_argument("--out", default="output", help="출력 폴더 (기본: output)")
    parser.add_argument("--subs", help="SRT 자막 파일 (subtitle.mode=file 일 때)")
    parser.add_argument("--hook", help="첫 화면에 띄울 훅 문구 (프리셋 hook 을 켜고 덮어씀)")
    parser.add_argument("--slug", help="출력 파일명에 쓸 이름 (기본: 입력 파일명)")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="경로=값", help="프리셋 항목 즉석 변경 (예: subtitle.font_size=90)")
    parser.add_argument("--no-subs", action="store_true", help="이번 실행만 자막 끄기")
    parser.add_argument("--dry-run", action="store_true", help="ffmpeg 명령만 출력하고 끝")
    args = parser.parse_args(argv)

    require_tool("ffmpeg")
    require_tool("ffprobe")

    cfg = preset_mod.load(args.preset)
    if args.overrides:
        cfg = preset_mod.apply_overrides(cfg, args.overrides)
    if args.no_subs:
        cfg["subtitle"]["enabled"] = False
    if args.hook:
        cfg["hook"]["enabled"] = True
        cfg["hook"]["text"] = args.hook
        preset_mod.validate(cfg)

    with tempfile.TemporaryDirectory(prefix="shorts-") as tmpdir:
        tmp = Path(tmpdir)

        print(f"[1/4] 소스 준비: {args.input}")
        input_path = source_mod.resolve(args.input, tmp / "download")
        info = probe(str(input_path))
        if not info.has_video:
            raise SystemExit(f"영상 스트림이 없습니다: {input_path}")
        print(f"      {info.width}x{info.height} / {info.duration:.1f}s / "
              f"{info.fps:.2f}fps / audio={'있음' if info.has_audio else '없음'}")

        src = cfg["source"]
        speed = src["speed"]
        start = float(src["trim"]["start"] or 0.0)
        end = src["trim"]["end"]
        segment = (float(end) if end is not None else info.duration) - start
        if segment <= 0:
            raise SystemExit("트림 구간이 비어 있습니다. source.trim 값을 확인하세요.")
        duration = segment / speed
        max_duration = cfg["output"]["max_duration"]
        if max_duration:
            duration = min(duration, float(max_duration))
        print(f"[2/4] 타임라인: {start:.1f}s 부터 {duration:.1f}s "
              f"(속도 {speed}x, {cfg['output']['width']}x{cfg['output']['height']})")

        ass_path = None
        if cfg["subtitle"]["enabled"] and cfg["subtitle"]["mode"] != "none":
            print("[3/4] 자막 생성")
            cues = prepare_cues(cfg, args, input_path, tmp, start=start,
                                duration=duration, speed=speed, has_audio=info.has_audio)
            if cues:
                ass_path = str(sub_mod.write_ass(
                    cues, tmp / "subs.ass", style=cfg["subtitle"],
                    width=cfg["output"]["width"], height=cfg["output"]["height"],
                ))
                print(f"      자막 {len(cues)}개 생성")
        else:
            print("[3/4] 자막 없음")

        hook_textfile = None
        if cfg["hook"]["enabled"] and cfg["hook"]["text"]:
            hook_textfile = str(tmp / "hook.txt")
            Path(hook_textfile).write_text(cfg["hook"]["text"], encoding="utf-8")

        if args.slug:
            slug = slugify(args.slug)
        elif source_mod.is_url(args.input):
            slug = slugify(input_path.stem if input_path.stem != "source" else "web")
        else:
            slug = slugify(Path(args.input).stem)
        out_path = build_output_path(cfg, Path(args.out), slug)

        cmd = filters.build_command(
            cfg,
            input_path=str(input_path),
            output_path=str(out_path),
            ass_path=ass_path,
            hook_textfile=hook_textfile,
            duration=duration,
            start=start,
            has_source_audio=info.has_audio,
        )

        if args.dry_run:
            print("\n" + " ".join(_quote(part) for part in cmd))
            return 0

        print(f"[4/4] 렌더링 -> {out_path}")
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print("\nffmpeg 명령:\n" + " ".join(_quote(part) for part in cmd), file=sys.stderr)
            raise SystemExit("렌더링 실패")

    result = probe(str(out_path))
    print(f"\n완료: {out_path}  ({result.width}x{result.height}, {result.duration:.1f}s, "
          f"{out_path.stat().st_size / 1024 / 1024:.1f}MB)")
    return 0


def _quote(part: str) -> str:
    return f"'{part}'" if re.search(r"[\s;\[\]']", part) else part


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (preset_mod.PresetError, sub_mod.SubtitleError,
            FileNotFoundError, RuntimeError) as exc:
        print(f"\n오류: {exc}", file=sys.stderr)
        sys.exit(1)
