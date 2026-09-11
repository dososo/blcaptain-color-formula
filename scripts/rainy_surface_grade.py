#!/usr/bin/env python3
"""雨夜蓝绿的逐帧反射近似与暖光保护研究链。

湿面选择使用画面位置、综合色度、亮度与冷色关系的启发式；它不是干湿表面语义分割。
当真实画面不产生足够湿面或暖光证据时，资格门阻断，而不是退回全局毒蓝。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

try:
    from . import video_memory_protection as video_masks
except ImportError:
    import video_memory_protection as video_masks


SCHEMA_VERSION = "4.8.3-phase2e"
LOWER_FRAME_START = 0.45
WET_MIN_COVERAGE = 0.01
REFLECTION_SEED_MIN_COVERAGE = 0.002
WARM_MIN_COVERAGE = 0.0005


class RainySurfaceGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask_bytes(frame: Path, width: int, height: int) -> tuple[bytes, bytes, float]:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    wet = bytearray(width * height)
    warm = bytearray(width * height)
    road_candidates = bytearray(width * height)
    reflection_seeds = 0
    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high = max(red, green, blue)
        low = min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0
        warm_pixel = (
            0.25 <= luma <= 0.98
            and red - green >= 12
            and red - blue >= 30
        )
        if warm_pixel:
            warm[index] = 255
            continue
        row = index // width
        lower_frame = row / max(1, height - 1) >= LOWER_FRAME_START
        cool_reflection = ((green + blue) / 2.0 - red) >= 8 and chroma >= 0.05
        neutral_reflection = luma >= 0.55 and chroma <= 0.08
        if lower_frame and 0.12 <= luma <= 0.90 and (cool_reflection or neutral_reflection):
            reflection_seeds += 1
        if lower_frame and 0.08 <= luma <= 0.90:
            road_candidates[index] = 255
    total = max(1, width * height)
    seed_coverage = reflection_seeds / total
    if seed_coverage >= REFLECTION_SEED_MIN_COVERAGE:
        wet[:] = road_candidates
    return bytes(wet), bytes(warm), round(seed_coverage, 6)


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def write_reflection_masks(frame: Path, wet_output: Path, warm_output: Path) -> dict:
    width, height = video_masks._dimensions(frame)
    wet, warm, seed_coverage = _mask_bytes(frame, width, height)
    video_masks._write_pgm(wet_output, width, height, wet)
    video_masks._write_pgm(warm_output, width, height, warm)
    total = max(1, width * height)
    return {
        "wet_path": str(wet_output),
        "warm_path": str(warm_output),
        "wet_coverage": round(sum(value >= 128 for value in wet) / total, 6),
        "warm_coverage": round(sum(value >= 128 for value in warm) / total, 6),
        "reflection_seed_coverage": seed_coverage,
        "meaning": "逐帧反光种子约束的下部湿路代理；不是干湿表面语义分割",
    }


def build_reflection_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise RainySurfaceGradeError("没有输入帧")
    wet_dir = mask_dir / "wet"
    warm_dir = mask_dir / "warm"
    wet_dir.mkdir(parents=True, exist_ok=True)
    warm_dir.mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    wet_coverages = []
    warm_coverages = []
    seed_coverages = []
    wet_hashes = []
    warm_hashes = []
    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise RainySurfaceGradeError("输入帧尺寸不一致")
        wet_path = wet_dir / f"mask-{index:06d}.pgm"
        warm_path = warm_dir / f"mask-{index:06d}.pgm"
        result = write_reflection_masks(frame, wet_path, warm_path)
        wet_coverages.append(result["wet_coverage"])
        warm_coverages.append(result["warm_coverage"])
        seed_coverages.append(result["reflection_seed_coverage"])
        wet_hashes.append(_sha256(wet_path))
        warm_hashes.append(_sha256(warm_path))

    wet_missing = [index for index, value in enumerate(wet_coverages)
                   if value < WET_MIN_COVERAGE]
    reasons = []
    seed_missing = [index for index, value in enumerate(seed_coverages)
                    if value < REFLECTION_SEED_MIN_COVERAGE]
    if seed_missing and len(seed_missing) / len(frames) > 0.02:
        reasons.append("反光种子覆盖不足或跨帧不连续")
    if wet_missing and len(wet_missing) / len(frames) > 0.02:
        reasons.append("湿面反射近似覆盖不足或跨帧不连续")
    if max(warm_coverages, default=0.0) < WARM_MIN_COVERAGE:
        reasons.append("没有得到可保护的窄域暖光锚点")
    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-lower-reflection-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "wet_coverage": wet_coverages,
        "warm_coverage": warm_coverages,
        "reflection_seed_coverage": seed_coverages,
        "wet_mask_sha256": wet_hashes,
        "warm_mask_sha256": warm_hashes,
        "wet_missing_frames": wet_missing,
        "reflection_seed_missing_frames": seed_missing,
        "reasons": reasons,
        "boundary": (
            "先用画面下部的亮度、综合色度与冷色关系确认逐帧反光种子，"
            "再传播到同帧下部非纯黑道路代理；不是干湿表面语义分割，"
            "可能误纳下部干燥路面或物体。"
            "暖光只按逐帧暖色高亮条件保护；不做光流、身份跟踪或跨镜头传播。"
        ),
    }


def capabilities_from_report(report: dict) -> set[str]:
    wet = Path((report.get("wet_mask_video") or {}).get("path") or "")
    warm = Path((report.get("warm_mask_video") or {}).get("path") or "")
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and wet.is_file() and warm.is_file()):
        return {"wet-surface-reflection-approximation"}
    return set()


def build_layered_filter_complex(width: int | None = None,
                                 height: int | None = None,
                                 fps_fraction: str = "25/1") -> str:
    fps = Fraction(fps_fraction)
    if fps <= 0:
        raise RainySurfaceGradeError("帧率必须为正数")
    clock = f"settb=AVTB,setpts=N*{fps.denominator}/({fps.numerator}*TB)"
    wet_mask = f"format=gbrp,{clock}"
    warm_mask = f"format=gbrp,{clock}"
    memory_mask = f"format=gbrp,{clock}"
    if width and height:
        scale = f"scale={int(width)}:{int(height)}:flags=bilinear,"
        wet_mask = scale + wet_mask
        warm_mask = scale + warm_mask
        memory_mask = scale + memory_mask
    return (
        f"[0:v]format=gbrp,{clock},split=2[blbase][blbasewarm];"
        f"[1:v]format=gbrp,{clock}[blwet];"
        f"[2:v]{wet_mask}[blwetmask];"
        "[blbase][blwet][blwetmask]maskedmerge=planes=7[blwetmerged];"
        f"[3:v]{warm_mask}[blwarmmask];"
        f"[4:v]{memory_mask}[blmemorymask];"
        "[blwarmmask][blmemorymask]blend=all_mode=max[blprotectmask];"
        "[blwetmerged][blbasewarm][blprotectmask]maskedmerge=planes=7,"
        "format=yuv420p[blrainy]"
    )


def wet_teal_filter(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    red = 1.0 - 0.22 * mix
    green = 1.0 + 0.07 * mix
    blue = 1.0 + 0.16 * mix
    return f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}"


def build_dynamic_reflection_masks(source: Path, wet_output: Path, warm_output: Path,
                                   workdir: Path, analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_reflection_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    report["wet_mask_video"] = video_masks.assemble_mask_video(
        workdir / "masks" / "wet", wet_output, probe["fps_fraction"], len(frames))
    report["warm_mask_video"] = video_masks.assemble_mask_video(
        workdir / "masks" / "warm", warm_output, probe["fps_fraction"], len(frames))
    return report


def render_wet_layer(base_video: Path, output: Path, strength: float) -> dict:
    if output.exists():
        raise RainySurfaceGradeError(f"输出已存在，不会覆盖：{output}")
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(base_video), "-vf", wet_teal_filter(strength),
        "-map", "0:v:0", "-an", "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise RainySurfaceGradeError(
            "湿面层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def render_layered_video(base_video: Path, wet_video: Path, wet_mask: Path,
                         warm_mask: Path, memory_mask: Path,
                         audio_source: Path, output: Path) -> dict:
    if output.exists():
        raise RainySurfaceGradeError(f"输出已存在，不会覆盖：{output}")
    info = video_masks._video_probe(audio_source)
    graph = build_layered_filter_complex(info["width"], info["height"], info["fps_fraction"])
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(base_video), "-i", str(wet_video), "-i", str(wet_mask),
        "-i", str(warm_mask), "-i", str(memory_mask), "-i", str(audio_source),
        "-filter_complex", graph, "-map", "[blrainy]", "-map", "5:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "copy",
        "-movflags", "+faststart", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise RainySurfaceGradeError(
            "雨夜分层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {
        "path": str(output), "sha256": _sha256(output), "command": command,
        "filter_complex": graph, "probe": video_masks._video_probe(output),
    }
