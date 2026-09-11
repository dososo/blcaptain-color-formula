"""深海航线全片分层代理门；只验证方向、安全与时序。"""
import json
import subprocess

import numpy as np


EXPECTED_FRAMES = 265


def _probe(path):
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
        "-of", "json", str(path)]))


def _video(path, width=320, height=180):
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf",
        f"format=rgb24,scale={width}:{height}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    size = width * height * 3
    if len(raw) % size:
        raise ValueError("深海航线RGB帧不完整")
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3).astype(np.float32) / 255


def _mask(path, width=320, height=180):
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf",
        f"scale={width}:{height}:flags=neighbor,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    size = width * height
    if len(raw) % size:
        raise ValueError("深海航线蒙版帧不完整")
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width) >= 128


def _mean(values):
    return float(np.mean(values)) if values.size else float("nan")


def measure(foundation, candidate, environment_mask, anchor_mask):
    before, after = _video(foundation), _video(candidate)
    environment, anchor = _mask(environment_mask), _mask(anchor_mask)
    count = min(len(before), len(after), len(environment), len(anchor))
    before, after = before[:count], after[:count]
    environment, anchor = environment[:count], anchor[:count]
    y0 = .2126 * before[..., 0] + .7152 * before[..., 1] + .0722 * before[..., 2]
    y1 = .2126 * after[..., 0] + .7152 * after[..., 1] + .0722 * after[..., 2]
    c0 = before.max(axis=-1) - before.min(axis=-1)
    c1 = after.max(axis=-1) - after.min(axis=-1)
    difference = np.mean(np.abs(after - before), axis=-1)
    old_endpoint = (before.min(axis=-1) <= 0) | (before.max(axis=-1) >= 1)
    new_endpoint = (after.min(axis=-1) <= 0) | (after.max(axis=-1) >= 1)
    anchor_luma_before, anchor_luma_after = _mean(y0[anchor]), _mean(y1[anchor])
    anchor_chroma_before, anchor_chroma_after = _mean(c0[anchor]), _mean(c1[anchor])
    spans = []
    for original, result in zip(y0, y1):
        denominator = np.percentile(original, 95) - np.percentile(original, 5)
        if denominator > 1e-6:
            spans.append((np.percentile(result, 95) - np.percentile(result, 5)) / denominator)
    measured = {
        "frames": int(count), "foundation_frames": int(len(before)),
        "candidate_frames": int(len(after)), "environment_frames": int(len(environment)),
        "anchor_frames": int(len(anchor)), "mean_mae": float(np.mean(difference)),
        "environment_mae": _mean(difference[environment]),
        "environment_blue_red_delta": _mean(((after[..., 2] - after[..., 0]) -
                                               (before[..., 2] - before[..., 0]))[environment]),
        "environment_chroma_delta": _mean((c1 - c0)[environment]),
        "mean_luma_delta": float(np.mean(y1 - y0)),
        "mean_tone_span_ratio": float(np.mean(spans)) if spans else float("nan"),
        "anchor_mae": _mean(difference[anchor]),
        "anchor_luma_mae": _mean(np.abs(y1 - y0)[anchor]),
        "anchor_luma_retention": (anchor_luma_after / anchor_luma_before
                                   if anchor_luma_before > 1e-6 else float("nan")),
        "anchor_chroma_retention": (anchor_chroma_after / anchor_chroma_before
                                     if anchor_chroma_before > 1e-6 else float("nan")),
        "new_endpoint_ratio": float(np.mean(new_endpoint & ~old_endpoint)),
        "region_pixels": {"environment": int(environment.sum()), "anchor": int(anchor.sum())},
        "finite": bool(np.isfinite(after).all()),
        "candidate_probe": _probe(candidate),
        "boundary": "逐帧亮度/通道/面积代理，不识别海面、船、白帆、灯点或真实光源",
    }
    measured["finite"] = measured["finite"] and all(np.isfinite(measured[key]) for key in
        ("environment_mae", "environment_blue_red_delta", "environment_chroma_delta",
         "mean_tone_span_ratio", "anchor_mae", "anchor_luma_mae",
         "anchor_luma_retention", "anchor_chroma_retention"))
    return measured


def evaluate(measured):
    reasons = []
    if measured.get("frames") != EXPECTED_FRAMES:
        reasons.append("时序或代理帧数不完整")
    if not measured.get("finite"):
        reasons.append("代理区域缺失或存在非有限值")
    if measured.get("mean_mae", 0) < .006 or measured.get("environment_mae", 0) < .006:
        reasons.append("55%深海环境可见变化不足")
    if measured.get("environment_blue_red_delta", 0) < .010:
        reasons.append("深海蓝侧分离不足")
    if measured.get("environment_chroma_delta", 0) < .003:
        reasons.append("深海色彩密度不足")
    if abs(measured.get("mean_luma_delta", 1)) > .025 or measured.get("mean_tone_span_ratio", 0) < .88:
        reasons.append("整体压黑或影调跨度损失")
    if (not .84 <= measured.get("anchor_luma_retention", 0) <= 1.08
            or not .78 <= measured.get("anchor_chroma_retention", 0) <= 1.08):
        reasons.append("灯点或白帆的亮度/色彩锚保留不足")
    if measured.get("new_endpoint_ratio", 1) >= .001:
        reasons.append("新增通道端点超过0.1%")
    return {"status": "blocked" if reasons else "passed", "reasons": reasons,
            "boundary": "只验证分层方向和媒体安全，不代替人工审美"}
