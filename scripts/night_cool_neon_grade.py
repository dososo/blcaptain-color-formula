#!/usr/bin/env python3
"""冷调霓虹夜景的 Foundation-first 单素材视频研究链。

逐帧代理只按亮度、HSV 彩度与色相生成近白灯芯、青色衰减带和品红衰减带；
它不是灯具、招牌、人物、道路或材质语义分割。Creative Look 只在这些代理区内
工作，真实黑位保留 Foundation，证据不足时阻断而不是退回整幅染蓝。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageChops

import video_memory_protection as video_masks


SCHEMA_VERSION = "4.8.3-phase3i2"
# 360x640 分析帧至少约 92 像素；低于此值不足以形成可复核灯芯路径。
CORE_MIN_COVERAGE = 0.0004
CORE_MAX_COVERAGE = 0.18
CYAN_MIN_COVERAGE = 0.004
MAGENTA_MIN_COVERAGE = 0.0001


class NightCoolNeonGradeError(Exception):
    pass


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path) -> dict:
    result = subprocess.run([
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-count_frames",
        "-show_entries",
        "stream=index,codec_type,width,height,avg_frame_rate,nb_frames,nb_read_frames,"
        "color_space,color_transfer,color_primaries:format=duration",
        "-of", "json", str(path),
    ], capture_output=True, text=True)
    if result.returncode:
        raise NightCoolNeonGradeError(result.stderr.strip())
    return json.loads(result.stdout)


def _video_stream(probe: dict) -> dict:
    stream = next((item for item in probe.get("streams", [])
                   if item.get("codec_type") == "video"), None)
    if not stream:
        raise NightCoolNeonGradeError("没有可验证的视频流")
    return stream


def _frame_count(stream: dict) -> int:
    for key in ("nb_frames", "nb_read_frames"):
        value = stream.get(key)
        if value not in (None, "", "N/A"):
            return int(value)
    return 0


def _ramp(low: int, high: int, invert: bool = False) -> list[int]:
    values = []
    for value in range(256):
        if value <= low:
            result = 0
        elif value >= high:
            result = 255
        else:
            result = round((value - low) * 255 / max(1, high - low))
        values.append(255 - result if invert else result)
    return values


def _band(low: int, peak_low: int, peak_high: int, high: int) -> list[int]:
    values = []
    for value in range(256):
        if value <= low or value >= high:
            result = 0
        elif value < peak_low:
            result = round((value - low) * 255 / max(1, peak_low - low))
        elif value <= peak_high:
            result = 255
        else:
            result = round((high - value) * 255 / max(1, high - peak_high))
        values.append(max(0, min(255, result)))
    return values


def _hue_mask(hue: Image.Image, ranges: list[tuple[int, int]]) -> Image.Image:
    table = [255 if any(low <= value <= high for low, high in ranges) else 0
             for value in range(256)]
    return hue.point(table)


def write_decay_masks(frame: Path, core: Path, cyan: Path,
                      magenta: Path) -> dict:
    """写出软灯芯与两支衰减带；输出为分析分辨率 PGM。"""
    with Image.open(frame) as opened:
        rgb = opened.convert("RGB")
    luma = rgb.convert("L")
    hue, saturation, _ = rgb.convert("HSV").split()

    neutral_core = ImageChops.multiply(
        luma.point(_ramp(158, 228)),
        saturation.point(_ramp(18, 85, invert=True)),
    )
    # 霓虹灯芯可能在源片中已经是高彩发光体；极亮端仍需进入“通往白”路径。
    # 这里只按亮度取极亮像素，不把它称为灯具或招牌语义。
    saturated_core = luma.point(_ramp(176, 232))
    core_mask = ImageChops.lighter(neutral_core, saturated_core)
    # 允许源绿色灯带进入青色塑形区；只在中低亮度保留颜色，灯芯由 core 覆盖。
    mid_luma = luma.point(_band(18, 42, 168, 224))
    colored = saturation.point(_ramp(24, 80))
    not_core = ImageChops.invert(core_mask)
    cyan_hue = _hue_mask(hue, [(48, 154)])
    magenta_hue = _hue_mask(hue, [(0, 13), (202, 255)])
    cyan_mask = ImageChops.multiply(
        ImageChops.multiply(ImageChops.multiply(cyan_hue, colored), mid_luma),
        not_core,
    )
    magenta_mask = ImageChops.multiply(
        ImageChops.multiply(ImageChops.multiply(magenta_hue, colored), mid_luma),
        not_core,
    )

    outputs = {"core": (core, core_mask), "cyan": (cyan, cyan_mask),
               "magenta": (magenta, magenta_mask)}
    total = max(1, rgb.width * rgb.height)
    coverage = {}
    for name, (path, mask) in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        mask.save(path, format="PPM")
        coverage[name] = round(sum(value >= 32 for value in mask.getdata()) / total, 6)
    return {
        "paths": {name: str(path) for name, (path, _) in outputs.items()},
        "coverage": coverage,
        "meaning": "逐帧亮度、HSV 彩度与色相得到的灯芯／青／品红软代理；不是光源语义分割",
    }


def build_decay_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise NightCoolNeonGradeError("没有输入帧")
    names = ("core", "cyan", "magenta")
    for name in names:
        (mask_dir / name).mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    coverages = {name: [] for name in names}
    hashes = {name: [] for name in names}
    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise NightCoolNeonGradeError("输入帧尺寸不一致")
        outputs = {name: mask_dir / name / f"mask-{index:06d}.pgm"
                   for name in names}
        report = write_decay_masks(frame, **outputs)
        for name in names:
            coverages[name].append(report["coverage"][name])
            hashes[name].append(_sha256(outputs[name]))

    reasons = []
    core_missing = [i for i, value in enumerate(coverages["core"])
                    if value < CORE_MIN_COVERAGE]
    core_oversized = [i for i, value in enumerate(coverages["core"])
                      if value > CORE_MAX_COVERAGE]
    cyan_missing = [i for i, value in enumerate(coverages["cyan"])
                    if value < CYAN_MIN_COVERAGE]
    magenta_missing = [i for i, value in enumerate(coverages["magenta"])
                       if value < MAGENTA_MIN_COVERAGE]
    if len(core_missing) / len(frames) > 0.02:
        reasons.append("近白灯芯覆盖不足或跨帧不连续")
    if core_oversized:
        reasons.append("近白灯芯面积过大，画面可能已过曝")
    if len(cyan_missing) / len(frames) > 0.02:
        reasons.append("青色衰减带覆盖不足或跨帧不连续")
    if len(magenta_missing) / len(frames) > 0.05:
        reasons.append("品红衰减带覆盖不足或跨帧不连续")
    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-luma-hsv-neon-decay-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "coverage": coverages,
        "mask_sha256": hashes,
        "missing_frames": {"core": core_missing, "cyan": cyan_missing,
                           "magenta": magenta_missing},
        "core_oversized_frames": core_oversized,
        "reasons": reasons,
        "boundary": "只按逐帧像素条件近似，不识别灯具、招牌、人物、道路或材质，也不做光流与身份跟踪。",
    }


def build_dynamic_decay_masks(source: Path, outputs: dict[str, Path],
                              workdir: Path, analysis_width: int = 540) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_decay_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    for name in ("core", "cyan", "magenta"):
        report[f"{name}_mask_video"] = video_masks.assemble_mask_video(
            workdir / "masks" / name, outputs[name],
            probe["fps_fraction"], len(frames))
    return report


def capabilities_from_report(report: dict) -> set[str]:
    paths = [Path((report.get(f"{name}_mask_video") or {}).get("path") or "")
             for name in ("core", "cyan", "magenta")]
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and all(path.is_file() for path in paths)):
        return {"temporal-neon-decay-band-approximation"}
    return set()


def creative_controls(strength: float) -> dict[str, float]:
    """把单次感知强度映射为局部霓虹控制，供实现与合同共用。"""
    mix = _clamp(strength)
    return {
        "cyan_hue": 42.0 * mix,
        "cyan_sat": 0.95 * mix,
        "magenta_hue": -26.0 * mix,
        "magenta_sat": 0.72 * mix,
        "core_sat": max(0.05, 1.0 - 1.46 * mix),
        "shoulder": 0.85 - 0.045 * mix,
        "spill_mid_lift": 0.036 * mix,
        "spill_upper_lift": 0.026 * mix,
        "core_mask_gain": 1.40 + 2.00 * mix,
    }


def build_filter_complex(strength: float, width: int, height: int) -> str:
    controls = creative_controls(strength)
    cyan_hue = controls["cyan_hue"]
    cyan_sat = controls["cyan_sat"]
    magenta_hue = controls["magenta_hue"]
    magenta_sat = controls["magenta_sat"]
    core_sat = controls["core_sat"]
    shoulder = controls["shoulder"]
    spill_mid = 0.35 + controls["spill_mid_lift"]
    spill_upper = 0.65 + controls["spill_upper_lift"]
    core_mask_gain = controls["core_mask_gain"]
    color_mask = (f"scale={int(width)}:{int(height)}:flags=bilinear,format=gray,"
                  "lut=y='clip(val*1.35,0,255)',format=gbrp,setpts=PTS-STARTPTS")
    # 灯芯最后合成且回护更强，避免高强度青色层从软边缘反向污染灯芯。
    core_mask = (f"scale={int(width)}:{int(height)}:flags=bilinear,format=gray,"
                 f"lut=y='clip(val*{core_mask_gain:.6f},0,255)',"
                 "format=gbrp,setpts=PTS-STARTPTS")
    return (
        "[0:v]format=gbrp,setpts=PTS-STARTPTS,split=4[nbase][ncyanbase][nmagentabase][ncorebase];"
        f"[ncyanbase]huesaturation=colors=g+c:hue={cyan_hue:.6f}:"
        f"saturation={cyan_sat:.6f}:strength=100:lightness=1,"
        f"curves=all='0/0 0.12/0.12 0.35/{spill_mid:.6f} "
        f"0.65/{spill_upper:.6f} 0.88/0.88 1/1',format=gbrp[ncyan];"
        f"[nmagentabase]huesaturation=colors=r+m:hue={magenta_hue:.6f}:"
        f"saturation={magenta_sat:.6f}:strength=100:lightness=1,format=gbrp[nmagenta];"
        f"[ncorebase]hue=s={core_sat:.6f},"
        f"curves=all='0/0 0.72/0.72 0.90/{shoulder:.6f} 1/1',format=gbrp[ncore];"
        f"[1:v]{core_mask}[ncoremask];"
        f"[2:v]{color_mask}[ncyanmask];[3:v]{color_mask}[nmagentamask];"
        "[nbase][ncyan][ncyanmask]maskedmerge=planes=7[ncyanmerged];"
        "[ncyanmerged][nmagenta][nmagentamask]maskedmerge=planes=7[naccented];"
        "[naccented][ncore][ncoremask]maskedmerge=planes=7,format=yuv420p[nout]"
    )


def evaluate_signature(baseline: dict, candidate: dict,
                       strength_percent: int) -> dict:
    scale = max(0.3, min(1.0, float(strength_percent) / 55.0))
    axes = []
    base_gap = baseline["halo_chroma"] - baseline["core_chroma"]
    candidate_gap = candidate["halo_chroma"] - candidate["core_chroma"]
    if candidate_gap >= base_gap + 0.035 * scale:
        axes.append("color_moves_from_core_to_decay_band")
    if (candidate["cyan_halo_chroma"] >= baseline["cyan_halo_chroma"] + 0.025 * scale
            and candidate["magenta_halo_chroma"] >= baseline["magenta_halo_chroma"] + 0.020 * scale):
        axes.append("cyan_magenta_decay_hierarchy")

    safety = []
    if candidate["black_luma"] > baseline["black_luma"] + 0.010:
        safety.append("black_lifted")
    if candidate["shadow_luma"] < baseline["shadow_luma"] - 0.018:
        safety.append("shadows_crushed")
    if candidate["neutral_white_chroma"] > baseline["neutral_white_chroma"] + 0.012:
        safety.append("neutral_white_drifted")
    if candidate["highlight_clip_ratio"] > baseline["highlight_clip_ratio"] + 0.003:
        safety.append("highlight_clip_regressed")
    if candidate["colored_frame_ratio"] > 0.82:
        safety.append("colored_frame_overrun")
    blocking = ([] if len(axes) >= 2 else ["fewer_than_two_signature_axes"]) + safety
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if not blocking else "blocked",
        "strength_percent": int(strength_percent),
        "passed_signature_axes": axes,
        "safety_failures": safety,
        "blocking_failures": blocking,
        "boundary": "统计只验证灯芯—衰减带方向和安全上限，不替代人工审美。",
    }


def strength_advice(strength_percent: int, passed: bool) -> str:
    if passed:
        return "保持当前强度并进入人工连续回放。"
    if int(strength_percent) >= 80:
        return "80% 已失败：回退到 55% 或 Foundation，本轮停止加力。"
    return "回退一档，先检查灯芯、衰减带、暗部与时序，再决定是否重做。"


def render(foundation: Path, masks: dict[str, Path], output: Path,
           strength: float) -> dict:
    if output.exists():
        raise NightCoolNeonGradeError(f"输出已存在，不会覆盖：{output}")
    inputs = [foundation, masks["core"], masks["cyan"], masks["magenta"]]
    if output.resolve() in {path.resolve() for path in inputs}:
        raise NightCoolNeonGradeError("输出不得覆盖 Foundation 或蒙版")
    probes = [_probe(path) for path in inputs]
    streams = [_video_stream(probe) for probe in probes]
    counts = [_frame_count(stream) for stream in streams]
    if counts[0] <= 0 or any(count != counts[0] for count in counts[1:]):
        raise NightCoolNeonGradeError("蒙版与 Foundation 帧数不一致")
    tags = {streams[0].get("color_space"), streams[0].get("color_transfer"),
            streams[0].get("color_primaries")}
    if tags != {"bt709"}:
        raise NightCoolNeonGradeError("Foundation 不是完整 BT.709 显示参照")
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    graph = build_filter_complex(strength, width, height)
    output.parent.mkdir(parents=True, exist_ok=True)
    has_audio = any(item.get("codec_type") == "audio"
                    for item in probes[0].get("streams", []))
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n"]
    for path in inputs:
        command.extend(["-i", str(path)])
    command.extend(["-filter_complex", graph, "-map", "[nout]"])
    if has_audio:
        command.extend(["-map", "0:a:0?", "-c:a", "copy"])
    command.extend([
        "-frames:v", str(counts[0]), "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-movflags", "+faststart", str(output),
    ])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode or not output.is_file():
        raise NightCoolNeonGradeError(result.stderr.strip())
    rendered_probe = _probe(output)
    if _frame_count(_video_stream(rendered_probe)) != counts[0]:
        raise NightCoolNeonGradeError("成片帧数验收失败")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "rendered_pending_human",
        "foundation": str(foundation),
        "foundation_sha256": _sha256(foundation),
        "masks": {name: {"path": str(path), "sha256": _sha256(path)}
                  for name, path in masks.items()},
        "output": str(output),
        "output_sha256": _sha256(output),
        "strength_percent": round(_clamp(strength) * 100),
        "audio_preserved": has_audio,
        "probe": rendered_probe,
        "filter_complex": graph,
        "command": command,
        "aesthetic_status": "pending_human",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foundation", type=Path, required=True)
    parser.add_argument("--core-mask", type=Path, required=True)
    parser.add_argument("--cyan-mask", type=Path, required=True)
    parser.add_argument("--magenta-mask", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strength", type=float, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    receipt = render(
        args.foundation,
        {"core": args.core_mask, "cyan": args.cyan_mask,
         "magenta": args.magenta_mask},
        args.output,
        args.strength,
    )
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
