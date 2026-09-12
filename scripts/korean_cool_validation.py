"""韩系保护内核逐帧安全与固定源区域签名；统计通过不是人工接受。"""
from __future__ import annotations

from contextlib import ExitStack
from itertools import zip_longest
from pathlib import Path
import subprocess
import tempfile

try:
    from . import korean_cool_grade as grade, korean_cool_protection as protection
except ImportError:
    import korean_cool_grade as grade
    import korean_cool_protection as protection

REGIONS = ("world", "protected", "white", "black", "whole")
BOUNDARY = ("人物联合与近中性亮部蒙版，不是肤色专用蒙版；只测权重>=.95的低分辨率内核。"
            "逐帧独立分割不等于身份跟踪；发丝、手指、遮挡边缘及人工连续审片未由此通过。")


def _frames(path, width, height, gray=False, p3=False):
    """保持帧序号读取全部帧，不使用fps抽样，不将全片像素驻留内存。"""
    filters = []
    if p3 and not gray:
        filters += ["format=gbrp16le", "zscale=primariesin=smpte432:transferin=iec61966-2-1:matrixin=gbr:rangein=full:"
                    "primaries=709:transfer=13:matrix=gbr:range=full"]
    filters += [f"scale={width}:{height}:flags=area", "format=gray" if gray else "format=rgb24"]
    command = ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
               "-vf", ",".join(filters), "-fps_mode", "passthrough", "-f", "rawvideo",
               "-pix_fmt", "gray" if gray else "rgb24", "-"]
    size = width * height * (1 if gray else 3)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            while True:
                raw = process.stdout.read(size)
                if not raw:
                    break
                if len(raw) != size:
                    raise ValueError("解码得到不完整帧，停止内核验收")
                yield raw
            if process.wait():
                errors.seek(0)
                raise ValueError("验收解码失败：" + errors.read().decode("utf-8", "replace"))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def _signature(totals, clips, pixel_count):
    means = {name: [float(v / row[0]) for v in row[1:]]
             for name, row in zip(REGIONS, totals) if row[0] > 0}
    result = {"highlight_clip_ratio": float(clips / pixel_count),
              "sample_counts": {name: int(row[0]) for name, row in zip(REGIONS, totals)}}
    for region in ("world", "protected"):
        if region in means:
            result.update({f"{region}_{key}": value for key, value in
                           zip(("luma", "chroma", "cool_bias"), means[region])})
    if "world" in means and "protected" in means:
        result["spatial_cool_partition"] = means["world"][2] - means["protected"][2]
    if "white" in means:
        result["neutral_white_chroma"] = means["white"][1]
    if "black" in means:
        result["black_luma"] = means["black"][0]
    return result


def _safety(base, out):
    # 与既有grade.evaluate_signature相同的安全阈值，不因新素材结果调宽。
    failures = []
    if abs(out["protected_cool_bias"] - base["protected_cool_bias"]) > .012:
        failures.append("protected_region_cooled")
    if out["protected_chroma"] < base["protected_chroma"] * .96:
        failures.append("protected_memory_color_regressed")
    if abs(out["protected_luma"] - base["protected_luma"]) > .018:
        failures.append("protected_luma_drifted")
    if "world_luma" in base and out["world_luma"] < base["world_luma"] - .045:
        failures.append("world_crushed")
    if "neutral_white_chroma" in base and out["neutral_white_chroma"] > base["neutral_white_chroma"] + .010:
        failures.append("neutral_white_drifted")
    if "black_luma" in base and out["black_luma"] > base["black_luma"] + .010:
        failures.append("black_lifted")
    if out["highlight_clip_ratio"] > base["highlight_clip_ratio"] + .003:
        failures.append("highlight_clip_regressed")
    return failures


