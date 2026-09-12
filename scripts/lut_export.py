#!/usr/bin/env python3
"""把已确认计划的全局色彩链烘焙为 .cube，并实测单素材往返误差。"""

from __future__ import annotations

import shutil
import hashlib
import json
import statistics
import subprocess
from pathlib import Path

from color_diff import delta_e_2000, rgb8_to_lab


def color_only_filter(engine, plan: dict) -> str:
    from signature_regions import ROLES
    if ((plan.get('style') or {}).get('id') in ROLES or any(
            key in plan for key in ('signature_regions', 'signature_execution_sha256'))):
        raise ValueError('当前签名依赖人工区域或序列时序，普通 3D LUT 无法保留，拒绝全局替代导出。')
    if ((plan.get("style") or {}).get("id") == "korean-cool" or any(
            key in plan for key in ('korean_cool_protection', 'korean_execution_sha256'))):
        raise ValueError(
            "韩系清冷依赖当前素材的空间保护，普通 3D LUT 无法表达人物与亮部蒙版。"
            "不会导出丢失保护的全局版本；旧计划也不例外。"
            "请重新 plan，并确认计划编号与 korean-cool-protection 后使用 render 生成成片。")
    parameters = dict(plan["parameters"])
    for spatial in ("sharpness", "vignette", "grain"):
        parameters[spatial] = 0.0
    return engine.build_filter(
        parameters,
        plan.get("tone_curve"),
        plan.get("hsl_bands"),
        plan.get("primary_grade"),
    )


def identity_bytes(size: int) -> bytes:
    values = bytearray()
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                values.extend((
                    round(red * 255 / (size - 1)),
                    round(green * 255 / (size - 1)),
                    round(blue * 255 / (size - 1)),
                ))
    return bytes(values)


def transform_grid(filtergraph: str, size: int) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("缺少 ffmpeg")
    width = size * size
    command = [
        ffmpeg, "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{size}", "-i", "-", "-vf", filtergraph,
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    result = subprocess.run(command, input=identity_bytes(size), capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    expected = size ** 3 * 3
    if len(result.stdout) != expected:
        raise RuntimeError(f"LUT 网格输出长度异常：{len(result.stdout)} != {expected}")
    return result.stdout


def write_cube(path: Path, size: int, transformed: bytes, title: str) -> None:
    if path.exists():
        raise FileExistsError(f"LUT 文件已存在，不覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'TITLE "{title}"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    for index in range(0, len(transformed), 3):
        lines.append(" ".join(f"{transformed[index + channel] / 255:.8f}" for channel in range(3)))
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def _sample(source: Path, filtergraph: str) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    command = [ffmpeg, "-v", "error", "-i", str(source), "-vf",
               f"{filtergraph},scale=128:72", "-frames:v", "1",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def precision_report(source: Path, direct_filter: str, cube_path: Path) -> dict:
    direct = _sample(source, direct_filter)
    escaped = str(cube_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    baked = _sample(source, f"lut3d=file='{escaped}'")
    deltas = []
    for index in range(0, min(len(direct), len(baked)), 3):
        first = rgb8_to_lab(direct[index], direct[index + 1], direct[index + 2])
        second = rgb8_to_lab(baked[index], baked[index + 1], baked[index + 2])
        deltas.append(delta_e_2000(first, second))
    ordered = sorted(deltas)
    p95 = ordered[min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))]
    return {
        "sampled_pixels": len(deltas),
        "delta_e00_median": round(statistics.median(deltas), 4),
        "delta_e00_p95": round(p95, 4),
        "threshold_status": "awaiting-review-set-calibration",
        "boundary": "当前只报告实测误差；正式阈值需由独立评审集的标定集与留出集共同确定。",
    }


def review_set_calibration(manifest_path: Path, direct_filter: str, cube_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = sorted(manifest.get("photos") or [], key=lambda item: item["sha256"])
    if len(items) < 40:
        raise ValueError("LUT 标定要求至少 40 张许可照片")
    rows = []
    for item in items:
        measured = precision_report(Path(item["path"]), direct_filter, cube_path)
        rows.append({"key": item["key"], "sha256": item["sha256"],
                     "delta_e00_median": measured["delta_e00_median"],
                     "delta_e00_p95": measured["delta_e00_p95"]})
    calibration, holdout = rows[:28], rows[28:40]

    def p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))]

    thresholds = {
        "delta_e00_median_max": round(p95([row["delta_e00_median"] for row in calibration]), 4),
        "delta_e00_p95_max": round(p95([row["delta_e00_p95"] for row in calibration]), 4),
    }
    passed = [
        row for row in holdout
        if row["delta_e00_median"] <= thresholds["delta_e00_median_max"]
        and row["delta_e00_p95"] <= thresholds["delta_e00_p95_max"]
    ]
    return {
        "split": {"calibration": 28, "holdout": 12, "rule": "按源文件 SHA-256 排序固定切分"},
        "thresholds": thresholds, "holdout_passed": len(passed), "holdout_total": 12,
        "status": "passed" if len(passed) == 12 else "not_met",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "rows": rows,
        "boundary": "阈值取标定集逐图误差的 P95；留出集逐图同时满足中位数与 P95 才计通过。",
    }


def export(engine, plan: dict, output: Path, size: int,
           calibration_manifest: Path | None = None) -> dict:
    if size not in {17, 33, 65}:
        raise ValueError("LUT 尺寸只支持 17、33、65")
    filtergraph = color_only_filter(engine, plan)
    transformed = transform_grid(filtergraph, size)
    write_cube(output, size, transformed, f"BLCaptain {plan['style']['id']} {plan['plan_id'][:12]}")
    report = precision_report(Path(plan["source"]["path"]), filtergraph, output)
    result = {
        "schema_version": "1.0.0", "plan_id": plan["plan_id"],
        "lut_path": str(output), "lut_size": size, "precision": report,
        "included": ["全局影调", "全局色彩", "曲线", "HSL", "全局一级校正"],
        "excluded": ["构图", "语义局部蒙版", "径向注意力", "锐化", "暗角", "颗粒", "逐镜头匹配"],
        "boundary": "LUT 只代表已确认计划的全局色彩链；空间性算子与逐镜头动作不会被烘焙。",
    }
    if calibration_manifest:
        result["review_set_calibration"] = review_set_calibration(
            calibration_manifest, filtergraph, output)
        result["precision"]["threshold_status"] = (
            "calibrated-holdout-passed"
            if result["review_set_calibration"]["status"] == "passed"
            else "calibrated-holdout-not-met"
        )
        result["precision"]["calibrated_thresholds"] = result["review_set_calibration"]["thresholds"]
    return result
