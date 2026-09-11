#!/usr/bin/env python3
"""失败模式自动检测：把「会让作品立刻显廉价」的处理变成可拦截的门。

来源：research/master-aesthetics-v4.json 的 failure_catalogue（18 条）。
本模块实现其中 12 条，另 6 条（白平衡加暖冒充怀旧、沉重题材的重口味三件套、
肤色被风格化卷走、均匀降饱和杀掉色彩事件、跨 log 空间搬参数、
单一 contrast 导致撞味）依赖语境判断，如实留给人工复核，不假装能自动判定。
12 + 6 = 18，账要对得上。

早先这段写的是「11 条可自动化，本模块实现这 11 条」，而实际只有 9 条 add()，
人工清单 6 条，9 + 6 = 15——有三条（提饱和时静默改变明度、全画面等幅颗粒、
磨皮与全局去纹理）既没实现也没列进人工清单，凭空消失了。现已补齐。

所有阈值都是工程提议，与 visual_grammar 同源，须经真实素材标定。
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
from visual_grammar import (ENGINEERING_PROPOSAL, NEON_SLOPE_TOLERANCE,
                            check_grain_modulation)

DETECT_SCHEMA_VERSION = "4.0.0"
GRID_W, GRID_H = 96, 72

# 需要语境判断、不假装能自动检测的失败模式。
MANUAL_ONLY = [
    "白平衡整体加暖冒充怀旧",
    "沉重题材上的重口味三件套（高对比暗调 + 重晕影 + 强冷青暖橙分离）",
    "肤色被风格化色偏卷走",
    "均匀降饱和杀掉色彩事件",
    "跨 log 空间直接搬运参数",
    "单一 contrast 参数导致配方撞味（已由 tone_axes 去重覆盖）",
]


class DetectError(Exception):
    pass


def _grid(path: Path) -> list[tuple[int, int, int]]:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"format=rgb24,scale={GRID_W}:{GRID_H}:flags=area",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True)
    if result.returncode or not result.stdout:
        raise DetectError(f"无法采样：{path}")
    data = result.stdout
    return [(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data) - 2, 3)]


def _luma(rgb: tuple[int, int, int]) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def _high_freq(pixels: list[tuple[int, int, int]]) -> float:
    """相邻像素亮度差的均值，作为高频能量的代理。"""
    total = 0.0
    count = 0
    for y in range(GRID_H):
        row = y * GRID_W
        for x in range(1, GRID_W):
            if row + x >= len(pixels):
                break
            total += abs(_luma(pixels[row + x]) - _luma(pixels[row + x - 1]))
            count += 1
    return total / max(1, count)


def detect(source: Path, rendered: Path, declared: dict | None = None) -> dict:
    """对一次真实渲染跑全部可自动化的失败检测。

    declared 里可以声明配方自己承认的取舍（例如 allowed_highlight_clip_ratio），
    声明过的不再报警——重点是拦「没声明却做了」，而不是禁止一切强处理。
    """
    declared = declared or {}
    before, after = _grid(source), _grid(rendered)
    count = min(len(before), len(after))
    if count < 500:
        raise DetectError("采样点不足")
    before, after = before[:count], after[:count]
    labs_after = [rgb8_to_oklab(*p) for p in after]
    findings = []

    def add(name: str, triggered: bool, detail: str, advice: str, severity: str = "high"):
        findings.append({"failure": name, "triggered": triggered, "detail": detail,
                         "advice": advice if triggered else None, "severity": severity,
                         "threshold_status": ENGINEERING_PROPOSAL})

    # 1) 加法抬黑冒充胶片褪色
    ordered_before = sorted(_luma(p) for p in before)
    ordered_after = sorted(_luma(p) for p in after)
    p05_before = ordered_before[int(count * 0.05)]
    p05_after = ordered_after[int(count * 0.05)]
    black_after = ordered_after[int(count * 0.01)]
    lift = p05_after - p05_before
    add("用加法 offset 大幅抬黑做「胶片褪色」",
        lift > 0.01 and black_after > 8 / 255 and not declared.get("declared_faded_black"),
        f"暗部 5% 分位抬升 {lift:.4f}，黑场 {black_after * 255:.1f}/255",
        "加法抬黑在物理上等价于加眩光，暗部斜率会塌陷、细节永久损失。"
        "要柔黑请改用比例型软趾 out = x²/(x+t)。")

    # 2) 全局 saturation 乘法导致高光霓虹化
    highlights = [(l, c) for l, c, _ in
                  (oklab_to_oklch(*lab) for lab in labs_after) if l > 0.8]
    slope = None
    if len(highlights) >= 12:
        mx = statistics.fmean(p[0] for p in highlights)
        my = statistics.fmean(p[1] for p in highlights)
        den = sum((p[0] - mx) ** 2 for p in highlights)
        slope = (sum((p[0] - mx) * (p[1] - my) for p in highlights) / den) if den else 0.0
    add("全局 saturation 乘法（高光霓虹化）",
        slope is not None and slope >= NEON_SLOPE_TOLERANCE,
        f"高光段 chroma 对 L 的回归斜率 {slope:.5f}" if slope is not None else "高光样本不足",
        "饱和增益必须随明度单调不增，强制 path-to-white。真实世界里强光下的物体会失去彩度。")

    # 3) 逐通道 clip 造成色相硬截断
    def clip_ratio(pixels):
        return sum(1 for p in pixels if max(p) >= 253 and min(p) < 200) / len(pixels)
    clip_delta = clip_ratio(after) - clip_ratio(before)
    add("RGB 域逐通道 clip 造成色相硬截断",
        clip_delta > 0.01,
        f"「一通道≥253 而其余<200」的像素占比增量 {clip_delta:.4f}",
        "色域压缩必须在等色相切片内进行，只动明度与彩度，不要在 RGB 域逐通道 clip。")

    # 4) 背景压暗到内容移除等级（全图近似判据）
    near_black = (sum(1 for p in after if max(p) < 2) - sum(1 for p in before if max(p) < 2)) / count
    add("压暗到内容移除等级",
        near_black > 0.03 and not declared.get("declared_low_key"),
        f"近黑像素占比增量 {near_black:.4f}",
        "压暗只能重排已有亮度关系，不能等同于删除内容。")

    # 5) 锐化造边缘
    hf_before, hf_after = _high_freq(before), _high_freq(after)
    hf_gain = (hf_after - hf_before) / max(1e-6, hf_before)
    add("锐化造边缘",
        hf_gain > 0.25 and not declared.get("declared_crisp"),
        f"高频能量增量 {hf_gain:+.2%}",
        "锐化只能在既有边缘上做，不得造边缘。高频能量高于原图即为造边缘。")

    # 6) 全局 clarity / dehaze 拉升
    add("全局 clarity 或 dehaze 拉升",
        hf_gain > 0.15 and clip_delta > 0.005,
        f"高频增量 {hf_gain:+.2%} 且伴随通道剪切增量 {clip_delta:.4f}",
        "去朦胧会同时抬高暗部对比与彩度，在平滑渐变区放大噪点并引入蓝移；应局部使用。",
        "medium")

    # 7) 高光切平冒充滚降
    near_clip = (sum(1 for p in after if _luma(p) >= 0.99)
                 - sum(1 for p in before if _luma(p) >= 0.99)) / count
    add("高光切平冒充滚降",
        near_clip > 0.01 and not declared.get("allowed_highlight_clip_ratio"),
        f"近剪切像素占比增量 {near_clip:.4f}",
        "滚降是渐进压缩，切平是信息丢失。配方若确实接受剪切，必须显式声明比例。")

    # 8) 无目的的压暗
    mean_before = statistics.fmean(_luma(p) for p in before)
    mean_after = statistics.fmean(_luma(p) for p in after)
    contrast_gain = hf_after - hf_before
    add("无目的的压暗",
        mean_after < mean_before - 0.06 and contrast_gain <= 0 and not declared.get("declared_low_key"),
        f"整体亮度 {mean_before:.3f} → {mean_after:.3f}，局部对比未上升（{contrast_gain:+.5f}）",
        "压暗若没有让任何区域的相对可见度上升，就只是把画面变暗，不是塑光。",
        "medium")

    # 4') 提饱和时静默改变明度：加彩度的像素上不该顺带搬明度。
    # 研究失败清单第 4 条。判据只看「彩度确实上升的那些像素」，
    # 因为整体明度位移是配方的合法动作，只有跟着彩度走的明度漂移才是这条要抓的。
    chroma_up = []
    for index in range(count):
        lab_before = rgb8_to_oklab(*before[index])
        lab_after = rgb8_to_oklab(*after[index])
        _, chroma_b, _ = oklab_to_oklch(*lab_before)
        _, chroma_a, _ = oklab_to_oklch(*lab_after)
        if chroma_a - chroma_b > 0.01:
            chroma_up.append((lab_after[0] - lab_before[0], chroma_a - chroma_b))
    global_shift = statistics.fmean(
        rgb8_to_oklab(*after[i])[0] - rgb8_to_oklab(*before[i])[0] for i in range(count))
    if len(chroma_up) >= 20:
        # 扣掉全幅的整体明度位移，只留「与彩度上升挂钩」的那部分。
        residual = statistics.median(abs(d - global_shift) for d, _ in chroma_up)
        residual_detail = (f"彩度上升区的明度残差中位 {residual:.4f}"
                           f"（已扣除全幅位移 {global_shift:.4f}）")
    else:
        residual = 0.0
        residual_detail = "彩度上升的像素不足 20 个，跳过判定"
    add("提饱和时静默改变明度",
        residual > 0.02,
        residual_detail,
        "加彩度不应顺带搬明度。等亮度的做法是在 RGB 域用 Rec.709 权重矩阵，"
        "每行系数和为 1；YUV 域的 chroma 缩放做不到这一点。",
        "medium")

    # 7') 全画面等幅颗粒：直接调用 visual_grammar 的颗粒门。
    # 这条判据早就写好了，但 check_grain_modulation 从未被任何执行链调用过。
    grain_pairs = []
    for y in range(2, GRID_H - 2, 3):
        for x in range(2, GRID_W - 2, 3):
            index = y * GRID_W + x
            if index + GRID_W >= count:
                continue
            # 局部幅度用十字邻域的亮度离差代理，避免把内容边缘算成颗粒。
            neighbours = [after[index - 1], after[index + 1],
                          after[index - GRID_W], after[index + GRID_W]]
            local = _luma(after[index])
            amplitude = statistics.fmean(abs(_luma(n) - local) for n in neighbours)
            grain_pairs.append((local, amplitude))
    grain_report = check_grain_modulation(grain_pairs)
    grain_added = declared.get("declared_grain") or _high_freq(after) > _high_freq(before) * 1.15
    add("全画面等幅颗粒",
        grain_added and not grain_report.get("passed", True),
        f"颗粒幅度与明度的相关系数 {grain_report.get('correlation', 'n/a')}"
        f"（判据来自 visual_grammar.check_grain_modulation）",
        "等幅颗粒一眼看出是叠加上去的。幅度应当在欠曝区最高、"
        "在高光衰减到峰值的 20% 以下，跟着影调走。",
        "medium")

    # 10') 磨皮与全局去纹理：中间调的高频能量被抹掉。
    # 皮肤多落在中间调，没有人像蒙版时用明度带做全图近似，并在 detail 里写明这是近似。
    def textured_midtone_positions(pixels):
        """原片里**本来就有纹理**的中间调位置。

        直接拿全部中间调像素算平均高频会被稀释：色块内部本来就平坦，
        模糊前后都没有高频，把它们计进分母会让下降比例系统性偏低。
        实测 boxblur=4:2 这种一眼可见的全局模糊，全图中间调只算出 29% 下降，
        低于 35% 的门限——不是门太严，是判据把分母掺水了。
        只在原片自己有纹理的位置上比较，才问得出「纹理被抹掉了多少」。
        """
        samples = []
        for y in range(GRID_H):
            row = y * GRID_W
            for x in range(1, GRID_W):
                left, right = row + x - 1, row + x
                if right >= len(pixels):
                    break
                luma_left, luma_right = _luma(pixels[left]), _luma(pixels[right])
                if 0.30 <= luma_left <= 0.70 and 0.30 <= luma_right <= 0.70:
                    samples.append((left, right, abs(luma_right - luma_left)))
        if len(samples) < 40:
            return []
        cut = statistics.median(delta for _, _, delta in samples)
        return [(left, right) for left, right, delta in samples if delta >= cut]

    textured = textured_midtone_positions(before)
    if textured:
        freq_before = statistics.fmean(
            abs(_luma(before[r]) - _luma(before[l])) for l, r in textured)
        freq_after = statistics.fmean(
            abs(_luma(after[r]) - _luma(after[l])) for l, r in textured)
        smooth_drop = 1.0 - freq_after / freq_before if freq_before > 1e-6 else 0.0
        smooth_detail = (
            f"原片有纹理的中间调位置上，高频能量下降 {smooth_drop:.1%}"
            f"（{len(textured)} 个取样点；全图近似：无人像蒙版时用 L 0.30-0.70 代替皮肤区）")
    else:
        smooth_drop = 0.0
        smooth_detail = "中间调有纹理的取样点不足 40 个，跳过判定"
    add("磨皮与全局去纹理",
        smooth_drop > 0.35 and not declared.get("declared_smoothing"),
        smooth_detail,
        "去掉皮肤纹理会让人变成塑料。要柔化请用局部对比而不是全局模糊，"
        "并且必须在配方里显式声明。",
        "medium")

    # 9) 用暗角画圈
    cx, cy = GRID_W / 2, GRID_H / 2
    radial = []
    for index in range(count):
        x, y = index % GRID_W, index // GRID_W
        r = math.hypot((x - cx) / cx, (y - cy) / cy)
        radial.append((r, _luma(after[index]) - _luma(before[index])))
    inner = [d for r, d in radial if r < 0.4]
    outer = [d for r, d in radial if r > 0.85]
    vignette_gap = (statistics.fmean(inner) - statistics.fmean(outer)) if inner and outer else 0.0
    add("用暗角画圈制造氛围",
        vignette_gap > 0.05 and not declared.get("declared_vignette"),
        f"中心与边缘的亮度变化差 {vignette_gap:.4f}",
        "径向衰减若与画面内容无关，只是在画圈；几何引导必须能追溯到已确认的视觉重心。",
        "medium")

    triggered = [f for f in findings if f["triggered"]]
    return {
        "schema_version": DETECT_SCHEMA_VERSION,
        "source": str(source),
        "rendered": str(rendered),
        "checked": len(findings),
        "triggered_count": len(triggered),
        "findings": findings,
        "manual_review_required": MANUAL_ONLY,
        "passes": not any(f["severity"] == "high" for f in triggered),
        "boundary": (
            "只检测「有没有做出已知会显廉价的处理」，不判断作品好不好。"
            f"另有 {len(MANUAL_ONLY)} 条失败模式依赖语境判断，如实留给人工复核，不假装能自动判定。"
        ),
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="失败模式自动检测")
    parser.add_argument("--source", required=True)
    parser.add_argument("--rendered", required=True)
    parser.add_argument("--declared", help="配方声明的取舍 JSON")
    args = parser.parse_args()
    declared = json.loads(Path(args.declared).read_text(encoding="utf-8")) if args.declared else {}
    report = detect(Path(args.source).expanduser().resolve(),
                    Path(args.rendered).expanduser().resolve(), declared)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passes"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