def measure(plan: dict, foundation_path: Path, rendered_path: Path) -> dict:
    """无效绑定/缺帧/空内核抛ValueError；统计越线返回passed=false。"""
    try:
        import numpy as np
    except ImportError as error:
        raise ValueError("韩系逐帧验收缺少NumPy，请在当前解释器安装依赖") from error
    evidence = plan.get("korean_cool_protection")
    if not evidence or not plan.get("source"):
        raise ValueError("缺少本方案绑定的人物保护证据")
    strength = float(plan["strength"])
    if not 0 <= strength <= 1:
        raise ValueError("验收强度必须为0到1的方案值")
    source = plan["source"]
    protection.validate(evidence, source)
    paths = [Path(foundation_path), Path(rendered_path), Path(evidence["mask"]["path"])]
    expected = evidence["mask"]["frame_count"]
    if expected < 1 or (source["media_type"] == "photo" and expected != 1):
        raise ValueError("保护蒙版帧数无效")
    for path in paths[:2]:
        stream = protection._probe(path)["streams"][0]
        if [stream["width"], stream["height"]] != [source["width"], source["height"]]:
            raise ValueError("验收输入几何与原片不一致，不能通过缩放掩盖裁切")
        if source["media_type"] == "video":
            protection.validate_output_timeline(evidence, path)
    w, h = evidence["analysis_dimensions"]
    scale = min(1, 320 / max(w, h))
    width, height = max(1, round(w * scale)), max(1, round(h * scale))
    pixels = width * height
    totals, clips = np.zeros((2, 5, 4), dtype=np.float64), [0, 0]
    rows = []
    p3 = source.get("color", {}).get("profile") == "display-p3"
    streams = [_frames(path, width, height, n == 2, p3) for n, path in enumerate(paths)]
    with ExitStack() as stack:
        for stream in streams:
            stack.callback(stream.close)
        for index, raw in enumerate(zip_longest(*streams)):
            if any(frame is None for frame in raw) or index >= expected:
                raise ValueError("Foundation、成片与蒙版完整帧数不一致")
            base, out = [np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.float64) / 255
                         for data in raw[:2]]
            weights = np.frombuffer(raw[2], dtype=np.uint8).astype(np.float64) / 255
            core = weights >= .95
            if not core.any():
                raise ValueError(f"第{index}帧没有权重>=.95的保护内核，不作零误差通过")
            luma = [rgb[:, 0] * .2126 + rgb[:, 1] * .7152 + rgb[:, 2] * .0722 for rgb in (base, out)]
            chroma = [rgb.max(axis=1) - rgb.min(axis=1) for rgb in (base, out)]
            cool = [rgb[:, 2] - rgb[:, 0] for rgb in (base, out)]
            regions = [weights <= .05, core, (luma[0] >= .75) & (chroma[0] <= .10),
                       luma[0] <= .15, np.ones(pixels, dtype=bool)]
            current = np.zeros((2, 5, 4), dtype=np.float64)
            frame_clips = [int((rgb.max(axis=1) >= 254 / 255).sum()) for rgb in (base, out)]
            for side in range(2):
                for n, region in enumerate(regions):
                    current[side, n] = [int(region.sum()), float(luma[side][region].sum()),
                                        float(chroma[side][region].sum()), float(cool[side][region].sum())]
                clips[side] += frame_clips[side]
            totals += current
            signatures = [_signature(current[n], frame_clips[n], pixels) for n in range(2)]
            difference = out[core] - base[core]
            dy, dc = luma[1][core] - luma[0][core], cool[1][core] - cool[0][core]
            rows.append({"frame_index": index, "protected_pixels": int(core.sum()),
                         "rgb_abs_mean": float(np.abs(difference).mean()), "rgb_abs_max": float(np.abs(difference).max()),
                         "luma_delta": float(dy.mean()), "luma_abs_mean": float(np.abs(dy).mean()),
                         "cool_bias_delta": float(dc.mean()), "cool_bias_abs_mean": float(np.abs(dc).mean()),
                         "baseline": signatures[0], "candidate": signatures[1],
                         "failures": _safety(*signatures)})
    if len(rows) != expected:
        raise ValueError("Foundation、成片或蒙版缺帧")
    base_sig, out_sig = [_signature(totals[n], clips[n], pixels * expected) for n in range(2)]
    missing = [name for name, count in base_sig["sample_counts"].items() if not count]
    if missing:
        signature_report = {"status": "blocked", "blocking_failures": ["missing_signature_regions"],
                            "missing_regions": missing, "passed_signature_axes": [], "safety_failures": []}
    else:
        signature_report = grade.evaluate_signature(base_sig, out_sig, round(strength * 100))
    failed = [row["frame_index"] for row in rows if row["failures"]]
    worst = max(rows, key=lambda row: (bool(row["failures"]), row["rgb_abs_mean"]))
    passed = not failed and signature_report["status"] == "passed"
    return {"schema_version": "korean-cool-validation-v1", "passed": passed,
            "protection_passed": not failed, "status": "passed" if passed else "blocked",
            "frame_count": len(rows), "analysis_dimensions": [width, height], "frames": rows,
            "failed_frame_indices": failed, "worst_frame_index": worst["frame_index"],
            "signature": {"baseline": base_sig, "candidate": out_sig}, "signature_report": signature_report,
            "baseline_metrics": base_sig, "candidate_metrics": out_sig,
            "human_accepted": False, "boundary": BOUNDARY,
            "absolute_metric_boundary": "RGB/逐像素亮度与冷偏差绝对值是补充诊断；安全门沿用既有区域均值阈值，均值不能排除局部误差抵消。",
            "mask_sha256": evidence["mask"]["sha256"], "protection_fingerprint": evidence["fingerprint"]}
