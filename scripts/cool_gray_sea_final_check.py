"""冷灰海水最终媒体工程代理门；不是语义海面识别或审美评分。"""
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np


EXPECTED_FRAMES = 294


def _probe(path):
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]))


def _frames(path, width=320):
    probe = _probe(path)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    height = round(int(video["height"]) * width / int(video["width"]))
    height += height % 2
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf",
        f"format=rgb24,scale={width}:{height}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    frame_size = width * height * 3
    if len(raw) % frame_size:
        raise ValueError("冷灰海水解码帧不完整")
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, height, width, 3).astype(np.float32) / 255, probe


def measure(source, candidate):
    before, source_probe = _frames(source)
    after, candidate_probe = _frames(candidate)
    count = min(len(before), len(after))
    if count == 0:
        return {"frames": 0, "usable_frames": 0, "finite": False}
    before, after = before[:count], after[:count]
    y0 = .2126 * before[..., 0] + .7152 * before[..., 1] + .0722 * before[..., 2]
    y1 = .2126 * after[..., 0] + .7152 * after[..., 1] + .0722 * after[..., 2]
    top, bottom = round(before.shape[1] * .43), round(before.shape[1] * .65)
    ratios = []
    for original, result in zip(y0[:, top:bottom], y1[:, top:bottom]):
        denominator = np.percentile(original, 90) - np.percentile(original, 10)
        if denominator > 1e-6:
            ratios.append(float((np.percentile(result, 90) - np.percentile(result, 10)) / denominator))
    old_endpoint = (before.min(axis=-1) <= 0) | (before.max(axis=-1) >= 1)
    new_endpoint = (after.min(axis=-1) <= 0) | (after.max(axis=-1) >= 1)
    result = {
        "frames": int(count),
        "source_frames": int(len(before)),
        "candidate_frames": int(len(after)),
        "usable_frames": int(count),
        "mean_mae": float(np.mean(np.abs(after - before))),
        "mean_sea_contrast_ratio": float(np.mean(ratios)) if ratios else float("nan"),
        "max_new_endpoint_ratio": float(np.mean(new_endpoint & ~old_endpoint)),
        "finite": bool(np.isfinite(after).all() and np.isfinite(ratios).all()),
        "source_probe": source_probe,
        "candidate_probe": candidate_probe,
        "boundary": "固定纵向海面带与显示参照RGB代理，不是海天岸语义蒙版或审美评分",
    }
    return result


def evaluate(measured):
    reasons = []
    if measured.get("frames") != EXPECTED_FRAMES or measured.get("usable_frames") != EXPECTED_FRAMES:
        reasons.append("帧数不完整")
    if not measured.get("finite"):
        reasons.append("存在非有限值或没有可用海面跨度")
    if measured.get("mean_mae", 0) < .005:
        reasons.append("55%相对原片变化不足")
    if measured.get("mean_sea_contrast_ratio", 0) < 1.05:
        reasons.append("固定海面带结构分离不足")
    if measured.get("max_new_endpoint_ratio", 1) >= .001:
        reasons.append("新增通道端点超过0.1%")
    return {"status": "blocked" if reasons else "passed", "reasons": reasons,
            "boundary": "只验证本素材的变化、固定带结构和端点安全；不判定审美"}
