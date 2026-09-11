"""冷灰海水v2最终分区代理门；不是语义识别或自动审美评分。"""
import json
import subprocess

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
    size = width * height * 3
    if len(raw) % size:
        raise ValueError("冷灰海水v2解码帧不完整")
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, height, width, 3).astype(np.float32) / 255, probe


def _safe_mean(values):
    return float(np.mean(values)) if values.size else float("nan")


def measure(source, candidate):
    before, source_probe = _frames(source)
    after, candidate_probe = _frames(candidate)
    count = min(len(before), len(after))
    if count == 0:
        return {"frames": 0, "usable_frames": 0, "finite": False}
    before, after = before[:count], after[:count]
    y0 = .2126 * before[..., 0] + .7152 * before[..., 1] + .0722 * before[..., 2]
    y1 = .2126 * after[..., 0] + .7152 * after[..., 1] + .0722 * after[..., 2]
    c0 = before.max(axis=-1) - before.min(axis=-1)
    c1 = after.max(axis=-1) - after.min(axis=-1)
    rows = np.linspace(0, 1, before.shape[1])[None, :, None]
    sea = (rows >= .43) & (rows <= .68) & (y0 > .10) & (y0 < .82)
    foam = (rows >= .43) & (rows <= .68) & (y0 > .67) & (c0 < .14)
    shore = (rows >= .70) & (before[..., 0] - before[..., 2] > .035) & (c0 > .08)
    sky = rows < .39
    sea_ratios = []
    for original, result in zip(y0[:, round(before.shape[1] * .43):round(before.shape[1] * .68)],
                                y1[:, round(before.shape[1] * .43):round(before.shape[1] * .68)]):
        denominator = np.percentile(original, 90) - np.percentile(original, 10)
        if denominator > 1e-6:
            sea_ratios.append((np.percentile(result, 90) - np.percentile(result, 10)) / denominator)
    old_endpoint = (before.min(axis=-1) <= 0) | (before.max(axis=-1) >= 1)
    new_endpoint = (after.min(axis=-1) <= 0) | (after.max(axis=-1) >= 1)
    shore_before = _safe_mean((before[..., 0] - before[..., 2])[shore])
    shore_after = _safe_mean((after[..., 0] - after[..., 2])[shore])
    measured = {
        "frames": int(count), "source_frames": int(len(before)),
        "candidate_frames": int(len(after)), "usable_frames": int(count),
        "mean_mae": float(np.mean(np.abs(after - before))),
        "sea_mae": _safe_mean(np.mean(np.abs(after - before), axis=-1)[sea]),
        "sea_cool_delta": _safe_mean(((after[..., 2] - after[..., 0]) -
                                        (before[..., 2] - before[..., 0]))[sea]),
        "mean_sea_contrast_ratio": float(np.mean(sea_ratios)) if sea_ratios else float("nan"),
        "sky_mae": _safe_mean(np.abs(after - before)[np.broadcast_to(sky[..., None], before.shape)]),
        "foam_chroma": _safe_mean(c1[foam]),
        "foam_chroma_delta": _safe_mean((c1 - c0)[foam]),
        "foam_luma_mae": _safe_mean(np.abs(y1 - y0)[foam]),
        "shore_warm_retention": shore_after / shore_before if shore_before > 1e-6 else float("nan"),
        "max_new_endpoint_ratio": float(np.mean(new_endpoint & ~old_endpoint)),
        "finite": bool(np.isfinite(after).all()),
        "region_pixel_counts": {"sea": int(sea.sum()), "foam": int(foam.sum()),
        "shore": int(shore.sum()), "sky": int(np.broadcast_to(sky, y0.shape).sum())},
        "source_probe": source_probe, "candidate_probe": candidate_probe,
        "boundary": "固定纵向带加显示RGB颜色条件，不是海水、浪花或礁岸语义分割",
    }
    measured["finite"] = measured["finite"] and all(np.isfinite(measured[key]) for key in (
        "sea_mae", "sea_cool_delta", "mean_sea_contrast_ratio", "sky_mae", "foam_chroma_delta",
        "foam_luma_mae", "shore_warm_retention"))
    return measured


def evaluate(measured):
    reasons = []
    if measured.get("frames") != EXPECTED_FRAMES or measured.get("usable_frames") != EXPECTED_FRAMES:
        reasons.append("帧数不完整")
    if not measured.get("finite"):
        reasons.append("分区缺失或存在非有限值")
    # 这是局部海体Look，不能要求天空和受保护浪花一起变化来抬高全画面MAE。
    # 同时保留较低的全画面下限，防止分区虽有数值但整体观看仍无感。
    if measured.get("mean_mae", 0) < .004 or measured.get("sea_mae", 0) < .012:
        reasons.append("55%海体局部或全画面可见变化不足")
    if measured.get("sea_cool_delta", 0) < .012:
        reasons.append("海体冷侧分离不足")
    if measured.get("mean_sea_contrast_ratio", 0) < 1.06:
        reasons.append("海面明暗结构不足")
    if measured.get("sky_mae", 1) > .012:
        reasons.append("天空被无依据污染")
    if measured.get("foam_chroma_delta", 1) > .008 or measured.get("foam_luma_mae", 1) > .035:
        reasons.append("白浪染色或亮度漂移")
    if not .65 <= measured.get("shore_warm_retention", 0) <= 1.02:
        reasons.append("暖礁岸反色锚丢失或膨胀")
    if measured.get("max_new_endpoint_ratio", 1) >= .001:
        reasons.append("新增通道端点超过0.1%")
    return {"status": "blocked" if reasons else "passed", "reasons": reasons,
            "boundary": "验证空间方向和安全，不判断作品是否动人"}
