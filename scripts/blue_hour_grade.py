#!/usr/bin/env python3
"""蓝调时刻的逐帧垂直时间梯度与小面积暖灯保护研究链。

所有选区都来自画面位置、亮度与通道关系，只是像素代理。它不识别天空、
地平线、建筑、人物或真实灯光；证据不足时必须阻断，不能退回全局染蓝。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from fractions import Fraction

try:
    from . import video_memory_protection as video_masks
except ImportError:
    import video_memory_protection as video_masks


SCHEMA_VERSION = "4.8.3-phase2g"
BAND_MIN_COVERAGE = 0.025
ANCHOR_MIN_COVERAGE = 0.00005
ANCHOR_MAX_COVERAGE = 0.08
MIN_VERTICAL_COOLNESS_DELTA = 0.015


class BlueHourGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask_bytes(frame: Path, width: int, height: int) -> tuple[bytes, bytes, bytes, bytes, dict]:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    masks = {name: bytearray(width * height)
             for name in ("top", "middle", "horizon", "anchor")}
    coolness = {name: [] for name in ("top", "middle", "horizon")}
    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high, low = max(red, green, blue), min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0
        row = index // width
        vertical = row / max(1, height - 1)

        warm_light = (
            vertical >= 0.25
            and 0.25 <= luma <= 0.98
            and red - green >= 14
            and red - blue >= 28
        )
        neutral_light = vertical >= 0.45 and luma >= 0.72 and chroma <= 0.09
        if warm_light or neutral_light:
            masks["anchor"][index] = 255
            continue

        sky_like = (
            vertical < 0.78
            and 0.055 <= luma <= 0.92
            and (blue - red >= 6 or chroma <= 0.18)
        )
        if not sky_like:
            continue
        if vertical < 0.30:
            band = "top"
        elif vertical < 0.58:
            band = "middle"
        else:
            band = "horizon"
        masks[band][index] = 255
        coolness[band].append((blue - red) / 255.0)

    statistics = {
        "mean_coolness": {
            name: round(sum(values) / len(values), 6) if values else None
            for name, values in coolness.items()
        }
    }
    return (bytes(masks["top"]), bytes(masks["middle"]),
            bytes(masks["horizon"]), bytes(masks["anchor"]), statistics)


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def write_blue_hour_masks(frame: Path, top: Path, middle: Path,
                          horizon: Path, anchor: Path) -> dict:
    width, height = video_masks._dimensions(frame)
    top_data, middle_data, horizon_data, anchor_data, statistics = _mask_bytes(
        frame, width, height)
    payloads = {
        "top": (top, top_data),
        "middle": (middle, middle_data),
        "horizon": (horizon, horizon_data),
        "anchor": (anchor, anchor_data),
    }
    total = max(1, width * height)
    coverage = {}
    for name, (path, data) in payloads.items():
        video_masks._write_pgm(path, width, height, data)
        coverage[name] = round(sum(value >= 128 for value in data) / total, 6)
    return {
        "paths": {name: str(path) for name, (path, _) in payloads.items()},
        "coverage": coverage,
        **statistics,
        "meaning": "逐帧垂直冷色带与小面积暖灯代理；不是天空、地平线、建筑或灯光语义分割",
    }


def build_blue_hour_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise BlueHourGradeError("没有输入帧")
    names = ("top", "middle", "horizon", "anchor")
    for name in names:
        (mask_dir / name).mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    coverages = {name: [] for name in names}
    hashes = {name: [] for name in names}
    coolness = {name: [] for name in ("top", "middle", "horizon")}

    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise BlueHourGradeError("输入帧尺寸不一致")
        paths = {name: mask_dir / name / f"mask-{index:06d}.pgm" for name in names}
        result = write_blue_hour_masks(frame, **paths)
        for name in names:
            coverages[name].append(result["coverage"][name])
            hashes[name].append(_sha256(paths[name]))
        for name in coolness:
            coolness[name].append(result["mean_coolness"][name])

    reasons = []
    missing = {
        name: [index for index, value in enumerate(coverages[name])
               if value < BAND_MIN_COVERAGE]
        for name in ("top", "middle", "horizon")
    }
    for name, indices in missing.items():
        if indices and len(indices) / len(frames) > 0.02:
            reasons.append(f"{name} 冷色带覆盖不足或跨帧不连续")
    anchor_missing = [index for index, value in enumerate(coverages["anchor"])
                      if value < ANCHOR_MIN_COVERAGE]
    anchor_oversized = [index for index, value in enumerate(coverages["anchor"])
                        if value > ANCHOR_MAX_COVERAGE]
    if anchor_missing and len(anchor_missing) / len(frames) > 0.02:
        reasons.append("没有得到可保护的小面积暖灯")
    if anchor_oversized:
        reasons.append("暖灯面积过大，不能作为蓝调时刻锚点")

    weak_gradient = []
    for index in range(len(frames)):
        top = coolness["top"][index]
        horizon = coolness["horizon"][index]
        if top is None or horizon is None or top - horizon < MIN_VERTICAL_COOLNESS_DELTA:
            weak_gradient.append(index)
    if weak_gradient and len(weak_gradient) / len(frames) > 0.02:
        reasons.append("天顶到近地平线的冷色差不足")

    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-vertical-cool-band-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "coverage": coverages,
        "mean_coolness": coolness,
        "mask_sha256": hashes,
        "band_missing_frames": missing,
        "anchor_missing_frames": anchor_missing,
        "anchor_oversized_frames": anchor_oversized,
        "weak_gradient_frames": weak_gradient,
        "reasons": reasons,
        "boundary": (
            "三段只按画面垂直位置、亮度与冷色关系近似，可能误纳云、水面或建筑；"
            "暖灯只按逐帧暖色或近白高亮近似。不识别天空、地平线、建筑、人物或真实灯光，"
            "也不做光流与身份跟踪。"
        ),
    }


def capabilities_from_report(report: dict) -> set[str]:
    paths = [Path((report.get(f"{name}_mask_video") or {}).get("path") or "")
             for name in ("top", "middle", "horizon", "anchor")]
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and all(path.is_file() for path in paths)):
        return {"blue-hour-vertical-gradient-approximation",
                "isolated-anchor-protection"}
    return set()


def vertical_gradient_filters(strength: float) -> dict[str, str]:
    mix = max(0.0, min(1.0, float(strength)))
    # 三段均为乘法关系，并按 Rec.709 权重近似保持亮度，避免“时间梯度”退化成压暗。
    coefficients = {
        "top": (1.0 - 0.16 * mix, 1.0 + 0.020 * mix, 1.0 + 0.27 * mix),
        "middle": (1.0 - 0.10 * mix, 1.0 + 0.018 * mix, 1.0 + 0.118 * mix),
        "horizon": (1.0 - 0.035 * mix, 1.0 + 0.004 * mix, 1.0 + 0.063 * mix),
    }
    return {
        name: f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}"
        for name, (red, green, blue) in coefficients.items()
    }


def build_layered_filter_complex(width: int | None = None,
                                 height: int | None = None,
                                 fps_fraction: str = '30000/1001') -> str:
    fps = Fraction(fps_fraction)
    if fps <= 0:
        raise BlueHourGradeError('帧率必须为正')
    clock = f'settb=AVTB,setpts=N*{fps.denominator}/({fps.numerator}*TB)'
    mask = f"format=gbrp,{clock}"
    if width and height:
        mask = f"scale={int(width)}:{int(height)}:flags=bilinear," + mask
    return (
        f"[0:v]format=gbrp,{clock},split=2[blbase][blbaseprotect];"
        f"[1:v]format=gbrp,{clock}[bltop];"
        f"[2:v]format=gbrp,{clock}[blmiddle];"
        f"[3:v]format=gbrp,{clock}[blhorizon];"
        f"[4:v]{mask}[bltopmask];"
        "[blbase][bltop][bltopmask]maskedmerge=planes=7[bltopmerged];"
        f"[5:v]{mask}[blmiddlemask];"
        "[bltopmerged][blmiddle][blmiddlemask]maskedmerge=planes=7[blmiddlemerged];"
        f"[6:v]{mask}[blhorizonmask];"
        "[blmiddlemerged][blhorizon][blhorizonmask]maskedmerge=planes=7[blgradient];"
        f"[7:v]{mask}[blanchormask];"
        f"[8:v]{mask}[blmemorymask];"
        "[blanchormask][blmemorymask]blend=all_mode=max[blprotectmask];"
        "[blgradient][blbaseprotect][blprotectmask]maskedmerge=planes=7,"
        "format=yuv420p[blbluehour]"
    )


def exact_frame_args(info: dict) -> list[str]:
    frame_count = int(info.get("nb_frames") or 0)
    if frame_count <= 0:
        raise BlueHourGradeError("源视频没有可验证的帧数")
    return ["-frames:v", str(frame_count)]


def build_dynamic_blue_hour_masks(source: Path, outputs: dict[str, Path],
                                  workdir: Path, analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_blue_hour_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    for name in ("top", "middle", "horizon", "anchor"):
        report[f"{name}_mask_video"] = video_masks.assemble_mask_video(
            workdir / "masks" / name, outputs[name],
            probe["fps_fraction"], len(frames))
    return report


def render_gradient_layer(base_video: Path, output: Path,
                          filter_chain: str) -> dict:
    if output.exists():
        raise BlueHourGradeError(f"输出已存在，不会覆盖：{output}")
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(base_video), "-vf", filter_chain,
        "-map", "0:v:0", "-an", "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise BlueHourGradeError(
            "蓝调分层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def render_layered_video(base_video: Path, layer_videos: dict[str, Path],
                         mask_videos: dict[str, Path], memory_mask: Path,
                         audio_source: Path, output: Path) -> dict:
    if output.exists():
        raise BlueHourGradeError(f"输出已存在，不会覆盖：{output}")
    info = video_masks._video_probe(audio_source)
    graph = build_layered_filter_complex(info["width"], info["height"], info['fps_fraction'])
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(base_video),
        "-i", str(layer_videos["top"]), "-i", str(layer_videos["middle"]),
        "-i", str(layer_videos["horizon"]),
        "-i", str(mask_videos["top"]), "-i", str(mask_videos["middle"]),
        "-i", str(mask_videos["horizon"]), "-i", str(mask_videos["anchor"]),
        "-i", str(memory_mask), "-i", str(audio_source),
        "-filter_complex", graph, "-map", "[blbluehour]", "-map", "9:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "copy",
        *exact_frame_args(info), "-movflags", "+faststart", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise BlueHourGradeError(
            "蓝调时刻分层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {
        "path": str(output), "sha256": _sha256(output), "command": command,
        "filter_complex": graph, "probe": video_masks._video_probe(output),
    }
