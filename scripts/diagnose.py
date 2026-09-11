#!/usr/bin/env python3
"""原图／视频真实诊断：从像素得出影调、白平衡、综合色彩、空间与材质事实。

这是智能推荐的唯一合法输入。禁止用文件名、用户描述或风格关键词冒充图像理解。
只依赖标准库与 ffmpeg；语义层是可选增强，缺席时明确降级。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from color_space import eotf, hue_distance, oklab_to_oklch, rgb8_to_oklab  # noqa: E402
import scene_facts  # noqa: E402

DIAGNOSIS_SCHEMA_VERSION = "4.0.0"
TARGET_SAMPLES = 22000
NEUTRAL_CANDIDATE_CHROMA = 0.055
ALREADY_POLISHED_THRESHOLDS = {
    "p01_max": 0.04,
    "p50_min": 0.08,
    "p50_max": 0.62,
    "tone_span_min": 0.35,
    "tone_span_max": 0.75,
    "colorfulness_min": 0.16,
    "chromatic_ratio_min": 0.65,
    "local_contrast_min": 0.025,
    "local_contrast_max": 0.06,
    "noise_max": 0.0045,
    "cast_max": 0.03,
    "coherent_color_ratio_min": 0.85,
    "luma_clip_max": 0.01,
}


class DiagnoseError(Exception):
    pass


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise DiagnoseError(f"缺少 {name}")
    return found


def probe_stream(path: Path) -> dict:
    result = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise DiagnoseError(f"无法探测素材：{result.stderr.strip()[:200]}")
    payload = json.loads(result.stdout)
    video = [s for s in payload.get("streams", []) if s.get("codec_type") == "video"]
    if not video:
        raise DiagnoseError("素材没有图像轨")
    stream = video[0]
    duration = float(stream.get("duration") or payload.get("format", {}).get("duration") or 0)
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": duration,
        "has_audio": any(s.get("codec_type") == "audio" for s in payload.get("streams", [])),
    }


def sample_frame(path: Path, width: int, height: int, at_time: float | None = None) -> tuple[list[int], int, int]:
    scale = math.sqrt(TARGET_SAMPLES / float(max(1, width * height)))
    sample_w = max(24, min(width, int(round(width * scale))))
    sample_h = max(24, min(height, int(round(height * scale))))
    command = [_tool("ffmpeg"), "-v", "error"]
    if at_time is not None:
        command += ["-ss", f"{at_time:.3f}"]
    command += ["-i", str(path), "-vf", f"format=rgb24,scale={sample_w}:{sample_h}:flags=area",
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not result.stdout:
        raise DiagnoseError("无法采样画面")
    return list(result.stdout[: sample_w * sample_h * 3]), sample_w, sample_h


def _percentile(ordered: list[float], ratio: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * ratio
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def _box_blur(values: list[float], width: int, height: int, radius: int) -> list[float]:
    horizontal = [0.0] * len(values)
    for y in range(height):
        row = y * width
        running = 0.0
        for x in range(min(radius, width - 1) + 1):
            running += values[row + x]
        count = min(radius, width - 1) + 1
        for x in range(width):
            horizontal[row + x] = running / count
            add = x + radius + 1
            drop = x - radius
            if add < width:
                running += values[row + add]
                count += 1
            if drop >= 0:
                running -= values[row + drop]
                count -= 1
    output = [0.0] * len(values)
    for x in range(width):
        running = 0.0
        for y in range(min(radius, height - 1) + 1):
            running += horizontal[y * width + x]
        count = min(radius, height - 1) + 1
        for y in range(height):
            output[y * width + x] = running / count
            add = y + radius + 1
            drop = y - radius
            if add < height:
                running += horizontal[add * width + x]
                count += 1
            if drop >= 0:
                running -= horizontal[drop * width + x]
                count -= 1
    return output


def analyze_frame(pixels: list[int], width: int, height: int, profile: str) -> dict:
    count = width * height
    luminance: list[float] = []
    labs: list[tuple[float, float, float]] = []
    clipped_low = clipped_high = 0
    crushed_black = blown_white = 0
    coefficients = (0.2289746, 0.6917385, 0.0792869) if profile == "display-p3" else (0.2126, 0.7152, 0.0722)
    rg_values: list[float] = []
    yb_values: list[float] = []
    for index in range(count):
        offset = index * 3
        red, green, blue = pixels[offset], pixels[offset + 1], pixels[offset + 2]
        r, g, b = eotf(red / 255.0), eotf(green / 255.0), eotf(blue / 255.0)
        luminance.append(coefficients[0] * r + coefficients[1] * g + coefficients[2] * b)
        labs.append(rgb8_to_oklab(red, green, blue, profile))
        rg_values.append((red - green) / 255.0)
        yb_values.append(((red + green) / 2 - blue) / 255.0)
        if min(red, green, blue) <= 1:
            clipped_low += 1
        if max(red, green, blue) >= 254:
            clipped_high += 1
        # 通道剪切与亮度剪切是两回事：纯红 (255,0,0) 的最小通道为 0，
        # 但它既不是黑也没有丢失暗部细节。彩条类画面会让通道口径虚高到 80% 以上，
        # 用它判断「镜头是否健康」会选出最差的镜头当基准。
        luma_value = luminance[-1]
        if luma_value <= 0.004:
            crushed_black += 1
        if luma_value >= 0.99:
            blown_white += 1

    ordered = sorted(luminance)
    p01, p05, p50, p95, p99 = (_percentile(ordered, r) for r in (0.01, 0.05, 0.50, 0.95, 0.99))
    tone_span = p95 - p05

    # Hasler-Süsstrunk 综合色彩，与 OKLab 平均彩度互为佐证。
    rg_std, yb_std = statistics.pstdev(rg_values), statistics.pstdev(yb_values)
    rg_mean, yb_mean = statistics.fmean(rg_values), statistics.fmean(yb_values)
    colorfulness = math.sqrt(rg_std ** 2 + yb_std ** 2) + 0.3 * math.sqrt(rg_mean ** 2 + yb_mean ** 2)
    chromas = [math.hypot(lab[1], lab[2]) for lab in labs]
    mean_chroma = statistics.fmean(chromas)
    chromatic_ratio = sum(1 for c in chromas if c >= 0.028) / count

    # 白平衡：只用近中性候选像素做偏色估计，避免被大面积彩色主体带偏。
    neutral_candidates = [lab for lab, c in zip(labs, chromas) if c < NEUTRAL_CANDIDATE_CHROMA]
    if len(neutral_candidates) >= max(40, count * 0.02):
        cast_a = statistics.fmean(lab[1] for lab in neutral_candidates)
        cast_b = statistics.fmean(lab[2] for lab in neutral_candidates)
        cast_magnitude = math.hypot(cast_a, cast_b)
        cast_hue = math.degrees(math.atan2(cast_b, cast_a)) % 360.0
        cast_basis = f"{len(neutral_candidates)} 个近中性像素"
    else:
        cast_a = cast_b = cast_magnitude = 0.0
        cast_hue = 0.0
        cast_basis = "近中性像素不足，无法可靠估计偏色"

    if cast_magnitude < 0.006:
        cast_label = "白平衡基本中性"
    else:
        if 20.0 <= cast_hue <= 110.0:
            axis = "偏暖（黄／橙）"
        elif 190.0 <= cast_hue <= 300.0:
            axis = "偏冷（青／蓝）"
        elif 110.0 < cast_hue < 190.0:
            axis = "偏绿"
        else:
            axis = "偏洋红"
        strength = "轻微" if cast_magnitude < 0.014 else ("明显" if cast_magnitude < 0.028 else "强烈")
        cast_label = f"{strength}{axis}"

    # 色相分布：只统计有彩像素，防止中性像素把直方图铺平。
    hue_bins = [0.0] * 36
    for lab, chroma in zip(labs, chromas):
        if chroma < 0.028:
            continue
        _, _, hue = oklab_to_oklch(*lab)
        hue_bins[int(hue // 10) % 36] += chroma
    hue_total = sum(hue_bins) or 1.0
    dominant = sorted(range(36), key=lambda i: -hue_bins[i])[:3]
    dominant_hues = [
        {"hue_center": index * 10 + 5, "weight": round(hue_bins[index] / hue_total, 4),
         "family": _hue_family(index * 10 + 5)}
        for index in dominant if hue_bins[index] > 0
    ]

    # 多尺度局部对比
    local = {}
    for name, radius in (("texture", 1), ("modeling", 4), ("region", 12)):
        blurred = _box_blur(luminance, width, height, radius)
        local[name] = round(statistics.fmean(abs(a - b) for a, b in zip(luminance, blurred)), 6)

    # 噪声：最小尺度残差在平坦区的强度
    flat_residual = []
    blurred_1 = _box_blur(luminance, width, height, 1)
    blurred_4 = _box_blur(luminance, width, height, 4)
    for index in range(count):
        if abs(blurred_1[index] - blurred_4[index]) < 0.01:
            flat_residual.append(abs(luminance[index] - blurred_1[index]))
    noise = round(statistics.fmean(flat_residual), 6) if flat_residual else 0.0

    # 空间：亮度重心与中心／边缘关系
    weight_total = sum(luminance) or 1.0
    centroid_x = sum(luminance[i] * ((i % width) / max(1, width - 1)) for i in range(count)) / weight_total
    centroid_y = sum(luminance[i] * ((i // width) / max(1, height - 1)) for i in range(count)) / weight_total
    center, outer = [], []
    for index in range(count):
        x, y = (index % width) / width, (index // width) / height
        (center if 0.25 <= x < 0.75 and 0.2 <= y < 0.8 else outer).append(luminance[index])
    center_edge = round(statistics.fmean(center) - statistics.fmean(outer), 6) if center and outer else 0.0
    top_third = statistics.fmean(luminance[: count // 3]) if count >= 3 else 0.0
    bottom_third = statistics.fmean(luminance[2 * count // 3:]) if count >= 3 else 0.0

    return {
        "tone": {
            "p01": round(p01, 6), "p05": round(p05, 6), "p50": round(p50, 6),
            "p95": round(p95, 6), "p99": round(p99, 6),
            "tone_span": round(tone_span, 6),
            "black_point_health": "有真实黑位" if p01 <= 0.012 else ("黑位偏高，画面发灰" if p01 > 0.04 else "黑位偏软"),
            "white_point_health": "亮部有余量" if p99 < 0.93 else "亮部已接近上限",
            "highlight_clip_ratio": round(clipped_high / count, 6),
            "shadow_clip_ratio": round(clipped_low / count, 6),
            "clip_metric_note": "highlight/shadow_clip_ratio 是通道口径；判断影调健康请用下面的亮度口径",
            "luma_blown_ratio": round(blown_white / count, 6),
            "luma_crushed_ratio": round(crushed_black / count, 6),
            "exposure_class": _exposure_class(p05, p50, p95),
            # 夜景的直方图特征是「大片暗 + 少量点光源」：p99 远高于 p95。
            # 灰片虽然也暗，但亮端是连续的，这个比值接近 1。
            "point_light_ratio": round(p99 / max(1e-4, p95), 3),
            "headroom_ev_up": round(math.log2(max(0.02, min(0.62, p50 * 2.2)) / max(0.005, p50)), 3),
            "headroom_ev_down": round(math.log2(max(0.03, p50 * 0.55) / max(0.005, p50)), 3),
        },
        "color": {
            "colorfulness": round(colorfulness, 6),
            "mean_chroma_oklab": round(mean_chroma, 6),
            "chromatic_pixel_ratio": round(chromatic_ratio, 4),
            "white_balance": {
                "cast_label": cast_label,
                "cast_magnitude": round(cast_magnitude, 6),
                "cast_hue": round(cast_hue, 1),
                "cast_a": round(cast_a, 6),
                "cast_b": round(cast_b, 6),
                "neutral_sample_count": len(neutral_candidates),
                "neutral_sample_ratio": round(len(neutral_candidates) / count, 6),
                "basis": cast_basis,
            },
            "dominant_hues": dominant_hues,
        },
        "texture": {
            "local_contrast_texture": local["texture"],
            "local_contrast_modeling": local["modeling"],
            "local_contrast_region": local["region"],
            "noise_estimate": noise,
            "noise_label": "干净" if noise < 0.004 else ("轻微噪点" if noise < 0.009 else "噪点明显"),
        },
        "space": {
            "luma_centroid_x": round(centroid_x, 4),
            "luma_centroid_y": round(centroid_y, 4),
            "center_minus_edge": center_edge,
            "top_third_luma": round(top_third, 6),
            "bottom_third_luma": round(bottom_third, 6),
            "vertical_gradient": round(top_third - bottom_third, 6),
            "note": "亮度分布属几何统计，不代表识别了天空、地面或主体",
        },
    }


NIGHT_LABELS = {"night", "dark", "nightlife", "neon", "moon", "star", "fireworks"}
DAY_LABELS = {"sky", "cloudy", "outdoor", "daylight", "sunny", "sunrise", "sunset", "beach",
              "mountain", "field", "snow", "forest", "garden", "land"}
INDOOR_LABELS = {"indoor", "room", "kitchen", "restaurant", "office", "furniture", "table"}


def infer_light_context(tone: dict, scene_labels: list[dict] | None) -> dict:
    """推断拍摄光照语境。夜景配方误用在白天素材上是已记录的 P0 失败根因之一。"""
    labels = {item["label"]: item["confidence"] for item in (scene_labels or [])}
    night_score = max((labels[k] for k in labels if k in NIGHT_LABELS), default=0.0)
    day_score = max((labels[k] for k in labels if k in DAY_LABELS), default=0.0)
    indoor_score = max((labels[k] for k in labels if k in INDOOR_LABELS), default=0.0)
    point_light = float(tone["point_light_ratio"])
    p50 = float(tone["p50"])
    evidence = []
    if night_score > 0.2:
        evidence.append(f"图像分类出现夜景类标签（{night_score:.2f}）")
    if day_score > 0.2:
        evidence.append(f"图像分类出现日光／户外类标签（{day_score:.2f}）")
    if p50 < 0.06 and point_light > 2.0:
        evidence.append(f"直方图呈现「大片暗＋点光源」特征（p99/p95={point_light:.2f}）")

    # 夜景的可测特征：大片暗部 + 少量点光源，因此 p99/p95 明显大于 1。
    # 这条纯测量判据在没有语义后端（普通用户零安装）时依然成立，
    # 用来阻止夜景专用配方被推给白天素材。
    if point_light < 1.5 and p50 >= 0.09:
        night_plausible = False
        evidence.append(
            f"直方图连续且中位亮度 {p50:.3f}、p99/p95={point_light:.2f}，不具备夜景的「大片暗＋点光源」特征"
        )
    elif point_light >= 2.0 and p50 < 0.12:
        night_plausible = True
    else:
        night_plausible = None

    if night_score > max(0.25, day_score) or (p50 < 0.06 and point_light > 2.5 and day_score < 0.3):
        context = "night"
    elif day_score > 0.25:
        context = "day"
    elif indoor_score > 0.25:
        context = "indoor"
    else:
        context = "unknown"
        evidence.append("没有足够证据判断白天／夜晚，配方的光照条件门按可测夜景判据处理")
    return {"context": context, "night_plausible": night_plausible,
            "night_score": round(night_score, 3), "day_score": round(day_score, 3),
            "indoor_score": round(indoor_score, 3), "point_light_ratio": point_light, "evidence": evidence}


def resolve_light_context(tone: dict, scene_labels: list[dict] | None) -> dict:
    """把同一份三态场景事实同时用于排序与用户展示。"""
    legacy = infer_light_context(tone, scene_labels)
    fact = scene_facts.derive_light_fact(
        tone,
        {"night": legacy["night_score"], "day": legacy["day_score"],
         "indoor": legacy["indoor_score"]},
    )
    resolved = dict(legacy)
    if fact.get("state") == "known" and fact.get("value") in {"day", "night", "indoor"}:
        resolved["context"] = fact["value"]
    elif fact.get("state") == "conflicting":
        resolved["context"] = "unknown"
    resolved["fact"] = fact
    resolved["evidence"] = fact.get("evidence") or legacy["evidence"]
    return resolved


def _hue_family(hue: float) -> str:
    table = [
        (0, 20, "红"), (20, 55, "橙／肤色带"), (55, 95, "黄"), (95, 140, "黄绿"),
        (140, 175, "绿"), (175, 210, "青绿"), (210, 250, "青蓝"), (250, 285, "蓝"),
        (285, 320, "紫"), (320, 360, "洋红"),
    ]
    for low, high, name in table:
        if low <= hue < high:
            return name
    return "红"


def _exposure_class(p05: float, p50: float, p95: float) -> str:
    if p50 < 0.08:
        return "严重欠曝"
    if p50 < 0.16:
        return "偏暗"
    if p50 > 0.62 and p05 > 0.22:
        return "高调"
    if p50 > 0.5:
        return "偏亮"
    if p95 < 0.45 and p50 < 0.25:
        return "低调"
    return "曝光可用"


def visual_brief_text(analysis: dict, semantic: dict | None) -> list[str]:
    """一句话诊断：普通用户能懂，且每句都能追溯到上面的数值。"""
    tone, color, texture, space = analysis["tone"], analysis["color"], analysis["texture"], analysis["space"]
    lines = []
    lines.append(
        f"影调：{tone['exposure_class']}，中位亮度 {tone['p50']:.3f}，明暗跨度 {tone['tone_span']:.3f}，{tone['black_point_health']}。"
    )
    if color["chromatic_pixel_ratio"] < 0.20:
        lines.append(
            f"颜色：只有 {color['chromatic_pixel_ratio']:.0%} 的像素带明显色相，画面主要靠明暗说话；"
            f"{color['white_balance']['cast_label']}。"
        )
    else:
        families = "、".join(item["family"] for item in color["dominant_hues"][:2]) or "无明显主导色"
        lines.append(
            f"颜色：{color['chromatic_pixel_ratio']:.0%} 像素带色相，主导色相是{families}；{color['white_balance']['cast_label']}。"
        )
    lines.append(
        f"质感：中尺度立体感 {texture['local_contrast_modeling']:.4f}，{texture['noise_label']}。"
    )
    if semantic and semantic.get("available"):
        faces = semantic["classes"].get("face", {}).get("count", 0)
        coverage = semantic["classes"].get("person", {}).get("coverage", 0.0)
        if faces:
            lines.append(f"主体：检测到 {faces} 个人物，人物区约占画面 {coverage:.0%}（模型输出，可人工修订）。")
        else:
            lines.append("主体：未检测到人物；主体判断需要人工确认。")
    else:
        reason = (semantic or {}).get("degraded_reason") or "语义后端未启用"
        lines.append(f"主体：{reason}；本次不声称已自动识别人脸、天空或商品。")
    return lines


def already_polished_assessment(analysis: dict, media_type: str) -> dict:
    """识别“技术完成度可能已高”的照片；不把统计启发式冒充审美结论。"""
    if media_type != "photo":
        return {
            "candidate": False, "status": "not-applicable-video",
            "reason": "视频需逐镜头判断，不能用代表帧给整段签完成度结论。",
        }
    tone, color, texture = analysis["tone"], analysis["color"], analysis["texture"]
    t = ALREADY_POLISHED_THRESHOLDS
    cast = float(color["white_balance"]["cast_magnitude"])
    coherent_color_override = (
        float(color["chromatic_pixel_ratio"]) >= t["coherent_color_ratio_min"]
        and float(color["colorfulness"]) >= t["colorfulness_min"]
    )
    checks = {
        "black_point": float(tone["p01"]) <= t["p01_max"],
        "exposure": t["p50_min"] <= float(tone["p50"]) <= t["p50_max"],
        "tone_span": t["tone_span_min"] <= float(tone["tone_span"]) <= t["tone_span_max"],
        "colorfulness": float(color["colorfulness"]) >= t["colorfulness_min"],
        "chromatic_coverage": float(color["chromatic_pixel_ratio"]) >= t["chromatic_ratio_min"],
        "local_contrast": (
            t["local_contrast_min"]
            <= float(texture["local_contrast_modeling"])
            <= t["local_contrast_max"]),
        "clean_texture": float(texture["noise_estimate"]) <= t["noise_max"],
        "white_balance_or_coherent_palette": cast <= t["cast_max"] or coherent_color_override,
        "luma_clipping": (
            float(tone["luma_blown_ratio"]) <= t["luma_clip_max"]
            and float(tone["luma_crushed_ratio"]) <= t["luma_clip_max"]),
    }
    candidate = all(checks.values())
    return {
        "candidate": candidate,
        "status": "candidate-not-aesthetic-verdict" if candidate else "not-candidate",
        "checks": checks,
        "failed_checks": [key for key, passed in checks.items() if not passed],
        "coherent_color_override": coherent_color_override,
        "thresholds": t,
        "calibration": "references/already-polished-calibration-v5.json",
        "boundary": "只表示技术指标组合像一张已完成照片；不代表审美通过，也不阻止用户主动选择强风格。",
    }



def _representative_sample(samples: list[dict]) -> tuple[int, dict]:
    """从多窗口采样里挑一帧代表整段片子。

    原来是 `samples[len(samples)//2]`——按**位置**取中间那一帧。
    位置居中不等于内容有代表性：实测一段 30 秒的白天美食车片子，
    第 5 个采样点（15.00s）刚好落在 14.93s 那个硬切之后的暗内景上，
    是 9 个采样里第 2 暗的，于是整段被判「严重欠曝」——
    而这段片子逐时刻的中位亮度是 0.27～0.53（gamma 口径），完全正常。
    照这个判断推荐下去，用户会拿到一套为救暗片准备的提亮方案。

    改成按**代表性**取：选 tone.p50 最接近所有采样中位数的那一帧。
    仍然是一帧真实存在的画面（所有数值内部自洽，不是拼出来的平均脸），
    只是不再让「第几个」决定「像不像」。
    """
    values = [item["analysis"]["tone"]["p50"] for item in samples]
    ordered = sorted(values)
    middle = ordered[len(ordered) // 2]
    index = min(range(len(samples)), key=lambda i: abs(values[i] - middle))
    return index, samples[index]


def video_topic_evidence(per_shot: list[dict]) -> dict:
    """只陈述逐镜头统计能证明的事，不从颜色或影调猜题材。"""
    return {
        "status": "insufficient",
        "topic_labels": [],
        "allowed_scope": "foundation-correction",
        "allowed_recipe_ids": ["natural-clean"],
        "reason": (
            f"已取得 {len(per_shot)} 个镜头的影调与色彩统计，但没有逐镜头题材语义证据；"
            "不能据此声称识别了森林、风景、美食或其他题材。"
        ),
        "boundary": "像素统计只用于一级校正；题材风格必须由可靠语义证据或用户明确选择解锁。",
    }


def _per_shot_statistics(path: Path, stream: dict, profile: str) -> dict:
    """检测全部镜头，并在每个镜头内部取样；失败时显式降级。"""
    try:
        import shots as shots_module

        timeline = shots_module.detect(path)
    except Exception as error:  # noqa: BLE001
        return {
            "status": "unavailable",
            "reason": f"逐镜头检测失败：{error}",
            "shot_count": 0,
            "statistics": [],
        }

    shot_statistics = []
    for shot in timeline["shots"]:
        margin = min(0.20, float(shot["duration"]) * 0.12)
        start = float(shot["start"]) + margin
        end = max(start + 0.001, float(shot["end"]) - margin)
        count = 3 if float(shot["duration"]) >= 0.75 else 1
        analyses = []
        times = []
        for index in range(count):
            at = start + (end - start) * (index + 0.5) / count
            try:
                pixels, width, height = sample_frame(
                    path, stream["width"], stream["height"], at_time=at
                )
            except DiagnoseError:
                continue
            analyses.append(analyze_frame(pixels, width, height, profile))
            times.append(round(at, 3))
        if not analyses:
            continue
        casts = [item["color"]["white_balance"]["cast_label"] for item in analyses]
        shot_statistics.append({
            "index": shot["index"],
            "start": shot["start"],
            "end": shot["end"],
            "sample_times": times,
            "sample_count": len(analyses),
            "p50": round(statistics.fmean(item["tone"]["p50"] for item in analyses), 4),
            "tone_span": round(statistics.fmean(
                item["tone"]["tone_span"] for item in analyses), 4),
            "colorfulness": round(statistics.fmean(
                item["color"]["colorfulness"] for item in analyses), 4),
            "highlight_clip": round(statistics.fmean(
                item["tone"]["highlight_clip_ratio"] for item in analyses), 6),
            "cast": max(set(casts), key=casts.count),
        })
    return {
        "status": "available" if len(shot_statistics) == timeline["shot_count"] else "partial",
        "coverage": timeline["coverage"],
        "shot_count": timeline["shot_count"],
        "sampled_shot_count": len(shot_statistics),
        "needs_human_review": timeline["needs_human_review"],
        "statistics": shot_statistics,
    }


def diagnose(path: Path, profile: str = "srgb", use_semantic: bool = True, mask_dir: Path | None = None) -> dict:
    stream = probe_stream(path)
    media_type = "video" if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv"} else "photo"
    samples = []
    if media_type == "photo":
        pixels, width, height = sample_frame(path, stream["width"], stream["height"])
        samples.append({"at_time": None, "analysis": analyze_frame(pixels, width, height, profile)})
    else:
        duration = max(0.001, stream["duration"])
        # 长视频必须多窗口采样。v3 只看前 15~20 秒的盲区在这里被明确关闭。
        points = 9 if duration > 20 else (5 if duration > 4 else 2)
        for index in range(points):
            at = duration * (index + 0.5) / points
            try:
                pixels, width, height = sample_frame(path, stream["width"], stream["height"], at_time=at)
            except DiagnoseError:
                continue
            samples.append({"at_time": round(at, 3), "analysis": analyze_frame(pixels, width, height, profile)})
    if not samples:
        raise DiagnoseError("没有可用采样帧")

    semantic = None
    if use_semantic and media_type == "photo":
        try:
            import semantic_backend
            backend = semantic_backend.resolve_backend()
            if semantic_backend.person_classes_available():
                semantic = backend.analyze(path, mask_dir)
            else:
                semantic = backend.analyze(path)
        except Exception as error:  # noqa: BLE001
            semantic = {"available": False, "degraded_reason": f"语义后端调用失败：{error}"}

    if media_type == "video":
        primary_index, primary_sample = _representative_sample(samples)
        primary = primary_sample["analysis"]
    else:
        primary_index, primary_sample, primary = 0, samples[0], samples[0]["analysis"]
    result = {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "path": str(path),
        "media_type": media_type,
        "working_profile": "Display P3" if profile == "display-p3" else "sRGB",
        "duration": round(stream["duration"], 3),
        "has_audio": stream["has_audio"],
        "sample_count": len(samples),
        "sample_coverage": (
            "整段均匀多窗口采样" if media_type == "video" else "单帧全画面"
        ),
        "analysis": primary,
        "boundary": "全部数值来自真实像素；语义层缺席时不得声称识别了人脸、天空或商品。",
    }
    if media_type == "video":
        result["temporal"] = _temporal_summary(samples)
        # 说清「这段诊断是看哪一帧得出的」，以及这一帧有多能代表整段。
        # 亮度跨度大时，任何单帧的曝光判语都只对它自己成立。
        result["primary_frame"] = {
            "at_time": primary_sample["at_time"],
            "index": primary_index,
            "of_samples": len(samples),
            "chosen_by": "tone.p50 最接近全部采样的中位数",
            "why_not_positional": "按位置取中间的那一帧可能正好落在硬切后的暗镜头上，"
                                  "会把一整段片子判成欠曝",
            "p50_spread": result["temporal"].get("p50_span"),
            "spread_warning": (
                "采样点之间亮度跨度较大，任何单帧的曝光判语都只对那一帧成立，"
                "整段应走逐镜头校正"
                if (result["temporal"].get("p50_span") or 0) > 0.18 else None),
        }
        result["per_sample"] = [
            {"at_time": item["at_time"],
             "p50": item["analysis"]["tone"]["p50"],
             "tone_span": item["analysis"]["tone"]["tone_span"],
             "colorfulness": item["analysis"]["color"]["colorfulness"],
             "cast": item["analysis"]["color"]["white_balance"]["cast_label"],
             "highlight_clip": item["analysis"]["tone"]["highlight_clip_ratio"]}
            for item in samples
        ]
        result["per_shot"] = _per_shot_statistics(path, stream, profile)
        result["video_topic_evidence"] = video_topic_evidence(
            result["per_shot"]["statistics"]
        )
    if semantic is not None:
        result["semantic"] = semantic
    result["light_context"] = resolve_light_context(
        primary["tone"], (semantic or {}).get("scene_labels")
    )
    result["light_fact"] = result["light_context"]["fact"]
    result["already_polished"] = already_polished_assessment(primary, media_type)
    result["visual_brief"] = visual_brief_text(primary, semantic)
    # 显示层只给结论与一句最短依据；完整证据留在 light_context.evidence 里供复核。
    # 复杂留给内部，简单留给用户。
    context_label = {"day": "白天／户外光", "night": "夜晚／人工光",
                     "indoor": "室内", "unknown": "无法确定"}[result["light_context"]["context"]]
    evidence = result["light_context"]["evidence"]
    short = evidence[0] if evidence else "无强证据"
    if len(short) > 34:
        short = short[:32] + "…"
    result["visual_brief"].insert(1, f"光照语境：{context_label}（{short}）。")
    return result


def _temporal_summary(samples: list[dict]) -> dict:
    p50 = [item["analysis"]["tone"]["p50"] for item in samples]
    colorfulness = [item["analysis"]["color"]["colorfulness"] for item in samples]
    hues = [
        (item["analysis"]["color"]["dominant_hues"][0]["hue_center"]
         if item["analysis"]["color"]["dominant_hues"] else None)
        for item in samples
    ]
    valid_hues = [h for h in hues if h is not None]
    hue_swing = max(
        (hue_distance(a, b) for a in valid_hues for b in valid_hues), default=0.0
    )
    span = max(p50) - min(p50)
    jumps = []
    for index in range(1, len(samples)):
        delta = abs(p50[index] - p50[index - 1])
        if delta > 0.12:
            jumps.append({"between": [samples[index - 1]["at_time"], samples[index]["at_time"]],
                          "luma_delta": round(delta, 4)})
    return {
        "p50_min": round(min(p50), 4),
        "p50_max": round(max(p50), 4),
        "p50_span": round(span, 4),
        "colorfulness_span": round(max(colorfulness) - min(colorfulness), 4),
        "dominant_hue_swing_deg": round(hue_swing, 1),
        "large_luma_jumps": jumps,
        "multi_shot_suspected": bool(jumps) or span > 0.18 or hue_swing > 60.0,
        "note": (
            "采样点之间出现大幅亮度或色相跳变，几乎可以确定是多镜头素材；"
            "单一全局中位数会被某几个镜头支配，必须走逐镜头校正。"
            if jumps or span > 0.18 else "采样点之间影调与色彩较为一致"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="从真实像素诊断素材，输出可追溯的视觉简报")
    parser.add_argument("--input", required=True)
    parser.add_argument("--profile", default="srgb", choices=["srgb", "display-p3"])
    parser.add_argument("--no-semantic", action="store_true")
    parser.add_argument("--mask-dir")
    args = parser.parse_args()
    try:
        payload = diagnose(
            Path(args.input).expanduser().resolve(), args.profile,
            use_semantic=not args.no_semantic,
            mask_dir=Path(args.mask_dir).expanduser().resolve() if args.mask_dir else None,
        )
    except DiagnoseError as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
