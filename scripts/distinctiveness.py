#!/usr/bin/env python3
"""配方独特性的可测量框架。

为什么需要它：体检发现 32 套配方只有 7 种 visual_targets 组合，其中 10 套共用同一种；
参数向量最近的两套距离只有 0.1812。「独特」如果只是名字和文案不同，那就是自欺。

这个模块不判断「好不好看」——那是人的事。它只回答一个能被证伪的问题：
**把同一张素材交给两套配方，结果在感知空间里分得开吗？**

指纹取自渲染结果而不是参数表，因为参数不同不代表结果不同（曲线与增益会互相抵消），
参数相同也不代表结果相同（自适应一级校正会让同参数在不同素材上落到不同地方）。
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from color_space import oklab_to_oklch, rgb8_to_oklab

DISTINCTIVENESS_SCHEMA_VERSION = "4.0.0"
GRID_W, GRID_H = 64, 48
# 指纹的六个分量，各自描述一个独立的视觉维度。
FINGERPRINT_DIMS = (
    "tone_curve",      # 影调映射形状：把源亮度分桶后看各桶落到哪
    "hue_histogram",   # 色相分布：画面的颜色构成
    "chroma_by_luma",  # 明度依赖彩度：暗部/中间调/亮部各自多彩
    "local_contrast",  # 局部对比：中频结构强度
    "neutral_axis",    # 中性轴偏移：白平衡性格
    "shadow_shape",    # 暗部处理：抬趾还是压趾
)


class DistinctivenessError(Exception):
    pass


def _sample(path: Path) -> list[tuple[int, int, int]]:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"format=rgb24,scale={GRID_W}:{GRID_H}:flags=area",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True)
    if result.returncode or not result.stdout:
        raise DistinctivenessError(f"无法采样：{path}")
    data = result.stdout
    return [(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data) - 2, 3)]


def fingerprint(source: Path, rendered: Path, profile: str = "srgb") -> dict:
    """从「同一素材的源与结果」这一对里提取六维指纹。

    每一维都归一化到可比较的尺度，否则某一维会仅因量纲大而支配距离。
    """
    before, after = _sample(source), _sample(rendered)
    count = min(len(before), len(after))
    if count < 100:
        raise DistinctivenessError("采样点不足")

    labs_before = [rgb8_to_oklab(*before[i], profile=profile) for i in range(count)]
    labs_after = [rgb8_to_oklab(*after[i], profile=profile) for i in range(count)]

    # 1) 影调映射：源亮度分 8 桶，记录每桶的输出亮度均值
    buckets: list[list[float]] = [[] for _ in range(8)]
    for i in range(count):
        index = min(7, int(labs_before[i][0] * 8))
        buckets[index].append(labs_after[i][0])
    tone_curve = [round(statistics.fmean(b), 5) if b else -1.0 for b in buckets]
    filled = [v for v in tone_curve if v >= 0]
    if filled:
        tone_curve = [v if v >= 0 else statistics.fmean(filled) for v in tone_curve]

    # 2) 色相直方图：12 扇区，按彩度加权（灰色不该投票给任何色相）
    hue_hist = [0.0] * 12
    total_chroma = 0.0
    for lab in labs_after:
        _, chroma, hue = oklab_to_oklch(*lab)
        if chroma < 0.02:
            continue
        hue_hist[int(hue // 30) % 12] += chroma
        total_chroma += chroma
    if total_chroma > 0:
        hue_hist = [round(v / total_chroma, 5) for v in hue_hist]

    # 3) 明度依赖彩度：暗/中/亮三段各自的平均彩度
    zones: list[list[float]] = [[], [], []]
    for lab in labs_after:
        _, chroma, _ = oklab_to_oklch(*lab)
        zones[0 if lab[0] < 0.33 else (1 if lab[0] < 0.66 else 2)].append(chroma)
    chroma_by_luma = [round(statistics.fmean(z), 5) if z else 0.0 for z in zones]

    # 4) 局部对比：相邻像素亮度差的均值
    edges = 0.0
    edge_count = 0
    for y in range(GRID_H):
        row = y * GRID_W
        for x in range(1, GRID_W):
            if row + x >= count:
                break
            edges += abs(labs_after[row + x][0] - labs_after[row + x - 1][0])
            edge_count += 1
    local_contrast = round(edges / max(1, edge_count), 6)

    # 5) 中性轴偏移：近中性像素在 a/b 上的落点
    neutral = [lab for lab in labs_after if math.hypot(lab[1], lab[2]) < 0.05]
    if neutral:
        neutral_axis = [round(statistics.fmean(x[1] for x in neutral), 5),
                        round(statistics.fmean(x[2] for x in neutral), 5)]
    else:
        neutral_axis = [0.0, 0.0]

    # 6) 暗部形状：最暗 20% 的源像素被映射到哪，抬趾为正、压趾为负
    ordered = sorted(range(count), key=lambda i: labs_before[i][0])
    dark = ordered[: max(1, count // 5)]
    shadow_shape = round(
        statistics.fmean(labs_after[i][0] - labs_before[i][0] for i in dark), 5)

    return {
        "tone_curve": tone_curve,
        "hue_histogram": hue_hist,
        "chroma_by_luma": chroma_by_luma,
        "local_contrast": local_contrast,
        "neutral_axis": neutral_axis,
        "shadow_shape": shadow_shape,
    }


def distance(first: dict, second: dict) -> dict:
    """两个指纹的分维距离与总距离。

    分维报告是必要的：两套配方可能总距离够远但只在一个维度上不同，
    那种「独特」很脆弱——换个素材就重合了。
    """
    tone = math.sqrt(sum((a - b) ** 2 for a, b in
                         zip(first["tone_curve"], second["tone_curve"])) / 8)
    hue = sum(abs(a - b) for a, b in
              zip(first["hue_histogram"], second["hue_histogram"])) / 2
    chroma = math.sqrt(sum((a - b) ** 2 for a, b in
                           zip(first["chroma_by_luma"], second["chroma_by_luma"])) / 3)
    contrast = abs(first["local_contrast"] - second["local_contrast"])
    axis = math.hypot(first["neutral_axis"][0] - second["neutral_axis"][0],
                      first["neutral_axis"][1] - second["neutral_axis"][1])
    shadow = abs(first["shadow_shape"] - second["shadow_shape"])
    # 权重让六维在典型取值下量级相当，避免某一维支配总距离。
    per_dim = {
        "tone_curve": round(tone * 6.0, 5),
        "hue_histogram": round(hue * 2.0, 5),
        "chroma_by_luma": round(chroma * 12.0, 5),
        "local_contrast": round(contrast * 25.0, 5),
        "neutral_axis": round(axis * 20.0, 5),
        "shadow_shape": round(shadow * 12.0, 5),
    }
    total = math.sqrt(sum(v ** 2 for v in per_dim.values()))
    active = [k for k, v in per_dim.items() if v >= 0.05]
    return {
        "per_dimension": per_dim,
        "total": round(total, 5),
        "active_dimensions": active,
        "dimension_count": len(active),
    }


def analyze(pairs: dict[str, dict], min_distance: float = 0.25,
            min_dimensions: int = 2) -> dict:
    """给一批「配方 → 指纹」做两两比较，找出坍缩在一起的配方。

    两条判据缺一不可：
    - 总距离要够远；
    - 至少在两个维度上不同。只靠单一维度拉开的差异换个素材就会重合。
    """
    ids = sorted(pairs)
    collisions = []
    fragile = []
    distances = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            result = distance(pairs[ids[i]], pairs[ids[j]])
            distances.append((result["total"], ids[i], ids[j], result))
            if result["total"] < min_distance:
                collisions.append({"pair": [ids[i], ids[j]], **result})
            elif result["dimension_count"] < min_dimensions:
                fragile.append({"pair": [ids[i], ids[j]], **result})
    distances.sort(key=lambda item: item[0])
    values = [d[0] for d in distances]
    return {
        "schema_version": DISTINCTIVENESS_SCHEMA_VERSION,
        "recipe_count": len(ids),
        "pair_count": len(distances),
        "min_distance_threshold": min_distance,
        "min_dimensions_threshold": min_dimensions,
        "closest_pairs": [
            {"pair": [a, b], "total": total, "per_dimension": r["per_dimension"],
             "active_dimensions": r["active_dimensions"]}
            for total, a, b, r in distances[:12]
        ],
        "distance_median": round(statistics.median(values), 5) if values else 0.0,
        "distance_p05": round(sorted(values)[max(0, int(len(values) * 0.05))], 5) if values else 0.0,
        "collisions": collisions,
        "fragile_pairs": fragile,
        "passes": not collisions and not fragile,
        "boundary": (
            "本指标只回答「两套配方在感知空间里分得开吗」，"
            "不回答「哪一套更好看」。审美结论必须由真人盲测给出。"
        ),
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="配方独特性测量")
    parser.add_argument("--report", required=True, help="指纹 JSON（recipe_id → fingerprint）")
    parser.add_argument("--min-distance", type=float, default=0.25)
    args = parser.parse_args()
    pairs = json.loads(Path(args.report).read_text(encoding="utf-8"))
    print(json.dumps(analyze(pairs, args.min_distance), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
