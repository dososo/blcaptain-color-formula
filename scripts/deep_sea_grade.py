#!/usr/bin/env python3
"""深海航线的逐帧环境空间与孤立灯点研究链。

这里的蒙版只依据亮度、通道关系和面积建立代理，不识别海面、地平线、
负空间或光源。证据不足时必须阻断，不能退回全局染蓝。
"""

from __future__ import annotations

import hashlib
import math
import shutil
import statistics
import subprocess
from pathlib import Path

import video_memory_protection as video_masks


SCHEMA_VERSION = "4.8.3-phase2f"
ENVIRONMENT_MIN_COVERAGE = 0.20
ANCHOR_MIN_COVERAGE = 0.00002
ANCHOR_MAX_COVERAGE = 0.12
ANCHOR_NEIGHBORHOOD_MAX_EV = 2.0


class DeepSeaGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _linear(value: int) -> float:
    encoded = value / 255.0
    return encoded / 12.92 if encoded <= 0.04045 else ((encoded + 0.055) / 1.055) ** 2.4


def _anchor_neighborhood_ev(rgb: bytes, anchor: bytes,
                            width: int, height: int) -> float | None:
    radius = max(2, round(min(width, height) * 0.05))
    lumas = [0.0] * (width * height)
    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        lumas[index] = 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)
    integral = [0.0] * ((width + 1) * (height + 1))
    stride = width + 1
    for row in range(height):
        running = 0.0
        for column in range(width):
            running += lumas[row * width + column]
            integral[(row + 1) * stride + column + 1] = integral[row * stride + column + 1] + running
    differences = []
    for index, selected in enumerate(anchor):
        if selected < 128:
            continue
        row, column = divmod(index, width)
        left, right = max(0, column - radius), min(width, column + radius + 1)
        top, bottom = max(0, row - radius), min(height, row + radius + 1)
        total = (integral[bottom * stride + right]
                 - integral[top * stride + right]
                 - integral[bottom * stride + left]
                 + integral[top * stride + left])
        neighborhood = total / max(1, (right - left) * (bottom - top))
        differences.append(math.log2(max(lumas[index], 1e-6) / max(neighborhood, 1e-6)))
    return round(statistics.median(differences), 6) if differences else None


def _mask_bytes(frame: Path, width: int, height: int) -> tuple[bytes, bytes, float | None]:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    environment = bytearray(width * height)
    anchor = bytearray(width * height)
    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high = max(red, green, blue)
        low = min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0
        red_signal = luma >= 0.10 and red - green >= 35 and red - blue >= 25
        warm_or_ivory = (
            luma >= 0.48
            and red >= green >= blue
            and red - blue >= 10
        )
        near_white = luma >= 0.72 and chroma <= 0.10
        if red_signal or warm_or_ivory or near_white:
            anchor[index] = 255
            continue
        cool_or_neutral = blue - red >= 4 or chroma <= 0.12
        if 0.055 <= luma <= 0.68 and cool_or_neutral:
            environment[index] = 255
    anchor_bytes = bytes(anchor)
    return bytes(environment), anchor_bytes, _anchor_neighborhood_ev(
        rgb, anchor_bytes, width, height)


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def write_deep_sea_masks(frame: Path, environment_output: Path,
                         anchor_output: Path) -> dict:
    width, height = video_masks._dimensions(frame)
    environment, anchor, anchor_neighborhood_ev = _mask_bytes(frame, width, height)
    video_masks._write_pgm(environment_output, width, height, environment)
    video_masks._write_pgm(anchor_output, width, height, anchor)
    total = max(1, width * height)
    return {
        "environment_path": str(environment_output),
        "anchor_path": str(anchor_output),
        "environment_coverage": round(sum(value >= 128 for value in environment) / total, 6),
        "anchor_coverage": round(sum(value >= 128 for value in anchor) / total, 6),
        "anchor_neighborhood_ev": anchor_neighborhood_ev,
        "meaning": "逐帧低中亮度冷色环境与孤立灯点代理；不是海面、地平线、负空间或光源语义分割",
    }


