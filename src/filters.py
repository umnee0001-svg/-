"""프리셋 -> ffmpeg 커맨드 조립."""
from __future__ import annotations

from pathlib import Path

import fonts


def esc(value: str) -> str:
    """필터그래프 인자 안에 넣을 문자열(주로 경로) 이스케이프."""
    out = str(value)
    for ch in ("\\", "'", ":", ",", "[", "]", ";"):
        out = out.replace(ch, "\\" + ch)
    return out


def atempo_chain(speed: float) -> list[str]:
    """atempo 는 0.5~2.0 만 지원하므로 필요하면 여러 번 건다."""
    if speed == 1.0:
        return []
    chain: list[str] = []
    remaining = speed
    while remaining > 2.0:
        chain.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        chain.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-6:
        chain.append(f"atempo={remaining:.6f}")
    return chain


def _crop_xy(anchor: str) -> tuple[str, str]:
    return {
        "center": ("(in_w-out_w)/2", "(in_h-out_h)/2"),
        "top": ("(in_w-out_w)/2", "0"),
        "bottom": ("(in_w-out_w)/2", "in_h-out_h"),
        "left": ("0", "(in_h-out_h)/2"),
        "right": ("in_w-out_w", "(in_h-out_h)/2"),
    }[anchor]


def _watermark_xy(position: str, margin: int) -> str:
    return {
        "top-left": f"{margin}:{margin}",
        "top-right": f"W-w-{margin}:{margin}",
        "bottom-left": f"{margin}:H-h-{margin}",
        "bottom-right": f"W-w-{margin}:H-h-{margin}",
    }[position]


def build_video_chain(preset: dict, *, ass_path: str | None,
                      watermark_label: str | None,
                      hook_textfile: str | None) -> list[str]:
    """[0:v] 부터 [vout] 까지의 필터 체인 조각들을 만든다."""
    out = preset["output"]
    src = preset["source"]
    width, height = out["width"], out["height"]
    parts: list[str] = []

    pre = []
    if src["speed"] != 1.0:
        pre.append(f"setpts=PTS/{src['speed']}")
    pre.append(f"fps={out['fps']}")
    pre_str = ",".join(pre)

    fit = src["fit"]
    if fit == "crop":
        x, y = _crop_xy(src["crop_anchor"])
        parts.append(
            f"[0:v]{pre_str},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:{x}:{y},setsar=1[base]"
        )
    elif fit == "pad":
        color = src["pad_color"].replace("#", "0x")
        parts.append(
            f"[0:v]{pre_str},scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={color},setsar=1[base]"
        )
    else:  # blur_pad
        sigma = max(1, min(int(src["blur_strength"]), 128))
        parts.append(f"[0:v]{pre_str},setsar=1,split=2[bgsrc][fgsrc]")
        parts.append(
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma={sigma}[bgblur]"
        )
        parts.append(
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fgscaled]"
        )
        parts.append(f"[bgblur][fgscaled]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")

    label = "base"

    if watermark_label:
        wm = preset["watermark"]
        target_w = max(2, int(width * wm["scale"]))
        parts.append(
            f"[{watermark_label}]scale={target_w}:-1,format=rgba,"
            f"colorchannelmixer=aa={wm['opacity']}[wmark]"
        )
        xy = _watermark_xy(wm["position"], wm["margin"])
        parts.append(f"[{label}][wmark]overlay={xy}:format=auto[wmarked]")
        label = "wmarked"

    if ass_path:
        sub = preset["subtitle"]
        args = [f"filename={esc(ass_path)}"]
        if sub["font_file"]:
            args.append(f"fontsdir={esc(str(Path(sub['font_file']).parent.resolve()))}")
        parts.append(f"[{label}]subtitles={':'.join(args)}[subbed]")
        label = "subbed"

    if hook_textfile:
        hook = preset["hook"]
        font_file = hook["font_file"] or preset["subtitle"]["font_file"]
        args = [
            f"textfile={esc(hook_textfile)}",
            "reload=0",
            f"fontsize={hook['font_size']}",
            f"fontcolor={hook['color'].replace('#', '0x')}",
            f"borderw={hook['outline']}",
            f"bordercolor={hook['outline_color'].replace('#', '0x')}",
            "x=(w-text_w)/2",
            f"y={int(height * hook['position_y'])}",
            "line_spacing=12",
            "enable=" + esc("between(t,%.3f,%.3f)" % (hook["start"], hook["start"] + hook["duration"])),
        ]
        font_arg = fonts.drawtext_font_args(
            font_file,
            preset["subtitle"]["font_name"],
            preset["subtitle"]["language"] or None,
        )
        key, _, value = font_arg.partition("=")
        args.insert(0, f"{key}={esc(value)}")
        if hook["box"]:
            args.append("box=1")
            args.append(f"boxcolor={hook['box_color'].replace('#', '0x')}@{hook['box_opacity']}")
            args.append("boxborderw=24")
        parts.append(f"[{label}]drawtext={':'.join(args)}[hooked]")
        label = "hooked"

    parts.append(f"[{label}]format={out['pixel_format']}[vout]")
    return parts


