#!/usr/bin/env python3
"""美食鲜亮的逐帧综合色彩分离研究链。

选区只来自亮度与 RGB 通道关系：它不识别食物、餐盘、手或人物。调用方必须先有
可靠题材观察；代理证据不足时阻断，不能退回全局加暖、加饱和或锐化。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import video_memory_protection as video_masks


SCHEMA_VERSION = "4.8.3-phase3a1"
FOOD_MIN_COVERAGE = 0.035
WHITE_REFERENCE_MIN_COVERAGE = 0.02
FOOD_MAX_COVERAGE = 0.65


class FoodVividGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask_bytes(frame: Path, width: int, height: int) -> tuple[bytes, bytes, bytes, dict]:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    food = bytearray(width * height)
    environment = bytearray(width * height)
    protect = bytearray(width * height)
    categories = {"white": 0, "black": 0, "skin_proxy": 0}

    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high, low = max(red, green, blue), min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0

        white = luma >= 0.68 and chroma <= 0.11
        black = luma <= 0.055
        skin_proxy = (
            0.18 <= luma <= 0.92
            and red > green > blue
            and 5 <= red - green <= 55
            and 3 <= green - blue <= 45
            and 0.06 <= chroma <= 0.30
        )
        if white or black or skin_proxy:
            protect[index] = 255
            if white:
                categories["white"] += 1
            if black:
                categories["black"] += 1
            if skin_proxy:
                categories["skin_proxy"] += 1
            continue

        green_food = (
            0.08 <= luma <= 0.90
            and chroma >= 0.12
            and green - red >= 6
            and green - blue >= 3
        )
        warm_food = (
            0.08 <= luma <= 0.92
            and chroma >= 0.16
            and red - green >= 22
            and red - blue >= 28
        )
        yellow_food = (
            0.12 <= luma <= 0.94
            and chroma >= 0.15
            and red >= green >= blue
            and red - blue >= 30
            and green - blue >= 20
        )
        if green_food or warm_food or yellow_food:
            food[index] = 255
        elif 0.075 < luma < 0.94:
            environment[index] = 255

    total = max(1, width * height)
    statistics = {
        "protection_components": {
            name: round(value / total, 6) for name, value in categories.items()
        },
        "boundary": (
            "暖红黄、绿色、环境与保护区均由逐帧亮度和 RGB 通道关系近似；"
            "肤色代理可能误纳浅木材，食物代理可能误纳彩色包装或桌布。"
        ),
    }
    return bytes(food), bytes(environment), bytes(protect), statistics


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def write_food_vivid_masks(frame: Path, food: Path, environment: Path,
                           protect: Path) -> dict:
    width, height = video_masks._dimensions(frame)
    food_data, environment_data, protect_data, statistics = _mask_bytes(
        frame, width, height)
    payloads = {
        "food": (food, food_data),
        "environment": (environment, environment_data),
        "protect": (protect, protect_data),
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
        "meaning": "逐帧食物综合色彩、环境退让与白位／黑位／肤色保护像素代理",
    }


def build_food_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise FoodVividGradeError("没有输入帧")
    names = ("food", "environment", "protect")
    for name in names:
        (mask_dir / name).mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    coverages = {name: [] for name in names}
    hashes = {name: [] for name in names}
    protection_components = {name: [] for name in ("white", "black", "skin_proxy")}

    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise FoodVividGradeError("输入帧尺寸不一致")
        paths = {name: mask_dir / name / f"mask-{index:06d}.pgm" for name in names}
        result = write_food_vivid_masks(frame, **paths)
        for name in names:
            coverages[name].append(result["coverage"][name])
            hashes[name].append(_sha256(paths[name]))
        for name in protection_components:
            protection_components[name].append(result["protection_components"][name])

    reasons = []
    food_missing = [index for index, value in enumerate(coverages["food"])
                    if value < FOOD_MIN_COVERAGE]
    food_oversized = [index for index, value in enumerate(coverages["food"])
                      if value > FOOD_MAX_COVERAGE]
    white_missing = [index for index, value in enumerate(protection_components["white"])
                     if value < WHITE_REFERENCE_MIN_COVERAGE]
    if food_missing and len(food_missing) / len(frames) > 0.08:
        reasons.append("可见食物综合色彩代理覆盖不足或跨帧不连续")
    if food_oversized:
        reasons.append("食物代理面积过大，无法证明主体与环境分离")
    if white_missing and len(white_missing) / len(frames) > 0.08:
        reasons.append("近中性白位／白盘证据不足或跨帧不连续")
    if max(coverages["environment"], default=0.0) < 0.03:
        reasons.append("环境退让层覆盖不足")

    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-food-color-separation-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "coverage": coverages,
        "protection_components": protection_components,
        "mask_sha256": hashes,
        "food_missing_frames": food_missing,
        "food_oversized_frames": food_oversized,
        "white_missing_frames": white_missing,
        "white_reference_min_coverage": WHITE_REFERENCE_MIN_COVERAGE,
        "reasons": reasons,
        "boundary": (
            "食物、环境、白盘、黑位和肤色均为像素代理，不是语义分割；"
            "可能误纳彩色包装、桌布、木材或浅暖器皿。必须与可靠题材观察共同使用。"
        ),
    }


def capabilities_from_report(report: dict) -> set[str]:
    paths = [Path((report.get(f"{name}_mask_video") or {}).get("path") or "")
             for name in ("food", "environment", "protect")]
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and all(path.is_file() for path in paths)):
        return {
            "food-color-separation-approximation",
            "neutral-white-protection",
            "temporal-skin-proxy-protection",
        }
    return set()


def selective_color_filters(strength: float) -> dict[str, str]:
    mix = max(0.0, min(1.0, float(strength)))
    food_saturation = 1.0 + 0.50 * mix
    environment_saturation = 1.0 - 0.24 * mix
    return {
        "food": f"eq=saturation={food_saturation:.6f}",
        "environment": f"eq=saturation={environment_saturation:.6f}",
    }


def filter_saturation(filter_chain: str) -> float:
    marker = "saturation="
    if marker not in filter_chain:
        raise FoodVividGradeError("滤镜链缺少 saturation")
    return float(filter_chain.split(marker, 1)[1].split(":", 1)[0])


def masked_chroma_metrics(frames: list[Path], food_masks: list[Path],
                          environment_masks: list[Path]) -> dict:
    """用同一组逐帧代理蒙版测量主体／环境综合色彩，不把覆盖率变化当增彩。"""
    if not frames or not (len(frames) == len(food_masks) == len(environment_masks)):
        raise FoodVividGradeError("综合色彩测量的帧与蒙版数量不一致")
    totals = {"whole": 0.0, "food": 0.0, "environment": 0.0}
    counts = {"whole": 0, "food": 0, "environment": 0}
    for frame, food_mask, environment_mask in zip(
            frames, food_masks, environment_masks):
        width, height = video_masks._dimensions(frame)
        rgb = video_masks._read_raw(frame, "rgb24", width, height)
        food = read_gray_mask(food_mask, width, height)
        environment = read_gray_mask(environment_mask, width, height)
        for index in range(width * height):
            offset = index * 3
            red, green, blue = rgb[offset:offset + 3]
            chroma = (max(red, green, blue) - min(red, green, blue)) / 255.0
            totals["whole"] += chroma
            counts["whole"] += 1
            if food[index] >= 128:
                totals["food"] += chroma
                counts["food"] += 1
            if environment[index] >= 128:
                totals["environment"] += chroma
                counts["environment"] += 1
    if not counts["food"] or not counts["environment"]:
        raise FoodVividGradeError("食物或环境代理没有可测像素")
    means = {name: totals[name] / counts[name] for name in totals}
    return {
        "whole_chroma": round(means["whole"], 6),
        "food_chroma": round(means["food"], 6),
        "environment_chroma": round(means["environment"], 6),
        "food_environment_ratio": round(
            means["food"] / max(means["environment"], 1e-9), 6),
        "sample_pixels": counts,
        "boundary": "RGB 极差综合色彩代理；用于同素材同蒙版单调性，不等同于感知色彩模型或审美结论",
    }


def build_layered_filter_complex(width: int | None = None,
                                 height: int | None = None) -> str:
    mask = "format=gbrp,setpts=PTS-STARTPTS"
    if width and height:
        mask = f"scale={int(width)}:{int(height)}:flags=bilinear," + mask
    return (
        "[0:v]format=gbrp,setpts=PTS-STARTPTS,split=2[blbase][blbaseprotect];"
        "[1:v]format=gbrp,setpts=PTS-STARTPTS[blfood];"
        "[2:v]format=gbrp,setpts=PTS-STARTPTS[blenvironment];"
        f"[3:v]{mask}[blfoodmask];"
        "[blbase][blfood][blfoodmask]maskedmerge=planes=7[blfoodmerged];"
        f"[4:v]{mask}[blenvironmentmask];"
        "[blfoodmerged][blenvironment][blenvironmentmask]maskedmerge=planes=7[blseparated];"
        f"[5:v]{mask}[blprotectmask];"
        "[blseparated][blbaseprotect][blprotectmask]maskedmerge=planes=7,"
        "format=yuv420p[blfoodvivid]"
    )


def exact_frame_args(info: dict) -> list[str]:
    frame_count = int(info.get("nb_frames") or 0)
    if frame_count <= 0:
        raise FoodVividGradeError("源视频没有可验证的帧数")
    return ["-frames:v", str(frame_count)]


def build_dynamic_food_masks(source: Path, outputs: dict[str, Path],
                             workdir: Path, analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_food_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    for name in ("food", "environment", "protect"):
        report[f"{name}_mask_video"] = video_masks.assemble_mask_video(
            workdir / "masks" / name, outputs[name],
            probe["fps_fraction"], len(frames))
    return report


def render_color_layer(base_video: Path, output: Path, filter_chain: str) -> dict:
    if output.exists():
        raise FoodVividGradeError(f"输出已存在，不会覆盖：{output}")
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
        raise FoodVividGradeError(
            "美食分层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def _has_audio(path: Path) -> bool:
    result = subprocess.run([
        shutil.which("ffprobe") or "ffprobe", "-v", "error",
        "-select_streams", "a:0", "-show_entries", "stream=index",
        "-of", "csv=p=0", str(path),
    ], capture_output=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def render_layered_video(base_video: Path, food_video: Path,
                         environment_video: Path, mask_videos: dict[str, Path],
                         output: Path, source_info: dict) -> dict:
    if output.exists():
        raise FoodVividGradeError(f"输出已存在，不会覆盖：{output}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n"]
    for path in (base_video, food_video, environment_video,
                 mask_videos["food"], mask_videos["environment"],
                 mask_videos["protect"]):
        command.extend(["-i", str(path)])
    command.extend([
        "-filter_complex", build_layered_filter_complex(
            source_info.get("width"), source_info.get("height")),
        "-map", "[blfoodvivid]",
    ])
    audio = _has_audio(base_video)
    if audio:
        command.extend(["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"])
    command.extend([
        *exact_frame_args(source_info), "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-shortest", str(output),
    ])
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise FoodVividGradeError(
            "美食分层合成失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {
        "path": str(output), "sha256": _sha256(output), "command": command,
        "audio_preserved": audio,
    }


def probe(path: Path) -> dict:
    return video_masks._video_probe(path)


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