def build_deep_sea_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise DeepSeaGradeError("没有输入帧")
    environment_dir = mask_dir / "environment"
    anchor_dir = mask_dir / "anchor"
    environment_dir.mkdir(parents=True, exist_ok=True)
    anchor_dir.mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    environment_coverages = []
    anchor_coverages = []
    anchor_neighborhood_evs = []
    environment_hashes = []
    anchor_hashes = []
    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise DeepSeaGradeError("输入帧尺寸不一致")
        environment_path = environment_dir / f"mask-{index:06d}.pgm"
        anchor_path = anchor_dir / f"mask-{index:06d}.pgm"
        result = write_deep_sea_masks(frame, environment_path, anchor_path)
        environment_coverages.append(result["environment_coverage"])
        anchor_coverages.append(result["anchor_coverage"])
        anchor_neighborhood_evs.append(result["anchor_neighborhood_ev"])
        environment_hashes.append(_sha256(environment_path))
        anchor_hashes.append(_sha256(anchor_path))

    reasons = []
    environment_missing = [index for index, value in enumerate(environment_coverages)
                           if value < ENVIRONMENT_MIN_COVERAGE]
    anchor_missing = [index for index, value in enumerate(anchor_coverages)
                      if value < ANCHOR_MIN_COVERAGE]
    anchor_oversized = [index for index, value in enumerate(anchor_coverages)
                        if value > ANCHOR_MAX_COVERAGE]
    anchor_overcontrast = [index for index, value in enumerate(anchor_neighborhood_evs)
                           if value is not None and value > ANCHOR_NEIGHBORHOOD_MAX_EV]
    if environment_missing and len(environment_missing) / len(frames) > 0.02:
        reasons.append("深蓝环境代理覆盖不足或跨帧不连续")
    if anchor_missing and len(anchor_missing) / len(frames) > 0.02:
        reasons.append("没有得到可保护的孤立灯点")
    if anchor_oversized:
        reasons.append("灯点面积过大，不能作为孤立锚点")
    if anchor_overcontrast and len(anchor_overcontrast) / len(frames) > 0.02:
        reasons.append("灯点与 5% 画幅邻域反差超过 2 挡")
    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-low-mid-cool-environment-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "environment_coverage": environment_coverages,
        "anchor_coverage": anchor_coverages,
        "anchor_neighborhood_ev": anchor_neighborhood_evs,
        "environment_mask_sha256": environment_hashes,
        "anchor_mask_sha256": anchor_hashes,
        "environment_missing_frames": environment_missing,
        "anchor_missing_frames": anchor_missing,
        "anchor_oversized_frames": anchor_oversized,
        "anchor_overcontrast_frames": anchor_overcontrast,
        "reasons": reasons,
        "boundary": (
            "环境层仅按逐帧低中亮度、低彩或偏冷关系近似，可能误纳天空、岩石或建筑；"
            "灯点层仅按小面积红色、暖象牙或近白高亮近似。"
            "不识别海面、地平线、负空间、船、人或真实光源，也不做光流与身份跟踪。"
        ),
    }


def capabilities_from_report(report: dict) -> set[str]:
    environment = Path((report.get("environment_mask_video") or {}).get("path") or "")
    anchor = Path((report.get("anchor_mask_video") or {}).get("path") or "")
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and environment.is_file() and anchor.is_file()):
        return {"deep-environment-approximation", "isolated-anchor-protection"}
    return set()


def build_layered_filter_complex(width: int | None = None,
                                 height: int | None = None) -> str:
    environment_mask = "format=gbrp,setpts=PTS-STARTPTS"
    anchor_mask = "format=gbrp,setpts=PTS-STARTPTS"
    memory_mask = "format=gbrp,setpts=PTS-STARTPTS"
    if width and height:
        scale = f"scale={int(width)}:{int(height)}:flags=bilinear,"
        environment_mask = scale + environment_mask
        anchor_mask = scale + anchor_mask
        memory_mask = scale + memory_mask
    return (
        "[0:v]format=gbrp,setpts=PTS-STARTPTS,split=2[blbase][blbaseprotect];"
        "[1:v]format=gbrp,setpts=PTS-STARTPTS[bldeep];"
        f"[2:v]{environment_mask}[blenvironmentmask];"
        "[blbase][bldeep][blenvironmentmask]maskedmerge=planes=7[blmerged];"
        f"[3:v]{anchor_mask}[blanchormask];"
        f"[4:v]{memory_mask}[blmemorymask];"
        "[blanchormask][blmemorymask]blend=all_mode=max[blprotectmask];"
        "[blmerged][blbaseprotect][blprotectmask]maskedmerge=planes=7,"
        "format=yuv420p[bldeepsea]"
    )


def deep_environment_filter(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    red = 1.0 - 0.18 * mix
    # Rec.709 加权下近似保持综合色亮度，避免“加深蓝色关系”退化成压暗。
    green = 1.0 + 0.04 * mix
    blue = 1.0 + 0.13 * mix
    return f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}"


def exact_frame_args(info: dict) -> list[str]:
    frame_count = int(info.get("nb_frames") or 0)
    if frame_count <= 0:
        raise DeepSeaGradeError("源视频没有可验证的帧数")
    return ["-frames:v", str(frame_count)]


def build_dynamic_deep_sea_masks(source: Path, environment_output: Path,
                                 anchor_output: Path, workdir: Path,
                                 analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_deep_sea_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    report["environment_mask_video"] = video_masks.assemble_mask_video(
        workdir / "masks" / "environment", environment_output,
        probe["fps_fraction"], len(frames))
    report["anchor_mask_video"] = video_masks.assemble_mask_video(
        workdir / "masks" / "anchor", anchor_output,
        probe["fps_fraction"], len(frames))
    return report


def render_deep_layer(base_video: Path, output: Path, strength: float) -> dict:
    if output.exists():
        raise DeepSeaGradeError(f"输出已存在，不会覆盖：{output}")
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(base_video), "-vf", deep_environment_filter(strength),
        "-map", "0:v:0", "-an", "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise DeepSeaGradeError(
            "深蓝环境层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def render_layered_video(base_video: Path, deep_video: Path,
                         environment_mask: Path, anchor_mask: Path,
                         memory_mask: Path, audio_source: Path,
                         output: Path) -> dict:
    if output.exists():
        raise DeepSeaGradeError(f"输出已存在，不会覆盖：{output}")
    info = video_masks._video_probe(audio_source)
    graph = build_layered_filter_complex(info["width"], info["height"])
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(base_video), "-i", str(deep_video),
        "-i", str(environment_mask), "-i", str(anchor_mask),
        "-i", str(memory_mask), "-i", str(audio_source),
        "-filter_complex", graph, "-map", "[bldeepsea]", "-map", "5:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "copy",
        *exact_frame_args(info), "-movflags", "+faststart", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise DeepSeaGradeError(
            "深海分层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {
        "path": str(output), "sha256": _sha256(output), "command": command,
        "filter_complex": graph, "probe": video_masks._video_probe(output),
    }
