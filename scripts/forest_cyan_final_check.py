"""森林青绿v3全片代理门；验证方向与安全，不代替人工审美。"""
import json
import subprocess

import cv2
import numpy as np


EXPECTED_FRAMES = 231


def _frames(path, width=320):
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
        "-of", "json", str(path)]))
    stream = next(item for item in probe["streams"] if item["codec_type"] == "video")
    height = round(int(stream["height"]) * width / int(stream["width"]))
    height += height % 2
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf",
        f"format=rgb24,scale={width}:{height}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    size = width * height * 3
    if len(raw) % size:
        raise ValueError("森林青绿解码帧不完整")
    frames = np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3).astype(np.float32) / 255
    return frames, probe


def _mean(values):
    return float(np.mean(values)) if values.size else float("nan")


def measure(source, candidate):
    before, source_probe = _frames(source)
    after, candidate_probe = _frames(candidate)
    count = min(len(before), len(after))
    before, after = before[:count], after[:count]
    hsv = np.stack([cv2.cvtColor(frame, cv2.COLOR_RGB2HSV) for frame in before])
    hue, saturation = hsv[..., 0], hsv[..., 1]
    y0 = .2126 * before[..., 0] + .7152 * before[..., 1] + .0722 * before[..., 2]
    y1 = .2126 * after[..., 0] + .7152 * after[..., 1] + .0722 * after[..., 2]
    chroma0 = before.max(axis=-1) - before.min(axis=-1)
    chroma1 = after.max(axis=-1) - after.min(axis=-1)
    foliage = (hue >= 72) & (hue <= 172) & (saturation >= .10) & (y0 >= .05) & (y0 <= .76)
    warm = (hue >= 15) & (hue <= 62) & (saturation >= .10) & (y0 >= .06) & (y0 <= .72)
    neutral = ((saturation <= .065) | (y0 >= .86))
    difference = np.mean(np.abs(after - before), axis=-1)
    old_endpoint = (before.min(axis=-1) <= 0) | (before.max(axis=-1) >= 1)
    new_endpoint = (after.min(axis=-1) <= 0) | (after.max(axis=-1) >= 1)
    warm_before = _mean((before[..., 0] - before[..., 2])[warm])
    warm_after = _mean((after[..., 0] - after[..., 2])[warm])
    measured = {
        "frames": int(count), "source_frames": int(len(before)), "candidate_frames": int(len(after)),
        "mean_mae": float(np.mean(difference)), "foliage_mae": _mean(difference[foliage]),
        "foliage_cyan_delta": _mean(((after[..., 2] - after[..., 0]) -
                                       (before[..., 2] - before[..., 0]))[foliage]),
        "foliage_chroma_delta": _mean((chroma1 - chroma0)[foliage]),
        "warm_mae": _mean(difference[warm]),
        "warm_retention": warm_after / warm_before if warm_before > 1e-6 else float("nan"),
        "neutral_mae": _mean(difference[neutral]),
        "new_endpoint_ratio": float(np.mean(new_endpoint & ~old_endpoint)),
        "region_pixels": {"foliage": int(foliage.sum()), "warm": int(warm.sum()),
                          "neutral": int(neutral.sum())},
        "finite": bool(np.isfinite(after).all()),
        "source_probe": source_probe, "candidate_probe": candidate_probe,
        "boundary": "显示RGB色相、饱和度与亮度代理，不是树叶、林地或景深语义分割",
    }
    measured["finite"] = measured["finite"] and all(np.isfinite(measured[key]) for key in
        ("foliage_mae", "foliage_cyan_delta", "warm_mae", "warm_retention", "neutral_mae"))
    return measured


def evaluate(measured):
    reasons = []
    if measured.get("frames") != EXPECTED_FRAMES:
        reasons.append("帧数不完整")
    if not measured.get("finite"):
        reasons.append("代理区域不足或存在非有限值")
    if measured.get("mean_mae", 0) < .0045 or measured.get("foliage_mae", 0) < .012:
        reasons.append("55%整体或叶幕可见变化不足")
    if measured.get("foliage_cyan_delta", 0) < .012:
        reasons.append("叶幕冷青分离不足")
    if measured.get("foliage_chroma_delta", -1) < .004:
        reasons.append("叶幕色彩密度不足")
    if measured.get("warm_mae", 1) > .018 or not .78 <= measured.get("warm_retention", 0) <= 1.08:
        reasons.append("暖棕反色锚漂移")
    if measured.get("neutral_mae", 1) > .014:
        reasons.append("近中性或高光被污染")
    if measured.get("new_endpoint_ratio", 1) >= .001:
        reasons.append("新增通道端点超过0.1%")
    return {"status": "blocked" if reasons else "passed", "reasons": reasons,
            "boundary": "仅验证全片方向、安全和时序，不判断作品是否动人"}
