#!/usr/bin/env python3
"""奶油柔光的逐帧肩部、暖中间调与静态几何塑光研究链。

本模块只依据亮度与 RGB 关系定位“未剪切、低彩的高光肩部”和“已有暖色中间调”。
经人工确认当前镜头构图后，还可用固定椭圆选择中间调，组织观看路径。它不识别人脸、
肤色、衣服、墙面或植物，也不跟踪主体；题材、光线与记忆色仍必须由可靠观察和人工
回放确认。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import video_memory_protection as video_masks
import cream_shadow_check


SCHEMA_VERSION = "4.8.3-phase3b1-v2d"
SHOULDER_MIN_COVERAGE = 0.01
SHOULDER_MAX_COVERAGE = 0.45
DARK_ANCHOR_MIN_COVERAGE = 0.003
ATTENTION_MIN_COVERAGE = 0.01
ATTENTION_MAX_COVERAGE = 0.50
ATTENTION_CENTER = (0.40, 0.52)
ATTENTION_RADIUS = (0.32, 0.46)


class CreamSoftGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask_bytes(frame: Path, width: int, height: int) -> tuple:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    shoulder = bytearray(width * height)
    warm_midtone = bytearray(width * height)
    neutral_midtone = bytearray(width * height)
    attention_midtone = bytearray(width * height)
    attention_holdout = bytearray(width * height)
    protect = bytearray(width * height)
    dark_pixels = 0
    protected_shadow_pixels = 0

    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high, low = max(red, green, blue), min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0
        x = (index % width + 0.5) / width
        y = (index // width + 0.5) / height
        inside_attention = (
            ((x - ATTENTION_CENTER[0]) / ATTENTION_RADIUS[0]) ** 2
            + ((y - ATTENTION_CENTER[1]) / ATTENTION_RADIUS[1]) ** 2
            <= 1.0
        )

        dark = luma <= 0.055
        clipped_white_core = luma >= 0.97
        highlight_shoulder = 0.72 <= luma < 0.97 and chroma <= 0.11
        existing_warm_midtone = (
            0.20 <= luma < 0.72
            and 0.025 <= chroma <= 0.24
            and red >= green >= blue
            and red - green >= 2
            and green - blue >= 1
        )
        neutral_measure = 0.18 <= luma <= 0.65 and chroma <= 0.06
        attention_candidate = (
            inside_attention
            and 0.12 <= luma <= 0.68
            and chroma <= 0.24
            and not dark
        )
        attention_holdout_candidate = (
            not inside_attention
            and 0.18 <= luma <= 0.65
            and chroma <= 0.06
        )
        if highlight_shoulder:
            shoulder[index] = 255
        if existing_warm_midtone:
            warm_midtone[index] = 255
        if attention_candidate:
            attention_midtone[index] = 255
        if not (highlight_shoulder or existing_warm_midtone or attention_candidate):
            # 只有高光肩部、已有暖色中间调和确认的静态几何中间调可以变化。
            protect[index] = 255
        if neutral_measure and not existing_warm_midtone:
            neutral_midtone[index] = 255
        if attention_holdout_candidate and not existing_warm_midtone:
            attention_holdout[index] = 255
        if dark:
            dark_pixels += 1
        if clipped_white_core:
            protect[index] = 255
        if luma < 0.12 and protect[index] == 255:
            protected_shadow_pixels += 1

    total = max(1, width * height)
    return (
        bytes(shoulder), bytes(warm_midtone), bytes(neutral_midtone),
        bytes(attention_midtone), bytes(attention_holdout), bytes(protect), {
        "dark_anchor": round(dark_pixels / total, 6),
        "protected_shadow_reference": round(protected_shadow_pixels / total, 6),
        "boundary": (
            "高光肩部由 L=0.72-0.97 的低彩像素近似；暖中间调由 L=0.20-0.72、"
            "红不低于绿且绿不低于蓝的已有暖色像素近似；注意力中间调是本镜头经确认的"
            "L1 静态几何椭圆，不是人物、肤色、物体语义分割或跨帧主体跟踪。"
        ),
    })


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def write_cream_soft_masks(frame: Path, shoulder: Path, protect: Path,
                           midtone: Path | None = None,
                           warm_midtone: Path | None = None,
                           neutral_midtone: Path | None = None,
                           attention_midtone: Path | None = None,
                           attention_holdout: Path | None = None) -> dict:
    width, height = video_masks._dimensions(frame)
    (shoulder_data, warm_midtone_data, neutral_midtone_data,
     attention_midtone_data, attention_holdout_data,
     protect_data, statistics) = _mask_bytes(frame, width, height)
    payloads = {"shoulder": (shoulder, shoulder_data), "protect": (protect, protect_data)}
    if midtone is not None:
        payloads["midtone"] = (midtone, neutral_midtone_data)
    if warm_midtone is not None:
        payloads["warm_midtone"] = (warm_midtone, warm_midtone_data)
    if neutral_midtone is not None:
        payloads["neutral_midtone"] = (neutral_midtone, neutral_midtone_data)
    if attention_midtone is not None:
        payloads["attention_midtone"] = (attention_midtone, attention_midtone_data)
    if attention_holdout is not None:
        payloads["attention_holdout"] = (attention_holdout, attention_holdout_data)
    total = max(1, width * height)
    coverage = {}
    for name, (path, data) in payloads.items():
        video_masks._write_pgm(path, width, height, data)
        coverage[name] = round(sum(value >= 128 for value in data) / total, 6)
    return {
        "paths": {name: str(path) for name, (path, _) in payloads.items()},
        "coverage": coverage,
        **statistics,
        "meaning": "逐帧高光肩部、已有暖中间调、静态几何中间调与 Foundation 恢复代理",
    }


def build_cream_soft_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise CreamSoftGradeError("没有输入帧")
    names = (
        "shoulder", "warm_midtone", "neutral_midtone",
        "attention_midtone", "attention_holdout", "protect",
    )
    for name in names:
        (mask_dir / name).mkdir(parents=True, exist_ok=True)
    dimensions = video_masks._dimensions(frames[0])
    coverages = {name: [] for name in names}
    hashes = {name: [] for name in names}
    dark_anchors = []
    shadow_references = []

    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise CreamSoftGradeError("输入帧尺寸不一致")
        paths = {name: mask_dir / name / f"mask-{index:06d}.pgm" for name in names}
        result = write_cream_soft_masks(frame, **paths)
        for name in names:
            coverages[name].append(result["coverage"][name])
            hashes[name].append(_sha256(paths[name]))
        dark_anchors.append(result["dark_anchor"])
        shadow_references.append(result["protected_shadow_reference"])

    shoulder_missing = [index for index, value in enumerate(coverages["shoulder"])
                        if value < SHOULDER_MIN_COVERAGE]
    shoulder_oversized = [index for index, value in enumerate(coverages["shoulder"])
                          if value > SHOULDER_MAX_COVERAGE]
    dark_missing = [index for index, value in enumerate(dark_anchors)
                    if value < DARK_ANCHOR_MIN_COVERAGE]
    shadow_missing = [index for index, value in enumerate(shadow_references)
                      if value < DARK_ANCHOR_MIN_COVERAGE]
    attention_missing = [index for index, value in enumerate(coverages["attention_midtone"])
                         if value < ATTENTION_MIN_COVERAGE]
    attention_oversized = [index for index, value in enumerate(coverages["attention_midtone"])
                           if value > ATTENTION_MAX_COVERAGE]
    reasons = []
    if shoulder_missing and len(shoulder_missing) / len(frames) > 0.08:
        reasons.append("高光肩部代理覆盖不足或跨帧不连续")
    if shoulder_oversized:
        reasons.append("高光肩部代理面积过大，无法证明定向柔光")
    if shadow_missing and len(shadow_missing) / len(frames) > 0.08:
        reasons.append("深色锚点／保护阴影参考不足，无法验证阴影保持中性且不抬灰")
    if attention_missing and len(attention_missing) / len(frames) > 0.08:
        reasons.append("静态几何注意力中间调覆盖不足或跨帧不连续")
    if attention_oversized:
        reasons.append("静态几何注意力中间调面积过大，无法证明局部塑光")
    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "per-frame-shoulder-warm-midtone-and-static-attention-approximation",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "coverage": coverages,
        "dark_anchor_coverage": dark_anchors,
        "shadow_reference_coverage": shadow_references,
        "shadow_reference_missing_frames": shadow_missing,
        "warnings": ["深黑面积不足；必须通过同源保护阴影输出检查，不能沿用历史暗锚通过"] if dark_missing else [],
        "mask_sha256": hashes,
        "shoulder_missing_frames": shoulder_missing,
        "shoulder_oversized_frames": shoulder_oversized,
        "dark_anchor_missing_frames": dark_missing,
        "attention_missing_frames": attention_missing,
        "attention_oversized_frames": attention_oversized,
        "dark_anchor_min_coverage": DARK_ANCHOR_MIN_COVERAGE,
        "attention_geometry": {
            "kind": "confirmed-static-ellipse-l1",
            "center": list(ATTENTION_CENTER),
            "radius": list(ATTENTION_RADIUS),
            "luma_range": [0.12, 0.68],
            "max_chroma": 0.24,
        },
        "reasons": reasons,
        "boundary": (
            "高光肩部、暖中间调、深色锚点和中性测量区均为亮度／RGB 像素代理；"
            "注意力层是当前固定构图经确认的 L1 静态椭圆，不是语义分割或主体跟踪。"
        ),
    }


def capabilities_from_report(report: dict) -> set[str]:
    paths = [Path((report.get(f"{name}_mask_video") or {}).get("path") or "")
             for name in ("shoulder", "warm_midtone", "attention_midtone", "protect")]
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and all(path.is_file() for path in paths)):
        return {"temporal-highlight-shoulder-isolation",
                "temporal-warm-midtone-separation",
                "temporal-static-attention-midtone"}
    return set()


def shoulder_filters(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    # Rec.709 近似亮度守恒：增加红、减少蓝，并用极小绿回撤抵消综合色度增益。
    # 这样“变暖”不会再次伪装成高光变亮。
    red = 1.0 + 0.100 * mix
    green = 1.0 - 0.0136 * mix
    blue = 1.0 - 0.160 * mix
    return f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}"


def warm_midtone_filters(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    red = 1.0 + 0.060 * mix
    green = 1.0 - 0.0098 * mix
    blue = 1.0 - 0.080 * mix
    lift = 0.28 + 0.045 * mix
    roll = 0.65 + 0.025 * mix
    return (
        f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f},"
        f"curves=master='0/0 0.28/{lift:.6f} 0.65/{roll:.6f} 1/1':interp=pchip"
    )


def attention_midtone_filters(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    low_mid = 0.28 + 0.060 * mix
    mid = 0.52 + 0.036 * mix
    return (
        f"curves=master='0/0 0.12/0.12 0.28/{low_mid:.6f} "
        f"0.52/{mid:.6f} 0.68/0.68 1/1':interp=pchip"
    )


def attention_midtone_lift(strength: float) -> float:
    """返回两个塑光控制点的平均纵向位移，仅用于合同单调性。"""
    mix = max(0.0, min(1.0, float(strength)))
    return round((0.060 * mix + 0.036 * mix) / 2.0, 6)


def channel_warmth_gain(filter_chain: str) -> float:
    values = {}
    mixer = filter_chain.split(",", 1)[0]
    for segment in mixer.split("=", 1)[1].split(":"):
        key, value = segment.split("=", 1)
        values[key] = float(value)
    return round((values["rr"] + values["gg"]) / 2.0 - values["bb"], 6)


def channel_luma_gain(filter_chain: str) -> float:
    values = {}
    mixer = filter_chain.split(",", 1)[0]
    for segment in mixer.split("=", 1)[1].split(":"):
        key, value = segment.split("=", 1)
        values[key] = float(value)
    return round(
        0.2126 * (values["rr"] - 1.0)
        + 0.7152 * (values["gg"] - 1.0)
        + 0.0722 * (values["bb"] - 1.0),
        6,
    )


def shoulder_warmth_gain(filter_chain: str) -> float:
    return channel_warmth_gain(filter_chain)


def masked_warmth_metrics(frames: list[Path], shoulder_masks: list[Path],
                          warm_midtone_masks: list[Path],
                          neutral_midtone_masks: list[Path] | None = None) -> dict:
    legacy = neutral_midtone_masks is None
    if legacy:
        neutral_midtone_masks = warm_midtone_masks
    if not frames or not (len(frames) == len(shoulder_masks)
                          == len(warm_midtone_masks) == len(neutral_midtone_masks)):
        raise CreamSoftGradeError("暖度测量的帧与蒙版数量不一致")
    names = ("shoulder", "neutral_midtone") if legacy else (
        "shoulder", "warm_midtone", "neutral_midtone")
    totals = {name: 0.0 for name in names}
    counts = {name: 0 for name in names}
    for frame, shoulder_mask, warm_mask, neutral_mask in zip(
            frames, shoulder_masks, warm_midtone_masks, neutral_midtone_masks):
        width, height = video_masks._dimensions(frame)
        rgb = video_masks._read_raw(frame, "rgb24", width, height)
        shoulder = read_gray_mask(shoulder_mask, width, height)
        warm_midtone = read_gray_mask(warm_mask, width, height)
        neutral_midtone = read_gray_mask(neutral_mask, width, height)
        for index in range(width * height):
            offset = index * 3
            red, green, blue = rgb[offset:offset + 3]
            warmth = ((red + green) / 2.0 - blue) / 255.0
            if shoulder[index] >= 128:
                totals["shoulder"] += warmth
                counts["shoulder"] += 1
            if not legacy and warm_midtone[index] >= 128:
                totals["warm_midtone"] += warmth
                counts["warm_midtone"] += 1
            if neutral_midtone[index] >= 128:
                totals["neutral_midtone"] += warmth
                counts["neutral_midtone"] += 1
    if any(not count for count in counts.values()):
        raise CreamSoftGradeError("肩部、暖中间调或中性测量区没有可测像素")
    result = {
        "shoulder_warmth": round(totals["shoulder"] / counts["shoulder"], 6),
        "sample_pixels": counts,
        "boundary": "固定蒙版 RGB 暖度代理；只用于同素材三档单调性，不等同于肤色或审美结论",
    }
    if legacy:
        result["midtone_warmth"] = round(
            totals["neutral_midtone"] / counts["neutral_midtone"], 6)
        result["sample_pixels"] = {
            "shoulder": counts["shoulder"], "midtone": counts["neutral_midtone"]}
    else:
        result["warm_midtone_warmth"] = round(
            totals["warm_midtone"] / counts["warm_midtone"], 6)
        result["neutral_midtone_warmth"] = round(
            totals["neutral_midtone"] / counts["neutral_midtone"], 6)
    return result


def mean_rgb_delta(base_frames: list[Path], graded_frames: list[Path]) -> float:
    if not base_frames or len(base_frames) != len(graded_frames):
        raise CreamSoftGradeError("RGB 差异测量的基础帧与成片帧数量不一致")
    total = 0
    samples = 0
    for base, graded in zip(base_frames, graded_frames):
        width, height = video_masks._dimensions(base)
        if video_masks._dimensions(graded) != (width, height):
            raise CreamSoftGradeError("RGB 差异测量的帧尺寸不一致")
        left = video_masks._read_raw(base, "rgb24", width, height)
        right = video_masks._read_raw(graded, "rgb24", width, height)
        total += sum(abs(a - b) for a, b in zip(left, right))
        samples += len(left)
    return round(total / max(1, samples) / 255.0, 6)


def masked_luma_metrics(frames: list[Path], attention_masks: list[Path],
                        holdout_masks: list[Path]) -> dict:
    if not frames or not (len(frames) == len(attention_masks) == len(holdout_masks)):
        raise CreamSoftGradeError("亮度测量的帧与蒙版数量不一致")
    totals = {"attention_midtone": 0.0, "attention_holdout": 0.0}
    counts = {"attention_midtone": 0, "attention_holdout": 0}
    for frame, attention_path, holdout_path in zip(
            frames, attention_masks, holdout_masks):
        width, height = video_masks._dimensions(frame)
        rgb = video_masks._read_raw(frame, "rgb24", width, height)
        attention = read_gray_mask(attention_path, width, height)
        holdout = read_gray_mask(holdout_path, width, height)
        for index in range(width * height):
            offset = index * 3
            red, green, blue = rgb[offset:offset + 3]
            luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
            if attention[index] >= 128:
                totals["attention_midtone"] += luma
                counts["attention_midtone"] += 1
            if holdout[index] >= 128:
                totals["attention_holdout"] += luma
                counts["attention_holdout"] += 1
    if any(not value for value in counts.values()):
        raise CreamSoftGradeError("注意力中间调或椭圆外保持区没有可测像素")
    return {
        "attention_midtone_luma": round(
            totals["attention_midtone"] / counts["attention_midtone"], 6),
        "attention_holdout_luma": round(
            totals["attention_holdout"] / counts["attention_holdout"], 6),
        "sample_pixels": counts,
        "boundary": "固定 L1 椭圆亮度代理；只比较同素材输出，不等同于人物语义或审美结论",
    }


def build_layered_filter_complex(width: int | None = None,
                                 height: int | None = None,
                                 strength: float = 0.55) -> str:
    # 毫秒时基蒙版与MP4不可仅清零PTS；逐帧重建同一时钟，避免连续maskedmerge累积错位。
    clock = "settb=AVTB,setpts=N/FRAME_RATE/TB"
    mask = "format=gbrp," + clock
    if width and height:
        mask = f"scale={int(width)}:{int(height)}:flags=bilinear," + mask
    return (
        f"[0:v]format=gbrp,{clock},split=2[blbase][blbaseprotect];"
        f"[1:v]format=gbrp,{clock}[blshoulderwarm];"
        f"[2:v]format=gbrp,{clock}[blmidtonewarm];"
        f"[3:v]{mask}[blshouldermask];"
        "[blbase][blshoulderwarm][blshouldermask]maskedmerge=planes=7[blshoulder];"
        f"[4:v]{mask}[blwarmmidmask];"
        "[blshoulder][blmidtonewarm][blwarmmidmask]maskedmerge=planes=7[bltwolevels];"
        "[bltwolevels]split=2[bltwobase][blattentionseed];"
        f"[blattentionseed]{attention_midtone_filters(strength)},"
        "format=gbrp[blattentionlight];"
        f"[5:v]{mask}[blattentionmask];"
        "[bltwobase][blattentionlight][blattentionmask]maskedmerge=planes=7[blthreelayers];"
        f"[6:v]{mask}[blprotectmask];"
        "[blthreelayers][blbaseprotect][blprotectmask]maskedmerge=planes=7,"
        "format=yuv420p[blcreamsoft]"
    )


def exact_frame_args(info: dict) -> list[str]:
    frame_count = int(info.get("nb_frames") or 0)
    if frame_count <= 0:
        raise CreamSoftGradeError("源视频没有可验证的帧数")
    return ["-frames:v", str(frame_count)]


def build_dynamic_masks(source: Path, outputs: dict[str, Path], workdir: Path,
                        analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_cream_soft_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    for name in (
            "shoulder", "warm_midtone", "neutral_midtone",
            "attention_midtone", "attention_holdout", "protect"):
        report[f"{name}_mask_video"] = video_masks.assemble_mask_video(
            workdir / "masks" / name, outputs[name],
            probe["fps_fraction"], len(frames))
    return report


def render_warm_layer(base_video: Path, output: Path, strength: float) -> dict:
    return render_color_layer(base_video, output, shoulder_filters(strength), "高光肩部暖层")


def render_color_layer(base_video: Path, output: Path, filter_chain: str,
                       label: str) -> dict:
    if output.exists():
        raise CreamSoftGradeError(f"输出已存在，不会覆盖：{output}")
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
        raise CreamSoftGradeError(
            f"{label}渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def _has_audio(path: Path) -> bool:
    result = subprocess.run([
        shutil.which("ffprobe") or "ffprobe", "-v", "error",
        "-select_streams", "a:0", "-show_entries", "stream=index",
        "-of", "csv=p=0", str(path),
    ], capture_output=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def render_layered_video(base_video: Path, shoulder_video: Path,
                         warm_midtone_video: Path,
                         mask_videos: dict[str, Path], output: Path,
                         source_info: dict, strength: float = 0.55) -> dict:
    if output.exists():
        raise CreamSoftGradeError(f"输出已存在，不会覆盖：{output}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n"]
    for path in (base_video, shoulder_video, warm_midtone_video,
                 mask_videos["shoulder"], mask_videos["warm_midtone"],
                 mask_videos["attention_midtone"], mask_videos["protect"]):
        command.extend(["-i", str(path)])
    command.extend([
        "-filter_complex", build_layered_filter_complex(
            source_info.get("width"), source_info.get("height"), strength),
        "-map", "[blcreamsoft]",
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
        raise CreamSoftGradeError(
            "奶油柔光分层合成失败：" + result.stderr.decode("utf-8", "replace").strip())
    shadow_check = cream_shadow_check.verify_video(
        base_video, output, source_info['width'], source_info['height'], source_info['nb_frames'])
    save_report(output.with_suffix('.shadow-check.json'), shadow_check)
    if shadow_check['status'] != 'passed':
        raise CreamSoftGradeError('奶油同源阴影出现抬灰、染色、压黑或参考不足，未交付')
    return {
        "path": str(output), "sha256": _sha256(output), "command": command,
        "audio_preserved": audio,
        "shadow_preservation": shadow_check,
    }


def probe(path: Path) -> dict:
    return video_masks._video_probe(path)


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
