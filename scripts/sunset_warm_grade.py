#!/usr/bin/env python3
"""日落暖金的逐帧暖色可达区与冷锚保护研究链。

所有选区只来自逐帧位置、亮度与 RGB 通道关系。它不识别太阳、天空、人物、
肤色、地平线或受光轮廓；证据不足时必须阻断，不能退回整幅升温或加橙。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import video_memory_protection as video_masks
import visual_density_gate


SCHEMA_VERSION = "4.8.3-phase3c1"
WARM_MIN_COVERAGE = 0.025
WARM_MAX_COVERAGE = 0.32
# 首段真实素材的连续冷青天顶覆盖约 5.1%；4.5% 留出编码与微运动余量，
# 仍要求逐帧存在，不能把只有暖色的画面放行。
COOL_MIN_COVERAGE = 0.045
PROTECT_MAX_COVERAGE = 0.72


class SunsetWarmGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask_bytes(frame: Path, width: int, height: int) -> tuple[bytes, bytes, bytes, dict]:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    warm = bytearray(width * height)
    cool = bytearray(width * height)
    protect = bytearray(width * height)
    components = {"black": 0, "white": 0}

    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high, low = max(red, green, blue), min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0
        vertical = (index // width) / max(1, height - 1)

        black = luma <= 0.055
        white = luma >= 0.84 and chroma <= 0.075
        if black or white:
            protect[index] = 255
            components["black" if black else "white"] += 1
            continue

        # 暖色可达区：只承认画面本来已有的金／橙关系，并限制在上方至近地平线区域。
        # 这会纳入暖云、墙面或木材，因此必须与可靠日落观察共同使用。
        warm_reach = (
            vertical <= 0.78
            and 0.12 <= luma <= 0.84
            and chroma >= 0.075
            and red - blue >= 18
            and red - green >= 6
            and green - blue >= 6
        )
        # 冷锚：蓝／青占优且不是极暗或白核的区域。只恢复 Foundation，不额外染蓝。
        cool_anchor = (
            vertical <= 0.82
            and 0.075 <= luma <= 0.80
            and blue - red >= 8
            and blue - green >= 2
        )
        if warm_reach:
            warm[index] = 255
        elif cool_anchor:
            cool[index] = 255

    total = max(1, width * height)
    return bytes(warm), bytes(cool), bytes(protect), {
        "protection_components": {
            name: round(value / total, 6) for name, value in components.items()
        },
        "boundary": (
            "暖色可达区、冷锚、黑位与近中性白位均由逐帧位置、亮度和 RGB 通道关系近似；"
            "可能误纳暖云、墙面、木材、蓝色服装或水面，不是太阳、天空、人物、肤色、"
            "地平线或受光轮廓语义识别。"
        ),
    }


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def write_sunset_masks(frame: Path, warm: Path, cool: Path, protect: Path) -> dict:
    width, height = video_masks._dimensions(frame)
    warm_data, cool_data, protect_data, statistics = _mask_bytes(frame, width, height)
    payloads = {
        "warm": (warm, warm_data),
        "cool": (cool, cool_data),
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
        "meaning": "逐帧暖色可达、冷锚与黑位／白位保护像素代理",
    }


def build_sunset_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise SunsetWarmGradeError("没有输入帧")
    names = ("warm", "cool", "protect")
    for name in names:
        (mask_dir / name).mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    coverages = {name: [] for name in names}
    hashes = {name: [] for name in names}
    components = {name: [] for name in ("black", "white")}

    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise SunsetWarmGradeError("输入帧尺寸不一致")
        paths = {name: mask_dir / name / f"mask-{index:06d}.pgm" for name in names}
        result = write_sunset_masks(frame, **paths)
        for name in names:
            coverages[name].append(result["coverage"][name])
            hashes[name].append(_sha256(paths[name]))
        for name in components:
            components[name].append(result["protection_components"][name])

    reasons = []
    warm_missing = [index for index, value in enumerate(coverages["warm"])
                    if value < WARM_MIN_COVERAGE]
    warm_oversized = [index for index, value in enumerate(coverages["warm"])
                      if value > WARM_MAX_COVERAGE]
    cool_missing = [index for index, value in enumerate(coverages["cool"])
                    if value < COOL_MIN_COVERAGE]
    protect_oversized = [index for index, value in enumerate(coverages["protect"])
                         if value > PROTECT_MAX_COVERAGE]
    tolerated = int(len(frames) * 0.02)
    if len(warm_missing) > tolerated:
        reasons.append("暖色可达区覆盖不足或跨帧不连续")
    if warm_oversized:
        reasons.append("暖色可达区面积过大，已退化成整幅推暖")
    if len(cool_missing) > tolerated:
        reasons.append("冷锚覆盖不足或跨帧不连续")
    if protect_oversized:
        reasons.append("黑位／白位保护面积过大，素材缺少可调空间")

    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-warm-reach-and-cool-anchor-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "coverage": coverages,
        "protection_components": components,
        "mask_sha256": hashes,
        "warm_missing_frames": warm_missing,
        "warm_oversized_frames": warm_oversized,
        "cool_missing_frames": cool_missing,
        "protect_oversized_frames": protect_oversized,
        "reasons": reasons,
        "boundary": (
            "只证明同一段素材内暖色与冷色像素关系可逐帧分离；不识别太阳、天空、人物、"
            "肤色、地平线、真实光线传播或受光轮廓，也不做光流与身份跟踪。"
        ),
    }


def capabilities_from_report(report: dict) -> set[str]:
    paths = [Path((report.get(f"{name}_mask_video") or {}).get("path") or "")
             for name in ("warm", "cool", "protect")]
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and all(path.is_file() for path in paths)):
        return {"temporal-warm-reach-approximation",
                "temporal-cool-anchor-protection"}
    return set()


def warm_reach_gains(strength: float) -> tuple[float, float, float]:
    mix = max(0.0, min(1.0, float(strength)))
    amount = 0.22 * mix
    raw = (1.0 + amount, 1.0 + 0.12 * amount, 1.0 - 0.58 * amount)
    luma = 0.2126 * raw[0] + 0.7152 * raw[1] + 0.0722 * raw[2]
    return tuple(value / luma for value in raw)


def warm_reach_filter(strength: float) -> str:
    red, green, blue = warm_reach_gains(strength)
    contrast = 1.0 + 0.045 * max(0.0, min(1.0, float(strength)))
    return (f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f},"
            f"eq=contrast={contrast:.6f}:brightness=0:saturation=1")


def sunset_density_foundation_filter() -> str:
    """日落剪影本身就是成立的显示参照场景，Foundation 不统一提亮。"""
    return "null"


def density_first_warm_gains(strength: float) -> tuple[float, float, float]:
    """增加金橙分离同时保留亮度密度，不用等亮度归一化洗浅暖区。"""
    mix = max(0.0, min(1.0, float(strength)))
    # 只抵消通道增益带来的亮度漂移，不主动压暗暖区；综合色彩负责增加密度，
    # 光带与冷锚之间的原始亮度关系必须保留。
    density = 1.0 - 0.02 * mix
    return (
        density * (1.0 + 0.20 * mix),
        density * (1.0 - 0.02 * mix),
        density * (1.0 - 0.16 * mix),
    )


def density_first_warm_filter(strength: float) -> str:
    red, green, blue = density_first_warm_gains(strength)
    return f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}"


def strength_advice(strength_percent: int, passed: bool) -> str:
    if passed:
        return "保持当前强度并进入人工连续回放。"
    if int(strength_percent) >= 80:
        return "80% 已失败：回退到 55% 或 Foundation，禁止继续加力。"
    return "回退一档，先检查暖色面积、冷锚和天空梯度，再决定是否重做。"


def masked_color_metrics(frames: list[Path], warm_masks: list[Path],
                         cool_masks: list[Path], protect_masks: list[Path]) -> dict:
    """用固定逐帧代理测暖冷分离；覆盖率变化不能冒充调色变化。"""
    if not frames or not (len(frames) == len(warm_masks) == len(cool_masks)
                          == len(protect_masks)):
        raise SunsetWarmGradeError("综合色彩测量的帧与蒙版数量不一致")
    totals = {name: {
        "warmth": 0.0,
        "chroma": 0.0,
        "luma": 0.0,
        "density": 0.0,
        "red_green": 0.0,
        "green_blue": 0.0,
    }
              for name in ("whole", "warm", "cool", "protect")}
    counts = {name: 0 for name in totals}
    for frame, warm_mask, cool_mask, protect_mask in zip(
            frames, warm_masks, cool_masks, protect_masks):
        width, height = video_masks._dimensions(frame)
        rgb = video_masks._read_raw(frame, "rgb24", width, height)
        masks = {
            "warm": read_gray_mask(warm_mask, width, height),
            "cool": read_gray_mask(cool_mask, width, height),
            "protect": read_gray_mask(protect_mask, width, height),
        }
        for index in range(width * height):
            offset = index * 3
            red, green, blue = rgb[offset:offset + 3]
            values = {
                "warmth": (red - blue) / 255.0,
                "chroma": (max(red, green, blue) - min(red, green, blue)) / 255.0,
                "luma": (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0,
                "red_green": (red - green) / 255.0,
                "green_blue": (green - blue) / 255.0,
            }
            values["density"] = values["chroma"] * (1.0 - values["luma"])
            for key, value in values.items():
                totals["whole"][key] += value
            counts["whole"] += 1
            for name, mask in masks.items():
                if mask[index] >= 128:
                    for key, value in values.items():
                        totals[name][key] += value
                    counts[name] += 1
    if not counts["warm"] or not counts["cool"] or not counts["protect"]:
        raise SunsetWarmGradeError("暖区、冷锚或保护区没有可测像素")
    means = {
        name: {key: round(value / counts[name], 6)
               for key, value in totals[name].items()}
        for name in totals
    }
    return {
        "regions": means,
        "warm_cool_separation": round(
            means["warm"]["warmth"] - means["cool"]["warmth"], 6),
        "warm_cool_luma_contrast": round(
            abs(means["warm"]["luma"] - means["cool"]["luma"]), 6),
        "sample_pixels": counts,
        "boundary": (
            "固定逐帧 RGB 极差、通道差、亮度与彩度×暗度代理；用于揭露增彩却洗浅，"
            "不等于感知色彩模型、语义理解或审美结论"
        ),
    }


def evaluate_visual_density(baseline: dict, candidate: dict,
                            strength_percent: int) -> dict:
    return visual_density_gate.evaluate_region_density(
        baseline, candidate, strength_percent, focus_region="warm")


def build_layered_filter_complex(width: int | None = None,
                                 height: int | None = None) -> str:
    clock = "settb=AVTB,setpts=N/FRAME_RATE/TB"
    mask = "format=gbrp," + clock
    if width and height:
        mask = f"scale={int(width)}:{int(height)}:flags=bilinear," + mask
    return (
        f"[0:v]format=gbrp,{clock},split=2[blbase][blbaseprotect];"
        f"[1:v]format=gbrp,{clock}[blwarm];"
        f"[2:v]{mask}[blwarmmask];"
        "[blbase][blwarm][blwarmmask]maskedmerge=planes=7[blwarmed];"
        f"[3:v]{mask}[blcoolmask];"
        f"[4:v]{mask}[blprotectmask];"
        "[blcoolmask][blprotectmask]blend=all_mode=max[blrestoremask];"
        "[blwarmed][blbaseprotect][blrestoremask]maskedmerge=planes=7,"
        "format=yuv420p[blsunset]"
    )


def exact_frame_args(info: dict) -> list[str]:
    frame_count = int(info.get("nb_frames") or 0)
    if frame_count <= 0:
        raise SunsetWarmGradeError("源视频没有可验证的帧数")
    return ["-frames:v", str(frame_count)]


def build_dynamic_sunset_masks(source: Path, outputs: dict[str, Path],
                               workdir: Path, analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_sunset_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    for name in ("warm", "cool", "protect"):
        report[f"{name}_mask_video"] = video_masks.assemble_mask_video(
            workdir / "masks" / name, outputs[name],
            probe["fps_fraction"], len(frames))
    return report


def render_warm_layer(base_video: Path, output: Path, strength: float,
                      density_first: bool = False) -> dict:
    if output.exists():
        raise SunsetWarmGradeError(f"输出已存在，不会覆盖：{output}")
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(base_video), "-vf", (
            density_first_warm_filter(strength)
            if density_first else warm_reach_filter(strength)),
        "-map", "0:v:0", "-an", "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise SunsetWarmGradeError(
            "日落暖层渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def _has_audio(path: Path) -> bool:
    result = subprocess.run([
        shutil.which("ffprobe") or "ffprobe", "-v", "error",
        "-select_streams", "a:0", "-show_entries", "stream=index",
        "-of", "csv=p=0", str(path),
    ], capture_output=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def render_layered_video(base_video: Path, warm_video: Path,
                         mask_videos: dict[str, Path], output: Path,
                         source_info: dict) -> dict:
    if output.exists():
        raise SunsetWarmGradeError(f"输出已存在，不会覆盖：{output}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n"]
    for path in (base_video, warm_video, mask_videos["warm"],
                 mask_videos["cool"], mask_videos["protect"]):
        command.extend(["-i", str(path)])
    command.extend([
        "-filter_complex", build_layered_filter_complex(
            source_info.get("width"), source_info.get("height")),
        "-map", "[blsunset]",
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
        raise SunsetWarmGradeError(
            "日落暖金分层合成失败：" + result.stderr.decode("utf-8", "replace").strip())
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
