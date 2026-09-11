#!/usr/bin/env python3
"""跨 App 标定：基准包生成、采样、响应拟合与留出集验证。

核心纪律：
- 拟合与验收必须用不同样本。用同一批样本拟合又验收，得出的误差没有意义。
- 只有留出集 ΔE00 中位数达标的滑杆才能标 `calibrated`；其余保持 `heuristic` 或 `unsupported`。
- 适配器主键必须包含 app、平台、设备、系统版本、App版本、输入与输出配置、适配器版本。
  任何一项变化都产生新适配器，旧的自动进入待复核，不得静默沿用。
- 没有真实 App 导出就不得宣称完成标定；仿真数据只能验证工具链本身。

只依赖标准库与 ffmpeg。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from color_diff import delta_e_2000, hue_angle_error, rgb8_to_lab  # noqa: E402

CALIBRATION_SCHEMA_VERSION = "4.0.0"
PATCH = 96          # 每个色块边长（像素）
COLUMNS = 8
# 规格门：直接来自 tasks/v40-semantic-video-calibration-plan.md 第 103 行
# 「留出集综合色差中位数目标不高于3」。这是能否宣称跨 App 等效的唯一硬线。
SPEC_GATE = {"holdout_delta_e00_median_max": 3.0}
# 加严门：本项目在规格之外自加的更高要求。达不到不代表违反规格，
# 但也不允许标成完全 calibrated——分级如实反映，不四舍五入成「通过」。
STRICT_GATES = {
    "holdout_delta_e00_p95_max": 6.0,
    "grayscale_rmse_max": 0.02,
    "hue_error_deg_max": 4.0,
    "skin_delta_e00_median_max": 2.0,
}
PROMOTION_GATES = {**SPEC_GATE, **STRICT_GATES}


class CalibrationError(Exception):
    pass


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise CalibrationError(f"缺少 {name}")
    return found


# --- 基准包 -------------------------------------------------------------
def _grayscale_patches(steps: int = 21) -> list[dict]:
    return [
        {"id": f"gray-{index:02d}", "group": "grayscale",
         "rgb": (value, value, value),
         "note": f"21 阶灰阶第 {index} 阶"}
        for index, value in enumerate(round(255 * i / (steps - 1)) for i in range(steps))
    ]


def _color_step_patches() -> list[dict]:
    bases = {
        "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
        "cyan": (0, 255, 255), "magenta": (255, 0, 255), "yellow": (255, 255, 0),
    }
    patches = []
    for name, rgb in bases.items():
        for level in (0.25, 0.5, 0.75, 1.0):
            patches.append({
                "id": f"{name}-{int(level * 100)}",
                "group": "color-steps",
                "rgb": tuple(round(channel * level + 24 * (1 - level)) for channel in rgb),
                "note": f"{name} 在 {level:.0%} 强度",
            })
    return patches


def _skin_patches() -> list[dict]:
    """覆盖不同肤色。历史经验明确要求不能只用一种肤色做验收。"""
    tones = {
        "skin-i": (247, 220, 199), "skin-ii": (240, 202, 170), "skin-iii": (223, 178, 140),
        "skin-iv": (198, 148, 108), "skin-v": (150, 104, 72), "skin-vi": (94, 62, 45),
    }
    return [{"id": key, "group": "skin", "rgb": value,
             "note": "肤色参考块；覆盖由浅到深，不允许只用单一肤色验收"}
            for key, value in tones.items()]


def _scene_patches() -> list[dict]:
    scene = {
        "sky-zenith": (60, 110, 180), "sky-horizon": (150, 185, 215),
        "sky-overcast": (176, 180, 186),
        "foliage-shadow": (36, 62, 34), "foliage-mid": (74, 118, 56),
        "foliage-sun": (139, 176, 84),
        "concrete": (150, 146, 138), "brick": (140, 82, 62), "wood": (150, 108, 66),
        "product-white": (242, 242, 240), "product-black": (26, 26, 28),
        "product-metal": (170, 172, 176),
    }
    return [{"id": key, "group": "scene", "rgb": value, "note": f"{key} 记忆色参考块"}
            for key, value in scene.items()]


def benchmark_patches() -> list[dict]:
    return (_grayscale_patches() + _color_step_patches()
            + _skin_patches() + _scene_patches())


def build_benchmark_package(out_dir: Path, profile: str = "srgb") -> dict:
    """生成基准图与清单。色块位置写进清单，导出后可精确回采。"""
    patches = benchmark_patches()
    columns = COLUMNS
    rows = math.ceil(len(patches) / columns)
    width, height = columns * PATCH, rows * PATCH
    raw = bytearray(width * height * 3)
    placed = []
    for index, patch in enumerate(patches):
        column, row = index % columns, index // columns
        x0, y0 = column * PATCH, row * PATCH
        red, green, blue = patch["rgb"]
        for y in range(y0, y0 + PATCH):
            base = (y * width + x0) * 3
            raw[base:base + PATCH * 3] = bytes((red, green, blue)) * PATCH
        placed.append({**patch, "rgb": list(patch["rgb"]),
                       "x": x0, "y": y0, "w": PATCH, "h": PATCH,
                       "sample_box": [x0 + PATCH // 4, y0 + PATCH // 4, PATCH // 2, PATCH // 2]})

    if profile == "display-p3":
        tags = ("setparams=colorspace=gbr:color_primaries=smpte432:"
                "color_trc=iec61966-2-1:range=pc,format=rgb48be")
        pixel_format = "rgb48be"
    else:
        tags = ("setparams=colorspace=gbr:color_primaries=bt709:"
                "color_trc=iec61966-2-1:range=pc,format=rgb24")
        pixel_format = "rgb24"
    out_dir.mkdir(parents=True, exist_ok=True)
    chart = out_dir / "benchmark-chart.png"
    if chart.exists():
        raise CalibrationError(f"基准图已存在，不会覆盖：{chart}")
    result = subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-n", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-i", "pipe:0", "-vf", tags,
         "-frames:v", "1", "-c:v", "png", "-pix_fmt", pixel_format, str(chart)],
        input=bytes(raw), capture_output=True)
    if result.returncode:
        raise CalibrationError(
            f"基准图生成失败：{result.stderr.decode('utf-8','replace').strip()[:160]}")

    # 渐变与噪声图分开生成：它们检验的是色带与噪声保持，不是色块精度。
    auxiliary = {}
    for name, spec in (
        ("gradient", f"gradients=size={width}x{height}:c0=#000000:c1=#FFFFFF:x0=0:y0=0:x1={width}:y1=0"),
        ("noise", f"color=c=#7F7F7F:size={width}x{height}"),
        ("latitude", f"gradients=size={width}x{height}:c0=#050505:c1=#FAFAFA:x0=0:y0=0:x1=0:y1={height}"),
    ):
        path = out_dir / f"benchmark-{name}.png"
        filters = tags if name != "noise" else f"noise=alls=18:allf=t+u,{tags}"
        command = [_tool("ffmpeg"), "-v", "error", "-n", "-f", "lavfi", "-i", spec,
                   "-vf", filters, "-frames:v", "1", "-c:v", "png",
                   "-pix_fmt", pixel_format, str(path)]
        if not path.exists():
            result = subprocess.run(command, capture_output=True)
            if result.returncode or not path.is_file():
                raise CalibrationError(
                    f"辅助基准图生成失败（{name}）："
                    f"{result.stderr.decode('utf-8','replace').strip()[:160]}"
                )
        auxiliary[name] = str(path)

    manifest = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "chart": str(chart),
        "auxiliary": auxiliary,
        "profile": "Display P3" if profile == "display-p3" else "sRGB",
        "pixel_format": pixel_format,
        "size": [width, height],
        "patch_size": PATCH,
        "patch_count": len(placed),
        "groups": sorted({item["group"] for item in placed}),
        "patches": placed,
        "chart_sha256": hashlib.sha256(chart.read_bytes()).hexdigest(),
        "auxiliary_sha256": {
            name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for name, path in auxiliary.items()
        },
        "boundary": (
            "基准图只提供受控输入。它本身不构成标定；标定必须由真实 App 导出的结果反推，"
            "且拟合与验收使用不同样本。"
        ),
    }
    manifest_path = out_dir / "benchmark-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


# --- 采样 ---------------------------------------------------------------
def sample_chart(image_path: Path, manifest: dict) -> dict:
    """从（真实 App 导出的）图中回采每个色块的平均值。"""
    width, height = manifest["size"]
    result = subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-i", str(image_path),
         "-vf", f"format=rgb24,scale={width}:{height}:flags=neighbor",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True)
    if result.returncode or not result.stdout:
        raise CalibrationError(f"无法读取导出图：{image_path}")
    data = result.stdout
    samples = []
    for patch in manifest["patches"]:
        x, y, w, h = patch["sample_box"]
        total = [0, 0, 0]
        count = 0
        for row in range(y, min(y + h, height)):
            base = (row * width) * 3
            for column in range(x, min(x + w, width)):
                offset = base + column * 3
                if offset + 3 > len(data):
                    continue
                total[0] += data[offset]
                total[1] += data[offset + 1]
                total[2] += data[offset + 2]
                count += 1
        if not count:
            continue
        measured = tuple(round(channel / count) for channel in total)
        samples.append({"id": patch["id"], "group": patch["group"],
                        "reference_rgb": patch["rgb"], "measured_rgb": list(measured)})
    return {"image": str(image_path), "sample_count": len(samples), "samples": samples}


# --- 自然照片验证集 -----------------------------------------------------
NATURAL_SAMPLE_GRID = 12   # 每张照片取 12x12 个采样点


def natural_photo_manifest(photo_dir: Path) -> dict:
    """把一批自然照片登记为验证集。

    合成色块负责「拟合」——它们受控、可复现、覆盖色空间。
    自然照片负责「在真实内容上验收」——真实素材的色彩分布、噪声、镜头特性
    都不是色块能代表的。两者角色不同，不能互相替代，也不能混在一起拟合。
    """
    photos = sorted(item for item in photo_dir.glob("*.jpg") if item.is_file())
    if not photos:
        raise CalibrationError(f"目录中没有可用的自然照片：{photo_dir}")
    records = []
    for path in photos:
        category = path.stem.split("-", 2)[-1] if "-" in path.stem else "unknown"
        records.append({
            "id": path.stem,
            "path": str(path),
            "category": category,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    categories = sorted({item["category"] for item in records})
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "role": "holdout-on-real-content",
        "photo_count": len(records),
        "categories": categories,
        "category_count": len(categories),
        "sample_grid": NATURAL_SAMPLE_GRID,
        "photos": records,
        "boundary": (
            "自然照片只用于在真实内容上验收，绝不参与拟合。"
            "用它们拟合会把某一批素材的色彩偏好烧进适配器。"
        ),
    }


def sample_natural_pair(before: Path, after: Path,
                        grid: int = NATURAL_SAMPLE_GRID) -> list[dict]:
    """从「原图 / App 导出图」这一对里取匹配采样点。

    两张图必须逐点对应，因此统一缩放到同一网格。
    App 若做了裁切或几何变换，这个前提就不成立——那种情况必须单独处理，不能默认对应。
    """
    def read(path: Path) -> bytes:
        result = subprocess.run(
            [_tool("ffmpeg"), "-v", "error", "-i", str(path),
             "-vf", f"format=rgb24,scale={grid}:{grid}:flags=area",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            capture_output=True)
        if result.returncode or not result.stdout:
            raise CalibrationError(f"无法采样：{path.name}")
        return result.stdout

    source, target = read(before), read(after)
    if len(source) != len(target):
        raise CalibrationError("原图与导出图的采样网格不一致，可能发生了裁切或几何变换")
    samples = []
    for index in range(0, min(len(source), len(target)) - 2, 3):
        samples.append({
            "id": f"{before.stem}-p{index // 3:03d}",
            "group": "natural",
            "reference_rgb": [source[index], source[index + 1], source[index + 2]],
            "measured_rgb": [target[index], target[index + 1], target[index + 2]],
        })
    return samples


# --- 拟合与验收 ---------------------------------------------------------
def fit_grayscale_response(samples: list[dict]) -> dict:
    """灰阶响应：输入归一化亮度 → 输出归一化亮度的单调查找表。

    注意它只描述「亮度怎么变」，不描述「中性色是否还中性」。
    很多 App 会让灰阶带上偏色，那部分由颜色矩阵承担——
    早期实现把灰阶预测强行写成中性色，结果灰阶组的 ΔE00 反而是全场最差（中位 4.337），
    因为仿真 App 的通道增益让灰变暖了。模型必须是「影调响应 → 颜色矩阵」的级联。
    """
    points = []
    for item in samples:
        if item["group"] != "grayscale":
            continue
        source = item["reference_rgb"][0] / 255.0
        target = sum(item["measured_rgb"]) / 3 / 255.0
        points.append((source, target))
    if len(points) < 5:
        raise CalibrationError("灰阶样本不足，无法拟合响应")
    points.sort()
    # 强制单调，避免噪声导致的反相。
    monotone = []
    highest = -1.0
    for source, target in points:
        highest = max(highest, target)
        monotone.append((round(source, 6), round(highest, 6)))
    return {"type": "monotone-lut", "points": monotone, "sample_count": len(monotone)}


def apply_grayscale_response(response: dict, value: float) -> float:
    points = response["points"]
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for index in range(1, len(points)):
        x0, y0 = points[index - 1]
        x1, y1 = points[index]
        if value <= x1:
            span = x1 - x0
            ratio = 0.0 if span <= 0 else (value - x0) / span
            return y0 + (y1 - y0) * ratio
    return points[-1][1]


def fit_color_matrix(samples: list[dict], tone: dict | None = None,
                     exclude_groups: tuple[str, ...] = ()) -> dict:
    """3×3 线性最小二乘颜色矩阵。用正规方程解，不引入第三方依赖。

    输入端先过影调响应再拟合矩阵，因此矩阵只需要承担「颜色偏移」这一件事。
    如果不先去掉影调的非线性，矩阵会被迫同时拟合亮度曲线，两头都拟合不好。
    灰阶样本必须参与——它们正是约束「中性色跑到哪里去」的关键证据。
    """
    rows = [item for item in samples if item["group"] not in exclude_groups]
    if len(rows) < 6:
        raise CalibrationError("颜色样本不足，无法拟合矩阵")
    ata = [[0.0] * 3 for _ in range(3)]
    atb = [[0.0] * 3 for _ in range(3)]
    for item in rows:
        source = [channel / 255.0 for channel in item["reference_rgb"]]
        if tone is not None:
            source = [apply_grayscale_response(tone, value) for value in source]
        target = [channel / 255.0 for channel in item["measured_rgb"]]
        for i in range(3):
            for j in range(3):
                ata[i][j] += source[i] * source[j]
            for k in range(3):
                atb[i][k] += source[i] * target[k]
    # 高斯消元求 ata^-1 @ atb
    augmented = [ata[i][:] + atb[i][:] for i in range(3)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise CalibrationError("颜色样本退化，矩阵不可解")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        factor = augmented[column][column]
        augmented[column] = [value / factor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            scale = augmented[row][column]
            augmented[row] = [value - scale * augmented[column][index]
                              for index, value in enumerate(augmented[row])]
    matrix = [[round(augmented[i][3 + k], 6) for i in range(3)] for k in range(3)]
    return {"type": "linear-3x3", "matrix": matrix, "sample_count": len(rows)}


def _root_polynomial_terms(r: float, g: float, b: float) -> list[float]:
    """Finlayson 根多项式项。

    为什么用它而不是普通二次多项式：根号项与线性项同阶，因此整组基对曝光缩放是齐次的，
    亮度整体变化不会破坏拟合出的颜色关系。这是相机颜色特征化的标准做法。
    线性 3×3 建模不了饱和度这类非线性，实测在深肤色上留下 4.86° 的色相预测误差。
    """
    return [r, g, b,
            math.sqrt(max(0.0, r * g)),
            math.sqrt(max(0.0, g * b)),
            math.sqrt(max(0.0, r * b))]


def _solve_least_squares(rows: list[tuple[list[float], list[float]]], terms: int) -> list[list[float]]:
    ata = [[0.0] * terms for _ in range(terms)]
    atb = [[0.0] * 3 for _ in range(terms)]
    for basis, target in rows:
        for i in range(terms):
            for j in range(terms):
                ata[i][j] += basis[i] * basis[j]
            for k in range(3):
                atb[i][k] += basis[i] * target[k]
    # 轻微 Tikhonov 正则，避免样本共线导致数值不稳。
    for i in range(terms):
        ata[i][i] += 1e-7
    augmented = [ata[i][:] + atb[i][:] for i in range(terms)]
    for column in range(terms):
        pivot = max(range(column, terms), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise CalibrationError("样本退化，模型不可解")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        factor = augmented[column][column]
        augmented[column] = [value / factor for value in augmented[column]]
        for row in range(terms):
            if row == column:
                continue
            scale = augmented[row][column]
            augmented[row] = [value - scale * augmented[column][index]
                              for index, value in enumerate(augmented[row])]
    return [[round(augmented[i][terms + k], 8) for i in range(terms)] for k in range(3)]


def fit_root_polynomial(samples: list[dict], tone: dict | None = None,
                        exclude_groups: tuple[str, ...] = ()) -> dict:
    rows = [item for item in samples if item["group"] not in exclude_groups]
    if len(rows) < 8:
        raise CalibrationError("样本不足，无法拟合根多项式模型")
    prepared = []
    for item in rows:
        source = [channel / 255.0 for channel in item["reference_rgb"]]
        if tone is not None:
            source = [apply_grayscale_response(tone, value) for value in source]
        prepared.append((_root_polynomial_terms(*source),
                         [channel / 255.0 for channel in item["measured_rgb"]]))
    coefficients = _solve_least_squares(prepared, 6)
    return {"type": "root-polynomial-6", "coefficients": coefficients,
            "sample_count": len(rows),
            "basis": "[R, G, B, sqrt(RG), sqrt(GB), sqrt(RB)]",
            "note": "Finlayson 根多项式；对曝光缩放齐次，因此亮度变化不破坏颜色关系"}


def apply_root_polynomial(model: dict, rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    terms = _root_polynomial_terms(*[channel / 255.0 for channel in rgb])
    coefficients = model["coefficients"]
    out = [sum(coefficients[k][i] * terms[i] for i in range(6)) for k in range(3)]
    return tuple(max(0, min(255, round(value * 255))) for value in out)


def apply_color_matrix(matrix: dict, rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    m = matrix["matrix"]
    source = [channel / 255.0 for channel in rgb]
    out = [sum(m[k][i] * source[i] for i in range(3)) for k in range(3)]
    return tuple(max(0, min(255, round(value * 255))) for value in out)


def predict(model: dict, rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """级联预测：先过影调响应，再过颜色模型。顺序不能颠倒。"""
    toned = tuple(
        max(0, min(255, round(apply_grayscale_response(model["grayscale"], channel / 255.0) * 255)))
        for channel in rgb
    )
    color = model.get("color_model") or model.get("color_matrix")
    if color and color.get("type") == "root-polynomial-6":
        return apply_root_polynomial(color, toned)
    return apply_color_matrix(color, toned)


def holdout_validate(model: dict, holdout: list[dict], profile: str = "srgb") -> dict:
    """留出集验收。拟合样本与这里的样本必须不同，否则报告没有意义。"""
    deltas, hue_errors, gray_errors = [], [], []
    rows = []
    for item in holdout:
        reference = tuple(item["reference_rgb"])
        measured = tuple(item["measured_rgb"])
        predicted = predict(model, reference)
        if item["group"] == "grayscale":
            gray_errors.append(
                (sum(measured) / 3 / 255.0 - sum(predicted) / 3 / 255.0) ** 2
            )
        lab_measured = rgb8_to_lab(*measured, profile=profile)
        lab_predicted = rgb8_to_lab(*predicted, profile=profile)
        delta = delta_e_2000(lab_measured, lab_predicted)
        hue = hue_angle_error(lab_measured, lab_predicted)
        deltas.append(delta)
        if hue:
            hue_errors.append(hue)
        rows.append({"id": item["id"], "group": item["group"],
                     "reference_rgb": list(reference), "measured_rgb": list(measured),
                     "predicted_rgb": list(predicted),
                     "delta_e00": round(delta, 4), "hue_error_deg": round(hue, 3)})
    ordered = sorted(deltas)
    median = statistics.median(ordered) if ordered else 0.0
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0.0
    grayscale_rmse = math.sqrt(statistics.fmean(gray_errors)) if gray_errors else 0.0
    hue_max = max(hue_errors) if hue_errors else 0.0
    skin_deltas = [row["delta_e00"] for row in rows if row["group"] == "skin"]
    skin_median = statistics.median(skin_deltas) if skin_deltas else 0.0
    spec_ok = median <= SPEC_GATE["holdout_delta_e00_median_max"]
    strict_results = {
        "holdout_delta_e00_p95_max": p95 <= STRICT_GATES["holdout_delta_e00_p95_max"],
        "grayscale_rmse_max": grayscale_rmse <= STRICT_GATES["grayscale_rmse_max"],
        "hue_error_deg_max": hue_max <= STRICT_GATES["hue_error_deg_max"],
        "skin_delta_e00_median_max": (
            skin_median <= STRICT_GATES["skin_delta_e00_median_max"] if skin_deltas else True
        ),
    }
    strict_ok = all(strict_results.values())
    if not spec_ok:
        status = "heuristic"
    elif strict_ok:
        status = "calibrated"
    else:
        status = "calibrated-provisional"
    by_group = {}
    for row in rows:
        by_group.setdefault(row["group"], []).append(row["delta_e00"])
    return {
        "sample_count": len(rows),
        "per_group_delta_e00_median": {
            key: round(statistics.median(value), 4) for key, value in sorted(by_group.items())
        },
        "skin_delta_e00_median": round(skin_median, 4),
        "spec_gate": SPEC_GATE,
        "spec_gate_passed": spec_ok,
        "strict_gates": STRICT_GATES,
        "strict_gate_results": strict_results,
        "strict_gate_passed": strict_ok,
        "delta_e00_median": round(median, 4),
        "delta_e00_p95": round(p95, 4),
        "delta_e00_max": round(max(deltas), 4) if deltas else 0.0,
        "grayscale_rmse": round(grayscale_rmse, 5),
        "hue_error_deg_max": round(hue_max, 3),
        "passes_promotion_gate": spec_ok and strict_ok,
        "calibration_status": status,
        "status_meaning": {
            "calibrated": "规格门与全部加严门都通过，可作为该 App 该版本的标定适配器",
            "calibrated-provisional": "规格门通过但至少一项加严门未过；可用但必须标注受限项，不得宣称完全等效",
            "heuristic": "规格门未通过；只能作为人工起点，不得宣称跨 App 等效",
        }[status],
        "rows": rows,
        "boundary": (
            "本报告只说明「用受控基准图拟合出的模型能否预测该 App 的输出」。"
            "它不代表跨 App 审美等价，也不代表在此基准之外的素材同样准确。"
        ),
    }


def adapter_key(app: str, platform: str, device: str, os_version: str,
                app_version: str, input_profile: str, export_profile: str,
                adapter_version: str) -> str:
    parts = [app, platform, device, os_version, app_version, input_profile, export_profile, adapter_version]
    if not all(str(item).strip() for item in parts):
        raise CalibrationError("适配器主键的八个字段都必须非空，缺一不可")
    return "|".join(str(item).strip() for item in parts)


def build_adapter(app: str, platform: str, device: str, os_version: str, app_version: str,
                  input_profile: str, export_profile: str, adapter_version: str,
                  model: dict, validation: dict, evidence: dict) -> dict:
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "key": adapter_key(app, platform, device, os_version, app_version,
                           input_profile, export_profile, adapter_version),
        "app": app, "platform": platform, "device": device,
        "os_version": os_version, "app_version": app_version,
        "input_profile": input_profile, "export_profile": export_profile,
        "adapter_version": adapter_version,
        "calibration_status": validation["calibration_status"],
        "model": model,
        "holdout_validation": {key: value for key, value in validation.items() if key != "rows"},
        "evidence": evidence,
        "revalidation_policy": (
            "App 版本、系统版本、设备或导出配置任一变化即产生新主键；"
            "旧适配器自动进入待复核，不得静默沿用。"
        ),
    }