def build_audio_chain(preset: dict, *, source_audio: bool,
                      bgm_label: str | None,
                      duration: float | None = None) -> tuple[list[str], str | None]:
    """오디오 필터 체인과 최종 라벨. 오디오가 아예 없으면 라벨은 None.

    duration 을 알면 BGM 페이드아웃을 끝에 맞춰 건다.
    """
    src = preset["source"]
    bgm = preset["bgm"]
    parts: list[str] = []
    src_label = None

    if source_audio:
        chain = atempo_chain(src["speed"])
        chain.append("aresample=48000")
        parts.append(f"[0:a]{','.join(chain)}[asrc]")
        src_label = "asrc"

    bgm_out = None
    if bgm_label:
        chain = [f"volume={bgm['volume_db']}dB", "aresample=48000"]
        if bgm["fade_in"] > 0:
            chain.append(f"afade=t=in:st=0:d={bgm['fade_in']}")
        if bgm["fade_out"] > 0 and duration:
            fade_out = min(float(bgm["fade_out"]), duration)
            chain.append(f"afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f}")
        parts.append(f"[{bgm_label}]{','.join(chain)}[abgm0]")
        bgm_out = "abgm0"

        if src_label and bgm["duck"]["enabled"]:
            # 원본 오디오를 사이드체인으로 써서 말할 때 BGM 을 눌러준다.
            ratio = max(1.0, min(20.0, 10 ** (abs(bgm["duck"]["amount_db"]) / 20)))
            parts.append(f"[{src_label}]asplit=2[asrc_main][asrc_sc]")
            parts.append(
                f"[abgm0][asrc_sc]sidechaincompress="
                f"threshold=0.03:ratio={ratio:.2f}:attack=20:release=400[abgm1]"
            )
            src_label = "asrc_main"
            bgm_out = "abgm1"

    if src_label and bgm_out:
        parts.append(f"[{src_label}][{bgm_out}]amix=inputs=2:duration=first:"
                     f"dropout_transition=0:normalize=0[aout]")
        return parts, "aout"
    if src_label:
        parts.append(f"[{src_label}]anull[aout]")
        return parts, "aout"
    if bgm_out:
        parts.append(f"[{bgm_out}]anull[aout]")
        return parts, "aout"
    return parts, None


def build_command(preset: dict, *, input_path: str, output_path: str,
                  ass_path: str | None, hook_textfile: str | None,
                  duration: float | None, start: float,
                  has_source_audio: bool, overwrite: bool = True) -> list[str]:
    out = preset["output"]
    bgm = preset["bgm"]
    wm = preset["watermark"]

    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats"]
    cmd.append("-y" if overwrite else "-n")

    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        # 소스에서 읽을 길이는 speed 를 되돌린 값
        cmd += ["-t", f"{duration * preset['source']['speed']:.3f}"]
    cmd += ["-i", input_path]

    next_index = 1
    watermark_label = None
    if wm["enabled"] and wm["file"]:
        cmd += ["-i", str(Path(wm["file"]).resolve())]
        watermark_label = f"{next_index}:v"
        next_index += 1

    bgm_label = None
    if bgm["enabled"] and bgm["file"]:
        if bgm["loop"]:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", str(Path(bgm["file"]).resolve())]
        bgm_label = f"{next_index}:a"
        next_index += 1

    video_parts = build_video_chain(
        preset, ass_path=ass_path, watermark_label=watermark_label,
        hook_textfile=hook_textfile,
    )
    audio_parts, audio_label = build_audio_chain(
        preset,
        source_audio=has_source_audio and not preset["source"]["mute_source"],
        bgm_label=bgm_label,
        duration=duration,
    )

    cmd += ["-filter_complex", ";".join(video_parts + audio_parts)]
    cmd += ["-map", "[vout]"]
    if audio_label:
        cmd += ["-map", f"[{audio_label}]", "-c:a", out["audio_codec"],
                "-b:a", out["audio_bitrate"], "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]

    cmd += [
        "-c:v", out["video_codec"],
        "-crf", str(out["crf"]),
        "-preset", out["x264_preset"],
        "-profile:v", "high",
        "-r", str(out["fps"]),
        "-movflags", "+faststart",
    ]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    elif bgm_label and bgm["loop"]:
        # BGM 루프가 무한이므로 짧은 쪽(영상)에서 끊는다.
        cmd += ["-shortest"]
    cmd.append(output_path)
    return cmd
