#!/usr/bin/env python3
"""方向性视觉审计：发现变淡、变平和主体层级退化，不给作品打“审美分”。"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import shutil
import statistics
import subprocess
import sys
from pathlib import Path


WIDTH = 256
HEIGHT = 144
DIRECTION_TOLERANCE = 0.015


def sample_rgb(path: Path, video: bool = False) -> tuple[bytes, int]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("缺少 ffmpeg")
    frame_size = WIDTH * HEIGHT * 3
    filters = [f"fps=1,scale={WIDTH}:{HEIGHT}"] if video else [f"scale={WIDTH}:{HEIGHT}"]
    # 极短视频在 fps=1 的时间栅格上可能没有输出帧；退回首帧审计，不能因此误报渲染失败。
    if video:
        filters.append(f"scale={WIDTH}:{HEIGHT}")
    result = None
    frames = 0
    for filtergraph in filters:
        command = [
            ffmpeg, "-v", "error", "-i", str(path), "-vf", filtergraph,
            "-frames:v", "12" if video else "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        result = subprocess.run(command, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
        frames = len(result.stdout) // frame_size
        if frames:
            break
    if not frames:
        raise RuntimeError("无法读取有效画面")
    return result.stdout[:frames * frame_size], frames


def frame_metrics(data: bytes) -> dict[str, float]:
    pixels = len(data) // 3
    lumas: list[float] = []
    saturations: list[float] = []
    rg_values: list[float] = []
    yb_values: list[float] = []
    center_luma: list[float] = []
    outer_luma: list[float] = []
    edge_total = 0.0
    edge_count = 0
    hue_bins = [0.0] * 12
    warm_weight = 0.0
    cool_weight = 0.0
    chromatic_weight = 0.0
    previous_row = [0.0] * WIDTH

    for index in range(pixels):
        offset = index * 3
        red, green, blue = (data[offset] / 255, data[offset + 1] / 255, data[offset + 2] / 255)
        luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        lumas.append(luma)
        maximum, minimum = max(red, green, blue), min(red, green, blue)
        saturations.append((maximum - minimum) / maximum if maximum else 0.0)
        hue, saturation, _ = colorsys.rgb_to_hsv(red, green, blue)
        if saturation >= 0.08:
            weight = saturation * (0.35 + 0.65 * luma)
            hue_bins[min(11, int(hue * 12))] += weight
            chromatic_weight += weight
            degrees = hue * 360
            if degrees < 70 or degrees >= 330:
                warm_weight += weight
            if 165 <= degrees <= 265:
                cool_weight += weight
        rg_values.append(red - green)
        yb_values.append((red + green) / 2 - blue)
        x, y = index % WIDTH, index // WIDTH
        if WIDTH * 0.25 <= x < WIDTH * 0.75 and HEIGHT * 0.2 <= y < HEIGHT * 0.8:
            center_luma.append(luma)
        else:
            outer_luma.append(luma)
        if x:
            edge_total += abs(luma - lumas[-2])
            edge_count += 1
        if y:
            edge_total += abs(luma - previous_row[x])
            edge_count += 1
        previous_row[x] = luma

    ordered = sorted(lumas)
    percentile = lambda q: ordered[min(len(ordered) - 1, round((len(ordered) - 1) * q))]
    rg_std = statistics.pstdev(rg_values)
    yb_std = statistics.pstdev(yb_values)
    rg_mean = statistics.fmean(rg_values)
    yb_mean = statistics.fmean(yb_values)
    colorfulness = math.sqrt(rg_std ** 2 + yb_std ** 2) + 0.3 * math.sqrt(rg_mean ** 2 + yb_mean ** 2)
    separation = abs(statistics.fmean(center_luma) - statistics.fmean(outer_luma))
    dominant = sorted(range(12), key=lambda index: hue_bins[index], reverse=True)[:2]
    hue_delta = abs(dominant[0] - dominant[1]) * 30 if len(dominant) == 2 else 0
    hue_delta = min(hue_delta, 360 - hue_delta)
    p95 = percentile(0.95)
    p99 = percentile(0.99)
    return {
        "mean_luma": round(statistics.fmean(lumas), 6),
        "tone_span": round(percentile(0.95) - percentile(0.05), 6),
        "mean_saturation": round(statistics.fmean(saturations), 6),
        "colorfulness": round(colorfulness, 6),
        "local_contrast": round(edge_total / max(1, edge_count), 6),
        "subject_separation": round(separation, 6),
        "primary_secondary_hue_delta_deg": round(float(hue_delta), 3),
        "warm_anchor_ratio": round(warm_weight / max(chromatic_weight, 1e-9), 6),
        "cool_anchor_ratio": round(cool_weight / max(chromatic_weight, 1e-9), 6),
        "highlight_rolloff": round((p99 - p95) / max(0.001, 1.0 - p95), 6),
    }


def attention_separation(data: bytes, attention: dict) -> float:
    inner: list[float] = []
    outer: list[float] = []
    radius = float(attention["radius"])
    for index in range(len(data) // 3):
        offset = index * 3
        red, green, blue = (data[offset] / 255, data[offset + 1] / 255, data[offset + 2] / 255)
        luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        x = (index % WIDTH) / WIDTH
        y = (index // WIDTH) / HEIGHT
        distance = math.hypot(x - float(attention["center_x"]), y - float(attention["center_y"]))
        if distance <= radius * 0.3:
            inner.append(luma)
        elif distance >= radius * 0.75:
            outer.append(luma)
    if not inner or not outer:
        return 0.0
    return abs(statistics.fmean(inner) - statistics.fmean(outer))


def measure(path: Path, video: bool = False, attention: dict | None = None) -> dict:
    payload, frames = sample_rgb(path, video)
    frame_size = WIDTH * HEIGHT * 3
    measured = [frame_metrics(payload[index * frame_size:(index + 1) * frame_size]) for index in range(frames)]
    result = {
        key: round(statistics.fmean(item[key] for item in measured), 6)
        for key in measured[0]
    }
    if frames > 1:
        result["temporal_luma_std"] = round(statistics.pstdev(item["mean_luma"] for item in measured), 6)
        result["temporal_colorfulness_std"] = round(statistics.pstdev(item["colorfulness"] for item in measured), 6)
    result["sampled_frames"] = frames
    if attention and attention.get("mode") == "radial":
        result["subject_separation"] = round(statistics.fmean(
            attention_separation(payload[index * frame_size:(index + 1) * frame_size], attention)
            for index in range(frames)
        ), 6)
        result["subject_separation_basis"] = "confirmed-geometric-attention"
    return result


def evaluate_direction(baseline: dict, output: dict, targets: dict) -> dict:
    checks = {}
    failures = []
    for dimension in ("tone_span", "colorfulness", "subject_separation", "local_contrast"):
        intent = targets[dimension]
        before = float(baseline[dimension])
        after = float(output[dimension])
        tolerance = max(0.001, abs(before) * DIRECTION_TOLERANCE)
        if intent == "increase":
            passed = after >= before + tolerance
        elif intent in {"decrease", "soften", "compress"}:
            passed = after <= before - tolerance
        elif intent == "preserve":
            passed = abs(after - before) <= max(0.01, abs(before) * 0.2)
        else:
            raise ValueError(f"未知视觉方向：{dimension}={intent}")
        checks[dimension] = {
            "intent": intent,
            "before": before,
            "after": after,
            "delta": round(after - before, 6),
            "passed": passed,
        }
        if not passed:
            failures.append(dimension)
    return {
        "status": "passed" if not failures else "failed",
        "boundary": "只检查结果是否违背配方声明方向，不代表自动审美评分或获奖判断",
        "checks": checks,
        "failed_dimensions": failures,
    }


def impact_profile(baseline: dict, output: dict, style: dict) -> dict:
    """输出可证伪的情绪代理；它只查声明同向，不给审美打分。"""
    fields = (
        "tone_span", "subject_separation", "primary_secondary_hue_delta_deg",
        "colorfulness", "local_contrast", "warm_anchor_ratio", "cool_anchor_ratio",
        "highlight_rolloff",
    )
    metrics = {
        name: {
            "before": baseline.get(name),
            "after": output.get(name),
            "delta": round(float(output.get(name, 0)) - float(baseline.get(name, 0)), 6),
        }
        for name in fields
    }
    findings = []
    targets = style.get("visual_targets") or {}
    for name in ("tone_span", "subject_separation", "colorfulness", "local_contrast"):
        intent = targets.get(name)
        delta = metrics[name]["delta"]
        if intent == "increase" and delta <= 0:
            findings.append(f"配方声明{name}增加，但实测没有上移")
        if intent in {"decrease", "compress", "soften"} and delta >= 0:
            findings.append(f"配方声明{name}降低，但实测没有下移")
    temperature = float((style.get("parameters") or {}).get("temperature", 0.0))
    if temperature < -0.015:
        if metrics["cool_anchor_ratio"]["delta"] <= 0:
            findings.append("配方声明偏冷，但冷色锚点占比没有上移")
    if temperature > 0.015:
        if metrics["warm_anchor_ratio"]["delta"] <= 0:
            findings.append("配方声明偏暖，但暖色锚点占比没有上移")
    # 冷暖双锚必须读配方自己的结构化声明，不能用 palette_rule 的中文关键词。
    # lesson-69：同一个概念有两套词表时，只用其中一套的地方就是 bug——
    # 关键词版只命中 7 套 warm-cool-split 中的 2 套，teal-orange 这类
    # 最典型的青橙对立反而漏报，等于这道门对多数双锚配方从未生效。
    declares_split = (style.get("combo_tags") or {}).get("color") == "warm-cool-split"
    if declares_split and metrics["primary_secondary_hue_delta_deg"]["delta"] <= 0:
        findings.append("配方声明冷暖双锚，但主辅色色相分离没有增强")
    return {
        "status": "passed_with_findings" if findings else "same_direction",
        "metrics": metrics,
        "findings": findings,
        "calibration": {
            "rule": "只判断零点方向；幅度阈值必须由独立评审集实测后另行版本化",
            "source": "BLCaptain v4.8 独立评审集",
            "threshold_status": "direction-only",
        },
        "boundary": "代理指标只用于回归不倒退与配方声明同向检查，不等于审美结论。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查调色结果是否在声明方向上退化")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--targets-json", required=True)
    parser.add_argument("--video", action="store_true")
    parser.add_argument("--reference")
    args = parser.parse_args()
    targets = json.loads(Path(args.targets_json).read_text(encoding="utf-8"))
    baseline = measure(Path(args.input), args.video)
    output = measure(Path(args.output), args.video)
    result = evaluate_direction(baseline, output, targets)
    result["input_metrics"] = baseline
    result["output_metrics"] = output
    if args.reference:
        result["reference_metrics"] = measure(Path(args.reference), args.video)
        result["reference_boundary"] = "参考指标用于比较，不自动复制参考风格，也不代表版权授权"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 6


if __name__ == "__main__":
    sys.exit(main())
