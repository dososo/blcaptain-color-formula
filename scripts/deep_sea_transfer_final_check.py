"""深海航线异题材迁移的全片方向、安全与时序门。"""
import json
import subprocess

import numpy as np


EXPECTED_FRAMES = 288


def _probe(path):
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams",
        "-show_format", "-of", "json", str(path)]))


def _video(path, width=320, height=180):
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf",
        f"format=rgb24,scale={width}:{height}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    size = width * height * 3
    if len(raw) % size:
        raise ValueError("深海迁移RGB帧不完整")
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3).astype(np.float32) / 255


def _mask(path, width=320, height=180):
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf",
        f"scale={width}:{height}:flags=neighbor,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    size = width * height
    if len(raw) % size:
        raise ValueError("深海迁移代理帧不完整")
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width) >= 128


def _mean(values):
    return float(np.mean(values)) if values.size else float("nan")


def measure(source, candidate, environment_mask, subject_mask):
    before, after = _video(source), _video(candidate)
    environment, subject = _mask(environment_mask), _mask(subject_mask)
    count = min(len(before), len(after), len(environment), len(subject))
    before, after = before[:count], after[:count]
    environment, subject = environment[:count], subject[:count]
    y0 = .2126 * before[..., 0] + .7152 * before[..., 1] + .0722 * before[..., 2]
    y1 = .2126 * after[..., 0] + .7152 * after[..., 1] + .0722 * after[..., 2]
    c0 = before.max(axis=-1) - before.min(axis=-1)
    c1 = after.max(axis=-1) - after.min(axis=-1)
    difference = np.mean(np.abs(after - before), axis=-1)
    anchor = (y0 >= .58) & (before[..., 0] >= before[..., 2]) & ~environment
    old_endpoint = (before.min(axis=-1) <= 0) | (before.max(axis=-1) >= 1)
    new_endpoint = (after.min(axis=-1) <= 0) | (after.max(axis=-1) >= 1)
    anchor_y0, anchor_y1 = _mean(y0[anchor]), _mean(y1[anchor])
    anchor_c0, anchor_c1 = _mean(c0[anchor]), _mean(c1[anchor])
    spans = []
    for original, result in zip(y0, y1):
        denominator = np.percentile(original, 95) - np.percentile(original, 5)
        if denominator > 1e-6:
            spans.append((np.percentile(result, 95) - np.percentile(result, 5)) / denominator)
    effect = np.mean(difference, axis=(1, 2))
    measured = {
        "frames": int(count),
        "finite": bool(np.isfinite(after).all()),
        "mean_mae": float(np.mean(difference)),
        "environment_mae": _mean(difference[environment]),
        "environment_blue_red_delta": _mean(((after[..., 2] - after[..., 0]) -
                                                (before[..., 2] - before[..., 0]))[environment]),
        "environment_chroma_delta": _mean((c1 - c0)[environment]),
        "subject_mae": _mean(difference[subject]),
        "anchor_luma_retention": anchor_y1 / anchor_y0 if anchor_y0 > 1e-6 else float("nan"),
        "anchor_chroma_retention": anchor_c1 / anchor_c0 if anchor_c0 > 1e-6 else float("nan"),
        "mean_tone_span_ratio": float(np.mean(spans)) if spans else float("nan"),
        "new_endpoint_ratio": float(np.mean(new_endpoint & ~old_endpoint)),
        "max_effect_jump": float(np.max(np.abs(np.diff(effect)))) if len(effect) > 1 else 0.0,
        "region_pixels": {"environment": int(environment.sum()),
                          "subject": int(subject.sum()), "anchor": int(anchor.sum())},
        "candidate_probe": _probe(candidate),
        "boundary": "同源预计算明暗/运动代理；不识别人、肤色、建筑或真实灯光",
    }
    measured["finite"] = measured["finite"] and all(np.isfinite(measured[key]) for key in (
        "environment_mae", "environment_blue_red_delta", "environment_chroma_delta",
        "subject_mae", "anchor_luma_retention", "anchor_chroma_retention",
        "mean_tone_span_ratio", "max_effect_jump"))
    return measured


def evaluate(measured):
    reasons = []
    if measured.get("frames") != EXPECTED_FRAMES:
        reasons.append("全片或代理帧数不完整")
    if not measured.get("finite"):
        reasons.append("代理区域缺失或存在非有限值")
    if measured.get("mean_mae", 0) < .015 or measured.get("environment_mae", 0) < .022:
        reasons.append("55%迁移变化不足")
    if measured.get("environment_blue_red_delta", 0) < .045:
        reasons.append("深蓝空间分离不足")
    if measured.get("environment_chroma_delta", 0) < .012:
        reasons.append("环境综合色彩密度不足")
    if measured.get("subject_mae", 1) > .018:
        reasons.append("人物/黑衣保护不足")
    if not .90 <= measured.get("anchor_luma_retention", 0) <= 1.08:
        reasons.append("象牙暖锚亮度漂移")
    if not .82 <= measured.get("anchor_chroma_retention", 0) <= 1.16:
        reasons.append("象牙暖锚综合色彩漂移")
    if measured.get("mean_tone_span_ratio", 0) < .88:
        reasons.append("影调跨度损失")
    if measured.get("new_endpoint_ratio", 1) >= .001:
        reasons.append("新增通道端点超过0.1%")
    if measured.get("max_effect_jump", 1) >= .012:
        reasons.append("相邻帧效果量跳变")
    return {"status": "blocked" if reasons else "passed", "reasons": reasons,
            "boundary": "只验证可见迁移、保护和时序，不代替人工审美"}
