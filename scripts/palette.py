#!/usr/bin/env python3
"""配色色卡引擎：从真实像素提取主色／辅色／强调色／中性色。

设计约束（来自历史失败经验）：
- 必须在色彩管理之后、感知均匀空间中聚类；不得把 P3 像素按 sRGB 解释。
- 不得把肤色、天空与背景简单平均成脏灰：有彩与中性像素分池聚类，
  避免大面积低彩背景把有彩簇拉向灰轴。
- 色卡不是 LUT，也不是调色结果；它只描述颜色构成。
- 空间位置只作几何启发（上／中／下、中心／边缘），绝不声称语义识别。

只依赖标准库与 ffmpeg。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from color_space import (  # noqa: E402
    delta_e_ok,
    format_oklch,
    hex_from_rgb8,
    hue_distance,
    oklab_to_oklch,
    oklab_to_rgb8,
    rgb8_to_oklab,
)

PALETTE_SCHEMA_VERSION = "4.0.0"
# OKLab chroma 中性阈值。低于此值的像素在视觉上已经读作灰／白／黑。
NEUTRAL_CHROMA = 0.028
# 两个簇低于该感知距离时合并，避免输出「看起来一样」的两格色卡。
MERGE_DELTA_E = 0.055
MIN_COLORS = 3
MAX_COLORS = 7
SAMPLE_PIXELS = 26000
QUANT_BITS = 5


class PaletteError(Exception):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise PaletteError("缺少 ffmpeg，无法提取色卡")
    return found



# 视频色卡跨全片取多少帧。12 帧足以覆盖常见的多镜头结构，
# 又不至于让一次色卡提取变成一次全片解码。
VIDEO_PALETTE_FRAMES = 12


def _probe_duration(path: Path) -> float:
    """只在需要跨全片取样时才问一次时长。问不到就退回单帧，不猜。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True)
        return float(result.stdout.strip()) if result.returncode == 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0



