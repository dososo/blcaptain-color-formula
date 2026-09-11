#!/usr/bin/env python3
"""按光线可达性分配暖色：找到画面里的光源，让暖度随距离衰减。

sunset-warm 的命题是「够得到的地方发烫，够不到的地方立刻退回冷蓝」，
而它此前的实现是全局推暖——天顶的彩度涨得比中部云还多，
正是它自己 forbidden 里禁的「整幅推暖，旅行 App 的默认味道」。

这里做的是最小可信的一版：从画面自身找出光源位置，
用它到各处的距离调制暖度。**不猜光源存不存在**——
找不到足够强的高光核就如实说找不到，不给一个居中的假光源糊过去。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LIGHT_FALLOFF_SCHEMA_VERSION = "4.3.0"

# 光源核的亮度门槛（相对最亮值）。低于它的「亮区」不算光源，只是浅色物体。
CORE_LUMA_RATIO = 0.92
# 光源核最小面积占比。太小的点是噪点或反光，不是能照亮场景的光源。
MIN_CORE_AREA = 0.0008
# 光源核最大面积占比。超过它说明整幅都亮，没有「够得到与够不到」之分。
MAX_CORE_AREA = 0.35
# 光源核相对画面中位亮度的最小倍数。低于它就只是「比较亮」，不是光源。
MIN_CORE_CONTRAST = 1.6
GRID_W, GRID_H = 96, 54


class LightFalloffError(Exception):
    pass


def find_light_source(image: Path) -> dict:
    """找画面里的光源核：最亮那一撮像素的重心与范围。"""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(image),
         "-vf", f"format=rgb24,scale={GRID_W}:{GRID_H}:flags=area",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    if result.returncode or not result.stdout:
        raise LightFalloffError(f"无法采样：{image}")
    data = result.stdout
    luma = []
    for index in range(0, len(data) - 2, 3):
        luma.append((0.2126 * data[index] + 0.7152 * data[index + 1]
                     + 0.0722 * data[index + 2]) / 255.0)
    if not luma:
        raise LightFalloffError("采样为空")

    peak = max(luma)
    threshold = peak * CORE_LUMA_RATIO
    core = [i for i, value in enumerate(luma) if value >= threshold]
    area = len(core) / len(luma)

    if area < MIN_CORE_AREA:
        return {"found": False, "reason":
                f"最亮区只占 {area:.4%}，小于 {MIN_CORE_AREA:.2%}——"
                "那是噪点或小反光，不是能照亮场景的光源"}
    if area > MAX_CORE_AREA:
        return {"found": False, "reason":
                f"最亮区占了 {area:.1%}，超过 {MAX_CORE_AREA:.0%}——"
                "整幅都亮，没有「够得到与够不到」之分，径向衰减无从谈起"}

    # 光源不只是「最亮的一块」，它还得**显著亮于周围**。
    # 只看面积会把阴天的天空、人像的额头都判成光源——它们确实是画面最亮处，
    # 但与周围没有落差，谈不上「够得到与够不到」。
    ordered = sorted(luma)
    background = ordered[len(ordered) // 2]          # 中位亮度当背景基准
    core_mean = sum(luma[i] for i in core) / len(core)
    contrast = core_mean / max(1e-6, background)
    if contrast < MIN_CORE_CONTRAST:
        return {"found": False, "reason":
                f"最亮区只比画面中位亮度高 {contrast:.2f} 倍，低于 {MIN_CORE_CONTRAST} 倍——"
                "它是画面里最亮的部分，但没有亮到能在周围投出可分辨的衰减；"
                "阴天的天空、人像的额头都是这种情况，它们不是光源",
                "core_contrast": round(contrast, 3)}

    total_weight = 0.0
    cx = cy = 0.0
    for index in core:
        weight = luma[index]
        x = (index % GRID_W) / GRID_W
        y = (index // GRID_W) / GRID_H
        cx += x * weight
        cy += y * weight
        total_weight += weight
    cx /= total_weight
    cy /= total_weight

    # 光源半径：核内像素到重心的距离的 90 分位，作为「够得到」的范围
    distances = sorted(
        (((index % GRID_W) / GRID_W - cx) ** 2 + ((index // GRID_W) / GRID_H - cy) ** 2) ** 0.5
        for index in core)
    radius = distances[int(len(distances) * 0.9)] if distances else 0.0
    return {
        "found": True,
        "center_x": round(cx, 4),
        "center_y": round(cy, 4),
        "core_area": round(area, 5),
        "core_radius": round(max(radius, 0.02), 4),
        "peak_luma": round(peak, 4),
        "core_contrast": round(contrast, 3),
        "threshold": round(threshold, 4),
        "basis": "画面自身最亮的一撮像素的亮度加权重心；不猜、不给默认居中光源",
    }


def warmth_falloff_filter(light: dict, warmth: float, reach: float = 2.5) -> str:
    """暖度随距离光源衰减的滤镜。

    近光源处保留全部暖色增益，远处线性退回，最远端回到中性甚至微冷。
    用 geq 分通道做：红通道近处抬、蓝通道近处压，两者都随距离归零，
    所以远离光源的地方保持原样——这正是「够不到的地方退回冷蓝」。
    """
    if not light.get("found"):
        raise LightFalloffError("没有可用的光源位置，不生成衰减滤镜")
    cx, cy = light["center_x"], light["center_y"]
    span = max(0.05, light["core_radius"] * reach)
    # 距离归一化到 [0,1]：0 是光源核，1 是够不到的地方
    distance = f"min(1,hypot(X/W-{cx},Y/H-{cy})/{span})"
    warm = f"({warmth}*(1-{distance}))"
    return (f"geq="
            f"r='clip(r(X,Y)*(1+{warm}),0,255)':"
            f"g='clip(g(X,Y)*(1+{warm}*0.35),0,255)':"
            f"b='clip(b(X,Y)*(1-{warm}*0.55),0,255)'")


def measure_falloff(source: Path, rendered: Path, light: dict) -> dict:
    """验证暖度确实随距离衰减，而不是全幅一起变暖。

    判据：近光源区的暖度增量必须显著大于远处。做不到就说明它其实是全局推暖，
    「按可达性分配」这句话就不成立。
    """
    import color_space

    def sample(path: Path) -> list:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", f"format=rgb24,scale={GRID_W}:{GRID_H}:flags=area",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True)
        if result.returncode or not result.stdout:
            raise LightFalloffError(f"无法采样：{path}")
        return list(result.stdout)

    before, after = sample(source), sample(rendered)
    cx, cy = light["center_x"], light["center_y"]
    span = max(0.05, light["core_radius"] * 2.5)
    near, far = [], []
    for index in range(min(len(before), len(after)) // 3):
        offset = index * 3
        x = (index % GRID_W) / GRID_W
        y = (index // GRID_W) / GRID_H
        distance = min(1.0, ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / span)
        lab_b = color_space.rgb8_to_oklab(before[offset], before[offset + 1], before[offset + 2])
        lab_a = color_space.rgb8_to_oklab(after[offset], after[offset + 1], after[offset + 2])
        # 暖度用 OKLab 的 b 轴（黄蓝）增量表示
        delta = lab_a[2] - lab_b[2]
        (near if distance < 0.35 else far if distance > 0.75 else []).append(delta)
    if len(near) < 20 or len(far) < 20:
        return {"measured": False,
                "reason": f"近光源区 {len(near)} 点、远处 {len(far)} 点，样本不足以比较"}
    near_mean = sum(near) / len(near)
    far_mean = sum(far) / len(far)
    gradient = near_mean - far_mean
    return {
        "measured": True,
        "near_warmth_delta": round(near_mean, 5),
        "far_warmth_delta": round(far_mean, 5),
        "gradient": round(gradient, 5),
        "passed": gradient > 0.004,
        "meaning": "近光源处的暖度增量减去远处的增量。大于 0 才说明暖色是按距离分配的；"
                   "接近 0 就是全局推暖，「够不到的地方退回冷蓝」不成立",
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="光源检测与暖度径向衰减")
    parser.add_argument("--input", required=True)
    parser.add_argument("--warmth", type=float, default=0.12)
    parser.add_argument("--output")
    args = parser.parse_args()
    source = Path(args.input).expanduser().resolve()
    light = find_light_source(source)
    payload = {"schema_version": LIGHT_FALLOFF_SCHEMA_VERSION, "light": light}
    if light["found"] and args.output:
        out = Path(args.output).expanduser().resolve()
        if out.exists():
            raise SystemExit(f"输出已存在，不会覆盖：{out}")
        chain = warmth_falloff_filter(light, args.warmth)
        subprocess.run(["ffmpeg", "-v", "error", "-n", "-i", str(source),
                        "-vf", chain, "-frames:v", "1", "-c:v", "png", str(out)], check=True)
        payload["filter"] = chain
        payload["output"] = str(out)
        payload["falloff"] = measure_falloff(source, out, light)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if light["found"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
