#!/usr/bin/env python3
"""暖调治愈的逐帧冷暖参照研究链。

选区只来自逐帧亮度与 RGB 通道关系，不识别灯、窗、床品、肤色、食物或
真实光线传播。证据不足时阻断，禁止退回整幅升温。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import sunset_warm_grade as shared_metrics
import video_memory_protection as video_masks
import visual_density_gate


SCHEMA_VERSION = "4.8.3-phase3e1"
WARM_MIN_COVERAGE = 0.006
WARM_MAX_COVERAGE = 0.04
COOL_MIN_COVERAGE = 0.035
PROTECT_MIN_COVERAGE = 0.006
PROTECT_MAX_COVERAGE = 0.52


class WarmCozyGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask_bytes(frame: Path, width: int, height: int) -> tuple[bytes, bytes, bytes]:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    warm = bytearray(width * height)
    cool = bytearray(width * height)
    protect = bytearray(width * height)
    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high, low = max(red, green, blue), min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0
        black = luma <= 0.045
        neutral_white = luma >= 0.68 and chroma <= 0.10
        if black or neutral_white:
            protect[index] = 255
            continue
        warm_reach = (
            0.10 <= luma <= 0.86
            and chroma >= 0.105
            and red - blue >= 60
            and red - green >= 5
            and green - blue >= 8
        )
        cool_anchor = (
            0.06 <= luma <= 0.82
            and chroma >= 0.045
            and blue - red >= 10
            and blue - green >= 2
        )
        if warm_reach:
            warm[index] = 255
        elif cool_anchor:
            cool[index] = 255
    return bytes(warm), bytes(cool), bytes(protect)


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def write_warm_cozy_masks(frame: Path, warm: Path, cool: Path,
                          protect: Path) -> dict:
    width, height = video_masks._dimensions(frame)
    data = dict(zip(("warm", "cool", "protect"),
                    _mask_bytes(frame, width, height)))
    total = max(1, width * height)
    coverage = {}
    for name, target in (("warm", warm), ("cool", cool), ("protect", protect)):
        video_masks._write_pgm(target, width, height, data[name])
        coverage[name] = round(sum(value >= 128 for value in data[name]) / total, 6)
    return {
        "paths": {"warm": str(warm), "cool": str(cool), "protect": str(protect)},
        "coverage": coverage,
        "meaning": "逐帧暖色、冷色与近中性白位／黑位像素代理",
        "boundary": "RGB 与亮度阈值不是灯、窗、床品、肤色、食物或光线语义识别。",
    }


def build_warm_cozy_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise WarmCozyGradeError("没有输入帧")
    names = ("warm", "cool", "protect")
    for name in names:
        (mask_dir / name).mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    coverages = {name: [] for name in names}
    hashes = {name: [] for name in names}
    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise WarmCozyGradeError("输入帧尺寸不一致")
        paths = {name: mask_dir / name / f"mask-{index:06d}.pgm" for name in names}
        result = write_warm_cozy_masks(frame, **paths)
        for name in names:
            coverages[name].append(result["coverage"][name])
            hashes[name].append(_sha256(paths[name]))

    reasons = []
    tolerated = int(len(frames) * 0.02)
    warm_missing = [i for i, value in enumerate(coverages["warm"])
                    if value < WARM_MIN_COVERAGE]
    warm_oversized = [i for i, value in enumerate(coverages["warm"])
                      if value > WARM_MAX_COVERAGE]
    cool_missing = [i for i, value in enumerate(coverages["cool"])
                    if value < COOL_MIN_COVERAGE]
    protect_missing = [i for i, value in enumerate(coverages["protect"])
                       if value < PROTECT_MIN_COVERAGE]
    protect_oversized = [i for i, value in enumerate(coverages["protect"])
                         if value > PROTECT_MAX_COVERAGE]
    if len(warm_missing) > tolerated:
        reasons.append("暖色可达区覆盖不足或跨帧不连续")
    if warm_oversized:
        reasons.append("暖色可达区面积过大，存在整幅推黄风险")
    if len(cool_missing) > tolerated:
        reasons.append("冷锚覆盖不足或跨帧不连续")
    if len(protect_missing) > tolerated:
        reasons.append("黑位／近中性白位保护锚点不足")
    if protect_oversized:
        reasons.append("保护区面积过大，素材缺少可调空间")
    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-warm-cool-neutral-rgb-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "coverage": coverages,
        "mask_sha256": hashes,
        "warm_missing_frames": warm_missing,
        "warm_oversized_frames": warm_oversized,
        "cool_missing_frames": cool_missing,
        "protect_missing_frames": protect_missing,
        "protect_oversized_frames": protect_oversized,
        "reasons": reasons,
        "boundary": (
            "只证明同一段素材内三类固定像素关系可逐帧分离；不识别灯、窗、床品、"
            "肤色、食物、真实光线衰减，也不做光流与身份跟踪。"
        ),
    }


def capabilities_from_report(report: dict) -> set[str]:
    paths = [Path((report.get(f"{name}_mask_video") or {}).get("path") or "")
             for name in ("warm", "cool", "protect")]
    if (report.get("status") == "ready" and report.get("capability_granted") is True
            and all(path.is_file() for path in paths)):
        return {"temporal-warm-accent-approximation",
                "temporal-cool-anchor-approximation",
                "temporal-neutral-protection"}
    return set()


def warm_accent_gains(strength: float) -> tuple[float, float, float]:
    mix = max(0.0, min(1.0, float(strength)))
    return 1.0 + 0.18 * mix, 1.0 + 0.025 * mix, 1.0 - 0.16 * mix


def cool_anchor_gains(strength: float) -> tuple[float, float, float]:
    mix = max(0.0, min(1.0, float(strength)))
    return 1.0 - 0.08 * mix, 1.0 + 0.015 * mix, 1.0 + 0.13 * mix


def _gain_filter(gains: tuple[float, float, float], saturation: float) -> str:
    red, green, blue = gains
    return (f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f},"
            f"eq=contrast=1.015:brightness=0:saturation={saturation:.6f}")


def warm_accent_filter(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    return _gain_filter(warm_accent_gains(mix), 1.0 + 0.10 * mix)


def cool_anchor_filter(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    return _gain_filter(cool_anchor_gains(mix), 1.0 + 0.055 * mix)


def foundation_filter() -> str:
    return "curves=all='0/0 0.12/0.105 0.38/0.395 0.76/0.775 1/1'"


def strength_advice(strength_percent: int, passed: bool) -> str:
    if passed:
        return "保持当前强度并进入人工连续回放。"
    if int(strength_percent) >= 80:
        return "80% 已失败：回退到 55% 或 Foundation，禁止继续加力。"
    return "回退一档，先检查暖区、冷锚与白位，再决定是否重做。"


def build_layered_filter_complex(width: int | None = None,
                                 height: int | None = None) -> str:
    mask = "format=gbrp,setpts=PTS-STARTPTS"
    if width and height:
        mask = f"scale={int(width)}:{int(height)}:flags=bilinear," + mask
    return (
        "[0:v]format=gbrp,setpts=PTS-STARTPTS,split=2[blbase][blbaseprotect];"
        "[1:v]format=gbrp,setpts=PTS-STARTPTS[blwarm];"
        f"[3:v]{mask}[blwarmmask];"
        "[blbase][blwarm][blwarmmask]maskedmerge=planes=7[blwarmed];"
        "[2:v]format=gbrp,setpts=PTS-STARTPTS[blcool];"
        f"[4:v]{mask}[blcoolmask];"
        "[blwarmed][blcool][blcoolmask]maskedmerge=planes=7[blsplit];"
        f"[5:v]{mask}[blprotectmask];"
        "[blsplit][blbaseprotect][blprotectmask]maskedmerge=planes=7,"
        "format=yuv420p[blcozy]"
    )


def masked_color_metrics(frames: list[Path], warm_masks: list[Path],
                         cool_masks: list[Path], protect_masks: list[Path]) -> dict:
    return shared_metrics.masked_color_metrics(
        frames, warm_masks, cool_masks, protect_masks)


def evaluate_visual_density(baseline: dict, candidate: dict,
                            strength_percent: int) -> dict:
    return visual_density_gate.evaluate_region_density(
        baseline, candidate, strength_percent, focus_region="warm")


def build_dynamic_masks(source: Path, outputs: dict[str, Path], workdir: Path,
                        analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_warm_cozy_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    for name in ("warm", "cool", "protect"):
        report[f"{name}_mask_video"] = video_masks.assemble_mask_video(
            workdir / "masks" / name, outputs[name],
            probe["fps_fraction"], len(frames))
    return report


def _render_layer(source: Path, output: Path, filter_chain: str) -> dict:
    if output.exists():
        raise WarmCozyGradeError(f"输出已存在，不会覆盖：{output}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
               "-i", str(source), "-vf", filter_chain, "-an", "-c:v", "libx264",
               "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
               "-color_primaries", "bt709", "-color_trc", "bt709",
               "-colorspace", "bt709", str(output)]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise WarmCozyGradeError(result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def _has_audio(path: Path) -> bool:
    result = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error",
                             "-select_streams", "a:0", "-show_entries", "stream=index",
                             "-of", "csv=p=0", str(path)], capture_output=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def _exact_frame_args(info: dict) -> list[str]:
    count = int(info.get("nb_frames") or 0)
    if count <= 0:
        raise WarmCozyGradeError("源视频没有可验证的帧数")
    return ["-frames:v", str(count)]


def render_layered_video(base: Path, warm: Path, cool: Path,
                         masks: dict[str, Path], output: Path,
                         source_info: dict, audio_source: Path | None = None) -> dict:
    if output.exists():
        raise WarmCozyGradeError(f"输出已存在，不会覆盖：{output}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n"]
    for path in (base, warm, cool, masks["warm"], masks["cool"], masks["protect"]):
        command.extend(["-i", str(path)])
    audio_input = audio_source if audio_source is not None else base
    audio = _has_audio(audio_input)
    audio_index = 0
    if audio and audio_input != base:
        command.extend(["-i", str(audio_input)])
        audio_index = 6
    command.extend(["-filter_complex", build_layered_filter_complex(
        source_info.get("width"), source_info.get("height")), "-map", "[blcozy]"])
    if audio:
        command.extend(["-map", f"{audio_index}:a:0", "-c:a", "aac", "-b:a", "192k"])
    command.extend([*_exact_frame_args(source_info), "-c:v", "libx264", "-crf", "18",
                    "-preset", "medium", "-pix_fmt", "yuv420p",
                    "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709", "-shortest", str(output)])
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise WarmCozyGradeError(result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output),
            "audio_preserved": audio, "command": command}


def render(source: Path, output: Path, workdir: Path, strength: float) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    for folder in ("mask-videos", "layers"):
        (workdir / folder).mkdir(exist_ok=True)
    mask_paths = {name: workdir / "mask-videos" / f"{name}.mp4"
                  for name in ("warm", "cool", "protect")}
    mask_report = build_dynamic_masks(source, mask_paths, workdir / "analysis")
    if mask_report["status"] != "ready":
        raise WarmCozyGradeError("素材资格不足：" + "；".join(mask_report["reasons"]))
    base = workdir / "layers" / "foundation.mp4"
    warm = workdir / "layers" / "warm.mp4"
    cool = workdir / "layers" / "cool.mp4"
    _render_layer(source, base, foundation_filter())
    _render_layer(base, warm, warm_accent_filter(strength))
    _render_layer(base, cool, cool_anchor_filter(strength))
    source_info = video_masks._video_probe(source)
    result = render_layered_video(base, warm, cool, mask_paths, output, source_info,
                                  audio_source=source)
    result["mask_report"] = mask_report
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="暖调治愈冷暖参照研究渲染")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--strength", type=float, default=0.55)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = render(args.input, args.output, args.workdir, args.strength)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