def sample_rgb_grid(
    path: Path,
    width: int,
    height: int,
    media_type: str = "photo",
    at_time: float | None = None,
    duration: float = 0.0,
    frames: int = 1,
) -> tuple[bytes, int, int]:
    """按原始宽高比采样，保证面积比例可信。返回 (rgb24 字节, 采样宽, 采样高)。"""
    if not width or not height:
        raise PaletteError("素材尺寸无效")
    scale = math.sqrt(SAMPLE_PIXELS / float(width * height))
    sample_w = max(16, min(width, int(round(width * scale))))
    sample_h = max(16, min(height, int(round(height * scale))))
    command = [_ffmpeg(), "-v", "error"]
    if at_time is not None:
        command += ["-ss", f"{at_time:.3f}"]
    command += ["-i", str(path)]
    # 顺序不能反：先 scale 等于在 yuv420 里缩放，色度插值把极值往里收，
    # 面积占比与彩度读数会系统性偏低。format=rgb24 必须排在 scale 之前。
    filters = ["format=rgb24", f"scale={sample_w}:{sample_h}:flags=area"]

    # 没有指定时刻的视频，以前只读第 0 帧。片子开头是黑帧或台标时，
    # 整支片子的色卡就从那一帧生成——实测一段白天美食车视频因此被判
    # 「几乎不存在有彩区域」，而同一支片子的诊断说 46% 像素带色相。
    # 色卡是面积占比的依据，取错一帧，后面所有「主色占多少」都跟着错。
    # 这里改成跨全片均匀取多帧，与 diagnose 的多窗口采样口径一致。
    if media_type == "video" and at_time is None:
        span = duration if duration > 0 else _probe_duration(path)
        if span > 0:
            frames = max(frames, VIDEO_PALETTE_FRAMES)
            filters.insert(0, f"fps={min(6.0, max(0.1, frames / span))}")
    command += ["-vf", ",".join(filters), "-frames:v", str(frames),
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not result.stdout:
        raise PaletteError(f"无法采样像素：{result.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return result.stdout, sample_w, sample_h


def build_bins(data: bytes, sample_w: int, sample_h: int, profile: str) -> list[dict]:
    """把像素量化成带权直方图，同时累计几何重心。避免逐像素聚类的开销。"""
    shift = 8 - QUANT_BITS
    bins: dict[int, dict] = {}
    frame_size = sample_w * sample_h * 3
    frame_count = max(1, len(data) // frame_size)
    for frame in range(frame_count):
        base = frame * frame_size
        for index in range(sample_w * sample_h):
            offset = base + index * 3
            if offset + 3 > len(data):
                break
            red, green, blue = data[offset], data[offset + 1], data[offset + 2]
            key = ((red >> shift) << (QUANT_BITS * 2)) | ((green >> shift) << QUANT_BITS) | (blue >> shift)
            entry = bins.get(key)
            x = (index % sample_w) / max(1, sample_w - 1)
            y = (index // sample_w) / max(1, sample_h - 1)
            if entry is None:
                bins[key] = {"r": red, "g": green, "b": blue, "count": 1, "x": x, "y": y}
            else:
                entry["count"] += 1
                entry["r"] += red
                entry["g"] += green
                entry["b"] += blue
                entry["x"] += x
                entry["y"] += y
    result = []
    for entry in bins.values():
        count = entry["count"]
        rgb = (entry["r"] / count, entry["g"] / count, entry["b"] / count)
        lab = rgb8_to_oklab(int(round(rgb[0])), int(round(rgb[1])), int(round(rgb[2])), profile)
        result.append({
            "lab": lab,
            "rgb": rgb,
            "count": count,
            "x": entry["x"] / count,
            "y": entry["y"] / count,
            "chroma": math.hypot(lab[1], lab[2]),
        })
    return result


def _weighted_kmeans(bins: list[dict], k: int, seed: int, iterations: int = 40) -> list[dict]:
    """确定性 k-means++：种子由内容派生，同输入必得同结果。"""
    if not bins:
        return []
    k = max(1, min(k, len(bins)))
    state = seed & 0xFFFFFFFF

    def next_random() -> float:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / float(0x7FFFFFFF)

    total_weight = sum(item["count"] for item in bins)
    first = max(bins, key=lambda item: item["count"])
    centers = [first["lab"]]
    while len(centers) < k:
        distances = []
        for item in bins:
            nearest = min(delta_e_ok(item["lab"], center) for center in centers)
            distances.append((nearest ** 2) * item["count"])
        total = sum(distances)
        if total <= 0:
            break
        threshold = next_random() * total
        cumulative = 0.0
        chosen = bins[-1]
        for item, value in zip(bins, distances):
            cumulative += value
            if cumulative >= threshold:
                chosen = item
                break
        centers.append(chosen["lab"])

    assignment = [0] * len(bins)
    for _ in range(iterations):
        changed = False
        for index, item in enumerate(bins):
            best, best_distance = 0, float("inf")
            for center_index, center in enumerate(centers):
                distance = delta_e_ok(item["lab"], center)
                if distance < best_distance:
                    best, best_distance = center_index, distance
            if assignment[index] != best:
                assignment[index] = best
                changed = True
        sums = [[0.0, 0.0, 0.0, 0.0] for _ in centers]
        for index, item in enumerate(bins):
            slot = sums[assignment[index]]
            weight = item["count"]
            slot[0] += item["lab"][0] * weight
            slot[1] += item["lab"][1] * weight
            slot[2] += item["lab"][2] * weight
            slot[3] += weight
        for center_index, slot in enumerate(sums):
            if slot[3] > 0:
                centers[center_index] = (slot[0] / slot[3], slot[1] / slot[3], slot[2] / slot[3])
        if not changed:
            break

    clusters = []
    for center_index, center in enumerate(centers):
        members = [item for index, item in enumerate(bins) if assignment[index] == center_index]
        weight = sum(item["count"] for item in members)
        if not weight:
            continue
        clusters.append({
            "lab": center,
            "weight": weight,
            "area": weight / total_weight,
            "x": sum(item["x"] * item["count"] for item in members) / weight,
            "y": sum(item["y"] * item["count"] for item in members) / weight,
            "spread": max((delta_e_ok(item["lab"], center) for item in members), default=0.0),
            # 留下成员，目标色卡才能预测「这组像素实际落到哪」而不是「簇心变换到哪」。
            # 调色是非线性的，簇一分散，f(簇心) 与 mean(f(成员)) 会明显分开，
            # 而用户在色卡上看到的那一格，对应的是后者。
            # bin 的 rgb 是加权均值，可能是浮点；变换器要整数通道，这里就地取整。
            "members": [(tuple(max(0, min(255, int(round(v)))) for v in item["rgb"]),
                         item["count"]) for item in members],
        })
    return clusters


def _merge_close(clusters: list[dict], threshold: float = MERGE_DELTA_E) -> list[dict]:
    ordered = sorted(clusters, key=lambda item: -item["area"])
    merged: list[dict] = []
    for cluster in ordered:
        target = None
        for existing in merged:
            if delta_e_ok(existing["lab"], cluster["lab"]) < threshold:
                target = existing
                break
        if target is None:
            merged.append(dict(cluster))
            continue
        total = target["weight"] + cluster["weight"]
        target["lab"] = tuple(
            (target["lab"][i] * target["weight"] + cluster["lab"][i] * cluster["weight"]) / total
            for i in range(3)
        )
        target["x"] = (target["x"] * target["weight"] + cluster["x"] * cluster["weight"]) / total
        target["y"] = (target["y"] * target["weight"] + cluster["y"] * cluster["weight"]) / total
        target["weight"] = total
        target["area"] = target["area"] + cluster["area"]
        target["spread"] = max(target["spread"], cluster["spread"])
        target["members"] = target.get("members", []) + cluster.get("members", [])
    return merged


def _merge_hue_family(clusters: list[dict], hue_window: float = 22.0, light_window: float = 0.26) -> list[dict]:
    """把同色相家族的明暗层级并成一格色卡。

    肤色天然横跨暗／中／亮三段；若不合并，色卡会用三格描述同一种材质，
    挤掉真正不同的颜色。合并后在 tonal_variants 中保留原始层级，不丢信息。
    """
    ordered = sorted(clusters, key=lambda item: -item["area"])
    merged: list[dict] = []
    for cluster in ordered:
        _, chroma, hue = oklab_to_oklch(*cluster["lab"])
        if chroma < NEUTRAL_CHROMA:
            merged.append(dict(cluster))
            continue
        target = None
        for existing in merged:
            e_light, e_chroma, e_hue = oklab_to_oklch(*existing["lab"])
            if e_chroma < NEUTRAL_CHROMA:
                continue
            if hue_distance(e_hue, hue) <= hue_window and abs(e_light - cluster["lab"][0]) <= light_window:
                target = existing
                break
        if target is None:
            item = dict(cluster)
            item["tonal_variants"] = [{
                "hex": hex_from_rgb8(oklab_to_rgb8(*cluster["lab"])),
                "lightness": round(cluster["lab"][0], 4),
                "area_ratio": round(cluster["area"], 4),
            }]
            merged.append(item)
            continue
        variants = target.setdefault("tonal_variants", [])
        variants.append({
            "hex": hex_from_rgb8(oklab_to_rgb8(*cluster["lab"])),
            "lightness": round(cluster["lab"][0], 4),
            "area_ratio": round(cluster["area"], 4),
        })
        total = target["weight"] + cluster["weight"]
        target["lab"] = tuple(
            (target["lab"][i] * target["weight"] + cluster["lab"][i] * cluster["weight"]) / total
            for i in range(3)
        )
        target["x"] = (target["x"] * target["weight"] + cluster["x"] * cluster["weight"]) / total
        target["y"] = (target["y"] * target["weight"] + cluster["y"] * cluster["weight"]) / total
        target["weight"] = total
        target["area"] += cluster["area"]
        target["spread"] = max(target["spread"], cluster["spread"])
        target["members"] = target.get("members", []) + cluster.get("members", [])
    return merged


def _tone_role(lightness: float) -> str:
    if lightness < 0.35:
        return "暗部"
    if lightness > 0.70:
        return "亮部"
    return "中间调"


def _position_hint(x: float, y: float) -> str:
    vertical = "上部" if y < 0.34 else ("下部" if y > 0.66 else "中部")
    horizontal = "偏左" if x < 0.38 else ("偏右" if x > 0.62 else "居中")
    return f"{vertical}{horizontal}"


def _plausible_skin(lightness: float, chroma: float, hue: float) -> bool:
    """肤色似然区间。这是几何启发，不是肤色识别；只用于提出人工验收提醒。"""
    return 0.40 <= lightness <= 0.92 and 0.020 <= chroma <= 0.115 and 25.0 <= hue <= 85.0


# 色相角度分级。来源：research/primary-sources-v4.json 的 V4S04（30/60/90/120/180 五档），
# 由 V4S03 独立印证（「120~180 对比色、60 以内调和色」）。属跨来源共识的技术事实，
# 因此可直接用于分析模块；它描述色相关系，不构成配方，也不代表审美结论。
HUE_TIERS = (
    (30.0, "类似色", "色相几乎重叠，画面高度统一，层次只能靠明度与彩度拉开"),
    (60.0, "邻近色", "色相相邻，统一而不单调，是最稳妥的和谐关系"),
    (90.0, "中差色", "色相拉开但未对立，既有变化又不冲突"),
    (150.0, "对比色", "色相明显对立，反差大，适合突出主体但需要控制面积"),
    (360.0, "互补色", "色相接近正对，冲突最强，必须靠面积与明度分出主次，否则互相污染"),
)
# 互补对。来源 V4S04；与本项目实测的通道增益→OKLab 轴向一致（R/B 增益移动黄蓝轴）。
COMPLEMENTARY_PAIRS = (("红", "青"), ("绿", "洋红"), ("黄", "蓝"))


def _hue_tier(distance: float) -> tuple[str, str]:
    for threshold, name, detail in HUE_TIERS:
        if distance <= threshold:
            return name, detail
    return HUE_TIERS[-1][1], HUE_TIERS[-1][2]


def detect_harmony(colors: list[dict]) -> dict:
    chromatic = [c for c in colors if c["role"] != "中性色" and c["oklch"]["chroma"] >= NEUTRAL_CHROMA]
    if len(chromatic) < 2:
        return {
            "relation": "单色／近中性",
            "max_hue_distance": 0.0,
            "detail": "画面颜色集中在一个色相或几乎无彩，靠明暗而非色相区分层次",
            "tier_source": "V4S04（由 V4S03 印证）",
        }
    ordered = sorted(chromatic, key=lambda c: -c["area_ratio"])
    hues = [c["oklch"]["hue"] for c in ordered]
    anchor = hues[0]
    distances = [hue_distance(anchor, h) for h in hues[1:]]
    widest = max(distances)
    relation, detail = _hue_tier(widest)

    compound = None
    if len(hues) >= 3:
        if any(120.0 <= d <= 150.0 for d in distances) and any(160.0 <= d <= 200.0 for d in distances):
            compound = "分裂互补"
        elif all(any(abs(hue_distance(a, b) - 120.0) < 30.0 for b in hues if b is not a)
                 for a in hues[:3]):
            compound = "三角对立"
    if compound:
        relation = f"{relation}（{compound}）"
        detail += f"；整体呈{compound}结构"

    warm = [h for h in hues if 20.0 <= h <= 115.0]
    cool = [h for h in hues if 190.0 <= h <= 300.0]
    if warm and cool:
        relation = f"{relation}（暖冷分立）"
        detail += "；同时存在暖冷两侧，可用于建立空间与情绪对立"

    return {
        "relation": relation,
        "max_hue_distance": round(widest, 1),
        "detail": detail,
        "tier_table": [
            {"upper_bound_deg": threshold, "name": name}
            for threshold, name, _ in HUE_TIERS
        ],
        "tier_source": "V4S04（由 V4S03 印证）；见 research/primary-sources-v4.json",
        "reinforcement_paths": (
            "要强化某个色相有三条等价路径：直接加它、加它两侧的邻近色、减它的互补色"
            f"（互补对：{'、'.join(a + '↔' + b for a, b in COMPLEMENTARY_PAIRS)}）。"
            "同时朝互补两侧用力会互相抵消，属于常见错误。"
        ),
    }


def assess_risks(colors: list[dict], source_label: str, person_present: bool | None = None) -> list[str]:
    risks: list[str] = []
    chromatic = [c for c in colors if c["oklch"]["chroma"] >= NEUTRAL_CHROMA]
    if chromatic:
        mean_chroma = sum(c["oklch"]["chroma"] * c["area_ratio"] for c in chromatic) / max(
            1e-6, sum(c["area_ratio"] for c in chromatic)
        )
        if mean_chroma < 0.042:
            risks.append("有彩区域彩度偏低，继续降饱和会整体坍缩为脏灰")
    else:
        risks.append("几乎不存在有彩区域；任何色彩语法都只能靠明暗成立")
    skin = [c for c in colors if c["skin_plausible"]]
    if skin:
        total = sum(c["area_ratio"] for c in skin)
        if person_present is False:
            # 语义后端已确认本图没有人物。暖色落在肤色色相区多半是木材、石材、
            # 秋叶或暖光，此时再喊肤色风险就是噪声，会淹没真正的风险。
            risks.append(
                f"有 {total:.1%} 面积落在暖色区间，但语义后端未检测到人物；按材质（石材／木材／暖光）处理，不按肤色处理"
            )
        elif person_present is True:
            risks.append(f"素材中确有人物，且 {total:.1%} 面积落在肤色似然区间；改色相或去饱和必须逐一确认")
        else:
            risks.append(
                f"存在肤色似然区间的颜色（合计面积 {total:.1%}）——这是**几何启发**不是肤色识别，"
                "暖色的木材、石材、秋叶、暖光都会落进这个区间。"
                "本次未拿到语义判定，改色相或去饱和前请自己看一眼画面里有没有人")
    accent = [c for c in colors if c["role"] == "强调色"]
    for item in accent:
        if item["area_ratio"] > 0.20:
            risks.append(f"强调色 {item['hex']} 面积达 {item['area_ratio']:.1%}，已不再是强调，会与主色争夺注意")
    for item in colors:
        if item["oklch"]["lightness"] > 0.90 and item["oklch"]["chroma"] > 0.05:
            risks.append(f"高光区 {item['hex']} 带明显色偏，容易读成染色而非光")
        if item["oklch"]["lightness"] < 0.10 and item["area_ratio"] > 0.08:
            risks.append(f"暗部 {item['hex']} 已接近死黑且占 {item['area_ratio']:.1%}，继续压暗会丢失结构")
        if item["y"] < 0.34 and item["area_ratio"] > 0.20 and item["spread"] < 0.05:
            risks.append(f"上部大面积均匀色块 {item['hex']}（{item['area_ratio']:.1%}），做渐变或去朦胧时需检查断层")
    if not risks:
        risks.append("未发现结构性风险；仍需人工确认主体是否仍是第一视觉重心")
    return list(dict.fromkeys(risks))[:6]


def build_palette(
    bins: list[dict],
    profile: str,
    seed: int,
    max_colors: int = MAX_COLORS,
    label: str = "原图",
    person_present: bool | None = None,
) -> dict:
    if not bins:
        raise PaletteError("没有可用像素")
    total_weight = sum(item["count"] for item in bins)
    chromatic_bins = [b for b in bins if b["chroma"] >= NEUTRAL_CHROMA]
    neutral_bins = [b for b in bins if b["chroma"] < NEUTRAL_CHROMA]
    chromatic_share = sum(b["count"] for b in chromatic_bins) / total_weight

    # 有彩与中性分池：防止大面积低彩背景把有彩簇拖向灰轴（历史「脏灰」失败）。
    if chromatic_share >= 0.06 and len(chromatic_bins) >= 3:
        neutral_slots = 1 if neutral_bins else 0
        if neutral_bins and sum(b["count"] for b in neutral_bins) / total_weight > 0.45:
            neutral_slots = 2
        chromatic_slots = max(2, max_colors - neutral_slots)
        clusters = _weighted_kmeans(chromatic_bins, chromatic_slots, seed)
        for cluster in clusters:
            cluster["area"] = cluster["weight"] / total_weight
            cluster["pool"] = "chromatic"
        if neutral_slots:
            neutral_clusters = _weighted_kmeans(neutral_bins, neutral_slots, seed ^ 0x5F5F)
            for cluster in neutral_clusters:
                cluster["area"] = cluster["weight"] / total_weight
                cluster["pool"] = "neutral"
            clusters += neutral_clusters
        pooling = "chromatic-neutral-split"
    else:
        clusters = _weighted_kmeans(bins, max_colors, seed)
        for cluster in clusters:
            cluster["pool"] = "single"
        pooling = "single-pool"

    clusters = _merge_close(clusters)
    clusters = _merge_hue_family(clusters)
    clusters = [c for c in clusters if c["area"] >= 0.012] or clusters
    clusters.sort(key=lambda item: -item["area"])
    if len(clusters) > max_colors:
        clusters = clusters[:max_colors]
    while len(clusters) < MIN_COLORS and len(clusters) < len(bins):
        extra = _weighted_kmeans(bins, len(clusters) + 1, seed + len(clusters))
        extra = _merge_close(extra)
        if len(extra) <= len(clusters):
            break
        clusters = sorted(extra, key=lambda item: -item["area"])[:max_colors]

    area_total = sum(c["area"] for c in clusters) or 1.0
    colors = []
    for cluster in clusters:
        lab = cluster["lab"]
        lightness, chroma, hue = oklab_to_oklch(*lab)
        rgb = oklab_to_rgb8(*lab, profile=profile)
        colors.append({
            "hex": hex_from_rgb8(rgb),
            "rgb": list(rgb),
            "oklch": {"lightness": round(lightness, 4), "chroma": round(chroma, 4), "hue": round(hue, 1)},
            "oklch_css": format_oklch(lightness, chroma, hue),
            "area_ratio": round(cluster["area"] / area_total, 4),
            "tone_role": _tone_role(lightness),
            "position_hint": _position_hint(cluster["x"], cluster["y"]),
            "x": round(cluster["x"], 4),
            "y": round(cluster["y"], 4),
            "spread": round(cluster["spread"], 4),
            "skin_plausible": _plausible_skin(lightness, chroma, hue),
            "pool": cluster.get("pool", "single"),
            "tonal_variants": cluster.get("tonal_variants", []),
            "_members": cluster.get("members", []),
        })

    colors.sort(key=lambda item: -item["area_ratio"])
    for rank, color in enumerate(colors, start=1):
        color["dominance_rank"] = rank
    chromatic = [c for c in colors if c["oklch"]["chroma"] >= NEUTRAL_CHROMA]
    primary = chromatic[0] if chromatic else colors[0]
    chromatic_area = sum(c["area_ratio"] for c in chromatic)
    if not chromatic:
        character = "全中性：画面几乎没有可辨色相，层次只能靠明暗建立"
    elif chromatic_area < 0.20:
        character = f"中性主导：有彩区域仅占 {chromatic_area:.1%}，主色是画面里少数带色相的区域，不是面积最大的区域"
    elif chromatic_area < 0.55:
        character = f"中性承载、彩色点题：有彩区域占 {chromatic_area:.1%}"
    else:
        character = f"有彩主导：有彩区域占 {chromatic_area:.1%}，色相关系直接决定观感"
    for color in colors:
        if color["oklch"]["chroma"] < NEUTRAL_CHROMA:
            color["role"] = "中性色"
            color["is_ground"] = color["dominance_rank"] == 1
        elif color is primary:
            color["role"] = "主色"
        elif color["area_ratio"] <= 0.18 and (
            color["oklch"]["chroma"] >= primary["oklch"]["chroma"] * 1.12
            or hue_distance(color["oklch"]["hue"], primary["oklch"]["hue"]) >= 90.0
        ):
            color["role"] = "强调色"
        else:
            color["role"] = "辅色"

    harmony = detect_harmony(colors)
    risks = assess_risks(colors, label, person_present)
    if chromatic and chromatic_area < 0.20:
        risks.insert(0, f"主色 {primary['hex']} 只占 {primary['area_ratio']:.1%}；把它当全画面基调会误判方向")
    return {
        "schema_version": PALETTE_SCHEMA_VERSION,
        "label": label,
        "working_profile": "Display P3" if profile == "display-p3" else "sRGB",
        "color_count": len(colors),
        "pooling": pooling,
        "chromatic_share": round(chromatic_share, 4),
        "palette_character": character,
        "extraction": {
            "space": "OKLab",
            "method": "内容派生种子的确定性加权 k-means++（有彩／中性分池）",
            "quantization_bits": QUANT_BITS,
            "sampled_bins": len(bins),
            "boundary": "色卡描述颜色构成与面积，不是 LUT，也不代表调色结果或审美结论",
        },
        "colors": colors,
        "harmony": harmony,
        "risks": risks,
    }


def content_seed(data: bytes) -> int:
    import hashlib
    return int(hashlib.sha256(data).hexdigest()[:8], 16)


def palette_svg(palette: dict, title: str) -> str:
    colors = palette["colors"]
    width, height = 900, 300
    bar_y, bar_h = 96, 120
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#FAFAF8"/>',
        f'<text x="32" y="46" font-family="-apple-system,Helvetica,sans-serif" font-size="22" fill="#1B1B1B">{title}</text>',
        f'<text x="32" y="72" font-family="-apple-system,Helvetica,sans-serif" font-size="13" fill="#6B6B6B">'
        f'{palette["harmony"]["relation"]} · {palette["working_profile"]} · {palette["color_count"]} 色</text>',
    ]
    x = 32.0
    usable = width - 64
    for color in colors:
        w = max(24.0, usable * color["area_ratio"])
        parts.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{w:.1f}" height="{bar_h}" fill="{color["hex"]}"/>')
        parts.append(
            f'<text x="{x + 6:.1f}" y="{bar_y + bar_h + 22}" font-family="-apple-system,Helvetica,sans-serif" '
            f'font-size="12" fill="#1B1B1B">{color["hex"]}</text>'
        )
        parts.append(
            f'<text x="{x + 6:.1f}" y="{bar_y + bar_h + 40}" font-family="-apple-system,Helvetica,sans-serif" '
            f'font-size="11" fill="#6B6B6B">{color["role"]} {color["area_ratio"]:.0%}</text>'
        )
        parts.append(
            f'<text x="{x + 6:.1f}" y="{bar_y + bar_h + 56}" font-family="-apple-system,Helvetica,sans-serif" '
            f'font-size="10" fill="#8A8A8A">{color["tone_role"]}</text>'
        )
        x += w
    parts.append("</svg>")
    return "\n".join(parts)


def extract_from_media(
    path: Path,
    profile: str,
    width: int,
    height: int,
    media_type: str = "photo",
    at_time: float | None = None,
    frames: int = 1,
    label: str = "原图",
    max_colors: int = MAX_COLORS,
    person_present: bool | None = None,
) -> dict:
    data, sample_w, sample_h = sample_rgb_grid(
        path, width, height, media_type=media_type, at_time=at_time, frames=frames
    )
    bins = build_bins(data, sample_w, sample_h, profile)
    palette = build_palette(bins, profile, content_seed(data), max_colors=max_colors, label=label,
                            person_present=person_present)
    palette["source"] = {"path": str(path), "sample": f"{sample_w}x{sample_h}", "at_time": at_time}
    return palette


def main() -> int:
    parser = argparse.ArgumentParser(description="从真实像素提取配色色卡（不生成调色结果）")
    parser.add_argument("--input", required=True)
    parser.add_argument("--profile", default="srgb", choices=["srgb", "display-p3"])
    parser.add_argument("--label", default="原图")
    parser.add_argument("--max-colors", type=int, default=MAX_COLORS)
    parser.add_argument("--at-time", type=float)
    parser.add_argument("--svg-out")
    args = parser.parse_args()
    path = Path(args.input).expanduser().resolve()
    probe = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    try:
        palette = extract_from_media(
            path, args.profile, int(stream["width"]), int(stream["height"]),
            at_time=args.at_time, label=args.label,
            max_colors=max(MIN_COLORS, min(MAX_COLORS, args.max_colors)),
        )
    except PaletteError as error:
        print(str(error), file=sys.stderr)
        return 3
    if args.svg_out:
        Path(args.svg_out).write_text(palette_svg(palette, args.label), encoding="utf-8")
        palette["svg_path"] = args.svg_out
    print(json.dumps(palette, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- 目标色卡预测 -------------------------------------------------------
# 思路：把原图色卡的色块喂进该方案的真实滤镜链，读回来的就是这些颜色在该方案下
# 的精确去向。它不是「意图描述」，而是可复现、可验证的逐点变换结果。
# 空间性滤镜（锐化、暗角、径向场、颗粒）对纯色块没有意义，必须剔除并声明。

SPATIAL_FILTER_PREFIXES = ("unsharp", "geq", "vignette", "noise", "split", "blend", "crop", "rotate")


def strip_spatial_filters(filtergraph: str) -> tuple[str, list[str]]:
    kept, dropped = [], []
    for item in filtergraph.split(","):
        name = item.strip()
        if not name:
            continue
        if name.startswith(SPATIAL_FILTER_PREFIXES) or "[" in name or "]" in name:
            dropped.append(name.split("=")[0])
        else:
            kept.append(name)
    return ",".join(kept), sorted(set(dropped))


def transform_colors(colors: list[tuple[int, int, int]], filtergraph: str) -> list[tuple[int, int, int]]:
    """用真实 ffmpeg 滤镜链变换一组颜色，返回变换后的颜色。"""
    if not colors:
        return []
    block = 8
    width, height = len(colors) * block, block
    raw = bytearray()
    for _ in range(height):
        for rgb in colors:
            raw.extend(bytes(rgb) * block)
    chain, _ = strip_spatial_filters(filtergraph)
    command = [
        _ffmpeg(), "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-i", "pipe:0",
    ]
    if chain:
        command += ["-vf", chain]
    command += ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    result = subprocess.run(command, input=bytes(raw), capture_output=True)
    if result.returncode or len(result.stdout) < width * height * 3:
        raise PaletteError(f"目标色卡预测失败：{result.stderr.decode('utf-8','replace').strip()[:200]}")
    data = result.stdout
    center_row = (height // 2) * width * 3
    output = []
    for index in range(len(colors)):
        offset = center_row + (index * block + block // 2) * 3
        output.append((data[offset], data[offset + 1], data[offset + 2]))
    return output



def _transform_by_group(entries: list[dict], filtergraph: str) -> list[tuple[int, int, int]]:
    """按成员加权预测每一格的落点；没有成员信息时退回簇心变换。

    一次 ffmpeg 调用变换全部成员色，代价与簇心法同量级。
    """
    flat: list[tuple[int, int, int]] = []
    spans: list[tuple[int, int]] = []
    for entry in entries:
        members = entry.get("_members") or []
        start = len(flat)
        flat.extend(tuple(rgb) for rgb, _ in members)
        spans.append((start, len(flat)))
    if not flat:
        return transform_colors([tuple(e["rgb"]) for e in entries], filtergraph)

    moved = transform_colors(flat, filtergraph)
    out: list[tuple[int, int, int]] = []
    for entry, (start, end) in zip(entries, spans):
        members = entry.get("_members") or []
        if start == end:
            out.append(transform_colors([tuple(entry["rgb"])], filtergraph)[0])
            continue
        total = sum(w for _, w in members) or 1
        acc = [0.0, 0.0, 0.0]
        for (_, weight), rgb in zip(members, moved[start:end]):
            for i in range(3):
                acc[i] += rgb[i] * weight
        out.append(tuple(max(0, min(255, int(round(v / total)))) for v in acc))
    return out



def predict_target_palette(source_palette: dict, filtergraph: str, profile: str = "srgb",
                           person_present: bool | None = None) -> dict:
    """原图色卡 → 目标色卡。面积比例沿用原图（调色不改变区域面积）。"""
    # 目标色卡承诺的是「色卡上这一格会变成什么」。诚实的答案是这组像素**实际落到哪**，
    # 也就是 mean(f(成员))，而不是 f(簇心)。调色是非线性的，簇一分散两者就分开：
    # 实测一张灰片的主色簇，f(簇心) 给 #643E1B，而整组实际落在 #362716——
    # 用户照着色卡等一个暖橙，拿到的是深褐。
    #
    # 之所以以前没暴露，是因为当时那次聚类恰好很紧；换一个种子（1 个 LSB 的采样差异
    # 就足以换种子）簇一变松，预测误差立刻从 0.012 跳到 0.070。
    # 那说明它一直是错的，只是被一次走运的聚类盖住了。
    source_colors = [tuple(item["rgb"]) for item in source_palette["colors"]]
    transformed = _transform_by_group(source_palette["colors"], filtergraph)
    _, dropped = strip_spatial_filters(filtergraph)
    colors = []
    for origin, rgb in zip(source_palette["colors"], transformed):
        lab = rgb8_to_oklab(*rgb, profile=profile)
        lightness, chroma, hue = oklab_to_oklch(*lab)
        source_lab = rgb8_to_oklab(*origin["rgb"], profile=profile)
        colors.append({
            "hex": hex_from_rgb8(rgb),
            "rgb": list(rgb),
            "from_hex": origin["hex"],
            "delta_e_ok": round(delta_e_ok(source_lab, lab), 4),
            "oklch": {"lightness": round(lightness, 4), "chroma": round(chroma, 4), "hue": round(hue, 1)},
            "oklch_css": format_oklch(lightness, chroma, hue),
            "area_ratio": origin["area_ratio"],
            "role": origin["role"],
            "tone_role": _tone_role(lightness),
            "position_hint": origin["position_hint"],
            "x": origin["x"], "y": origin["y"], "spread": origin["spread"],
            "skin_plausible": _plausible_skin(lightness, chroma, hue),
            "was_skin_plausible": origin["skin_plausible"],
            "pool": origin["pool"],
            "tonal_variants": [],
        })
    harmony = detect_harmony(colors)
    risks = assess_risks(colors, "目标", person_present)
    for item in colors:
        if item["was_skin_plausible"] and not item["skin_plausible"]:
            risks.insert(0, f"{item['from_hex']} 原本落在肤色似然区间，方案会把它推出该区间（ΔEok {item['delta_e_ok']}），必须人工确认是否为人物")
    # 大面积颜色的剧烈位移是「方向错误」的可测特征。
    # 灰片2 的历史 P0（黑曜金界把秋日建筑压暗染青）在这里表现为：
    # 53% 面积的中性色明度掉 15 点、色相从 78° 转到 124°。
    for origin, item in zip(source_palette["colors"], colors):
        if origin["area_ratio"] < 0.15:
            continue
        light_drop = origin["oklch"]["lightness"] - item["oklch"]["lightness"]
        if light_drop > 0.12:
            risks.insert(0, (
                f"占 {origin['area_ratio']:.0%} 面积的 {origin['hex']} 明度被压低 {light_drop:.2f}"
                f"（{origin['oklch']['lightness']:.0%}→{item['oklch']['lightness']:.0%}），"
                "大面积基调被整体压暗，请确认这是叙事意图而不是路由错误"
            ))
        if origin["oklch"]["chroma"] >= 0.008 and item["oklch"]["chroma"] >= 0.008:
            swing = hue_distance(origin["oklch"]["hue"], item["oklch"]["hue"])
            if swing > 30.0:
                risks.insert(0, (
                    f"占 {origin['area_ratio']:.0%} 面积的 {origin['hex']} 色相被推动 {swing:.0f}°"
                    f"（{origin['oklch']['hue']:.0f}°→{item['oklch']['hue']:.0f}°），"
                    "大面积区域改色相属于强干预，必须确认记忆色是否还可信"
                ))

    moved = [c for c in colors if c["delta_e_ok"] >= 0.02]
    return {
        "schema_version": PALETTE_SCHEMA_VERSION,
        "label": "目标色卡",
        "working_profile": source_palette["working_profile"],
        "color_count": len(colors),
        "colors": colors,
        "harmony": harmony,
        "risks": list(dict.fromkeys(risks))[:7],
        "max_delta_e_ok": round(max((c["delta_e_ok"] for c in colors), default=0.0), 4),
        "mean_delta_e_ok": round(sum(c["delta_e_ok"] for c in colors) / max(1, len(colors)), 4),
        "colors_meaningfully_moved": len(moved),
        "prediction": {
            "method": "把原图色卡色块送入该方案的真实 ffmpeg 滤镜链，读回逐点变换结果",
            "deterministic": True,
            "excluded_spatial_filters": dropped,
            "boundary": (
                "空间性滤镜（锐化／暗角／径向场／颗粒）对纯色块没有意义，已剔除；"
                "因此目标色卡描述颜色去向，不描述质感与局部效果。"
            ),
        },
    }


# --- PNG 输出 -----------------------------------------------------------
def palette_png(palette: dict, out_path: Path, profile: str = "srgb",
                width: int = 900, height: int = 220) -> dict:
    """把色卡渲染成带正确色彩标签的 PNG。

    色块必须用 ffmpeg 写出并打上与素材一致的色彩标签，
    否则用户在 P3 屏幕上看到的色卡和实际调色结果不是同一套颜色。
    """
    colors = palette["colors"]
    if not colors:
        raise PaletteError("色卡为空，无法输出 PNG")
    raw = bytearray()
    boundaries = []
    accumulated = 0.0
    for index, color in enumerate(colors):
        share = color["area_ratio"] if index < len(colors) - 1 else 1.0 - accumulated
        accumulated += share
        boundaries.append(max(1, int(round(share * width))))
    drift = width - sum(boundaries)
    boundaries[-1] += drift
    row = bytearray()
    for color, span in zip(colors, boundaries):
        row.extend(bytes(color["rgb"]) * max(0, span))
    row = row[: width * 3]
    if len(row) < width * 3:
        row.extend(bytes(colors[-1]["rgb"]) * ((width * 3 - len(row)) // 3))
    for _ in range(height):
        raw.extend(row)
    if profile == "display-p3":
        tags = ("setparams=colorspace=gbr:color_primaries=smpte432:"
                "color_trc=iec61966-2-1:range=pc,format=rgb48be")
        pixel_format = "rgb48be"
    else:
        tags = ("setparams=colorspace=gbr:color_primaries=bt709:"
                "color_trc=iec61966-2-1:range=pc,format=rgb24")
        pixel_format = "rgb24"
    if out_path.exists():
        raise PaletteError(f"色卡文件已存在，不会覆盖：{out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [_ffmpeg(), "-v", "error", "-n", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-i", "pipe:0", "-vf", tags,
         "-frames:v", "1", "-c:v", "png", "-pix_fmt", pixel_format, str(out_path)],
        input=bytes(raw), capture_output=True,
    )
    if result.returncode:
        raise PaletteError(
            f"色卡 PNG 写出失败：{result.stderr.decode('utf-8','replace').strip()[:160]}"
        )
    return {"path": str(out_path), "width": width, "height": height,
            "pixel_format": pixel_format,
            "color_profile": "Display P3" if profile == "display-p3" else "sRGB",
            "band_widths": boundaries,
            "boundary": "色块宽度按面积比例；色卡不是 LUT，也不代表调色结果"}


# --- 结果色卡与偏差比对 --------------------------------------------------
def verify_target_by_regions(source_path: Path, output_path: Path, source_palette: dict,
                             target_palette: dict, profile: str,
                             width: int, height: int) -> dict:
    """按同一空间网格的区域对应，验证目标色卡的预测是否成立。

    为什么不能直接拿「目标色卡」和「结果色卡」按最近色配对：
    结果色卡是对输出**重新聚类**得到的，聚类结构本身会变
    （实测面积从 53/45/2 变成 57/36/7），按最近色配对等于在比不同的区域，
    量出来的 ΔEok 0.119 反映的是聚类差异而不是预测误差。

    正确问法是：原图中属于色卡第 i 色的那些像素，在输出里实际变成了什么颜色？
    因为源与输出在同一网格上逐点对应，这个问题有确定答案。
    """
    try:
        source_data, sample_w, sample_h = sample_rgb_grid(source_path, width, height)
        output_data, out_w, out_h = sample_rgb_grid(output_path, width, height)
    except PaletteError as error:
        return {"available": False, "reason": str(error)}
    if (sample_w, sample_h) != (out_w, out_h):
        return {"available": False, "reason": "源与输出的采样网格尺寸不一致，无法做区域对应"}
    centres = [rgb8_to_oklab(*item["rgb"], profile=profile) for item in source_palette["colors"]]
    if not centres:
        return {"available": False, "reason": "源色卡为空"}

    # 归组必须与色卡自己的分池一致，否则比的是两批不同的像素。
    #
    # 色卡是**有彩池与中性池分开聚类**的（防止大面积低彩背景把有彩簇拖向灰轴）。
    # 这里原本用「全局最近色心」归组：一个近中性像素完全可能离某个有彩色心更近，
    # 于是被算进那一簇。实测一张灰片，主色簇的 source_area 是 4.04%，
    # 而全局最近口径把 20.76% 的像素算了进来——五倍多，
    # 于是「实际落点」根本不是这格色卡对应的那批像素，
    # 预测误差 0.12 量的是归组差异，不是预测精度。
    #
    # 这个口径冲突本来就写在下面的 area_note 里，却仍然被拿来算误差。
    # 现在改成按同一分池就近归组：有彩像素只落到有彩簇，中性像素只落到中性簇。
    pools = [item.get("pool", "single") for item in source_palette["colors"]]
    chroma_idx = [i for i, pool in enumerate(pools) if pool != "neutral"]
    neutral_idx = [i for i, pool in enumerate(pools) if pool == "neutral"]

    def candidates_for(chroma: float) -> list:
        if chroma >= NEUTRAL_CHROMA and chroma_idx:
            return chroma_idx
        if chroma < NEUTRAL_CHROMA and neutral_idx:
            return neutral_idx
        return list(range(len(centres)))

    sums = [[0.0, 0.0, 0.0, 0] for _ in centres]
    pixel_count = min(len(source_data), len(output_data)) // 3
    for index in range(pixel_count):
        offset = index * 3
        lab = rgb8_to_oklab(source_data[offset], source_data[offset + 1],
                            source_data[offset + 2], profile)
        _, chroma, _ = oklab_to_oklch(*lab)
        pool_candidates = candidates_for(chroma)
        best = min(pool_candidates, key=lambda i: delta_e_ok(lab, centres[i]))
        slot = sums[best]
        slot[0] += output_data[offset]
        slot[1] += output_data[offset + 1]
        slot[2] += output_data[offset + 2]
        slot[3] += 1
    rows = []
    for index, (item, target_item, slot) in enumerate(
            zip(source_palette["colors"], target_palette["colors"], sums)):
        if not slot[3]:
            continue
        actual = tuple(int(round(slot[c] / slot[3])) for c in range(3))
        predicted_lab = rgb8_to_oklab(*target_item["rgb"], profile=profile)
        actual_lab = rgb8_to_oklab(*actual, profile=profile)
        rows.append({
            "source_hex": item["hex"],
            "role": item["role"],
            "source_area": item["area_ratio"],
            "measured_area": round(slot[3] / max(1, pixel_count), 4),
            "predicted_hex": target_item["hex"],
            "actual_hex": hex_from_rgb8(actual),
            "prediction_error_delta_e_ok": round(delta_e_ok(predicted_lab, actual_lab), 4),
        })
    errors = [row["prediction_error_delta_e_ok"] for row in rows] or [0.0]
    mean_error = sum(errors) / len(errors)
    return {
        "available": True,
        "method": "源与输出在同一采样网格上逐点对应；按源色卡归属分组后比较实际去向与预测去向",
        "rows": rows,
        "mean_prediction_error": round(mean_error, 4),
        "max_prediction_error": round(max(errors), 4),
        "verdict": (
            "目标色卡预测成立" if mean_error < 0.02
            else ("预测基本成立，差异来自空间性滤镜与编码量化" if mean_error < 0.05
                  else "预测与实际偏差较大，目标色卡不可直接当作结果承诺")
        ),
        "area_note": (
            "source_area 与 measured_area 现在同为分池口径：有彩像素只归到有彩簇、"
            "中性像素只归到中性簇，与色卡自己的聚类方式一致。"
            "两者仍会有出入——source_area 按量化 bin 统计，measured_area 按逐像素统计，"
            "边界像素的归属会有差别；但不再是此前「全局最近色心」那种五倍量级的口径冲突"
            "（实测主色簇 4.04% 被算成 20.76%，误差量的是归组差异而不是预测精度）。"
        ),
        "boundary": "只验证颜色去向的预测精度，不代表审美通过",
    }


def compare_palettes(target: dict, result: dict) -> dict:
    """把「目标色卡」与「结果色卡」按最近色配对做粗比。

    注意：结果色卡是对输出重新聚类得到的，聚类结构会变，
    因此这里的 ΔEok 同时包含了聚类差异，**不能**当作预测误差。
    要验证预测精度请用 verify_target_by_regions。
    """
    pairs = []
    for target_color in target["colors"]:
        best = None
        best_distance = float("inf")
        for result_color in result["colors"]:
            distance = delta_e_ok(
                rgb8_to_oklab(*target_color["rgb"]),
                rgb8_to_oklab(*result_color["rgb"]),
            )
            if distance < best_distance:
                best, best_distance = result_color, distance
        if best is None:
            continue
        pairs.append({
            "target_hex": target_color["hex"],
            "result_hex": best["hex"],
            "role": target_color.get("role"),
            "target_area": target_color["area_ratio"],
            "result_area": best["area_ratio"],
            "delta_e_ok": round(best_distance, 4),
            "area_drift": round(best["area_ratio"] - target_color["area_ratio"], 4),
        })
    deltas = [item["delta_e_ok"] for item in pairs] or [0.0]
    mean_delta = sum(deltas) / len(deltas)
    return {
        "pairs": pairs,
        "mean_delta_e_ok": round(mean_delta, 4),
        "max_delta_e_ok": round(max(deltas), 4),
        "agreement": (
            "目标与结果高度一致" if mean_delta < 0.02
            else ("存在可见偏差，多由锐化／颗粒／暗角等空间性滤镜与编码量化引起"
                  if mean_delta < 0.06 else "偏差较大，需人工确认目标色卡是否仍然可信")
        ),
        "boundary": "颜色比对不代表审美一致；面积漂移主要来自聚类边界而非调色本身",
    }


# --- 视频：逐镜头色卡与全片共享色卡 --------------------------------------
def video_palettes(path: Path, profile: str, width: int, height: int,
                   shot_list: list[dict], max_colors: int = MAX_COLORS,
                   person_present: bool | None = None) -> dict:
    """逐镜头色卡 + 全片共享色卡。

    全片色卡不是把所有镜头的颜色平均——那会把不同镜头的主色糊成脏灰。
    正确做法是把各镜头的像素直方图按镜头时长加权合并后再聚类，
    这样一个短镜头里的强调色不会被长镜头淹没到消失，也不会被无中生有地放大。
    """
    per_shot = []
    merged_bins: list[dict] = []
    total_duration = sum(max(0.001, item["duration"]) for item in shot_list) or 1.0
    for shot in shot_list:
        centre = shot["start"] + shot["duration"] * 0.5
        try:
            data, sample_w, sample_h = sample_rgb_grid(
                path, width, height, media_type="video", at_time=centre
            )
        except PaletteError:
            continue
        bins = build_bins(data, sample_w, sample_h, profile)
        if not bins:
            continue
        shot_palette = build_palette(
            bins, profile, content_seed(data), max_colors=max_colors,
            label=f"镜头 #{shot['index']} 色卡", person_present=person_present,
        )
        shot_palette["shot_index"] = shot["index"]
        shot_palette["time_range"] = [shot["start"], shot["end"]]
        per_shot.append(shot_palette)
        weight = max(0.001, shot["duration"]) / total_duration
        for item in bins:
            merged_bins.append({**item, "count": item["count"] * weight})
    if not per_shot:
        raise PaletteError("没有任何镜头可以提取色卡")
    shared = build_palette(
        merged_bins, profile, content_seed(str(len(merged_bins)).encode()),
        max_colors=max_colors, label="全片共享色卡", person_present=person_present,
    )
    shared["derivation"] = (
        "各镜头像素直方图按镜头时长加权合并后聚类；不是把镜头色卡取平均，"
        "因此短镜头的强调色不会消失，也不会被放大"
    )
    shared["shot_count"] = len(per_shot)
    # 镜头间色彩一致性：全片色卡里每个色在各镜头中的最近距离
    divergence = []
    for shot_palette in per_shot:
        distances = []
        for shared_color in shared["colors"]:
            best = min(
                (delta_e_ok(rgb8_to_oklab(*shared_color["rgb"]), rgb8_to_oklab(*item["rgb"]))
                 for item in shot_palette["colors"]),
                default=0.0,
            )
            distances.append(best)
        divergence.append({
            "shot_index": shot_palette["shot_index"],
            "max_distance_to_shared": round(max(distances) if distances else 0.0, 4),
            "mean_distance_to_shared": round(sum(distances) / max(1, len(distances)), 4),
        })
    worst = max(divergence, key=lambda item: item["max_distance_to_shared"]) if divergence else None
    return {
        "schema_version": PALETTE_SCHEMA_VERSION,
        "per_shot": per_shot,
        "shared": shared,
        "shot_divergence": divergence,
        "least_consistent_shot": worst,
        "boundary": (
            "逐镜头色卡描述各镜头自身的颜色构成；全片共享色卡描述整体色彩语法。"
            "两者差距大说明镜头之间颜色不统一，需要回到逐镜头校正而不是靠全局 Look 掩盖。"
        ),
    }
