#!/usr/bin/env python3
"""BLCaptain 调色公式的本地、确认门禁式 FFmpeg 渲染器。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":
    from bootstrap import bootstrap_or_exit

    bootstrap_or_exit(Path(__file__).resolve().parents[1])

from aesthetic_audit import evaluate_direction, impact_profile, measure
from recipe_access import admitted_media, apply_execution_overrides, executable_media


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "references" / "recipes.json"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
LOG_TRANSFERS = {"log100", "log316"}
HSL_BANDS = {"r", "y", "g", "c", "b", "m"}
PARAMETER_LIMITS = {
    "brightness": (-0.25, 0.25),
    "contrast": (0.5, 1.5),
    "saturation": (0.0, 1.8),
    "gamma": (0.5, 1.5),
    "temperature": (-0.2, 0.2),
    "tint": (-0.2, 0.2),
    "sharpness": (0.0, 1.0),
    "vignette": (0.0, 1.0),
    "grain": (0.0, 10.0),
    "teal_orange": (-0.2, 0.2),
    "shadow_teal": (-0.2, 0.2),
}
RENDERER_VERSION = "ffmpeg-v3.0"


class SkillError(Exception):
    def __init__(self, message: str, code: int = 1, artifact_status: str | None = None,
                 *, recovery_step: str | None = None):
        super().__init__(message)
        self.code = code
        self.artifact_status = artifact_status
        self.recovery_step = recovery_step



# Rec.709 亮度权重。拉回矩阵靠「每行系数和为 1」保证中性恒等，权重必须与之一致。
LUMA_WR, LUMA_WG, LUMA_WB = 0.2126, 0.7152, 0.0722

def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise SkillError(f"缺少 {name}。请先获得用户授权，再安装 ffmpeg。", 5)
    return found


def run(command: list[str]) -> None:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1:] or ["未知错误"]
        raise SkillError(f"FFmpeg 渲染失败：{detail[0]}", 6)


def validate_curve(curve: dict, recipe_id: str) -> None:
    points = curve.get("points") if isinstance(curve, dict) else None
    if not isinstance(points, list) or not 2 <= len(points) <= 9:
        raise SkillError(f"配方曲线无效：{recipe_id}")
    # 曲线在 0 处抬起就是加法 offset 的定义，不管抬得多小。
    # 早先 5 套配方的起点在 0.005–0.024 之间，各自的总加法量都在门限内，
    # 于是一直没被发现——但把黑推离黑这件事本身不该由曲线偷偷完成。
    # 要提亮暗部请让权重在 0 处归零、在暗部中段拉满（见 apply_adjustments）。
    if points and float(points[0][0]) == 0.0 and float(points[0][1]) > 1e-9:
        raise SkillError(
            f"配方曲线的起点必须是 0：{recipe_id} 起点为 {points[0][1]}——"
            "曲线在 0 处抬起等于加法 offset，黑不再是黑")
    previous = None
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(value, (int, float)) for value in point):
            raise SkillError(f"配方曲线点无效：{recipe_id}")
        x, y = map(float, point)
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise SkillError(f"配方曲线超出 0–1：{recipe_id}")
        if previous:
            px, py = previous
            if x <= px or y < py or x - px < 0.05:
                raise SkillError(f"配方曲线必须单调且横坐标严格递增：{recipe_id}")
            slope = (y - py) / (x - px)
            if not 0.15 <= slope <= 4:
                raise SkillError(f"配方曲线斜率不安全：{recipe_id}")
        previous = (x, y)


def validate_hsl(items: list, label: str) -> None:
    if not isinstance(items, list):
        raise SkillError(f"HSL 色带无效：{label}")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"band", "hue", "saturation", "intensity"}:
            raise SkillError(f"HSL 色带字段无效：{label}")
        if item["band"] not in HSL_BANDS or not -12 <= float(item["hue"]) <= 12 or not -0.2 <= float(item["saturation"]) <= 0.2 or not -0.12 <= float(item["intensity"]) <= 0.12:
            raise SkillError(f"HSL 色带超出安全范围：{label}")


def validate_parameters(parameters: dict, label: str) -> None:
    required = set(PARAMETER_LIMITS) - {"teal_orange", "shadow_teal"}
    if not isinstance(parameters, dict) or not required <= set(parameters):
        raise SkillError(f"配方参数不完整：{label}")
    unknown = set(parameters) - set(PARAMETER_LIMITS)
    if unknown:
        raise SkillError(f"配方参数字段无效：{label} / {sorted(unknown)}")
    for name, value in parameters.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SkillError(f"配方参数必须是数值：{label} / {name}")
        low, high = PARAMETER_LIMITS[name]
        if not low <= float(value) <= high:
            raise SkillError(f"配方参数超出安全范围：{label} / {name}={value}")


def load_catalog(path: Path = DEFAULT_CATALOG) -> dict:
    try:
        payload = apply_execution_overrides(
            json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise SkillError(f"无法读取配方库：{error}") from error
    recipes = payload.get("recipes")
    if payload.get("schema_version") != "4.0.0" or not isinstance(recipes, list):
        raise SkillError("配方库 schema_version 或 recipes 无效")
    ids: set[str] = set()
    style_bible_required = {
        "id", "name", "version", "calibration_status", "worldview",
        "one_line_thesis", "emotional_target", "fit", "viewing_path",
        "light_philosophy", "five_zone_tone", "palette",
        "hue_chroma_lightness", "memory_color_protection", "spatial_layering",
        "texture", "forbidden", "strength_targets", "photo_video_delta",
        "fallback", "evidence",
    }
    for item in recipes:
        if not isinstance(item, dict):
            raise SkillError("配方记录必须是对象")
        if not {"id", "name", "aliases", "status", "media_types", "summary", "parameters", "warnings", "evidence"} <= set(item):
            raise SkillError("配方缺少必需字段")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item["id"]):
            raise SkillError(f"配方 ID 无效：{item['id']}")
        if item["id"] in ids:
            raise SkillError(f"配方 ID 重复：{item['id']}")
        ids.add(item["id"])
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise SkillError(f"配方名称无效：{item['id']}")
        if not isinstance(item["aliases"], list) or not all(isinstance(value, str) and value.strip() for value in item["aliases"]):
            raise SkillError(f"配方别名无效：{item['id']}")
        if item["name"] in item["aliases"] or len(set(item["aliases"])) != len(item["aliases"]):
            raise SkillError(f"配方名称或别名重复：{item['id']}")
        if not isinstance(item.get("scenes"), list) or not item["scenes"]:
            raise SkillError(f"配方场景无效：{item['id']}")
        if not isinstance(item["media_types"], list) or not item["media_types"] or not set(item["media_types"]) <= {"photo", "video"}:
            raise SkillError(f"配方媒体类型无效：{item['id']}")
        validate_parameters(item["parameters"], item["id"])
        if not isinstance(item["status"], str) or item["status"] not in {"active", "manual-executable", "research"} or not isinstance(item["evidence"], dict) or set(item["evidence"]) != {"provenance", "portability", "validation", "rights"}:
            raise SkillError(f"配方证据字段无效：{item['id']}")
        if not all(isinstance(value, str) and value.strip() for value in item["evidence"].values()):
            raise SkillError(f"配方证据内容无效：{item['id']}")
        try:
            accessible_media = admitted_media(item)
        except ValueError as error:
            raise SkillError(f"配方 {item['id']} 媒体准入无效：{error}", 3) from error
        style_bible = item.get("style_bible")
        if style_bible is not None and not isinstance(style_bible, dict):
            raise SkillError(f"配方 Style Bible 不完整：{item['id']}")
        if accessible_media and (
                not style_bible
                or not style_bible_required <= set(style_bible)
                or style_bible.get("id") != item["id"]):
            raise SkillError(f"配方 Style Bible 不完整：{item['id']}")
        signature_fields = {"collection", "design_fingerprint"} & set(item)
        if signature_fields:
            fingerprint = item.get("design_fingerprint")
            if item.get("collection") != "BLCaptain Signature" or not isinstance(fingerprint, dict) or set(fingerprint) != {"tone", "hue", "texture"}:
                raise SkillError(f"Signature 指纹字段无效：{item['id']}")
            if not all(isinstance(value, str) and value.strip() for value in fingerprint.values()):
                raise SkillError(f"Signature 指纹内容无效：{item['id']}")
            if "原创设计综合" not in item["evidence"]["provenance"]:
                raise SkillError(f"Signature 来源边界无效：{item['id']}")
            numeric = item.get("signature_numeric_fingerprint")
            if (not isinstance(numeric, dict)
                    or set(numeric) != {"tone", "color", "texture", "basis"}
                    or not all(isinstance(numeric[key], list) and numeric[key]
                               for key in ("tone", "color", "texture"))
                    or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                               for key in ("tone", "color", "texture") for value in numeric[key])
                    or not isinstance(numeric["basis"], str) or not numeric["basis"].strip()):
                raise SkillError(f"Signature 数值记忆手势无效：{item['id']}")
        if "tone_curve" in item:
            validate_curve(item["tone_curve"], item["id"])
        if "hsl_bands" in item:
            validate_hsl(item["hsl_bands"], item["id"])
        for media_type in (item.get("media_overrides") or {}):
            effective = recipe_for_media(item, media_type)
            label = f"{item['id']} / {media_type} 覆盖"
            validate_parameters(effective["parameters"], label)
            if "tone_curve" in effective:
                validate_curve(effective["tone_curve"], label)
            if "hsl_bands" in effective:
                validate_hsl(effective["hsl_bands"], label)
        art = item.get("art_direction")
        if not isinstance(art, dict) or set(art) != {"thesis", "emotion", "light", "composition", "palette_rule", "aftertaste", "forbidden"}:
            raise SkillError(f"配方视觉导演字段无效：{item['id']}")
        if not all(isinstance(value, str) and value.strip() for value in art.values()):
            raise SkillError(f"配方视觉导演内容无效：{item['id']}")
        impact = item.get("impact")
        impact_required = {"family", "strength_gamma", "tone_policy", "min_delta_e", "max_delta_e"}
        if not isinstance(impact, dict) or not impact_required <= set(impact) <= impact_required | {"min_delta_e_video"}:
            raise SkillError(f"配方效果标定字段无效：{item['id']}")
        if impact["family"] not in {"core", "dramatic", "signature"}:
            raise SkillError(f"配方效果家族无效：{item['id']}")
        tone_policy = impact["tone_policy"]
        if not isinstance(tone_policy, dict) or set(tone_policy) != {"midpoint_ev", "shadow_bias", "highlight_bias", "contrast_intent"}:
            raise SkillError(f"配方相对影调策略无效：{item['id']}")
        if not 0.45 <= float(impact["strength_gamma"]) < 1 or not -1 <= float(tone_policy["midpoint_ev"]) <= 1:
            raise SkillError(f"配方感知强度或相对EV无效：{item['id']}")
        if not -0.2 <= float(tone_policy["shadow_bias"]) <= 0.2 or not -0.2 <= float(tone_policy["highlight_bias"]) <= 0.2:
            raise SkillError(f"配方影调偏置无效：{item['id']}")
        if tone_policy["contrast_intent"] not in {"increase", "preserve", "soften"}:
            raise SkillError(f"配方对比意图无效：{item['id']}")
        if not 0.002 <= float(impact["min_delta_e"]) < float(impact["max_delta_e"]) <= 0.6:
            raise SkillError(f"配方视觉变化门限无效：{item['id']}")
        if "min_delta_e_video" in impact and not 0.004 <= float(impact["min_delta_e_video"]) <= float(impact["min_delta_e"]):
            raise SkillError(f"配方视频视觉变化门限无效：{item['id']}")
        suitability = item.get("suitability")
        if not isinstance(suitability, dict) or set(suitability) != {"best_for", "avoid_when", "required_observations", "fallback"}:
            raise SkillError(f"配方适用条件无效：{item['id']}")
        if not all(suitability.values()):
            raise SkillError(f"配方适用条件不能为空：{item['id']}")
        visual_targets = item.get("visual_targets")
        if not isinstance(visual_targets, dict) or set(visual_targets) != {"tone_span", "colorfulness", "subject_separation", "local_contrast", "texture"}:
            raise SkillError(f"配方视觉目标无效：{item['id']}")
        if visual_targets["tone_span"] not in {"increase", "preserve", "compress"} or visual_targets["colorfulness"] not in {"increase", "preserve", "decrease"}:
            raise SkillError(f"配方影调或综合色彩方向无效：{item['id']}")
        if visual_targets["subject_separation"] not in {"increase", "preserve"} or visual_targets["local_contrast"] not in {"increase", "preserve", "soften"}:
            raise SkillError(f"配方层级方向无效：{item['id']}")
        if visual_targets["texture"] not in {"clean", "soft", "grain", "crisp"}:
            raise SkillError(f"配方材质方向无效：{item['id']}")
        seasonal = item.get("seasonal_affinity")
        allowed_seasons = {"春", "夏", "秋", "冬", "全季"}
        if (not isinstance(seasonal, dict)
                or set(seasonal) != {"seasons", "basis", "confidence"}
                or not isinstance(seasonal["seasons"], list)
                or not seasonal["seasons"]
                or not set(seasonal["seasons"]) <= allowed_seasons
                or seasonal["confidence"] not in {"conditional", "broad"}
                or not isinstance(seasonal["basis"], str)
                or not seasonal["basis"].strip()):
            raise SkillError(f"配方季节亲和字段无效：{item['id']}")
        # 影调四轴的分化是目录期就能判的性质，不需要素材：
        # 高光轴与阴影轴取同值等于只有一个 contrast 滑块，配方未分化。
        # visual_grammar.check_axis_differentiation 此前只在测试里被调用过，
        # 从来没有接进任何执行链——门存在但从不运行，比没有门更危险。
        axes = item.get("tone_axes")
        if isinstance(axes, dict):
            import visual_grammar
            axis_report = visual_grammar.check_axis_differentiation(axes)
            if not axis_report["passed"]:
                raise SkillError(
                    f"配方影调轴未分化：{item['id']}——{axis_report['advice']}")
    drafts = payload.get("style_bible_drafts")
    if not isinstance(drafts, list):
        raise SkillError("配方库缺少 style_bible_drafts")
    for draft in drafts:
        bible = draft.get("style_bible") if isinstance(draft, dict) else None
        if (not isinstance(draft, dict)
                or draft.get("executable") is not False
                or draft.get("calibration_status") != "pending-calibration"
                or not isinstance(bible, dict)
                or not style_bible_required <= set(bible)
                or bible.get("id") != draft.get("id")):
            raise SkillError(f"Style Bible 草案无效：{draft.get('id', '未知') if isinstance(draft, dict) else '未知'}")
    return payload


def probe(path: Path) -> dict:
    ffprobe = require_tool("ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise SkillError(f"无法探测素材：{result.stderr.strip()}", 3)
    return json.loads(result.stdout)


def embedded_photo_profile(path: Path) -> str | None:
    """在 macOS 上补充读取 ICC/cICP 的用户可见配置名称；其他系统安全降级。"""
    sips = shutil.which("sips")
    if not sips:
        return None
    result = subprocess.run([sips, "-g", "profile", str(path)], text=True, capture_output=True)
    if result.returncode:
        return None
    match = re.search(r"^\s*profile:\s*(.+?)\s*$", result.stdout, re.MULTILINE)
    if not match or match.group(1) in {"<nil>", "(null)"}:
        return None
    return match.group(1)



def unsupported_color_message(color: dict, source_path: str) -> str:
    """硬停不变，但要给用户一条他自己能跑的路。

    以前这里只说「请转到专业色彩管理流程」——正确但没用：
    普通用户不知道那是什么流程，也不知道下一步该敲什么。
    实测 5 张 CC0 真实照片里有 2 张内嵌 Adobe RGB，直接卡死在这一句上。

    现在分两种情况如实回答：本机能做可靠 ICC 变换时给出确切命令；
    做不了时说清缺什么、怎么装。两种情况都不放松硬停——
    转换必须由用户显式发起，且写新文件，绝不在渲染链路里偷偷做。
    """
    from color_profile_convert import CONVERTIBLE, probe

    base = f"检测到 {color['label']}：{color['reason']}。"
    if color.get("profile") not in CONVERTIBLE:
        return base + "当前流程不对这种色域做静默转换。"
    cap = probe()
    if not cap["available"]:
        return (base + "可以先显式转换到 sRGB 再调色，但本机做不了："
                f"{cap['reason']}。装好后重试：{cap.get('how_to_enable', '')}")
    stem = Path(source_path).stem
    return (base + "硬停不变——这一步不做静默转换。"
            "如果你接受把超出 sRGB 的饱和色压回边界（有损、不可逆、原图不动），"
            "可以自己先转一次再调色：\n"
            f"  python3 scripts/color_profile_convert.py --input {source_path} "
            f"--output {stem}_srgb.png\n"
            "它会把损失量（剪切像素比例的前后变化）一并写在结果里，然后拿输出文件重新 plan。")


def classify_color(media_type: str, color: dict, profile_name: str | None = None) -> dict:
    primaries = color["primaries"]
    transfer = color["transfer"]
    matrix = color["matrix"]
    normalized_profile = (profile_name or "").lower()
    unknown = {"unknown", "unspecified", None}

    if media_type == "photo":
        if "display p3" in normalized_profile or (
            primaries == "smpte432" and transfer == "iec61966-2-1" and matrix in {"gbr", *unknown}
        ):
            return {"profile": "display-p3", "label": "Display P3", "support": "direct", "reason": "保持 Display P3 色域并输出带标签的高位深 PNG"}
        if "adobe rgb" in normalized_profile or primaries in {"jedec-p22", "ebu3213"}:
            return {"profile": "adobe-rgb", "label": "Adobe RGB / JEDEC P22", "support": "unsupported", "reason": "渲染链路不对色域做静默转换；Adobe RGB 当 sRGB 直接处理会掉饱和、偏色"}
        if "prophoto" in normalized_profile:
            return {"profile": "prophoto-rgb", "label": "ProPhoto RGB", "support": "unsupported", "reason": "渲染链路不对色域做静默转换；ProPhoto 的色域远大于 sRGB，直接处理必然偏色"}
        if primaries == "bt2020":
            return {"profile": "bt2020-photo", "label": "BT.2020 照片", "support": "unsupported", "reason": "正式配方尚未完成 BT.2020 照片标定"}
        if primaries == "smpte431":
            return {"profile": "dci-p3-photo", "label": "DCI-P3 照片", "support": "unsupported", "reason": "DCI-P3 不是 Display P3，当前流程不做静默转换"}
        if "srgb" in normalized_profile or (
            primaries == "bt709" and transfer == "iec61966-2-1" and matrix in {"gbr", *unknown}
        ):
            return {"profile": "srgb", "label": "sRGB", "support": "direct", "reason": "按 sRGB 处理并输出带标签 PNG"}
        if primaries in unknown and transfer in unknown:
            return {"profile": "untagged", "label": "无色彩标签", "support": "confirmation-required", "reason": "将按常见 sRGB 图片解释；确认方案即确认此假设"}
        return {"profile": "unsupported-photo", "label": f"未支持照片色域（{primaries}/{transfer}）", "support": "unsupported", "reason": "无法可靠确认输入色域，禁止静默处理"}

    if color["hdr"]:
        return {"profile": "hdr-video", "label": "HDR / PQ / HLG / BT.2020 视频", "support": "unsupported", "reason": "当前自动渲染器不做 HDR 色调映射"}
    if primaries == "smpte432":
        return {"profile": "display-p3-video", "label": "Display P3 视频", "support": "unsupported", "reason": "视频端尚未完成 P3 到交付色域的时序与色差标定"}
    if primaries == "smpte431":
        return {"profile": "dci-p3-video", "label": "DCI-P3 视频", "support": "unsupported", "reason": "当前视频端仅执行 Rec.709 SDR"}
    if primaries == "bt2020" or matrix in {"bt2020nc", "bt2020c"}:
        return {"profile": "bt2020-video", "label": "BT.2020 视频", "support": "unsupported", "reason": "当前视频端仅执行 Rec.709 SDR"}
    if primaries in {"smpte170m", "bt470bg"} or matrix in {"smpte170m", "bt470bg", "fcc"}:
        return {"profile": "rec601-video", "label": "Rec.601 / BT.470 标清视频", "support": "unsupported", "reason": "当前流程没有验证标清色度矩阵到 Rec.709 的转换"}
    if primaries == "bt709" and transfer == "bt709" and matrix == "bt709":
        return {"profile": "rec709-sdr", "label": "Rec.709 SDR", "support": "direct", "reason": "按 Rec.709 SDR 处理并保持完整标签"}
    if primaries in unknown and transfer == "bt709-matrix-inferred" and matrix == "bt709":
        return {"profile": "rec709-sdr-inferred", "label": "Rec.709 SDR（由 BT.709 矩阵推断）", "support": "direct", "reason": "输入至少带 BT.709 矩阵；输出会补齐 Rec.709 三项标签"}
    if primaries in unknown and transfer in {*unknown, "bt709-matrix-inferred"}:
        return {"profile": "untagged-video", "label": "无完整色彩标签的 SDR 视频", "support": "confirmation-required", "reason": "需明确确认后按 Rec.709 SDR 解释"}
    return {"profile": "unsupported-video", "label": f"未支持视频色域（{primaries}/{transfer}/{matrix}）", "support": "unsupported", "reason": "当前视频端仅执行 Rec.709 SDR"}


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def srgb_linear(value: int) -> float:
    channel = value / 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def analyze_luminance(path: Path, media_type: str, width: int, height: int, duration: float, color_profile: str = "srgb") -> dict:
    ffmpeg = require_tool("ffmpeg")
    if not width or not height:
        # ffprobe 读不出尺寸，多半是文件根本不是图片或视频（改了扩展名的文本、
        # 半个下载、损坏的编码）。此前这里直接除零，用户拿到
        # 「命令执行失败：ZeroDivisionError: division by zero」，无从下手。
        raise SkillError(
            f"读不出画面尺寸，这个文件可能不是有效的图片或视频：{path.name}"
            "（宽或高为 0）。请确认文件完整、扩展名与真实格式一致；"
            "用 `ffprobe <文件>` 可以看它到底是什么。", 3)
    sample_width = min(160, width)
    sample_height = max(2, round(height * sample_width / width))
    if sample_height % 2:
        sample_height += 1
    frame_count = 1 if media_type == "photo" else 12
    filters = []
    if media_type == "video" and duration > 0:
        filters.append(f"fps={min(6.0, max(0.1, frame_count / duration))}")
    # 顺序不能反：先 scale 是在 yuv420 域缩放，极值会被色度插值收窄。
    filters.extend(["format=rgb24", f"scale={sample_width}:{sample_height}"])
    command = [
        ffmpeg, "-v", "error", "-i", str(path), "-vf", ",".join(filters),
        "-frames:v", str(frame_count), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise SkillError("无法计算素材亮度诊断", 3)
    frame_size = sample_width * sample_height * 3
    sampled_frames = len(result.stdout) // frame_size
    luminance = []
    encoded_luminance = []
    usable = result.stdout[:sampled_frames * frame_size]
    coefficients = (0.2289746, 0.6917385, 0.0792869) if color_profile == "display-p3" else (0.2126, 0.7152, 0.0722)
    for index in range(0, len(usable), 3):
        encoded_luminance.append(
            coefficients[0] * usable[index] / 255.0
            + coefficients[1] * usable[index + 1] / 255.0
            + coefficients[2] * usable[index + 2] / 255.0)
        red = srgb_linear(usable[index])
        green = srgb_linear(usable[index + 1])
        blue = srgb_linear(usable[index + 2])
        luminance.append(coefficients[0] * red + coefficients[1] * green + coefficients[2] * blue)
    luminance.sort()
    encoded_luminance.sort()
    highlights = sum(value >= 0.99 for value in luminance) / len(luminance) if luminance else 0.0
    shadows = sum(value <= 0.01 for value in luminance) / len(luminance) if luminance else 0.0
    alerts = []
    if highlights > 0.005:
        alerts.append("全局亮部接近剪切；曲线不能恢复已经丢失的细节")
    if shadows > 0.02:
        alerts.append("全局暗部占比较高；提亮前需检查噪点和主体位置")
    return {
        "scope": "全局亮度统计；不识别人脸、天空、商品或其他语义区域",
        "luminance_model": "Display P3 D65" if color_profile == "display-p3" else "sRGB / Rec.709",
        "sampled_frames": sampled_frames,
        "p1": round(percentile(luminance, 0.01), 6),
        "p75": round(percentile(luminance, 0.75), 6),
        "p95": round(percentile(luminance, 0.95), 6),
        "p50": round(percentile(luminance, 0.50), 6),
        "p99": round(percentile(luminance, 0.99), 6),
        "mask_p75": round(percentile(encoded_luminance, 0.75), 6),
        "mask_p95": round(percentile(encoded_luminance, 0.95), 6),
        "highlight_clip_ratio": round(highlights, 6),
        "shadow_clip_ratio": round(shadows, 6),
        "alerts": alerts,
    }


def highlight_protection_for(source: dict, recipe: dict) -> dict:
    """从素材分位和风格声明编译高光保护策略。"""
    bible = recipe.get("style_bible") or {}
    forbidden = bible.get("forbidden") or []
    colored = any(str(item).strip() == "高光去饱和" for item in forbidden)
    if colored:
        return {
            "mode": "colored-highlight",
            "reason": "Style Bible 明确禁止高光去饱和；改用色相与白位双门",
            "max_hue_shift_deg": 8.0,
            "require_unclipped_white": True,
        }
    tone = source.get("luminance_diagnosis") or {}
    start = float(tone.get("mask_p75", tone.get("p75", 0.6)))
    full = float(tone.get("mask_p95", tone.get("p95", 0.85)))
    start = max(0.0, min(0.94, start))
    full = max(start + 0.02, min(0.99, full))
    return {
        "mode": "path-to-white",
        "mask_start": round(start, 6),
        "mask_full": round(full, 6),
        "source_quantiles": "encoded-luma-p75-to-p95",
    }


def highlight_mask_coverage(path: Path, source: dict, policy: dict) -> float:
    """用与滤镜蒙版相同的编码域亮度估算非零覆盖率。"""
    width = min(160, int(source["width"]))
    height = max(2, round(int(source["height"]) * width / int(source["width"])))
    if height % 2:
        height += 1
    result = subprocess.run([
        require_tool("ffmpeg"), "-v", "error", "-i", str(path),
        "-vf", f"format=rgb24,scale={width}:{height}", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], capture_output=True)
    if result.returncode or not result.stdout:
        raise SkillError("无法估算高光保护蒙版覆盖率", 3)
    threshold = float(policy["mask_start"])
    count = 0
    total = len(result.stdout) // 3
    for index in range(0, total * 3, 3):
        value = (LUMA_WR * result.stdout[index]
                 + LUMA_WG * result.stdout[index + 1]
                 + LUMA_WB * result.stdout[index + 2]) / 255.0
        count += value > threshold
    return round(count / total, 6)


def inspect_media(raw_path: str | Path) -> dict:
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise SkillError(f"输入文件不存在：{path}", 3)
    suffix = path.suffix.lower()
    if suffix not in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
        raise SkillError("首版只支持 JPEG、PNG、MP4、MOV", 3)
    data = probe(path)
    video_streams = [item for item in data.get("streams", []) if item.get("codec_type") == "video"]
    if len(video_streams) != 1:
        raise SkillError("素材必须且只能包含一个视频/图像轨", 3)
    stream = video_streams[0]
    media_type = "photo" if suffix in PHOTO_EXTENSIONS else "video"
    transfer = stream.get("color_transfer", "unknown")
    primaries = stream.get("color_primaries", "unknown")
    matrix = stream.get("color_space", "unknown")
    if transfer in {"unknown", "unspecified", None} and matrix == "bt709":
        transfer = "bt709-matrix-inferred"
    profile_name = embedded_photo_profile(path) if media_type == "photo" else None
    color = {
        "primaries": primaries,
        "transfer": transfer,
        "matrix": matrix,
        "range": stream.get("color_range", "unknown"),
        "hdr": transfer in HDR_TRANSFERS,
        "embedded_profile": profile_name,
    }
    color.update(classify_color(media_type, color, profile_name))
    result = {
        "path": str(path),
        "media_type": media_type,
        "extension": suffix,
        "sha256": sha256(path),
        "size": path.stat().st_size,
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration": float(stream.get("duration") or data.get("format", {}).get("duration") or 0),
        "has_audio": any(item.get("codec_type") == "audio" for item in data.get("streams", [])),
        "color": color,
    }
    result["luminance_diagnosis"] = analyze_luminance(
        path, media_type, int(stream.get("width")), int(stream.get("height")), result["duration"], color["profile"]
    )
    import diagnose as diagnose_module
    result["foundation_diagnosis"] = diagnose_module.diagnose(
        path,
        "display-p3" if color["profile"] == "display-p3" else "srgb",
        use_semantic=False,
    )
    return result


def find_recipe(catalog: dict, recipe_id: str, media_type: str,
                *, allow_manual: bool = False) -> dict:
    for item in catalog["recipes"]:
        if item["id"] == recipe_id:
            try:
                available = executable_media(item)
            except ValueError as error:
                raise SkillError(f"配方 {recipe_id} 媒体准入无效：{error}", 3) from error
            if media_type not in available:
                kind = "照片" if media_type == "photo" else "视频"
                declared = "、".join(item["media_types"])
                raise SkillError(
                    f"配方 {recipe_id} 不支持{kind}——它声明的媒体类型是 {declared}。"
                    "用 `list-styles` 看每套风格支持什么。", 3,
                    recovery_step=(
                        "运行 `python3 scripts/blcaptain_color.py list-styles --media "
                        f"{media_type}`，选择支持当前媒体的公式。"
                    ))
            return item
    raise SkillError(
        f"未知配方：{recipe_id}。请使用 `list-styles` 输出中的配方 id"
        "（例如 natural-clean）；当前版本不按中文名或别名匹配。", 3)


def recipe_for_media(recipe: dict, media_type: str) -> dict:
    """生成媒体专属执行副本，避免照片修正改写已验收的视频公式。"""
    effective = copy.deepcopy(recipe)
    override = (recipe.get("media_overrides") or {}).get(media_type) or {}
    unknown = set(override) - {
        "parameters", "tone_curve", "hsl_bands", "visual_targets", "capability_bindings",
        "execution_role",
    }
    if unknown:
        raise SkillError(
            f"配方 {recipe.get('id')} 的 {media_type} 覆盖字段无效：{sorted(unknown)}", 3)
    if "parameters" in override:
        effective["parameters"].update(copy.deepcopy(override["parameters"]))
    for key in ("tone_curve", "hsl_bands", "visual_targets", "capability_bindings",
                "execution_role"):
        if key in override:
            effective[key] = copy.deepcopy(override[key])
    return effective


def style_payload(recipe: dict, media_type: str | None = None) -> dict:
    import style_bible_gates

    keys = ["id", "name", "aliases", "summary", "warnings"]
    payload = {key: recipe[key] for key in keys}
    for key in ("collection", "design_fingerprint", "art_direction", "impact", "suitability",
                "visual_targets", "style_bible", "execution_role", "direction_scope"):
        if key in recipe:
            payload[key] = recipe[key]
    payload["gate_profile"] = style_bible_gates.compile_profile(recipe)
    if media_type is not None:
        payload["media_type"] = media_type
    return payload


def scaled_parameters(parameters: dict, strength: float) -> dict:
    neutral = {
        "brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0,
        "temperature": 0.0, "tint": 0.0, "sharpness": 0.0,
        "vignette": 0.0, "grain": 0.0, "teal_orange": 0.0,
        "shadow_teal": 0.0,
    }
    result = {}
    for key, base in neutral.items():
        target = float(parameters.get(key, base))
        result[key] = round(base + (target - base) * strength, 6)
    return result


def perceptual_strength(recipe: dict, strength: float) -> float:
    """把界面百分比映射到经风格家族标定的感知强度。"""
    return round(strength ** float(recipe["impact"]["strength_gamma"]), 6)


def strength_execution_for(recipe: dict, strength: float) -> tuple[float, float, dict]:
    """返回计划与执行安全门共同使用的强度语义。"""
    effective_strength = perceptual_strength(recipe, strength)
    render_mix = effective_strength
    explanation = "一级校正固定执行；创意目标经一次感知强度混合，不再逐参数缩放后重复混合"
    if recipe.get("execution_role") == "foundation-only":
        effective_strength = 0.0
        render_mix = 0.0
        explanation = "照片端为 Foundation-only：修复量由素材诊断决定，Creative 强度不适用"
    return effective_strength, render_mix, {
        "user_strength": strength,
        "effective_strength": effective_strength,
        "mapping_count": 1,
        "final_mix": render_mix,
        "explanation": explanation,
    }


def normalize_strength(value: object) -> dict:
    """同时接受界面百分比与 0～1 比例，并保留用户原始语义。"""
    raw = str(value).strip()
    try:
        number = float(raw)
    except (TypeError, ValueError) as error:
        raise SkillError(
            f"强度无法识别：{raw!r}。原文件未改动。下一步：使用 --strength 55 或 --strength 0.55。",
            3,
        ) from error
    if 0.1 <= number <= 1.0:
        normalized, mode = number, "ratio"
    elif 10 <= number <= 100:
        normalized, mode = number / 100.0, "percent"
    else:
        raise SkillError(
            f"强度 {raw} 不在支持范围。原文件未改动。"
            "下一步：百分比使用 --strength 10 到 100，或使用 --strength 0.1 到 1.0。",
            3,
        )
    return {
        "raw": raw,
        "input_mode": mode,
        "normalized": round(normalized, 6),
        "display_percent": round(normalized * 100, 4),
    }


def encode_srgb(linear: float) -> float:
    linear = max(0.0, min(1.0, linear))
    return 12.92 * linear if linear <= 0.0031308 else 1.055 * (linear ** (1 / 2.4)) - 0.055


def input_truth_for(source: dict, assume_sdr: bool = False) -> dict:
    """只根据媒体标签与显式确认解释输入，不根据画面“看起来灰”猜 Log。"""
    color = source["color"]
    profile = color["profile"]
    transfer = color.get("transfer")
    if color.get("hdr"):
        state, encoding, confidence = "hdr", color["label"], "metadata"
    elif transfer in LOG_TRANSFERS:
        state, encoding, confidence = "log/raw", transfer, "metadata"
    elif profile in {"srgb", "display-p3", "rec709-sdr", "rec709-sdr-inferred"}:
        state, encoding, confidence = "display-referred", color["label"], "metadata"
    elif profile in {"untagged", "untagged-video"} and assume_sdr:
        state = "display-referred"
        encoding = "sRGB（显式假设）" if source["media_type"] == "photo" else "Rec.709 SDR（显式假设）"
        confidence = "user-confirmed-assumption"
    else:
        state, encoding, confidence = "unknown", color["label"], "insufficient-metadata"
    return {
        "state": state,
        "encoding": encoding,
        "confidence": confidence,
        "requires_confirmation": bool(
            color["support"] == "confirmation-required" and not assume_sdr),
        "inferred_from_appearance": False,
        "evidence": {
            "profile": profile,
            "primaries": color.get("primaries"),
            "transfer": transfer,
            "matrix": color.get("matrix"),
        },
        "boundary": "低彩、低反差或发灰只是画面症状，不作为 Log／RAW 判据",
    }


def foundation_hash(grade: dict) -> str:
    encoded = json.dumps(grade, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _foundation_five_zone_curve(tone: dict) -> dict:
    """把显示参照灰片整理成健康底片；点位在编码域，诊断分位在相对线性亮度域。"""
    p01 = max(0.0, min(1.0, float(tone["p01"])))
    p05 = max(p01 + 1e-6, min(1.0, float(tone["p05"])))
    p50 = max(p05 + 1e-6, min(1.0, float(tone["p50"])))
    p95 = max(p50 + 1e-6, min(1.0, float(tone["p95"])))
    p99 = max(p95 + 1e-6, min(1.0, float(tone["p99"])))
    low_contrast = float(tone.get("tone_span", p95 - p05)) < 0.42
    meaningful_structure = p95 - p05 >= 0.04
    raised_black = p01 > 0.04 and p50 < 0.65 and p95 < 0.90
    if not meaningful_structure:
        return {
            "points": [[0.0, 0.0], [1.0, 1.0]],
            "mode": "identity-degenerate-histogram",
        }
    # 中位亮度低并不等于欠曝：只要黑位与影调跨度健康，就保留低键意图。
    # Foundation 只修复可诊断的问题，不能先替 Creative Look 完成提亮。
    if not low_contrast and not raised_black:
        return {"points": [[0.0, 0.0], [1.0, 1.0]], "mode": "identity-healthy-input"}

    # 显示参照照片的“黑位偏高”不等于 Log 底片。旧版把任何 raised_black
    # 直接钉到 0.02/0.05，实图会从空气雾感跳成旅游明信片，并因逐通道曲线
    # 把综合色彩成倍放大。这里只收回一部分灰底，让黑位重新出现但不消灭
    # 大气透视；真正 Log/RAW 必须由输入标签走另一条解释链。
    target01 = max(0.06, p01 - min(0.04, (p01 - 0.04) * 0.40)) if raised_black else p01
    target05 = max(0.11, p05 - min(0.035, (p05 - 0.05) * 0.20)) if raised_black else p05
    target50 = max(p50, min(0.24, p50 * 1.20)) if low_contrast and p50 < 0.24 else p50
    # 校正量必须覆盖自身 Readiness 的实际跨度目标，不能只取离0.42的
    # 距离再乘0.2；接近阈值的素材此前只增加约0.004，却要求增加0.04。
    highlight_gain = max(0.03, (p95 - p05) * 0.10) if low_contrast else 0.0
    if low_contrast:
        # 滤镜中间态存在8位量化；给目标亮度留两个编码级的执行余量，
        # 不修改Readiness阈值，避免理论恰达标而实际差一个量化级。
        encoded_target = min(1.0, encode_srgb(p95 + highlight_gain) + 2.0 / 255.0)
        highlight_gain = max(highlight_gain, srgb_linear(round(encoded_target * 255)) - p95)
    target95 = max(p95, min(0.68, p95 + highlight_gain)) if low_contrast else p95
    target99 = max(p99, min(0.82, p99 + highlight_gain * 1.5)) if low_contrast else p99
    raw = [
        [0.0, 0.0],
        [encode_srgb(p01), encode_srgb(target01)],
        [encode_srgb(p05), encode_srgb(target05)],
        [encode_srgb(p50), encode_srgb(target50)],
        [encode_srgb(p95), encode_srgb(target95)],
        # p95/p99 在灰片里经常只差几个编码百分点，直接同时作控制点会形成
        # 不稳定的窄肩。以固定 0.90 编码锚接住 p99 意图，保留安全斜率。
        [0.90, max(0.90, encode_srgb(target99))],
        [1.0, 1.0],
    ]
    points = []
    for x, y in raw:
        x, y = round(x, 6), round(y, 6)
        if points and (x <= points[-1][0] or x - points[-1][0] < 0.05):
            continue
        if points:
            previous_x, previous_y = points[-1]
            distance = x - previous_x
            y = max(previous_y + 0.15 * distance, y)
            y = min(previous_y + 4.0 * distance, y)
        if x < 1.0:
            remaining = 1.0 - x
            y = max(1.0 - 4.0 * remaining, y)
            y = min(1.0 - 0.15 * remaining, y)
        points.append([x, round(min(1.0, y), 6)])
    if points[-1][0] < 1.0:
        if 1.0 - points[-1][0] < 0.05:
            points.pop()
        points.append([1.0, 1.0])
    curve = {"points": points, "mode": "five-zone-display-referred"}
    validate_curve(curve, "Foundation 五区影调")
    return curve


def _foundation_neutral_balance(color: dict) -> dict:
    wb = color.get("white_balance") or {}
    ratio = float(wb.get("neutral_sample_ratio") or 0.0)
    magnitude = float(wb.get("cast_magnitude") or 0.0)
    # 高彩且大面积带色的夜景、舞台光或日落里，“近中性候选”常由灯光反射
    # 与高光构成；把它们当白卡会中和叙事主光。此时宁可保留现场色，交给
    # Creative Look 的记忆色与空间层处理，也不能让 Foundation 先洗掉暖锚点。
    colorfulness = float(color.get("colorfulness") or 0.0)
    chromatic_ratio = float(color.get("chromatic_pixel_ratio") or 0.0)
    narrative_color = (
        (colorfulness >= 0.16 and chromatic_ratio >= 0.40)
        # 蓝调时刻等低明度场景的综合色彩数值可能略低，但大面积像素已有
        # 一致色相；此时“近中性候选”多是叙事环境色，不能当白卡中和。
        or (colorfulness >= 0.12 and chromatic_ratio >= 0.65)
    )
    reliable = ratio >= 0.02 and magnitude >= 0.006 and not narrative_color
    cast_a = float(wb.get("cast_a") or 0.0)
    cast_b = float(wb.get("cast_b") or 0.0)
    return {
        "mode": (
            "neutral-candidate-limited" if reliable
            else "preserve-narrative-color" if narrative_color
            else "preserve-unreliable-neutral"
        ),
        "temperature": round(max(-0.06, min(0.06, -cast_b * 3.5)), 6) if reliable else 0.0,
        "tint": round(max(-0.025, min(0.025, -cast_a * 2.5)), 6) if reliable else 0.0,
        "source_cast_magnitude": round(magnitude, 6),
        "neutral_sample_ratio": round(ratio, 6),
        "basis": wb.get("basis", "未提供近中性像素证据"),
        "boundary": (
            "只对可靠近中性候选做有限适应，温度绝对值不超过 0.06、色调绝对值不超过 0.025；"
            "混合光与叙事主光不在本批自动中和"
        ),
    }


def _foundation_color_recovery(color: dict) -> dict:
    colorfulness = float(color.get("colorfulness") or 0.0)
    chromatic_ratio = float(color.get("chromatic_pixel_ratio") or 0.0)
    needs_recovery = colorfulness < 0.08 and chromatic_ratio < 0.20
    target = max(colorfulness + 0.01, colorfulness * 1.20)
    saturation = min(1.28, max(1.0, target / max(colorfulness, 0.02))) if needs_recovery else 1.0
    return {
        "mode": "midtone-weighted" if needs_recovery else "preserve",
        "saturation": round(saturation, 6),
        "source_colorfulness": round(colorfulness, 6),
        "source_chromatic_pixel_ratio": round(chromatic_ratio, 6),
        "target_min_colorfulness": round(target, 6) if needs_recovery else round(colorfulness * 0.90, 6),
        "highlight_protection": "中间调钟形蒙版；极暗与极亮不加彩",
    }


def foundation_grade(source: dict) -> dict:
    """仅响应素材诊断的 Foundation；不读取配方，也不读取创意强度。"""
    complete = source.get("foundation_diagnosis")
    if isinstance(complete, dict) and isinstance(complete.get("analysis"), dict):
        analysis = complete["analysis"]
        tone = analysis["tone"]
        color = analysis["color"]
        curve = _foundation_five_zone_curve(tone)
        neutral = _foundation_neutral_balance(color)
        recovery = _foundation_color_recovery(color)
        # 没有测到过量增彩就不预先全局褪色。旧固定0.86/0.92会把
        # 白平衡已消除的偏色再削一次，使真实素材跌破90%的保彩下限。
        # 实际过量增彩仍由原Readiness上限拒绝，不能为补偿而放宽门。
        tone_chroma_compensation = 1.0
        active_axes = []
        if curve["mode"] == "five-zone-display-referred":
            active_axes.append("tone_span")
        if recovery["mode"] == "midtone-weighted":
            active_axes.append("colorfulness")
        if neutral["mode"] == "neutral-candidate-limited":
            active_axes.append("neutral_cast")
        return {
            "contract": "foundation-v2",
            "mode": "source-adaptive",
            "target_mode": "five-zone-relative",
            "mix": 1.0,
            "diagnosis_scope": "whole-timeline-multi-window",
            "sample_count": int(complete.get("sample_count") or 0),
            "sample_coverage": complete.get("sample_coverage"),
            "temporal": complete.get("temporal"),
            "source_median": float(tone["p50"]),
            "target_median": next(
                (round(srgb_linear(round(y * 255)), 6)
                 for x, y in curve["points"] if abs(x - encode_srgb(float(tone["p50"]))) < 1e-5),
                float(tone["p50"]),
            ),
            "applied_midpoint_ev": 0.0,
            "exposure_ev_intent": 0.0,
            "gamma": 1.0,
            "tone_curve": curve,
            "neutral_balance": neutral,
            "color_recovery": recovery,
            "tone_chroma_compensation": tone_chroma_compensation,
            "readiness_targets": {
                "required": bool(active_axes),
                "active_axes": active_axes,
                "tone_span": "提高 max(0.03, 10%)",
                "colorfulness": "低彩素材提高 max(0.01, 20%)",
                "local_contrast": "不得灾难性下降；容许编码往返造成绝对 0.002 或相对 10% 的较大者",
                "neutral_cast": "不得增加超过 0.002",
                "calibration_status": "engineering-seed-pending-holdout",
            },
            "scope": "整段多窗口真实像素诊断驱动；不读取配方或创意强度，不识别语义区域",
            "limitations": "单镜头 Foundation P0；混合光、记忆色、语义局部和逐镜头匹配尚未实现",
        }

    diagnosis = source["luminance_diagnosis"]
    source_median = max(0.002, min(0.95, float(diagnosis["p50"])))
    if source_median < 0.20:
        requested_ev = math.log2(0.28 / source_median)
    elif source_median > 0.68:
        requested_ev = math.log2(0.62 / source_median)
    else:
        requested_ev = 0.0
    applied_midpoint_ev = round(max(-0.8, min(1.2, requested_ev)), 6)
    target_median = round(max(0.06, min(0.6, source_median * (2 ** applied_midpoint_ev))), 6)
    source_encoded = encode_srgb(source_median)
    target_encoded = encode_srgb(target_median)
    gamma = math.log(max(source_encoded, 0.01)) / math.log(max(target_encoded, 0.01))
    full_gamma = max(0.78, min(1.35, gamma))
    # target_median 已按 effective_strength 计算；这里直接求达到该目标的 gamma，避免再次缩放强度。
    gamma = round(full_gamma, 6)
    exposure_ev = round(max(-0.8, min(1.2, math.log2(target_median / source_median))), 4)
    p1 = float(diagnosis["p1"])
    p99 = float(diagnosis["p99"])
    shadow_need = max(0.0, min(1.0, (0.02 - p1) / 0.017))
    highlight_need = max(0.0, min(1.0, (p99 - 0.88) / 0.08))
    shadow_cap = 0.045
    highlight_cap = 0.055
    shadow_y = 0.18 + shadow_cap * shadow_need
    highlight_y = 0.82 - highlight_cap * highlight_need
    white_y = 1.0 - 0.025 * highlight_need
    full_points = [[0, 0], [0.18, shadow_y], [0.5, 0.5], [0.82, highlight_y], [1, white_y]]
    curve = {"points": [[x, round(y, 6)] for x, y in full_points]}
    validate_curve(curve, "素材自适应基础校正")
    return {
        "contract": "foundation-v1",
        "mode": "source-adaptive",
        "target_mode": "relative-ev",
        "mix": 1.0,
        "source_median": round(source_median, 6),
        "target_median": target_median,
        "applied_midpoint_ev": applied_midpoint_ev,
        "exposure_ev_intent": exposure_ev,
        "gamma": gamma,
        "tone_curve": curve,
        "scope": "只基于素材全局亮度分位数；不读取配方或创意强度，不识别语义区域",
        "limitations": "本阶段只完成影调 Foundation 解耦；白平衡、综合色彩、局部层次与记忆色恢复尚未实现",
    }


def adaptive_primary_grade(source: dict, recipe: dict | None = None) -> dict:
    """兼容旧调用名；recipe 仅为调用兼容，禁止影响 Foundation。"""
    _ = recipe
    return foundation_grade(source)


def foundation_readiness(before: dict, after: dict,
                         active_axes: list[str] | None = None) -> dict:
    """Foundation-only 前后同口径门；只判断是否建立健康底片，不判断审美。"""
    before_analysis = before["analysis"]
    after_analysis = after["analysis"]
    before_tone, after_tone = before_analysis["tone"], after_analysis["tone"]
    before_color, after_color = before_analysis["color"], after_analysis["color"]
    before_texture, after_texture = before_analysis["texture"], after_analysis["texture"]

    tone_before = float(before_tone["tone_span"])
    tone_after = float(after_tone["tone_span"])
    color_before = float(before_color["colorfulness"])
    color_after = float(after_color["colorfulness"])
    local_before = float(before_texture["local_contrast_region"])
    local_after = float(after_texture["local_contrast_region"])
    cast_before = float(before_color["white_balance"]["cast_magnitude"])
    cast_after = float(after_color["white_balance"]["cast_magnitude"])

    requested_axes = set(active_axes) if active_axes is not None else {
        "tone_span", "colorfulness", "neutral_cast"}
    if not requested_axes:
        return {
            "status": "not-required",
            "blocking_failures": [],
            "checks": {},
            "boundary": "本次 Foundation 没有激活校正轴，不要求为变化而变化",
            "calibration_status": "pending-independent-holdout",
        }
    low_tone = tone_before < 0.42 and "tone_span" in requested_axes
    low_color = ("colorfulness" in requested_axes
                 and color_before < 0.08
                 and float(before_color.get("chromatic_pixel_ratio") or 0.0) < 0.20)
    tone_min = tone_before + max(0.03, tone_before * 0.10) if low_tone else tone_before * 0.90
    color_min = color_before + max(0.01, color_before * 0.20) if low_color else color_before * 0.90
    color_max = (
        max(color_min + 0.015, color_before * 1.50)
        if low_color else color_before * 1.15 + 0.01
    )
    local_min = local_before - max(0.002, local_before * 0.10)
    cast_max = cast_before + 0.002
    checks = {
        "tone_span": {
            "before": round(tone_before, 6), "after": round(tone_after, 6),
            "minimum": round(tone_min, 6), "applicable": "tone_span" in requested_axes,
            "passed": tone_after >= tone_min if "tone_span" in requested_axes else True,
        },
        "colorfulness": {
            "before": round(color_before, 6), "after": round(color_after, 6),
            "minimum": round(color_min, 6), "maximum": round(color_max, 6),
            "applicable": True,
            "passed": color_min <= color_after <= color_max,
        },
        "local_contrast": {
            "before": round(local_before, 6), "after": round(local_after, 6),
            "minimum": round(local_min, 6), "applicable": True,
            "passed": local_after >= local_min,
        },
        "neutral_cast": {
            "before": round(cast_before, 6), "after": round(cast_after, 6),
            "maximum": round(cast_max, 6), "applicable": True,
            "passed": cast_after <= cast_max,
        },
    }
    failures = [name for name, item in checks.items() if not item["passed"]]
    return {
        "status": "blocked" if failures else "passed",
        "blocking_failures": failures,
        "checks": checks,
        "boundary": "工程种子门，仅证明 Foundation 没有在四个基础维度反向；不等于审美通过",
        "calibration_status": "pending-independent-holdout",
    }


def scaled_curve(curve: dict | None, strength: float) -> dict | None:
    if not curve:
        return None
    return {"points": [[x, round(x + (y - x) * strength, 6)] for x, y in curve["points"]]}


def scaled_hsl(items: list | None, strength: float) -> list:
    return [{"band": item["band"], "hue": round(item["hue"] * strength, 6), "saturation": round(item["saturation"] * strength, 6), "intensity": round(item["intensity"] * strength, 6)} for item in (items or [])]


def load_adjustments(path: str | None) -> dict:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"无法读取微调文件：{error}", 3) from error
    if not isinstance(payload, dict) or not set(payload) <= {"shadow_lift", "highlight_shift", "hsl_bands"}:
        raise SkillError("微调文件字段无效", 3)
    for key in ("shadow_lift", "highlight_shift"):
        if key in payload and not -0.15 <= float(payload[key]) <= 0.15:
            raise SkillError(f"{key} 超出安全范围", 3)
    validate_hsl(payload.get("hsl_bands", []), "用户微调")
    return payload


def apply_adjustments(curve: dict | None, hsl: list, adjustments: dict) -> tuple[dict | None, list]:
    if "shadow_lift" in adjustments or "highlight_shift" in adjustments:
        points = [list(point) for point in (curve or {"points": [[0, 0], [0.25, 0.25], [0.5, 0.5], [0.75, 0.75], [1, 1]]})["points"]]
        shadow = float(adjustments.get("shadow_lift", 0))
        highlight = float(adjustments.get("highlight_shift", 0))
        for point in points:
            x = point[0]
            # shadow_lift 的意图是「提亮暗部」，不是「抬黑场」。
            # 早先的权重 max(0, 1 - x/0.5) 在 x=0 处等于 1，等于给黑场做纯加法 offset——
            # 用户手滑一个 0.04 就把黑场推到眩光量级，而他要的只是暗部看得见。
            # 加一段 min(1, x/0.12) 让权重在 0 处归零、在暗部中段拉满：
            # 黑仍然是黑，暗部照样被抬起来。
            shadow_weight = max(0.0, 1 - x / 0.5) * min(1.0, x / 0.12)
            point[1] = round(max(0, min(1, point[1] + shadow * shadow_weight
                                        + highlight * max(0, (x - 0.5) / 0.5))), 6)
        curve = {"points": points}
        validate_curve(curve, "用户微调")
    merged = {item["band"]: dict(item) for item in hsl}
    for item in adjustments.get("hsl_bands", []):
        current = merged.setdefault(item["band"], {"band": item["band"], "hue": 0, "saturation": 0, "intensity": 0})
        for key in ("hue", "saturation", "intensity"):
            current[key] = round(current[key] + item[key], 6)
    hsl = [merged[band] for band in ("r", "y", "g", "c", "b", "m") if band in merged]
    validate_hsl(hsl, "合并微调")
    return curve, hsl



def _rgb_saturation(scale: float) -> str:
    """等亮度饱和度矩阵（Rec.709 权重）。

    R' = (s + (1−s)·wr)·R + (1−s)·wg·G + (1−s)·wb·B，其余两行同理。
    三行系数各自和为 1，所以对 R=G=B 的像素恒等——中性守恒不靠取整运气。
    """
    keep = 1.0 - scale
    mix = {
        "rr": scale + keep * LUMA_WR, "rg": keep * LUMA_WG, "rb": keep * LUMA_WB,
        "gr": keep * LUMA_WR, "gg": scale + keep * LUMA_WG, "gb": keep * LUMA_WB,
        "br": keep * LUMA_WR, "bg": keep * LUMA_WG, "bb": scale + keep * LUMA_WB,
    }
    return "colorchannelmixer=" + ":".join(f"{k}={v:.6f}" for k, v in mix.items())


def _foundation_color_filters(primary_grade: dict) -> list[str]:
    """Foundation 的中性校正与中间调增彩；两者都在 Creative Look 分支之前。"""
    filters = []
    neutral = primary_grade.get("neutral_balance") or {}
    temperature = float(neutral.get("temperature") or 0.0)
    tint = float(neutral.get("tint") or 0.0)
    recovery = primary_grade.get("color_recovery") or {}
    saturation = float(recovery.get("saturation") or 1.0)
    compensation = float(primary_grade.get("tone_chroma_compensation") or 1.0)
    if compensation < 1.0 - 1e-9:
        # 显示参照 S 曲线逐通道执行时会顺带放大彩度；先做等亮度补偿，
        # 让“解除雾灰”不自动变成“增强风景色”。低彩恢复随后仍可按诊断发生。
        filters.extend([
            "format=gbrp16le",
            _rgb_saturation(compensation),
            "format=gbrp",
        ])
    if saturation > 1.0 + 1e-9:
        to_luma = (
            f"colorchannelmixer=rr={LUMA_WR}:rg={LUMA_WG}:rb={LUMA_WB}"
            f":gr={LUMA_WR}:gg={LUMA_WG}:gb={LUMA_WB}"
            f":br={LUMA_WR}:bg={LUMA_WG}:bb={LUMA_WB}"
        )
        # 钟形蒙版只让中间调获得完整增彩；0.76 以上完全退出，避免白花与反光染色。
        filters.append(
            "format=gbrp16le,split=2[blfbase][blfcolor];"
            f"[blfcolor]{_rgb_saturation(saturation)}[blfcolorized];"
            "[blfbase]split=2[blfkeep][blfmasksrc];"
            f"[blfmasksrc]{to_luma},"
            "curves=master='0/0 0.12/0 0.24/1 0.58/1 0.76/0 1/0':interp=pchip[blfmask];"
            "[blfkeep][blfcolorized][blfmask]maskedmerge,format=gbrp"
        )
    # 增彩会同步放大残余偏色，因此中性校正必须在增彩之后，而不是之前。
    if temperature or tint:
        rr = round(1.0 + 0.6 * temperature, 6)
        gg = round(1.0 - 0.35 * tint, 6)
        bb = round(1.0 - 0.6 * temperature, 6)
        filters.extend([
            "format=gbrp16le",
            f"colorchannelmixer=rr={rr}:gg={gg}:bb={bb}",
            "format=gbrp",
        ])
    if saturation > 1.0 + 1e-9 or temperature or tint:
        to_luma = (
            f"colorchannelmixer=rr={LUMA_WR}:rg={LUMA_WG}:rb={LUMA_WB}"
            f":gr={LUMA_WR}:gg={LUMA_WG}:gb={LUMA_WB}"
            f":br={LUMA_WR}:bg={LUMA_WG}:bb={LUMA_WB}"
        )
        # Foundation 自己承担高光趋白，不依赖后续 Look 是否刚好启用了同类保护。
        filters.append(
            "format=gbrp16le,split=2[blfhighbase][blfhighpull];"
            f"[blfhighpull]{_rgb_saturation(0.72)}[blfhighdesat];"
            "[blfhighbase]split=2[blfhighkeep][blfhighmasksrc];"
            f"[blfhighmasksrc]{to_luma},"
            "curves=master='0/0 0.68/0 0.84/1 1/1':interp=pchip[blfhighmask];"
            "[blfhighkeep][blfhighdesat][blfhighmask]maskedmerge,format=gbrp"
        )
    return filters


def build_filter(
    parameters: dict,
    tone_curve: dict | None = None,
    hsl_bands: list | None = None,
    primary_grade: dict | None = None,
    composition: str = "",
    render_mix: float = 1.0,
    attention: dict | None = None,
    timeline_primary: str = "",
    timeline_trim: str = "",
    highlight_protection: dict | None = None,
) -> str:
    p = parameters
    primary_filters = []
    filters = []
    if timeline_primary:
        # 逐镜头一级校正走时间轴门控，每个镜头参数恒定，硬切两侧互不影响。
        # 它替代而不是叠加全片单一 primary_grade——否则等于校正两次。
        primary_filters.append(timeline_primary)
    elif primary_grade:
        primary_filters.append(f"eq=gamma={primary_grade['gamma']}")
        primary_values = " ".join(f"{x}/{y}" for x, y in primary_grade["tone_curve"]["points"])
        primary_filters.append(f"curves=master='{primary_values}':interp=pchip")
        primary_filters.extend(_foundation_color_filters(primary_grade))
    # 饱和度不能用全局乘法。research/visual-grammar-v4.json 的失败清单第 2 条点名：
    # 全局 saturation 乘法不知道「越亮的东西越该趋向白」，会把高光一起推成霓虹色。
    # 判据是 OKLab 中 L>0.8 段 chroma 对 L 的回归斜率——全局乘法会让它为正。
    #
    # 修法：先照常施加饱和，再把高光段单独拉回一部分，强制 path-to-white。
    # 用亮度蒙版在「全饱和」与「减饱和」两版之间 maskedmerge，
    # 这样只有高光被拉回，中间调与暗部的彩度不受影响。
    saturation = float(p["saturation"])
    # 饱和度不走 eq。eq 不支持 planar RGB，ffmpeg 会自动插入到 yuv444p
    # （csp 与 range 均为 unknown）的往返，实测只要 saturation ≠ 1，
    # 所有中性色就被推成 (v−2, v+1, v−2)：色相 146° 的绿，
    # 中灰 chroma 0.0057、深暗部 0.0137——而且偏移量与 saturation 取值无关
    #（1.0465、1.5、2.0 输出完全相同），显式声明 range 与矩阵也消不掉。
    # 对一套自称「中性零点」的配方来说，这是直接的自相矛盾。
    # 改在 RGB 域用等亮度矩阵做，与高光拉回同一套数学：每行系数和为 1，
    # 因此 R=G=B 的像素构造上恒等。副作用是 saturation=0 的黑白配方
    # 从「ffmpeg 协商出来的 YUV 矩阵」变成显式 Rec.709 权重。
    filters.append(
        "eq="
        f"brightness={p['brightness']}:contrast={p['contrast']}:gamma={p['gamma']}"
    )
    if abs(saturation - 1.0) > 1e-9:
        filters.append("format=gbrp16le")
        filters.append(_rgb_saturation(saturation))
        filters.append("format=gbrp")

    if tone_curve:
        values = " ".join(f"{x}/{y}" for x, y in tone_curve["points"])
        filters.append(f"curves=master='{values}':interp=pchip")
    red = max(-1.0, min(1.0, p["temperature"]))
    tint = max(-1.0, min(1.0, p["tint"]))
    if red or tint:
        # 正温度提高红、降低蓝；正色调向洋红，轻降绿。连续通道增益避免colorbalance保亮模式的跳变。
        rr = round(1.0 + 0.6 * red, 6)
        gg = round(1.0 - 0.35 * tint, 6)
        bb = round(1.0 - 0.6 * red, 6)
        filters.append(f"colorchannelmixer=rr={rr}:gg={gg}:bb={bb}")
    teal = p.get("teal_orange", 0.0)
    if teal:
        # 青橙分离不能用 colorbalance 的 shadow shift：那是加法 offset，
        # 实测它把注入几乎全压在最暗端——纯黑 (0,0,0) 被推成 (0,3,8)，
        # 而中间调 64 处一点没注入。物理上这是反的：
        # 纯黑没有颜色可以注入，注入应当随亮度分布而不是堆在黑场上。
        # 改用分通道曲线：起点锁死 0/0，阴影端抬蓝绿、高光端抬红，
        # 同一强度下暗部注入反而更明显（64 → 60,68,75）。
        shadow = round(0.25 + teal, 6)
        shadow_g = round(0.25 + teal * 0.35, 6)
        highlight = round(0.75 + teal, 6)
        highlight_g = round(0.75 + teal * 0.35, 6)
        filters.append(
            f"curves=b='0/0 0.25/{shadow} 0.6/0.6 1/1'"
            f":g='0/0 0.25/{shadow_g} 0.6/0.6 1/1'"
            f":r='0/0 0.5/0.5 0.75/{highlight} 1/1'"
            f":interp=pchip")
        _ = highlight_g  # 红高光已足够定义暖端，绿高光留中性避免橙脸
    shadow_teal = p.get("shadow_teal", 0.0)
    if shadow_teal:
        # 条件化青橙只允许冷暗部：黑点锁 0，中间调前归中性，
        # 高光完全恒等。暖端必须由现场肤色或实用光提供，渲染器不造橙。
        shadow_b = round(0.25 + shadow_teal, 6)
        shadow_g = round(0.25 + shadow_teal * 0.42, 6)
        filters.append(
            f"curves=b='0/0 0.25/{shadow_b} 0.58/0.58 0.75/0.75 0.9/0.9 1/1'"
            f":g='0/0 0.25/{shadow_g} 0.58/0.58 0.75/0.75 0.9/0.9 1/1'"
            # FFmpeg curves 只提供 natural/pchip；补足恒等锚点，把高光残余色差压回量化误差。
            ":r='0/0 1/1':interp=pchip")
    for item in hsl_bands or []:
        filters.append(f"huesaturation=colors={item['band']}:hue={item['hue']}:saturation={item['saturation']}:intensity={item['intensity']}:strength=1:lightness=1")

    # path-to-white：必须放在**所有**加彩度的操作之后。
    # 第一版放在 eq 之后，结果 HSL 与青橙又把高光彩度加回去了——
    # 实测闪光CCD 与森林青绿仍触发霓虹判据。拉回只有作为最后一道色彩操作才成立。
    # 通道增益（色温／色调）也在加彩度，而且方式最隐蔽：
    # 增益施加在接近白的像素上时，产生的彩度与亮度成正比，
    # 于是 chroma 随 L 单调上升——正是霓虹判据要抓的形状。
    # 实测 forest-cyan 没有任何 HSL 带、饱和度也只有 1.09，
    # 却仍触发霓虹门，根因就在这里。
    chroma_adders = (
        max(0.0, saturation - 1.0)
        + abs(p.get("teal_orange", 0.0)) * 2.0
        + abs(p.get("shadow_teal", 0.0)) * 1.5
        + (abs(p["temperature"]) + abs(p["tint"])) * 2.0
        + sum(max(0.0, float(item["saturation"])) for item in (hsl_bands or []))
    )
    protection = highlight_protection or {
        "mode": "path-to-white", "mask_start": 0.6, "mask_full": 0.85}
    if chroma_adders > 0.02 and protection.get("mode") == "path-to-white":
        # 拉回系数必须相对于「已施加的总增益」来算，不能给绝对值。
        # 目标是高光增益 = 中间调增益 × 0.6，因此
        #     pull = (1 + (effective − 1) × 0.6) / effective
        # 第一版写成 max(0.55, 1 − chroma×0.9)，等于把高光彩度砍掉近一半，
        # 结果感知变化、综合色彩、暗部全线退化，7 个测试同时失败。
        effective = 1.0 + chroma_adders
        pull = round((1.0 + (effective - 1.0) * 0.6) / effective, 6)
        # 拉回不能用 eq=saturation，也不能让蒙版走 format=gray：
        # eq 不支持 planar RGB，ffmpeg 会自动插入到 yuv444p 的转换，
        # 而且 csp 与 range 都是 unknown。实测这条往返把**所有中性色**染绿：
        #     255,255,255 → 253,255,253    128,128,128 → 126,129,126
        # 白点因此掉到 254 以下，通道剪切门被无声地弄失效——
        # 一个后段整体过曝的视频从「被拒」变成「放行」。
        # 改为在 planar RGB 里用矩阵做等亮度降饱和：
        #     R' = (s + (1−s)·wr)·R + (1−s)·wg·G + (1−s)·wb·B   （其余两行同理）
        # 三行系数各自和为 1，因此对 R=G=B 的像素**构造上恒等**，不靠取整运气。
        # 中间格式必须是 16bit：8bit 下矩阵取整仍会在 255 与 200 上各差 1。
        desaturate = _rgb_saturation(pull)
        to_luma = (
            f"colorchannelmixer=rr={LUMA_WR}:rg={LUMA_WG}:rb={LUMA_WB}"
            f":gr={LUMA_WR}:gg={LUMA_WG}:gb={LUMA_WB}"
            f":br={LUMA_WR}:bg={LUMA_WG}:bb={LUMA_WB}"
        )
        mask_start = float(protection.get("mask_start", 0.6))
        mask_full = float(protection.get("mask_full", 0.85))
        filters.append(
            f"format=gbrp16le,split=2[blsatfull][blsatpull];"
            f"[blsatpull]{desaturate}[blsatdone];"
            f"[blsatfull]split=2[blsatbase][blsatmask];"
            f"[blsatmask]{to_luma},"
            f"curves=master='0/0 {mask_start}/0 {mask_full}/1 1/1':interp=pchip[blsatm];"
            f"[blsatbase][blsatdone][blsatm]maskedmerge,format=gbrp"
        )
    if p["sharpness"] > 0:
        filters.append(f"unsharp=5:5:{p['sharpness']}:5:5:0")
    if attention and attention["mode"] == "radial":
        # 不使用偏心vignette：它会在远端生成黑色弧边。以有下限的连续乘法场实现轻量几何引导。
        shade = round(min(0.2, attention["strength"] + p["vignette"] * 0.35), 6)
        distance = (
            f"min(1,hypot(X/W-{attention['center_x']},Y/H-{attention['center_y']})/{attention['radius']})"
        )
        factor = f"(1-{shade}*{distance})"
        filters.append(f"geq=r='r(X,Y)*{factor}':g='g(X,Y)*{factor}':b='b(X,Y)*{factor}'")
    elif p["vignette"] > 0:
        filters.append(f"vignette=angle={p['vignette'] * 2.2}")
    if p["grain"] > 0:
        # 默认 -1 每次执行会换噪声；固定种子且不启用 temporal(t)，
        # 让同一计划跨帧、跨次执行都可复现，兑现 Style Bible 的视频颗粒禁项。
        filters.append(f"noise=alls={p['grain']}:allf=u:all_seed=23063")
    if timeline_trim:
        # 创意 Look 之后的镜头级 trim。放在最后是因为它要抵消的正是 Look 的非线性响应。
        filters.append(timeline_trim)
    grade = ",".join(filters)
    # 强度 100% 也保留同一分支拓扑。此前 99% 走 blend、100% 突然走直链，
    # 含内部 split/maskedmerge 的高光保护链在边界处产生不连续，实测灰片5
    # 从 85% 的 0.0434 跌到 100% 的 0.0107。A*0+B 与直链像素语义等价，
    # 但固定拓扑还能让 30/55/80/100 的执行路径真正可比较。
    if 0.0 <= render_mix <= 1.0:
        prefix_parts = [part for part in (composition, *primary_filters) if part]
        prefix = f"{','.join(prefix_parts)}," if prefix_parts else ""
        return (
            f"{prefix}split=2[blbase][blstyle];"
            f"[blstyle]{grade}[blgraded];"
            f"[blbase][blgraded]blend=all_expr='A*(1-{render_mix})+B*{render_mix}'"
        )
    return ",".join(part for part in (composition, *primary_filters, grade) if part)


def plan_fingerprint(plan: dict) -> str:
    payload = {
        "source": plan["source"],
        "style": plan["style"],
        "strength": plan["strength"],
        "effective_strength": plan["effective_strength"],
        "render_mix": plan["render_mix"],
        "visual_brief": plan["visual_brief"],
        "composition": plan["composition"],
        "attention_map": plan["attention_map"],
        "primary_grade": plan["primary_grade"],
        "input_truth": plan.get("input_truth"),
        "foundation_grade": plan.get("foundation_grade"),
        "foundation_hash": plan.get("foundation_hash"),
        "monotonicity": plan.get("monotonicity"),
        "review_focus": plan["review_focus"],
        "parameters": plan["parameters"],
        "tone_curve": plan.get("tone_curve"),
        "hsl_bands": plan.get("hsl_bands", []),
        "highlight_protection": plan.get("highlight_protection"),
        "adjustments": plan.get("adjustments", {}),
        "input_assumption": plan["input_assumption"],
        "color_pipeline": plan["color_pipeline"],
        "capability_profile": plan["capability_profile"],
        "shot_grade": plan.get("shot_grade"),
        # 局部候选（蒙版路径、蒙版哈希、预演状态、默认强度）必须绑进指纹：
        # 否则把 risky 改成 executable 或换掉 mask_path，plan_id 都不会失效。
        "local_grade": plan.get("local_grade"),
        "skin_strength_decision": plan.get("skin_strength_decision"),
        "assume_sdr": plan.get("assume_sdr", False),
        "output_path": plan["output_path"],
        "comparison_path": plan["comparison_path"],
        "receipt_path": plan["receipt_path"],
        "renderer": RENDERER_VERSION,
    }
    if "korean_cool_protection" in plan:
        payload["korean_cool_protection"] = plan["korean_cool_protection"]
        payload["korean_execution_sha256"] = plan.get("korean_execution_sha256")
        payload["execution_preflight"] = plan.get("execution_preflight")
    if "signature_regions" in plan:
        payload["signature_regions"] = plan["signature_regions"]
        payload["signature_execution_sha256"] = plan.get("signature_execution_sha256")
        payload["execution_preflight"] = plan.get("execution_preflight")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def color_pipeline_for(source: dict) -> dict:
    profile = source["color"]["profile"]
    requires_confirmation = source["color"]["support"] == "confirmation-required"
    if source["media_type"] == "video":
        return {
            "input_profile": source["color"]["label"],
            "working_profile": "Rec.709 SDR",
            "output_profile": "Rec.709 SDR",
            "mode": "preserve-rec709" if profile.startswith("rec709-sdr") else "confirmed-assumption",
            "output_format": "MP4 / H.264",
            "pixel_format": "yuv420p",
            "metadata_method": "H.264 VUI 与容器 Rec.709 标签",
            "requires_profile_confirmation": requires_confirmation,
            "limitations": "不执行 HDR 色调映射、Log 还原或广色域视频转换",
        }
    is_p3 = profile == "display-p3"
    return {
        "input_profile": source["color"]["label"],
        "working_profile": "Display P3" if is_p3 else "sRGB",
        "output_profile": "Display P3" if is_p3 else "sRGB",
        "mode": "preserve-tagged-gamut" if is_p3 else ("confirmed-assumption" if requires_confirmation else "preserve-srgb"),
        "output_format": "PNG",
        "pixel_format": "rgb48be" if is_p3 else "rgb24",
        "metadata_method": "PNG cICP 色彩标签",
        "requires_profile_confirmation": requires_confirmation,
        "limitations": "全局配方在源色域内执行；不等同于跨色域审美匹配，也不包含局部语义蒙版",
    }



def semantic_local_status(media_type: str, local_plan: dict | None = None) -> dict:
    """L2 语义局部的真实状态。

    这里此前硬编码 "unavailable"，而 scripts/local_grade.py 是一个完整实现、
    有独立 CLI 也有测试的模块——它只是从未被 plan/render 调用过。
    声明「不可用」比声明「可用但没接」更糟：它让这块能力永远不会被发现没接上，
    而「自动语义局部」正是 v4 的首要目标。

    四态，逐级都要能被证伪：
      unavailable  —— 后端缺失，或视频（跨帧跟踪属 L3，尚未实现）
      available    —— 后端就绪但本次没探测（默认路径不跑，探测要 1-2 秒）
      detected     —— 已探测并给出候选策略，等用户确认
      executable   —— 用户已确认某个策略，本次渲染会真的走蒙版
    """
    if local_plan and local_plan.get("strategy") == "korean-cool-protection":
        return {"level": "L2", "status": "detected",
                "scope": "当前素材独立人物与近中性亮部保护；需单独确认。视频为逐帧分割，不是身份跟踪。"}
    if local_plan and local_plan.get('strategy') == 'signature-regions':
        return {'level': 'manual-regions', 'status': 'detected',
                'scope': '本次人工区域与显式完整帧区间，须独立确认；不做自动材质理解或身份跟踪。'}
    if media_type != "photo":
        return {"level": "L2", "status": "unavailable",
                "scope": "视频的语义局部需要蒙版跨帧跟踪（L3），尚未实现；本次只做全局与镜头级"}
    if local_plan and local_plan.get("confirmed"):
        chosen = local_plan["confirmed"]
        return {"level": "L2", "status": "executable",
                "scope": f"已确认「{chosen['strategy_name']}」，只在 {chosen['class']} 蒙版内施加，"
                         f"覆盖率 {chosen['coverage']:.1%}；蒙版来自已登记的语义后端"}
    if local_plan and local_plan.get("candidates"):
        names = "、".join(c["strategy_name"] for c in local_plan["candidates"])
        return {"level": "L2", "status": "detected",
                "scope": f"已探测到可用的局部策略（{names}），需用 --confirm-local 单独确认后才执行；"
                         "局部改动不随全局方案一起生效"}
    if local_plan and local_plan.get("reason"):
        return {"level": "L2", "status": "unavailable", "scope": local_plan["reason"]}
    try:
        import semantic_backend
        report = semantic_backend.capability_report()
        if report["active_backend"].get("available"):
            return {"level": "L2", "status": "available",
                    "scope": "后端就绪但本次未探测。加 --detect-local 会识别人物／天空／植被／"
                             "建筑／商品并给出候选局部策略；探测约需 1-2 秒"}
        return {"level": "L2", "status": "unavailable",
                "scope": f"语义后端不可用：{report['active_backend'].get('reason', '未知')}"}
    except Exception:  # noqa: BLE001
        return {"level": "L2", "status": "unavailable",
                "scope": "语义后端探测失败；不自动识别人脸、皮肤、天空、植被、建筑或商品"}


def mask_provenance(info: dict, backend: dict, mask_path: str) -> dict:
    """生成会进入 plan 指纹的蒙版来源，不把“有文件”冒充成可追溯。"""
    path = Path(mask_path)
    confidence = info.get("mean_confidence", info.get("min_confidence"))
    return {
        "backend_id": backend.get("id") or backend.get("backend"),
        "model_id": backend.get("model_id") or backend.get("name"),
        "version_anchor": backend.get("os_build") or backend.get("weights_hash"),
        "weights_hash": backend.get("weights_hash"),
        "device": backend.get("device"),
        "confidence": round(float(confidence), 4) if confidence is not None else None,
        "soft_edge_ratio": info.get("soft_edge_ratio"),
        "mask_sha256": sha256(path) if path.is_file() else None,
        "revision_history": [{"revision": 0, "action": "auto-detected", "operator": "system"}],
        "needs_human_review": bool(info.get("needs_human_review")),
    }


def ensure_mask_integrity(candidate: dict) -> None:
    """确认执行前复核蒙版内容与计划记录的 SHA-256 一致。

    用户在 plan 阶段看过并确认的是**那一份**蒙版；文件在确认与渲染之间被换掉时，
    「文件还在」不等于「还是那份蒙版」。没有记录哈希的候选一律拒绝执行。
    """
    recorded = (candidate.get("mask_provenance") or {}).get("mask_sha256")
    if not recorded:
        raise SkillError(
            f"局部策略 {candidate.get('strategy')} 的计划里没有蒙版 SHA-256 记录，"
            "拒绝执行。请重新 plan --detect-local 生成带来源指纹的候选。", 2)
    actual = sha256(Path(candidate["mask_path"]))
    if actual != recorded:
        raise SkillError(
            f"蒙版文件与计划确认时不一致（SHA-256 变化）：{candidate['mask_path']}。"
            "原文件未改动。下一步：重新 plan --detect-local 并重新确认。", 2)


def detect_local_candidates(source: Path, recipe: dict, mask_dir: Path) -> dict:
    """探测语义类别并给出候选局部策略。

    只报「这张素材里真实存在、且覆盖率够产出蒙版」的类别——
    类别不存在时不给候选，绝不用固定 HSL 宽色带冒充语义区域。
    """
    import local_grade
    import semantic_backend
    backend = semantic_backend.resolve_backend()
    try:
        payload = backend.analyze(source, mask_dir)
    except Exception as error:  # noqa: BLE001
        return {"reason": f"语义探测失败：{error}", "candidates": []}
    if hasattr(payload, "__dict__"):
        payload = payload.__dict__
    classes = payload.get("classes", {})
    backend_description = payload.get("backend") if isinstance(payload.get("backend"), dict) else {}
    candidates = []
    for key, spec in local_grade.STRATEGIES.items():
        needed = spec["requires_class"]
        info = classes.get(needed) or {}
        if not info.get("present"):
            continue
        is_multi_skin = needed == "skin" and bool(info.get("per_instance"))
        if is_multi_skin and not spec.get("per_instance"):
            continue
        if not is_multi_skin and spec.get("per_instance"):
            continue
        mask_paths = []
        if is_multi_skin:
            mask_paths = [item.get("skin_mask_path") for item in info.get("instances", [])
                          if item.get("skin_mask_path")]
            if not mask_paths:
                continue
            mask_path = str(local_grade.union_masks(
                [Path(item) for item in mask_paths],
                mask_dir / f"{source.stem}__skin_instances_union.png"))
        else:
            mask_path = info.get("mask_path")
        if not mask_path:
            continue
        coverage = float(info.get("coverage") or info.get("area_ratio") or 0.0)
        candidates.append({
            "strategy": key,
            "strategy_name": spec["name"],
            "class": needed,
            "coverage": round(coverage, 4),
            "mask_path": mask_path,
            "mask_paths": mask_paths,
            "per_instance": bool(spec.get("per_instance")),
            "instance_count": len(mask_paths) if mask_paths else None,
            "default_strength": spec["default_strength"],
            "strength_meaning": spec["strength_meaning"],
            "description": spec["description"],
            "risk": spec.get("risk", "蒙版边缘在细碎结构处不可靠，成品必须人工复核边缘"),
            "mask_provenance": mask_provenance(info, backend_description, mask_path),
        })
    return {
        "candidates": candidates,
        "backend": backend_description.get("id"),
        "backend_metadata": backend_description,
        "warnings": payload.get("warnings", []),
        "confirmation_note":
            "局部改动不随全局方案一起生效。要执行请在 render 时加 --confirm-local <strategy>，"
            "并单独确认这一处改动——它只作用于蒙版内，蒙版边缘需要你亲自看过。",
        "reason": None if candidates else "本张素材里没有覆盖率足够的语义类别，不产出局部候选",
    }


def apply_local_direction_evidence(direction: dict, strategy: str,
                                   separation: dict, visual_targets: dict) -> dict:
    """用真实局部证据替换同一维度的无语义全局代理。"""
    if (
        strategy == "person-lift"
        and visual_targets.get("subject_separation") == "increase"
        and separation.get("separation_ratio", 0.0) >= 1.3
    ):
        direction["checks"]["subject_separation"].update({
            "passed": True,
            "basis": "real-person-mask-inside-versus-outside",
            "separation_ratio": separation["separation_ratio"],
        })
        direction["failed_dimensions"] = [
            item for item in direction.get("failed_dimensions", [])
            if item != "subject_separation"
        ]
        direction["status"] = (
            "passed" if not direction["failed_dimensions"] else "failed"
        )
    return direction


def preflight_local_candidates(plan: dict) -> dict | None:
    """用最终全局链与真实蒙版预演每个局部候选，阻断无效或反向组合。"""
    local_plan = plan.get("local_grade")
    if not local_plan or not local_plan.get("candidates"):
        return local_plan
    import local_grade
    source = Path(plan["source"]["path"])
    global_chain = build_filter(
        plan["parameters"], plan.get("tone_curve"), plan.get("hsl_bands"),
        plan.get("primary_grade"), composition_filter(plan), plan["render_mix"],
        plan["attention_map"], highlight_protection=plan.get("highlight_protection"),
    )
    baseline = measure(source, False, plan.get("attention_map"))
    for candidate in local_plan["candidates"]:
        with tempfile.TemporaryDirectory(prefix="blcaptain-local-preflight-") as folder:
            output = Path(folder) / "preview.png"
            graph = local_grade.build_local_filter_complex(
                candidate["strategy"], global_chain,
                int(plan["source"]["width"]), int(plan["source"]["height"]),
                float(candidate["default_strength"]), photo_output_filter(plan, ""),
            )
            try:
                run([
                    require_tool("ffmpeg"), "-v", "error", "-y", "-i", str(source),
                    "-i", candidate["mask_path"], "-filter_complex", graph,
                    "-frames:v", "1", "-c:v", "png",
                    "-pix_fmt", plan["color_pipeline"]["pixel_format"], str(output),
                ])
                separation = local_grade.measure_inside_outside(
                    source, output, Path(candidate["mask_path"]),
                    strategy=candidate["strategy"],
                )
                direction = evaluate_direction(
                    baseline, measure(output, False, plan.get("attention_map")),
                    plan["style"]["visual_targets"],
                )
                # 全局方向审计的 subject_separation 只是中央区与外围区的
                # 几何亮度差，不认识人物。已存在真实人物蒙版时，计划预演与
                # 成片复验必须共同使用同一份 L2 区内外证据。
                apply_local_direction_evidence(
                    direction, candidate["strategy"], separation,
                    plan["style"]["visual_targets"],
                )
                failed = direction.get("failed_dimensions") or []
                if separation["separation_ratio"] < 1.3:
                    status = "blocked"
                    reason = (f"局部分离比 {separation['separation_ratio']:.3f} 低于 1.3，"
                              "该策略不会作为可确认候选")
                elif {"tone_span", "colorfulness", "local_contrast"} <= set(failed):
                    status = "blocked"
                    reason = "局部与全局组合后，三个可测方向全部反转"
                elif failed:
                    status = "risky"
                    reason = "局部有效，但部分配方方向未达成：" + "、".join(failed)
                else:
                    status = "executable"
                    reason = "真实蒙版与最终全局链预演通过"
                candidate["execution_preflight"] = {
                    "status": status,
                    "reason": reason,
                    "separation": separation,
                    "direction_failures": failed,
                    "evidence": "same-chain-real-mask-preview",
                }
            except Exception as error:  # noqa: BLE001
                candidate["execution_preflight"] = {
                    "status": "blocked",
                    "reason": f"局部预演失败：{type(error).__name__}: {error}",
                    "evidence": "same-chain-real-mask-preview",
                }
    local_plan["recommended_candidates"] = [
        item for item in local_plan["candidates"]
        if item["execution_preflight"]["status"] == "executable"
    ]
    local_plan["confirmation_note"] = (
        "只确认 recommended_candidates 中标为 executable 的策略；risky／blocked 不得当作推荐。"
        "蒙版边缘仍需你亲自查看，再用 --confirm-local 单独确认。"
    )
    return local_plan


def preflight_skin_strength(source: dict, recipe: dict, mask_path: Path,
                            requested_strength: float) -> dict:
    """在用户确认计划前，用真实肤色蒙版尝试请求档与 30% 安全档。"""
    import memory_color_gate

    requested_percent = round(requested_strength * 100)
    levels = sorted({requested_percent, 30}, reverse=True)
    reports = {}
    source_path = Path(source["path"])
    for level in levels:
        effective = perceptual_strength(recipe, level / 100.0)
        chain = build_filter(
            scaled_parameters(recipe["parameters"], 1.0),
            scaled_curve(recipe.get("tone_curve"), 1.0),
            scaled_hsl(recipe.get("hsl_bands"), 1.0),
            adaptive_primary_grade(source, recipe), render_mix=effective,
            highlight_protection=highlight_protection_for(source, recipe))
        with tempfile.TemporaryDirectory(prefix="blcaptain-skin-preflight-") as folder:
            output = Path(folder) / "candidate.png"
            run([require_tool("ffmpeg"), "-v", "error", "-y", "-i", str(source_path),
                 "-vf", chain, "-frames:v", "1", "-c:v", "png", str(output)])
            protection = (recipe.get("style_bible") or {}).get(
                "memory_color_protection", {})
            reports[level] = memory_color_gate.evaluate_skin(
                _masked_rgb_samples(source_path, mask_path),
                _masked_rgb_samples(output, mask_path),
                float(protection.get("skin_chroma_max_gain", 1.1)))
    decision = memory_color_gate.select_safe_strength(requested_percent, reports)
    return {**decision, "reports": {str(key): value for key, value in reports.items()},
            "evidence": "real-mask-real-render-before-plan-confirmation"}


def capability_profile_for(media_type: str, shot_graded: bool = False,
                           local_plan: dict | None = None) -> dict:
    profile = {
        "global_grade": {"level": "L0", "status": "executable", "scope": "全局影调、综合色彩、宽色带HSL与材质"},
        "geometric_local": {"level": "L1", "status": "partial", "scope": "支持独立确认的裁切、小角度水平校正与单个径向注意力中心；不是语义蒙版"},
        "semantic_local": semantic_local_status(media_type, local_plan),
        "temporal_semantic": {
            "level": "L3", "status": "unavailable",
            "scope": "照片不适用" if media_type == "photo" else "语义蒙版跨帧跟踪尚未实现",
        },
        "shot_level": {
            "level": "S1",
            "status": "executable" if shot_graded else ("available" if media_type == "video" else "not-applicable"),
            "scope": (
                "照片不适用" if media_type == "photo"
                else ("已启用：镜头检测、逐镜头一级校正、锚点匹配；参数镜头内恒定，不跨硬切平滑"
                      if shot_graded else
                      "可用但本次未启用；加 --shot-grade 后按镜头分别校正，否则整段共用一套一级校正")
            ),
        },
    }
    return profile


def load_json_object(path: str | None, label: str) -> dict | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"无法读取{label}：{error}", 3) from error
    if not isinstance(payload, dict):
        raise SkillError(f"{label}必须是 JSON 对象", 3)
    return payload


def visual_brief_for(recipe: dict, raw_path: str | None) -> dict:
    payload = load_json_object(raw_path, "视觉意图")
    required = {"subject", "story", "emotion", "viewing_path", "visual_hierarchy", "semantic_analysis_claimed"}
    if payload is None:
        return {
            "subject": "未提供语义主体",
            "story": recipe["art_direction"]["thesis"],
            "emotion": [value.strip() for value in recipe["art_direction"]["emotion"].split("、") if value.strip()],
            "viewing_path": recipe["art_direction"]["composition"],
            "visual_hierarchy": ["整体影调", "综合色彩", "质感"],
            "semantic_analysis_claimed": False,
            "source": "配方默认视觉意图；未声称自动看懂人物、天空或商品",
        }
    if set(payload) != required:
        raise SkillError(f"视觉意图字段无效：必须且只能包含 {sorted(required)}", 3)
    for key in ("subject", "story", "viewing_path"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise SkillError(f"视觉意图 {key} 无效", 3)
    for key in ("emotion", "visual_hierarchy"):
        if not isinstance(payload[key], list) or not payload[key] or not all(isinstance(value, str) and value.strip() for value in payload[key]):
            raise SkillError(f"视觉意图 {key} 无效", 3)
    if not isinstance(payload["semantic_analysis_claimed"], bool):
        raise SkillError("视觉意图 semantic_analysis_claimed 必须是布尔值", 3)
    payload["source"] = "用户提供或代理在当前对话中检查素材后提供"
    return payload


def attention_for(raw_path: str | None) -> dict:
    payload = load_json_object(raw_path, "注意力方案")
    if payload is None:
        return {"mode": "none", "center_x": 0.5, "center_y": 0.5, "radius": 1.0, "strength": 0.0, "rationale": "默认不做几何局部塑形"}
    required = {"mode", "center_x", "center_y", "radius", "strength", "rationale"}
    if set(payload) != required or payload["mode"] not in {"none", "radial"}:
        raise SkillError("注意力方案字段或模式无效", 3)
    for key in ("center_x", "center_y", "radius", "strength"):
        if isinstance(payload[key], bool) or not isinstance(payload[key], (int, float)):
            raise SkillError(f"注意力方案 {key} 必须是数值", 3)
    if not 0 <= payload["center_x"] <= 1 or not 0 <= payload["center_y"] <= 1:
        raise SkillError("注意力中心必须在画面范围内", 3)
    if not 0.3 <= payload["radius"] <= 1 or not 0 <= payload["strength"] <= 0.2:
        raise SkillError("注意力半径或强度超出安全范围", 3)
    if not isinstance(payload["rationale"], str) or not payload["rationale"].strip():
        raise SkillError("注意力方案必须说明理由", 3)
    if payload["mode"] == "none" and payload["strength"] != 0:
        raise SkillError("none 注意力方案的强度必须为0", 3)
    return payload


def even_floor(value: int) -> int:
    return value - value % 2


def composition_for(source: dict, raw_path: str | None) -> dict:
    payload = load_json_object(raw_path, "构图方案")
    if payload is None:
        return {
            "action": "none", "crop": None, "rotate_deg": 0.0,
            "rationale": "默认保留原构图", "confirmation_required": False,
            "output_width": int(source["width"]), "output_height": int(source["height"]),
        }
    if set(payload) != {"action", "crop", "rotate_deg", "rationale"}:
        raise SkillError("构图方案字段无效", 3)
    if payload["action"] not in {"none", "crop", "crop_rotate"}:
        raise SkillError("构图动作只支持 none、crop、crop_rotate", 3)
    if not isinstance(payload["rationale"], str) or not payload["rationale"].strip():
        raise SkillError("构图理由不能为空", 3)
    rotate = float(payload["rotate_deg"])
    if not -3 <= rotate <= 3:
        raise SkillError("水平校正只支持 -3° 到 3°", 3)
    width, height = int(source["width"]), int(source["height"])
    crop = payload["crop"]
    if payload["action"] == "none":
        if crop is not None or rotate != 0:
            raise SkillError("none 构图不得包含裁切或旋转", 3)
        return {**payload, "confirmation_required": False, "output_width": width, "output_height": height}
    if payload["action"] == "crop" and rotate != 0:
        raise SkillError("crop 构图不得包含旋转；需要旋转时请使用 crop_rotate", 3)
    if payload["action"] == "crop_rotate" and rotate == 0:
        raise SkillError("crop_rotate 构图必须包含非零旋转", 3)
    if not isinstance(crop, dict) or set(crop) != {"x", "y", "width", "height"}:
        raise SkillError("裁切区域字段无效", 3)
    values = {key: float(value) for key, value in crop.items()}
    if not all(0 <= values[key] <= 1 for key in values) or values["width"] < 0.4 or values["height"] < 0.4:
        raise SkillError("裁切区域超出范围或保留画面不足40%", 3)
    if values["x"] + values["width"] > 1 or values["y"] + values["height"] > 1:
        raise SkillError("裁切区域越过画面边界", 3)
    out_width = max(2, round(width * values["width"]))
    out_height = max(2, round(height * values["height"]))
    if source["media_type"] == "video":
        out_width, out_height = even_floor(out_width), even_floor(out_height)
    normalized = {key: round(value, 6) for key, value in values.items()}
    return {
        "action": payload["action"], "crop": normalized, "rotate_deg": round(rotate, 4),
        "rationale": payload["rationale"], "confirmation_required": True,
        "output_width": out_width, "output_height": out_height,
    }


def composition_filter(plan: dict) -> str:
    composition = plan["composition"]
    filters = []
    if composition["rotate_deg"]:
        filters.append(f"rotate={composition['rotate_deg']}*PI/180:ow=iw:oh=ih:c=black")
    if composition["crop"]:
        source = plan["source"]
        crop = composition["crop"]
        x = min(round(int(source["width"]) * crop["x"]), int(source["width"]) - composition["output_width"])
        y = min(round(int(source["height"]) * crop["y"]), int(source["height"]) - composition["output_height"])
        filters.append(f"crop={composition['output_width']}:{composition['output_height']}:{x}:{y}")
    return ",".join(filters)


def apply_shot_boundary_confirmation(shot_grade_plan: dict, confirmed: bool) -> dict:
    pending = list(shot_grade_plan.get("needs_human_review") or [])
    if not pending:
        shot_grade_plan["boundary_review"] = {
            "status": "not-required",
            "confirmed_boundaries": [],
        }
        return shot_grade_plan
    if not confirmed:
        raise SkillError(
            "检测到需要人工复核的镜头边界："
            + "；".join(
                f"t={item['time']}s {item['kind']}(置信{item['confidence']})"
                for item in pending
            )
            + "。请先查看 shots 结果；确认这些候选边界可按当前检测使用后，"
              "加 --confirm-shot-boundaries 重新生成计划。--anchor 只选择匹配锚点，"
              "不代表确认镜头边界。",
            4,
        )
    shot_grade_plan["boundary_review"] = {
        "status": "user-confirmed",
        "confirmed_boundaries": pending,
    }
    shot_grade_plan["needs_human_review"] = []
    return shot_grade_plan


def recipe_execution_snapshot(recipe: dict, adjustments: dict | None = None) -> dict:
    """重建计划必须锁定的媒体配方执行值。"""
    tone_curve = scaled_curve(recipe.get("tone_curve"), 1.0)
    hsl_bands = scaled_hsl(recipe.get("hsl_bands"), 1.0)
    tone_curve, hsl_bands = apply_adjustments(
        tone_curve, hsl_bands, adjustments or {})
    return {
        "parameters": scaled_parameters(recipe["parameters"], 1.0),
        "tone_curve": tone_curve,
        "hsl_bands": hsl_bands,
    }


def make_plan(args: argparse.Namespace) -> dict:
    source = inspect_media(args.input)
    strength_input = normalize_strength(args.strength)
    strength = strength_input["normalized"]
    recipe = find_recipe(
        load_catalog(Path(args.catalog)), args.style, source["media_type"],
        allow_manual=True)
    recipe = recipe_for_media(recipe, source["media_type"])
    highlight_protection = highlight_protection_for(source, recipe)
    color = source["color"]
    if color["support"] == "unsupported":
        raise SkillError(unsupported_color_message(color, args.input), 3)
    if source["media_type"] == "video" and color["support"] == "confirmation-required" and not args.assume_sdr:
        raise SkillError("视频色彩标签缺失；请让用户确认按 Rec.709 SDR 解释后使用 --assume-sdr。", 4)
    input_truth = input_truth_for(source, bool(args.assume_sdr))
    if args.variant and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", args.variant):
        raise SkillError("variant 只能使用小写字母、数字和连字符", 3)
    adjustments = load_adjustments(args.adjustments_file)
    # 创意参数保持完整目标，只通过一次最终混合表达用户强度。
    # 一级校正位于分支之前，不参与混合，避免再出现“强度越大、基础校正越大”。
    effective_strength, render_mix, strength_pipeline = strength_execution_for(recipe, strength)
    parameters = scaled_parameters(recipe["parameters"], 1.0)
    tone_curve = scaled_curve(recipe.get("tone_curve"), 1.0)
    hsl_bands = scaled_hsl(recipe.get("hsl_bands"), 1.0)
    tone_curve, hsl_bands = apply_adjustments(tone_curve, hsl_bands, adjustments)
    visual_brief = visual_brief_for(recipe, args.visual_brief_file)
    composition = composition_for(source, args.composition_file)
    attention_map = attention_for(args.attention_file)
    import signature_regions as signature_module
    signature_evidence = None
    if recipe['id'] in signature_module.ROLES:
        signature_module.check_options({
            'composition': composition, 'attention_map': attention_map,
            'shot_grade': getattr(args, 'shot_grade', False), 'adjustments': adjustments,
            'detect_local': getattr(args, 'detect_local', False)})
        specification = getattr(args, 'signature_regions', None)
        if not specification:
            raise SkillError('当前签名需要本次实际区域审查：请提供 --signature-regions，不能回落全局版本。', 4)
        protection_root = Path(args.output_dir).expanduser().resolve() / 'masks'
        protection_root.mkdir(parents=True, exist_ok=True)
        evidence_dir = Path(tempfile.mkdtemp(prefix='signature-', dir=protection_root)) / 'evidence'
        signature_evidence = signature_module.prepare(
            source, specification, evidence_dir, signature_module.foundation_filter({'source': source}))
        if signature_evidence['specification']['style_id'] != recipe['id']:
            raise SkillError('区域声明的配方与当前请求不一致。', 3)
        recipe['_signature_regions'] = signature_evidence
        recipe['_signature_preview_dir'] = str(evidence_dir.parent / 'previews')
    elif getattr(args, 'signature_regions', None):
        raise SkillError('当前配方不接受六套签名区域协议。', 3)
    korean_protection = None
    if recipe['id'] == 'korean-cool':
        import korean_cool_execution
        import korean_cool_protection
        korean_cool_execution.check_options({
            'composition': composition, 'attention_map': attention_map,
            'shot_grade': getattr(args, 'shot_grade', False), 'adjustments': adjustments})
        protection_root = Path(args.output_dir).expanduser().resolve() / 'masks'
        protection_root.mkdir(parents=True, exist_ok=True)
        evidence_dir = Path(tempfile.mkdtemp(prefix='korean-', dir=protection_root)) / 'evidence'
        try:
            korean_protection = korean_cool_protection.prepare(
                source, evidence_dir, korean_cool_execution.foundation_filter({'source': source}))
        except korean_cool_protection.ProtectionError as error:
            raise SkillError(f'当前素材人物保护无法建立：{error}；不会回落全局调色。', 4) from error
        recipe['_korean_protection'] = korean_protection
        recipe['_korean_preview_dir'] = str(evidence_dir.parent / 'previews')
    shot_grade_plan = None
    if getattr(args, "shot_grade", False):
        if source["media_type"] != "video":
            raise SkillError("逐镜头校正只适用于视频", 3)
        import shot_grade as shot_grade_module
        working = "display-p3" if color["profile"] == "display-p3" else "srgb"
        shot_grade_plan = shot_grade_module.plan_shot_grade(
            Path(source["path"]), working, max(0.0, min(1.0, float(getattr(args, "match_strength", 1.0)))),
            getattr(args, "anchor", None),
        )
        shot_grade_plan = apply_shot_boundary_confirmation(
            shot_grade_plan, bool(getattr(args, "confirm_shot_boundaries", False))
        )
    if shot_grade_plan:
        creative_only = build_filter(
            parameters, tone_curve, hsl_bands, None, "", render_mix, attention_map,
            highlight_protection=highlight_protection)
        full_chain = ",".join(part for part in (shot_grade_plan["timeline_filter"], creative_only) if part)
        trim_report = shot_grade_module.measure_post_look_trim(
            Path(source["path"]), full_chain, shot_grade_plan["shots"],
            shot_grade_plan["anchor"]["index"],
            "display-p3" if color["profile"] == "display-p3" else "srgb",
        )
        if trim_report["available"]:
            trim_report["trims"] = shot_grade_module.guard_trims_by_match_eligibility(
                trim_report["trims"], shot_grade_plan["primaries"])
            trim_report["cross_scene_guard"] = {
                "status": "applied",
                "skipped_shot_indices": [
                    item["shot_index"] for item in trim_report["trims"]
                    if item.get("status") == "skipped-cross-scene-risk"],
                "boundary": "与一级匹配共用保守资格门；不把技术启发式冒充语义场景识别",
            }
        shot_grade_plan["trim_report"] = trim_report
        shot_grade_plan["trims"] = trim_report["trims"]
        shot_grade_plan["trim_filter"] = shot_grade_module.build_trim_filter(
            shot_grade_plan["shots"], trim_report["trims"]
        ) if trim_report["available"] else ""
        shot_grade_plan["guarantees"].append(
            "创意 Look 之后追加镜头级 trim（由低分辨率代理实测得出，不是解析预测）；"
            "幅度上限 ±6% 通道增益并做亮度守恒归一化"
            if trim_report["available"]
            else f"未能生成镜头级 trim：{trim_report['reason']}；Look 后的残余偏色需人工检查"
        )
    primary_grade = (
        {
            "contract": "foundation-v2c",
            "mode": "per-shot",
            "target_mode": "relative-ev",
            "shot_count": shot_grade_plan["shot_count"],
            "anchor_index": shot_grade_plan["anchor"]["index"],
            "anchor_reason": shot_grade_plan["anchor"]["reason"],
            "foundation_contract": shot_grade_plan["foundation_contract"],
            "foundation_hashes": shot_grade_plan["foundation_hashes"],
            "readiness_targets": shot_grade_plan["readiness_targets"],
            "scope": "逐镜头一级校正；整段不再共用单一中位数，硬切两侧互不影响",
        }
        if shot_grade_plan else foundation_grade(source)
    )
    foundation = primary_grade
    foundation_digest = foundation_hash(foundation)
    if source["media_type"] == "photo" or not shot_grade_plan:
        import suggest as suggest_module
        execution_preflight = suggest_module.run_execution_preflight(recipe, source, strength)
        monotonicity = execution_preflight.get("monotonicity")
        recovery_step = (
            "检查失败原因；需要换方向时运行 `python3 scripts/blcaptain_color.py suggest --input "
            + shlex.quote(str(source["path"])) + " --mode smart --display-only` 重新推荐。"
        )
        if execution_preflight.get("code") == "non-monotonic-strength":
            raise SkillError(
                f"当前方向在这张素材上的强度变化不单调：{execution_preflight['reason']}。"
                "原文件未改动。其他方向尚未预演；下一步：用 suggest 重新推荐后再选择。", 5,
                recovery_step=recovery_step)
        if execution_preflight.get("status") == "blocked":
            raise SkillError(
                f"当前方向未达到可确认计划资格：{execution_preflight.get('reason', '预演被阻断')}。"
                "原文件未改动；不会生成待确认计划。需要换方向时用 suggest 重新推荐。", 5,
                recovery_step=recovery_step)
        if korean_protection is not None and execution_preflight.get('status') != 'executable':
            raise SkillError('韩系保护预演仍有风险，不能生成可确认计划：'
                             + execution_preflight.get('reason', '未达执行资格'), 5)
        if signature_evidence is not None and execution_preflight.get('status') != 'executable':
            raise SkillError('人工签名区域预演未取得执行资格：' + execution_preflight.get('reason', ''), 5)
        if not isinstance(monotonicity, dict):
            raise SkillError(
                f"无法完成逐素材三档预演：{execution_preflight.get('reason', '未知原因')}。"
                "原文件未改动。下一步：检查素材；需要换方向时用 suggest 重新推荐。", 5,
                recovery_step=recovery_step)
    else:
        monotonicity = {
            "status": "not-run-shot-grade",
            "reason": "逐镜头视频三档预演需复用已确认时间轴，交由镜头级链路验收",
        }
    output_dir = Path(args.output_dir).expanduser().resolve()
    suffix = ".png" if source["media_type"] == "photo" else ".mp4"
    stem = Path(source["path"]).stem
    variant = f"__{args.variant}" if args.variant else ""
    output_path = output_dir / f"{stem}__{recipe['id']}{variant}__{round(strength * 100):02d}{suffix}"
    comparison_path = output_dir / (
        f"{stem}__{recipe['id']}{variant}__comparison.png"
        if source["media_type"] == "photo"
        else f"{stem}__{recipe['id']}{variant}__comparison.mp4"
    )
    if output_path == Path(source["path"]):
        raise SkillError("输出路径不得覆盖原文件", 3)
    # 语义局部探测。默认不跑：探测要 1-2 秒，而绝大多数请求只要全局方案。
    # 探测出来的也只是**候选**——局部改动必须在 render 时单独确认，
    # 不随全局方案一起生效。
    local_plan = None
    skin_strength_decision = None
    if signature_evidence is not None:
        local_plan = {'strategy': 'signature-regions', 'candidates': [],
                      'confirmation_required': True,
                      'reason': '人工限定区域与显式帧区间；请确认 signature-regions，不是自动跟踪。'}
    elif korean_protection is not None:
        local_plan = {'strategy': 'korean-cool-protection', 'candidates': [],
                      'confirmation_required': True,
                      'review_mask_path': korean_protection['mask']['path'],
                      'reason': '请看过当次蒙版边缘和三档预演后，独立确认 korean-cool-protection。'}
    elif getattr(args, "detect_local", False):
        if source["media_type"] != "photo":
            local_plan = {"candidates": [],
                          "reason": "视频的语义局部需要蒙版跨帧跟踪（L3），尚未实现"}
        else:
            local_plan = detect_local_candidates(
                Path(source["path"]), recipe, output_dir / "masks")
            skin_candidate = next((item for item in local_plan.get("candidates", [])
                                   if item.get("class") == "skin" and item.get("mask_path")), None)
            if skin_candidate:
                skin_strength_decision = preflight_skin_strength(
                    source, recipe, Path(skin_candidate["mask_path"]), strength)
                if skin_strength_decision["status"] == "blocked":
                    raise SkillError(
                        "最低预演档仍越出肤色记忆色走廊，拒绝生成可确认计划。"
                        "原文件未改动；请换方向或先做中性校正。", 5)
                if skin_strength_decision["status"] == "downgraded":
                    requested = strength
                    strength = skin_strength_decision["selected_strength"] / 100.0
                    effective_strength = perceptual_strength(recipe, strength)
                    render_mix = effective_strength
                    strength_input = {**strength_input,
                                      "requested_normalized": requested,
                                      "normalized": strength,
                                      "adjustment": "肤色走廊预演在计划确认前自动降档"}
                    output_path = output_dir / (
                        f"{stem}__{recipe['id']}{variant}__{round(strength * 100):02d}{suffix}")

    plan = {
        "schema_version": "4.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "confirmation_required": True,
        "confirmed": False,
        "source": source,
        "style": style_payload(recipe, source["media_type"]),
        "strength": strength,
        "strength_input": strength_input,
        "effective_strength": effective_strength,
        "render_mix": render_mix,
        "strength_pipeline": strength_pipeline,
        "visual_brief": visual_brief,
        "composition": composition,
        "attention_map": attention_map,
        "primary_grade": primary_grade,
        "input_truth": input_truth,
        "foundation_grade": foundation,
        "foundation_hash": foundation_digest,
        "monotonicity": monotonicity,
        "review_focus": args.review_focus,
        "parameters": parameters,
        "tone_curve": tone_curve,
        "hsl_bands": hsl_bands,
        "highlight_protection": highlight_protection,
        "adjustments": adjustments,
        "input_assumption": (
            "无色彩标签；确认本方案即同意按 sRGB 解释"
            if color["profile"] == "untagged"
            else ("无完整色彩标签；已确认按 Rec.709 SDR 解释" if color["profile"] == "untagged-video" else color["label"])
        ),
        "color_pipeline": color_pipeline_for(source),
        "local_grade": local_plan,
        "skin_strength_decision": skin_strength_decision,
        "capability_profile": capability_profile_for(
            source["media_type"], bool(shot_grade_plan), local_plan),
        "shot_grade": shot_grade_plan,
        "assume_sdr": bool(args.assume_sdr),
        "output_path": str(output_path),
        "comparison_path": str(comparison_path),
        "receipt_path": str(output_dir / f"{stem}__{recipe['id']}{variant}__receipt.json"),
    }
    if signature_evidence is not None:
        plan['signature_regions'] = signature_evidence
        plan['signature_execution_sha256'] = signature_module.implementation_hash()
        plan['execution_preflight'] = execution_preflight
    elif korean_protection is not None:
        plan['korean_cool_protection'] = korean_protection
        plan['korean_execution_sha256'] = korean_cool_execution.implementation_hash()
        plan['execution_preflight'] = execution_preflight
    else:
        plan["local_grade"] = preflight_local_candidates(plan)
    plan["capability_profile"] = capability_profile_for(
        source["media_type"], bool(shot_grade_plan), plan["local_grade"])
    plan["plan_id"] = plan_fingerprint(plan)
    if args.plan_out:
        plan_path = Path(args.plan_out).expanduser().resolve()
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        if plan_path.exists():
            raise SkillError(f"计划文件已存在，不会覆盖：{plan_path}", 3)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def partial_path(final_path: Path) -> Path:
    return final_path.with_name(f"{final_path.stem}.partial{final_path.suffix}")


def ensure_new(paths: list[Path], source: Path) -> None:
    for path in paths:
        if path == source:
            raise SkillError("输出路径不得覆盖原文件", 3)
        if path.exists():
            raise SkillError(
                f"输出已存在，不会覆盖：{path}。"
                "换个 --output-dir，或在 plan 时加 --variant <标签>（会生成带标签的文件名）；"
                "要重来就先把旧文件挪走。原片永远不会被改动。", 3)
        path.parent.mkdir(parents=True, exist_ok=True)


def cleanup_staged(paths: list[Path]) -> None:
    """只清理本次渲染明确创建的临时文件。"""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def publish_artifact_group(pairs: list[tuple[Path, Path]]) -> None:
    """把成片、对比和回执作为一个用户可见结果组发布。

    文件系统不提供多文件原子重命名，因此先完成全部校验，再逐个发布；若任一步
    失败，就删除已经发布的本组文件和剩余临时文件，避免留下半套“正式结果”。
    """
    staged = [source for source, _target in pairs]
    published: list[Path] = []
    try:
        for source, target in pairs:
            if not source.is_file():
                raise SkillError(f"待发布临时文件缺失：{source}", 6)
            if target.exists():
                raise SkillError(f"目标文件已存在，拒绝覆盖：{target}", 3)
        for source, target in pairs:
            os.replace(source, target)
            published.append(target)
    except Exception as error:  # noqa: BLE001
        cleanup_staged(published + staged)
        if isinstance(error, SkillError):
            raise
        raise SkillError(
            f"结果组发布失败，已回滚成片、对比图和回执：{type(error).__name__}: {error}",
            6,
        ) from error


def publish_directional_preview(plan: dict, visual: dict, staged_output: Path,
                                staged_comparison: Path) -> dict:
    """保存方向门失败候选，但绝不占用正式成片路径。"""
    formal_output = Path(plan["output_path"])
    formal_comparison = Path(plan["comparison_path"])
    preview_dir = formal_output.parent / "preview-candidates"
    preview_output = preview_dir / f"{formal_output.stem}__direction-preview{formal_output.suffix}"
    preview_comparison = preview_dir / (
        f"{formal_comparison.stem}__direction-preview{formal_comparison.suffix}")
    preview_receipt = preview_dir / f"{formal_output.stem}__direction-preview-receipt.json"
    ensure_new([preview_output, preview_comparison, preview_receipt], Path(plan["source"]["path"]))
    payload = {
        "schema_version": "4.0.0",
        "technical": {"status": "blocked", "output_published": False},
        "aesthetic": aesthetic_state(),
        "reason": "至少两项配方声明方向未达成；只保留预览候选，不发布正式成片。",
        "next_step": "重新 plan 并换一个视觉目标不同的方向；不要把本预览候选当作交付成片。",
        "plan_id": plan["plan_id"],
        "style": plan["style"],
        "strength": plan["strength"],
        "original_path": plan["source"]["path"],
        "original_sha256": plan["source"]["sha256"],
        "original_unchanged": sha256(Path(plan["source"]["path"])) == plan["source"]["sha256"],
        "formal_output_path": str(formal_output),
        "formal_comparison_path": str(formal_comparison),
        "preview_output_path": str(preview_output),
        "preview_comparison_path": str(preview_comparison),
        "preview_receipt_path": str(preview_receipt),
        "visual_validation": visual,
    }
    staged_receipt = partial_path(preview_receipt)
    staged_receipt.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    publish_artifact_group([
        (staged_output, preview_output),
        (staged_comparison, preview_comparison),
        (staged_receipt, preview_receipt),
    ])
    return payload


def photo_output_filter(plan: dict, vf: str) -> str:
    profile = plan["color_pipeline"]["output_profile"]
    if profile == "Display P3":
        # 带 ICC 的 JPEG 会把 ICC side data 传给 PNG 编码器；FFmpeg 8.1 随后优先按
        # 该 side data 写 gAMA，使 ffprobe 报 bt470m，覆盖我们声明的 sRGB transfer。
        # 先删掉输入帧的 ICC，再由 PNG cICP 写回明确的 Display P3 标签；像素不变。
        tags = ("sidedata=mode=delete:type=ICC_PROFILE,"
                "setparams=colorspace=gbr:color_primaries=smpte432:"
                "color_trc=iec61966-2-1:range=pc,format=rgb48be")
    else:
        tags = "setparams=colorspace=gbr:color_primaries=bt709:color_trc=iec61966-2-1:range=pc,format=rgb24"
    return f"{vf},{tags}" if vf else tags


def validate_photo_color(path: Path, plan: dict) -> dict:
    rendered = next(item for item in probe(path).get("streams", []) if item.get("codec_type") == "video")
    actual = {
        "pixel_format": rendered.get("pix_fmt"),
        "matrix": rendered.get("color_space"),
        "transfer": rendered.get("color_transfer"),
        "primaries": rendered.get("color_primaries"),
        "range": rendered.get("color_range"),
        "embedded_profile": embedded_photo_profile(path),
    }
    if plan["color_pipeline"]["output_profile"] == "Display P3":
        expected = {"pixel_format": "rgb48be", "matrix": "gbr", "transfer": "iec61966-2-1", "primaries": "smpte432", "range": "pc"}
    else:
        expected = {"pixel_format": "rgb24", "matrix": "gbr", "transfer": "iec61966-2-1", "primaries": "bt709", "range": "pc"}
    mismatches = {key: {"expected": value, "actual": actual[key]} for key, value in expected.items() if actual[key] != value}
    if mismatches:
        raise SkillError(f"照片输出色彩标签验收失败：{mismatches}", 6)
    return {"status": "passed", "expected": expected, "actual": actual}


def acceptance_sampling(media_type: str, duration: float) -> dict:
    """决定验收抽帧方案。

    v3 固定抽 4 帧且 fps 下限被钳在 0.2，实测 60 秒素材只覆盖前 15 秒——
    后 35 秒的纯白高光区完全不进验收，回执却报 channel_high_clip_ratio=0.0。
    时长 1800 秒时覆盖率不足 1%。现在按时长决定帧数并铺满整个时间轴。
    """
    if media_type == "photo" or duration <= 0:
        return {"frames": 1, "fps": None, "covered_seconds": 0.0, "coverage_ratio": 1.0}
    # 帧数随时长伸缩，fps 由「帧数 ÷ 时长」直接得出，保证覆盖率恒为 100%。
    # 早期给 fps 设了 0.02 的下限，结果 90 分钟素材只覆盖 55.6%——
    # 下限本身成了新的盲区。帧数上限 90 是为了控制纯 Python 感知计算的耗时。
    frames = max(8, min(90, int(duration / 30) + 8))
    fps = max(1e-4, min(30.0, frames / duration))
    covered = min(duration, frames / fps)
    return {"frames": frames, "fps": round(fps, 6),
            "covered_seconds": round(covered, 3),
            "coverage_ratio": round(covered / duration, 4)}


def sample_rgb(path: Path, media_type: str, duration: float, prefilter: str = "") -> bytes:
    plan = acceptance_sampling(media_type, duration)
    filters = [prefilter] if prefilter else []
    if plan["fps"] is not None:
        filters.append(f"fps={plan['fps']}")
    # 顺序不能反。先 scale 再 format 等于在 yuv420 里缩放，
    # 色度插值会把极值往里收：纯白 (253,255,255) 被读成 (251,253,253)，
    # 于是「max 通道 ≥ 254」这条剪切判据在视频上永远不成立——门是瞎的。
    # 先转 RGB 再缩放，读数与不缩放时一致。
    filters.extend(["format=rgb24", "scale=96:64"])
    result = subprocess.run(
        [require_tool("ffmpeg"), "-v", "error", "-i", str(path), "-vf", ",".join(filters),
         "-frames:v", str(plan["frames"]), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True,
    )
    if result.returncode or not result.stdout:
        raise SkillError("无法计算输出视觉变化", 6)
    return result.stdout


def rgb_to_oklab(red: int, green: int, blue: int, profile: str) -> tuple[float, float, float]:
    r, g, b = srgb_linear(red), srgb_linear(green), srgb_linear(blue)
    if profile == "display-p3":
        x = 0.48657095 * r + 0.26566769 * g + 0.19821729 * b
        y = 0.22897456 * r + 0.69173852 * g + 0.07928691 * b
        z = 0.0 * r + 0.04511338 * g + 1.04394437 * b
    else:
        x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
        y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
        z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    l = 0.8189330101 * x + 0.3618667424 * y - 0.1288597137 * z
    m = 0.0329845436 * x + 0.9293118715 * y + 0.0361456387 * z
    s = 0.0482003018 * x + 0.2643662691 * y + 0.6338517070 * z
    cbrt = lambda value: math.copysign(abs(value) ** (1 / 3), value)
    l_, m_, s_ = cbrt(l), cbrt(m), cbrt(s)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def tone_response_thresholds(filtergraph: str, transform=None) -> dict:
    """把灰阶推过本方案的真实滤镜链，求出「哪些输入亮度会被压到死黑／推到过曝」。

    风险带不能用一个拍脑袋的常数。同一个 0.06 阈值，在某些夹具上刚好漏掉
    13.2% 会越线的像素，在另一些上又过宽。正确做法是让配方自己回答：
    这条链路把输入亮度 L 映射到哪里？低于某个 L 的像素注定死黑，那才是它的风险带。

    逐点变换是确定性的，因此 33 级灰阶就能定出阈值，代价约 50ms。
    """
    if not filtergraph.strip():
        # 空链路时必须自己走回退，不能依赖调用方兜底——函数要自洽。
        return {"crush_input": 0.06, "blow_input": 0.94, "derived": False,
                "reason": "未提供滤镜链，使用保守常数"}
    try:
        import palette as palette_module
        ramp = [(value, value, value) for value in range(0, 256, 8)]
        transformed = transform(ramp) if transform else palette_module.transform_colors(ramp, filtergraph)
    except Exception:  # noqa: BLE001
        # 求不出就退回保守常数，并在回执里标明用的是回退值。
        return {"crush_input": 0.06, "blow_input": 0.94, "derived": False,
                "reason": "无法推导影调响应，使用保守常数"}
    crush_input, blow_input = 0.0, 1.0
    for source_rgb, out_rgb in zip(ramp, transformed):
        source_luma = source_rgb[0] / 255.0
        out_luma = (0.2126 * out_rgb[0] + 0.7152 * out_rgb[1] + 0.0722 * out_rgb[2]) / 255.0
        if out_luma <= 0.004:
            crush_input = max(crush_input, source_luma)
        if out_luma >= 0.99:
            blow_input = min(blow_input, source_luma)
    return {"crush_input": round(crush_input, 4), "blow_input": round(blow_input, 4),
            "derived": True,
            "meaning": "输入亮度低于 crush_input 的像素注定被压到死黑；高于 blow_input 的注定过曝"}



def _probe_black(chain: str) -> float:
    """把纯黑推过一条滤镜链，返回输出亮度。"""
    result = subprocess.run(
        [require_tool("ffmpeg"), "-v", "error", "-f", "lavfi",
         "-i", "color=c=black:size=16x16:d=1", "-vf", chain, "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], capture_output=True)
    if result.returncode or len(result.stdout) < 3:
        return -1.0
    data = result.stdout
    return (0.2126 * data[0] + 0.7152 * data[1] + 0.0722 * data[2]) / 255.0



def skin_tolerance_gate(plan: dict, source: Path, rendered: Path) -> dict:
    """肤色容差：只在拿得到肤色蒙版时才判，拿不到就说拿不到。

    这道门此前标着 pending，阻碍写的是「验收取样已缩到 96×64，蒙版对不齐」。
    L2 接入之后蒙版本来就要生成一次，对齐取样也已经有了现成的实现，
    所以阻碍消失了——但有一个边界没变：skin 是 face ∩ person ∩ 肤色似然的
    **派生结果**，后端自己标了 needs_human_review。
    拿一个自认不可靠的派生蒙版去拒绝渲染，就是用近似冒充能力，
    所以这道门只出告警与人工复核条目，永远不阻断。
    """
    import color_space
    import memory_color_gate
    import semantic_backend

    if plan["source"]["media_type"] != "photo":
        return {"gate": "skin_tolerance", "passed": None, "status": "skipped",
                "reason": "视频的逐帧肤色蒙版属 L3，本次不判"}
    if not semantic_backend.person_classes_available():
        return {"gate": "skin_tolerance", "passed": None, "status": "skipped",
                "reason": "人物语义后端不可用，拿不到肤色蒙版；"
                          "不用固定 HSL 橙色带冒充肤色区域"}
    try:
        backend = semantic_backend.resolve_backend()
        mask_dir = Path(plan["output_path"]).parent / "masks"
        payload = backend.analyze(source, mask_dir)
        if hasattr(payload, "__dict__"):
            payload = payload.__dict__
        skin = (payload.get("classes") or {}).get("skin") or {}
        # 只认肤色蒙版。退回人物蒙版是错的：人物蒙版里有衣服和头发，
        # 它们是低彩的，把肤色簇的彩度稀释掉之后色相角就没意义了——
        # 实测一张人像用人物蒙版求出来的簇中心 chroma 只有 0.021，
        # 而在这个彩度下 13° 到 332° 的「41 度位移」对应的绝对色差微乎其微。
        mask_path = skin.get("mask_path")
        if not skin.get("present") or not mask_path:
            return {"gate": "skin_tolerance", "passed": None, "status": "skipped",
                    "reason": "本张素材没有可用的**肤色**蒙版（没有人，或人脸框与人物蒙版的"
                              "交集为空）。不退回人物蒙版代替——那里面有衣服和头发。"}
        import local_grade
        stats = local_grade.measure_inside_outside(source, rendered, Path(mask_path))
    except Exception as error:  # noqa: BLE001
        return {"gate": "skin_tolerance", "passed": None, "status": "skipped",
                "reason": f"肤色取样失败：{type(error).__name__}: {error}"}

    before = _masked_mean_oklab(source, Path(mask_path), plan["source"]["color"]["profile"])
    after = _masked_mean_oklab(rendered, Path(mask_path), plan["source"]["color"]["profile"])
    if before is None or after is None:
        return {"gate": "skin_tolerance", "passed": None, "status": "skipped",
                "reason": "蒙版内有效像素不足，无法求肤色簇中心"}

    import math

    # 彩度太低时色相角不稳定，判它没有意义。
    # OKLab 里 chroma 0.01 量级的簇，a/b 的微小抖动就能让色相角跨越几十度，
    # 而那点位移对应的实际颜色差异肉眼根本看不出。
    min_chroma = 0.03
    chroma_before = math.hypot(before[1], before[2])
    if chroma_before < min_chroma:
        return {"gate": "skin_tolerance", "passed": None, "status": "skipped",
                "reason": f"肤色簇中心的彩度只有 {chroma_before:.4f}，低于 {min_chroma}——"
                          "这个彩度下色相角是噪声，判了也不作数",
                "chroma_before": round(chroma_before, 5)}

    import visual_grammar as grammar
    report = grammar.check_skin_tolerance(before, after)
    report["status"] = "measured"
    report["enforcement"] = "warning"
    report["mask_source"] = "derived：人脸框 ∩ 人物蒙版 ∩ 受限肤色似然像素"
    report["separation"] = stats
    report["needs_human_review"] = True
    bible = plan.get("style", {}).get("style_bible") or {}
    protection = bible.get("memory_color_protection") or {}
    before_rgb = _masked_rgb_samples(source, Path(mask_path))
    after_rgb = _masked_rgb_samples(rendered, Path(mask_path))
    memory = memory_color_gate.evaluate_skin(
        before_rgb, after_rgb,
        float(protection.get("skin_chroma_max_gain", 1.1)))
    report["memory_color"] = memory
    report["boundary"] = ("肤色蒙版是派生结果不是训练得到的分割，后端自标需要人工确认。"
                          "这道门只出告警，不拒绝渲染——用一个自认不可靠的蒙版去拦人，"
                          "本身就是拿近似冒充能力。")
    return report


def _masked_mean_oklab(image: Path, mask: Path, profile: str):
    """蒙版内像素的 OKLab 均值。取样在同一网格上做，保证空间对应。"""
    grid_w, grid_h = 96, 54

    def read(path: Path, gray: bool = False) -> bytes:
        fmt = "gray" if gray else "rgb24"
        result = subprocess.run(
            [require_tool("ffmpeg"), "-v", "error", "-i", str(path),
             "-vf", f"format={fmt},scale={grid_w}:{grid_h}:flags=area",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", fmt, "-"],
            capture_output=True)
        return result.stdout if result.returncode == 0 else b""

    pixels, mask_grid = read(image), read(mask, gray=True)
    if not pixels or not mask_grid:
        return None
    total = [0.0, 0.0, 0.0]
    count = 0
    for index in range(min(len(mask_grid), len(pixels) // 3)):
        if mask_grid[index] < 128:
            continue
        offset = index * 3
        lab = rgb_to_oklab(pixels[offset], pixels[offset + 1], pixels[offset + 2], profile)
        total = [total[i] + lab[i] for i in range(3)]
        count += 1
    if count < 24:
        return None
    return tuple(value / count for value in total)


def _masked_rgb_samples(image: Path, mask: Path) -> list[tuple[int, int, int]]:
    grid_w, grid_h = 96, 54

    def read(path: Path, gray: bool = False) -> bytes:
        fmt = "gray" if gray else "rgb24"
        result = subprocess.run(
            [require_tool("ffmpeg"), "-v", "error", "-i", str(path),
             "-vf", f"format={fmt},scale={grid_w}:{grid_h}:flags=area",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", fmt, "-"],
            capture_output=True)
        return result.stdout if result.returncode == 0 else b""

    pixels, mask_grid = read(image), read(mask, gray=True)
    result = []
    for index in range(min(len(mask_grid), len(pixels) // 3)):
        if mask_grid[index] < 128:
            continue
        offset = index * 3
        result.append((pixels[offset], pixels[offset + 1], pixels[offset + 2]))
    return result


def colored_highlight_gate(before_rgb: list[tuple[int, int, int]],
                           after_rgb: list[tuple[int, int, int]],
                           profile: str) -> dict:
    """带彩高光的替代门：允许保彩，但不允许换色或撞白。"""
    from color_space import oklab_to_oklch

    shifts = []
    for before, after in zip(before_rgb, after_rgb):
        before_lch = oklab_to_oklch(*rgb_to_oklab(*before, profile))
        after_lch = oklab_to_oklch(*rgb_to_oklab(*after, profile))
        if before_lch[1] < 0.01 or after_lch[1] < 0.01:
            continue
        delta = abs(before_lch[2] - after_lch[2]) % 360.0
        shifts.append(min(delta, 360.0 - delta))
    max_shift = max(shifts, default=0.0)
    white_clipped = any(max(pixel) >= 255 for pixel in after_rgb)
    passed = max_shift <= 8.0 and not white_clipped
    return {
        "gate": "colored_highlight_safety",
        "status": "measured",
        "passed": passed,
        "max_hue_shift_deg": round(max_shift, 6),
        "hue_shift_limit_deg": 8.0,
        "white_clipped": white_clipped,
        "advice": None if passed else (
            "带彩高光已换色超过 8° 或白位发生通道剪切；请降档或恢复默认 path-to-white。"),
        "boundary": "替代门只验证高光色相守恒与白位安全，不判断带彩高光是否好看。",
    }


def visual_grammar_gates(plan: dict, original: bytes, output: bytes,
                         usable: int, profile: str, filtergraph: str = "",
                         source_path: Path | None = None,
                         rendered_path: Path | None = None) -> dict:
    """把视觉语法的门接进渲染验收。

    v4 审计发现这层有 7 个门、其中 6 个从未接进任何执行链——声明了却不运行
    的门比没有门更危险。这个函数是它们的归宿；哪个门拦、哪个门只告警、
    哪个门根本不该自动判定，写在 visual_grammar.GATE_REGISTRY 里。
    """
    import visual_grammar as grammar

    report: dict = {"registry_version": len(grammar.GATE_REGISTRY), "gates": {}}
    gate_profile = plan.get("style", {}).get("gate_profile") or {}
    report["style_bible_gate_profile"] = gate_profile
    report["gates"]["style_bible_contract"] = {
        "gate": "style_bible_contract",
        "passed": not bool(gate_profile.get("violations")),
        "status": "compiled",
        "violations": gate_profile.get("violations") or [],
        "manual_review": (gate_profile.get("bindings", {}).get("forbidden", {})
                          .get("manual", [])),
        "boundary": "compiled 表示规则已绑定到自动门或人工项，不表示人工项已审美通过。",
    }

    # 加法抬升：解析链，与素材无关。预算默认 MAX_ADDITIVE_LIFT，
    # 配方可用 parameters.additive_lift_budget 显式声明更高的额度并写明理由。
    budget = float(plan["parameters"].get("additive_lift_budget", grammar.MAX_ADDITIVE_LIFT))
    lift = grammar.additive_lift_of_chain(
        build_filter, plan["parameters"], plan.get("tone_curve"),
        plan.get("hsl_bands"), _probe_black)
    if plan.get('korean_cool_protection'):
        import korean_cool_execution
        black = korean_cool_execution.probe_colors(plan, [(0, 0, 0)], with_foundation=False)[0]
        lift = sum(c * w for c, w in zip(black, (.2126, .7152, .0722))) / 255
    if lift < 0:
        report["gates"]["additive_lift"] = {
            "gate": "additive_lift", "passed": None, "status": "skipped",
            "reason": "无法探测链的黑场响应"}
    else:
        additive = grammar.check_additive_lift(0.0, lift, 1.0)
        additive["limit"] = budget
        additive["passed"] = lift <= budget
        additive["status"] = "measured"
        report["gates"]["additive_lift"] = additive

    # path-to-white：必须推探针过链，不能用输出画面的像素。
    #
    # 用输出像素测出来的是「内容 + 配方」的混合：一张有蓝天的照片，
    # 高光段的 chroma-L 关系主要由天空和云决定，与配方对高光彩度做了什么无关。
    # 实测按输出像素判，56 次渲染被误拒——和 additive_lift 是同一类错误：
    # 用输出侧统计去测一个只存在于链里的性质。
    # 改为推一组已知彩度的探针过链：探针覆盖高光段的多个色相与彩度，
    # 输出的 chroma 对 L 的斜率就只反映链的行为。
    probe_colors = []
    for level in (205, 220, 235, 250):
        probe_colors.append((level, level, level))
        for hue_shift in (0, 1, 2):
            base = [level, level, level]
            base[hue_shift] = min(255, level + 20)
            base[(hue_shift + 2) % 3] = max(0, level - 20)
            probe_colors.append(tuple(base))
    try:
        import palette as palette_module
        if plan.get('korean_cool_protection'):
            import korean_cool_execution
            transformed = korean_cool_execution.probe_colors(plan, probe_colors)
        else:
            transformed = palette_module.transform_colors(probe_colors, filtergraph)
    except Exception:  # noqa: BLE001
        transformed = []
    protection = plan.get("highlight_protection") or {"mode": "path-to-white"}
    if filtergraph and transformed and len(transformed) == len(probe_colors):
        samples = [rgb_to_oklab(r, g, b, profile) for r, g, b in transformed]
        if protection.get("mode") == "colored-highlight":
            replacement = colored_highlight_gate(probe_colors, transformed, profile)
            report["gates"]["path_to_white"] = {
                **replacement,
                "gate": "path_to_white",
                "replacement_gate": "colored_highlight_safety",
                "exemption_reason": protection.get("reason"),
            }
        else:
            report["gates"]["path_to_white"] = grammar.check_path_to_white(samples)
    else:
        report["gates"]["path_to_white"] = {
            "gate": "path_to_white", "passed": None, "status": "skipped",
            "reason": "无法把探针推过滤镜链，未做判定"}
    # 输出画面的高光样本另算，只作为回执参考，不参与阻断。
    samples = [rgb_to_oklab(output[i], output[i + 1], output[i + 2], profile)
               for i in range(0, usable, 3)]
    rendered_view = grammar.check_path_to_white(samples)
    report["gates"]["path_to_white"]["rendered_slope"] = rendered_view.get("slope")
    report["gates"]["path_to_white"]["rendered_note"] = (
        "输出画面的高光斜率含内容贡献，仅供参考，不作判据")

    # 色相守恒：只告警。多数创意配方本就要搬色相，容差无法一刀切，
    # 但把位移量写进回执，让人能看见自己搬了多少。
    before = [rgb_to_oklab(original[i], original[i + 1], original[i + 2], profile)
              for i in range(0, usable, 3)]
    hue = grammar.check_hue_preservation(before, samples, mode="creative")
    hue["enforcement"] = "warning"
    report["gates"]["hue_preservation"] = hue

    # 必须用**本次验收正在看的那个文件**。plan["output_path"] 是最终路径，
    # 而验收发生在改名之前，那时它还是个 .partial——照 plan 取会采样失败。
    if source_path is not None and rendered_path is not None:
        skin = skin_tolerance_gate(plan, source_path, rendered_path)
    else:
        skin = {"gate": "skin_tolerance", "passed": None, "status": "skipped",
                "reason": "调用方没有提供原片与输出路径，无法做蒙版对齐取样"}
    report["gates"]["skin_tolerance"] = skin
    memory = skin.get("memory_color") if isinstance(skin, dict) else None
    if memory is None:
        memory = {"gate": "skin_memory_corridor", "passed": None, "status": "skipped",
                  "reason": "本次没有可靠肤色蒙版或有效肤色样本"}
    if isinstance(memory, dict) and skin.get("enforcement") == "warning":
        memory = {**memory, "enforcement": "warning"}
    report["gates"]["skin_memory_corridor"] = grammar.check_skin_memory_corridor(memory)

    blocking = [name for name, item in report["gates"].items()
                if is_blocking_gate(name, item)]
    report["blocking_failures"] = blocking
    report["manual_review"] = [
        f"{name}：{item['reason_if_manual']}"
        for name, item in grammar.GATE_REGISTRY.items()
        if item["enforcement"] == "manual"]
    return report


def is_blocking_gate(name: str, item: dict) -> bool:
    """注册表是默认值；真实测量可把不可靠证据降为告警，不能反向升级。"""
    import visual_grammar as grammar

    registered = grammar.GATE_REGISTRY.get(f"check_{name}", {}).get("enforcement")
    enforcement = item.get("enforcement", registered)
    return enforcement == "blocking" and item.get("passed") is False


def visual_top_level_status(directional: dict | None, gates: dict,
                            tonal_health: dict | None) -> str:
    """顶层状态必须与回执首句同向。

    status 不能只看剪切与幅度就写 passed：同一段里 directional_audit 可能是 failed，
    门可能有 skipped，影调复诊可能判「发灰」或根本没能跑。任何会让回执首句
    不是干净「通过」的发现，顶层都不得写 passed——否则用户读到的第一句话
    与机器可读状态互相矛盾（ISSUE-007 的同类缺陷）。
    """
    tonal = tonal_health or {}
    has_findings = (
        bool((directional or {}).get("failed_dimensions"))
        or any(item.get("status") == "skipped" for item in gates.values())
        or bool(tonal.get("regressed"))
        or bool(tonal.get("lift_notable"))
        or bool(tonal.get("unavailable"))
    )
    return "passed_with_findings" if has_findings else "passed"


def impact_change_measurement(plan: dict, total_delta: float,
                              measured_creative_delta: float | None = None) -> dict:
    """把最终总变化与创意 Look 变化分开，避免拿不同口径比较。

    plan 的三档预演已经用同一条完整执行链，分别渲染零强度一级校正基线
    与 30/55/80 创意档。当前强度恰好有这份证据时，最低变化门应比较
    「创意成片 vs 一级校正基线」；总变化仍保留，用于安全上限与回执。
    任意非三档强度或旧计划没有该证据时，诚实退回总变化口径。
    """
    if (plan.get("style") or {}).get("execution_role") == "foundation-only":
        return {
            "value": float(total_delta),
            "basis": "source-versus-foundation-repair",
            "total_delta_e_ok": float(total_delta),
            "creative_delta_e_ok": (
                float(measured_creative_delta)
                if measured_creative_delta is not None else None
            ),
        }
    if measured_creative_delta is not None:
        return {
            "value": float(measured_creative_delta),
            "basis": "rendered-creative-look-versus-primary-baseline",
            "total_delta_e_ok": float(total_delta),
            "creative_delta_e_ok": float(measured_creative_delta),
        }
    monotonicity = plan.get("monotonicity") or {}
    strength_percent = str(int(round(float(plan.get("strength", 0.0)) * 100)))
    sample = (monotonicity.get("samples") or {}).get(strength_percent) or {}
    creative = sample.get("creative_delta_e_ok")
    basis = monotonicity.get("measurement_basis")
    if creative is not None and basis == "creative-look-versus-zero-strength-primary-baseline":
        return {
            "value": float(creative),
            "basis": basis,
            "total_delta_e_ok": float(total_delta),
            "creative_delta_e_ok": float(creative),
        }
    return {
        "value": float(total_delta),
        "basis": "source-versus-final-total-change",
        "total_delta_e_ok": float(total_delta),
        "creative_delta_e_ok": None,
    }


def low_change_recovery(strength: float, basis: str) -> str:
    """根据真实档位给低变化失败提供可执行恢复，不机械要求继续加档。"""
    if float(strength) >= 0.8:
        cancellation = (
            "基础校正与创意 Look 可能在综合色彩或影调上相互抵消；"
            if basis in {
                "creative-look-versus-zero-strength-primary-baseline",
                "rendered-creative-look-versus-primary-baseline",
            } else ""
        )
        return (
            "下一步：当前已经是大胆档，不再继续加档。"
            + cancellation
            + "请改选另一个方向，或先检查配方的基础校正与创意动作是否抵消。"
        )
    return (
        "下一步：保留原文件；若尚未尝试更高档，可重新 plan 后提高强度；"
        "若较高档已经失败，不要循环加档，直接改选另一个方向。"
    )


def validate_visual_impact(source: Path, rendered: Path, plan: dict, filtergraph: str = "",
                           minimum_override: float | None = None,
                           creative_baseline: Path | None = None) -> dict:
    signature_validation = None
    if plan.get('signature_regions'):
        import signature_regions
        if creative_baseline is None:
            raise SkillError('人工区域验收必须使用本次同链 Foundation。', 6)
        signature_validation = signature_regions.measure(plan, creative_baseline, rendered)
        # 多输入空间链没有唯一全局 RGB 探针；不得把空间处理伪装成全局色卡。
        filtergraph = ''
    protected_validation = None
    if plan.get('korean_cool_protection'):
        import korean_cool_validation
        if creative_baseline is None:
            raise SkillError('韩系保护验收缺少同执行链 Foundation，不能拿原片替代。', 6)
        protected_validation = korean_cool_validation.measure(plan, creative_baseline, rendered)
        if not protected_validation['passed']:
            failures = protected_validation['signature_report'].get('blocking_failures') or []
            raise SkillError('韩系空间签名或逐帧保护验收未通过：' + '、'.join(failures)
                             + f"；失败帧 {protected_validation['failed_frame_indices'][:12]}", 6)
    composition = composition_filter(plan)
    original = sample_rgb(source, plan["source"]["media_type"], plan["source"]["duration"], composition)
    output = sample_rgb(rendered, plan["source"]["media_type"], plan["source"]["duration"])
    usable = min(len(original), len(output)) // 3 * 3
    profile = plan["source"]["color"]["profile"]
    transform = None
    if plan.get('korean_cool_protection'):
        import korean_cool_execution
        transform = lambda colors: korean_cool_execution.probe_colors(plan, colors)
    thresholds = (tone_response_thresholds(filtergraph, transform) if transform
                  else tone_response_thresholds(filtergraph)) if filtergraph else {
        "crush_input": 0.06, "blow_input": 0.94, "derived": False,
        "reason": "未提供滤镜链，使用保守常数"}
    crush_input = float(thresholds["crush_input"])
    blow_input = float(thresholds["blow_input"])
    differences = []
    original_low = original_high = clipped_low = clipped_high = 0
    original_crushed = original_blown = crushed = blown = 0
    original_at_risk = original_bright_risk = 0
    pixels = usable // 3
    for index in range(0, usable, 3):
        first = rgb_to_oklab(original[index], original[index + 1], original[index + 2], profile)
        second = rgb_to_oklab(output[index], output[index + 1], output[index + 2], profile)
        differences.append(math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second))))
        original_low += min(original[index:index + 3]) <= 1
        original_high += max(original[index:index + 3]) >= 254
        clipped_low += min(output[index:index + 3]) <= 1
        clipped_high += max(output[index:index + 3]) >= 254
        # 亮度口径：只统计真正丢失细节的像素，不把纯饱和色算进去。
        source_luma = (0.2126 * original[index] + 0.7152 * original[index + 1]
                       + 0.0722 * original[index + 2]) / 255.0
        output_luma = (0.2126 * output[index] + 0.7152 * output[index + 1]
                       + 0.0722 * output[index + 2]) / 255.0
        original_crushed += source_luma <= 0.004
        original_blown += source_luma >= 0.99
        # 风险带由配方自身的影调响应决定，不用固定常数。
        original_at_risk += source_luma <= crush_input
        original_bright_risk += source_luma >= blow_input
        crushed += output_luma <= 0.004
        blown += output_luma >= 0.99
    mean_delta = sum(differences) / len(differences)
    rendered_creative_delta = None
    if creative_baseline is not None:
        baseline = sample_rgb(
            creative_baseline, plan["source"]["media_type"], plan["source"]["duration"])
        creative_usable = min(len(baseline), len(output)) // 3 * 3
        creative_differences = []
        for index in range(0, creative_usable, 3):
            first = rgb_to_oklab(
                baseline[index], baseline[index + 1], baseline[index + 2], profile)
            second = rgb_to_oklab(
                output[index], output[index + 1], output[index + 2], profile)
            creative_differences.append(
                math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second))))
        if creative_differences:
            rendered_creative_delta = sum(creative_differences) / len(creative_differences)
    impact_measurement = impact_change_measurement(
        plan, mean_delta, measured_creative_delta=rendered_creative_delta)
    measured_for_minimum = float(impact_measurement["value"])
    minimum_key = "min_delta_e_video" if plan["source"]["media_type"] == "video" and "min_delta_e_video" in plan["style"]["impact"] else "min_delta_e"
    preflight_floor = (plan.get("monotonicity") or {}).get("relative_min_delta_e")
    required = (
        float(minimum_override)
        if minimum_override is not None
        else (float(preflight_floor) if preflight_floor is not None
              else float(plan["style"]["impact"][minimum_key]) * float(plan["effective_strength"]))
    )
    allowed = float(plan["style"]["impact"]["max_delta_e"])
    low_ratio = clipped_low / pixels
    high_ratio = clipped_high / pixels
    original_low_ratio = original_low / pixels
    original_high_ratio = original_high / pixels
    luma_crushed_ratio = crushed / pixels
    luma_blown_ratio = blown / pixels
    original_luma_crushed = original_crushed / pixels
    original_luma_blown = original_blown / pixels
    original_at_risk = original_at_risk / pixels
    original_bright_risk = original_bright_risk / pixels
    if mean_delta > allowed:
        raise SkillError(
            f"视觉变化验收失败：感知变化 {mean_delta:.4f} 超过安全上限 {allowed:.4f}。"
            "下一步：保留原文件，重新 plan 并降低强度，或换更克制的方向。", 6)
    # 通道口径的容差必须同时有绝对下限和相对余量。
    # 素材本身越接近这个口径的天花板，它的读数噪声就越大：合成彩条的通道暗部
    # 读数是 83.9%、亮部 34.5%，而同一素材的亮度口径是 0.000／0.000——
    # 没有任何真实细节丢失。在 34.5% 的基数上只给 3 个百分点的绝对容差，
    # 等于只放 9% 的相对余量，一次正常渲染就会越线。
    # 干净素材（原片读数接近 0）仍由 0.2／0.12 的绝对下限严格约束，强度不变。
    low_allowance = max(0.2, original_low_ratio * 1.15 + 0.03)
    high_allowance = max(0.12, original_high_ratio * 1.15 + 0.02)
    if low_ratio > low_allowance or high_ratio > high_allowance:
        raise SkillError(
            f"视觉变化验收失败：输出通道剪切过多（暗 {low_ratio:.3f} / 亮 {high_ratio:.3f}，"
            f"容差 {low_allowance:.3f} / {high_allowance:.3f}，"
            f"原片 {original_low_ratio:.3f} / {original_high_ratio:.3f}）。"
            "下一步：保留原文件，重新 plan 并降低强度；若仍失败，换一套不继续压暗或推亮的方向。", 6)
    # 通道口径把纯饱和色也算成剪切，阈值必须放宽到 20%，对真正的死黑就失去了约束力；
    # 亮度口径只看真正丢细节的像素，可以更严。两套口径同时生效。
    #
    # 但绝对阈值同样不对：合成彩条里纯蓝的亮度只有 0.0722，任何压暗都会把它推过线。
    # 实测同一配方（黑白纪实）在真实照片上死黑 0.03%~2.9%，在彩条上 12.8%——那是夹具特性。
    # 因此容差由两部分构成：素材自身处在风险带的比例，加上配方**声明过的**压暗意图。
    # 没有声明压暗却大量堵黑，才是真正要拦的情况。
    tone_policy = plan["style"]["impact"]["tone_policy"]
    declared_darkening = (max(0.0, -float(tone_policy["midpoint_ev"])) * 0.30
                          + max(0.0, -float(tone_policy["shadow_bias"])) * 1.5)
    # 风险带内的像素被压到死黑是该方案影调响应的必然结果，不算缺陷；
    # 超出风险带才说明有额外的、未声明的堵黑。留 0.04 的余量吸收采样与编码误差。
    crush_allowance = original_at_risk + declared_darkening + original_luma_crushed + 0.04
    blow_allowance = original_bright_risk + original_luma_blown + 0.03
    if luma_crushed_ratio > crush_allowance:
        raise SkillError(
            f"视觉变化验收失败：暗部死黑 {luma_crushed_ratio:.3f} 超出容差 {crush_allowance:.3f}"
            f"（原片死黑 {original_luma_crushed:.3f}、由影调响应推出的风险带 {original_at_risk:.3f}"
            f"（阈值 {crush_input:.3f}）、配方声明压暗 {declared_darkening:.3f}）。"
            "下一步：保留原文件，重新 plan 并降低强度，或换不压暗部的方向。", 6)
    if luma_blown_ratio > blow_allowance:
        raise SkillError(
            f"视觉变化验收失败：亮部过曝 {luma_blown_ratio:.3f} 超出容差 {blow_allowance:.3f}"
            f"（原片过曝 {original_luma_blown:.3f}、风险带 {original_bright_risk:.3f}）。"
            "下一步：保留原文件，重新 plan 并降低强度，或换不推高光的方向。", 6)
    # 安全缺陷优先于“变化是否足够”的产品门：同一输出既改得少又爆高光时，
    # 必须先告诉用户真正有破坏性的原因，避免误导他继续提高强度。
    # 采样与视频量化会产生极小浮点误差；只放宽不可感知的 1e-4，避免边界值被误判。
    if measured_for_minimum + 1e-4 < required:
        label = (
            "创意 Look 相对一级校正基线的感知变化"
            if impact_measurement["creative_delta_e_ok"] is not None
            else "原片到成片的总感知变化"
        )
        raise SkillError(
            f"视觉变化验收失败：{label} {measured_for_minimum:.4f} "
            f"低于当前强度下限 {required:.4f}；原片到成片总变化为 {mean_delta:.4f}。"
            + low_change_recovery(float(plan.get("strength", 0.0)), impact_measurement["basis"]),
            6)
    directional = None
    impact = None
    if plan.get("composition", {}).get("action") == "none":
        video = plan["source"]["media_type"] == "video"
        attention = plan.get("attention_map")
        direction_source = (
            creative_baseline
            if ((plan.get("style") or {}).get("execution_role") in {'foundation-only', 'person-protected-environment'}
                and creative_baseline is not None)
            else source
        )
        baseline_metrics = measure(direction_source, video, attention)
        output_metrics = measure(rendered, video, attention)
        directional = evaluate_direction(baseline_metrics, output_metrics, plan["style"]["visual_targets"])
        if protected_validation:
            signature = protected_validation['signature']
            prior, current = signature['baseline'], signature['candidate']
            directional['global_observation'] = copy.deepcopy(directional['checks'])
            directional['checks']['colorfulness'] = {
                'intent': 'preserve', 'scope': '绑定人物与近中性亮部相对 Foundation',
                'before': prior['protected_chroma'], 'after': current['protected_chroma'],
                'passed': current['protected_chroma'] >= prior['protected_chroma'] * .96}
            spatial_required = .018 * max(.3, min(1, plan['strength'] * 100 / 55))
            directional['checks']['subject_separation'] = {
                'intent': 'increase', 'scope': '绑定蒙版的环境与保护区冷偏差',
                'before': prior['spatial_cool_partition'], 'after': current['spatial_cool_partition'],
                'required_delta': spatial_required,
                'passed': current['spatial_cool_partition'] >= prior['spatial_cool_partition'] + spatial_required}
            directional['failed_dimensions'] = [name for name, check in directional['checks'].items()
                                                if not check['passed']]
            directional['status'] = 'failed' if directional['failed_dimensions'] else 'passed'
        confirmed = (plan.get("local_grade") or {}).get("confirmed")
        if confirmed and plan["source"]["media_type"] == "photo":
            import local_grade
            separation = local_grade.measure_inside_outside(
                source, rendered, Path(confirmed["mask_path"]),
                strategy=confirmed["strategy"],
            )
            apply_local_direction_evidence(
                directional, confirmed["strategy"], separation,
                plan["style"]["visual_targets"],
            )
        impact = impact_profile(baseline_metrics, output_metrics, plan["style"])
        semantic_claimed = (
            bool(plan.get("visual_brief", {}).get("semantic_analysis_claimed"))
            or bool(confirmed)
        )
        enforcement = directional_enforcement(semantic_claimed, directional["failed_dimensions"])
        directional.update(enforcement)
    sampling = acceptance_sampling(plan["source"]["media_type"], plan["source"]["duration"])
    frame_pixels = 96 * 64
    per_frame = []
    if plan["source"]["media_type"] == "video" and pixels >= frame_pixels:
        for index in range(0, pixels - frame_pixels + 1, frame_pixels):
            window = differences[index:index + frame_pixels]
            if window:
                per_frame.append(round(sum(window) / len(window), 6))
    grammar_report = visual_grammar_gates(plan, original, output, usable, profile,
                                          filtergraph, source, rendered)
    # 渲染完复诊自己的输出。上一轮四张照片被判「通过」，
    # 而它们的输出按本流程自己的判据是「黑位偏高，画面发灰」——
    # 因为从来没有人对输出跑过一遍诊断。
    tonal_health = None
    try:
        import diagnose as _diag
        # source / rendered 才是路径；original / output 是像素字节
        _src_tone = _diag.diagnose(source, use_semantic=False)["analysis"]["tone"]
        _out_tone = _diag.diagnose(rendered, use_semantic=False)["analysis"]["tone"]
        tonal_health = tonal_health_regression(_src_tone, _out_tone)
    except Exception as error:  # noqa: BLE001
        # 复诊失败要如实说，不能静默当作通过
        tonal_health = {"regressed": False, "lift_notable": False, "message": "",
                        "unavailable": True, "reason": f"输出复诊未能完成：{error}"}
    if tonal_health_should_block(tonal_health):
        raise SkillError(
            "影调健康验收失败：" + tonal_health["message"]
            + "正式成片不会发布；请重写该媒体配方，不要继续提高强度。", 6)
    if grammar_report["blocking_failures"]:
        names = "、".join(grammar_report["blocking_failures"])
        details = "；".join(
            f"{n}: {grammar_report['gates'][n].get('advice') or grammar_report['gates'][n]}"
            for n in grammar_report["blocking_failures"])
        raise SkillError(f"视觉语法验收失败（{names}）：{details}", 6)
    return {
        "status": visual_top_level_status(directional, grammar_report["gates"], tonal_health),
        **({'signature_regions': signature_validation} if signature_validation else {}),
        "metric": "sampled OKLab distance；只验证变化幅度与技术安全，不代表审美通过",
        "sampling": sampling,
        "coverage_note": (
            f"验收抽帧覆盖 {sampling['covered_seconds']}s / {plan['source']['duration']}s"
            f"（{sampling['coverage_ratio']:.0%}）"
            if plan["source"]["media_type"] == "video" else "照片全画面单帧"
        ),
        "per_frame_delta_e_ok": per_frame,
        "per_frame_min": round(min(per_frame), 6) if per_frame else None,
        "per_frame_max": round(max(per_frame), 6) if per_frame else None,
        "per_frame_spread": round(max(per_frame) - min(per_frame), 6) if per_frame else None,
        "mean_delta_e_ok": round(mean_delta, 6),
        "total_delta_e_ok": round(impact_measurement["total_delta_e_ok"], 6),
        "creative_delta_e_ok": (
            round(impact_measurement["creative_delta_e_ok"], 6)
            if impact_measurement["creative_delta_e_ok"] is not None else None
        ),
        "minimum_change_measurement_basis": impact_measurement["basis"],
        "minimum_change_measured_delta_e_ok": round(measured_for_minimum, 6),
        "required_min_delta_e": round(required, 6),
        "required_min_delta_e_source": minimum_key,
        "allowed_max_delta_e": allowed,
        "channel_low_clip_allowance": round(low_allowance, 6),
        "channel_high_clip_allowance": round(high_allowance, 6),
        "channel_low_clip_ratio": round(low_ratio, 6),
        "channel_high_clip_ratio": round(high_ratio, 6),
        "original_channel_low_clip_ratio": round(original_low_ratio, 6),
        "original_channel_high_clip_ratio": round(original_high_ratio, 6),
        "luma_crushed_ratio": round(luma_crushed_ratio, 6),
        "luma_blown_ratio": round(luma_blown_ratio, 6),
        "original_luma_crushed_ratio": round(original_luma_crushed, 6),
        "original_luma_blown_ratio": round(original_luma_blown, 6),
        "original_dark_risk_ratio": round(original_at_risk, 6),
        "original_bright_risk_ratio": round(original_bright_risk, 6),
        "tone_response_thresholds": thresholds,
        "declared_darkening_allowance": round(declared_darkening, 6),
        "crush_allowance": round(crush_allowance, 6),
        "blow_allowance": round(blow_allowance, 6),
        "clip_metric_note": "通道口径含纯饱和色，阈值宽；亮度口径只含真正丢细节的像素，阈值严。两者同时生效",
        "sampled_pixels": pixels,
        "directional_audit": directional,
        "impact_profile": impact,
        "visual_grammar": grammar_report,
        "tonal_health": tonal_health,
        **({'korean_protection': protected_validation} if protected_validation else {}),
    }



COMPARISON_FONT_CANDIDATES = (
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def comparison_label_font() -> Path | None:
    for candidate in COMPARISON_FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def comparison_label_spec(plan: dict) -> dict:
    """返回用户可直接读懂的对比标签与布局，不让人再靠回执猜左右。"""
    width = max(1, int(plan["source"].get("width") or 1))
    height = max(1, int(plan["source"].get("height") or 1))
    top_bottom = width >= height
    style = plan.get("style") or {}
    strength = int(round(float(plan.get("strength", 0.0)) * 100))
    original = "原片 / ORIGINAL"
    graded = (
        f"修复后 / CORRECTED | {style.get('name', style.get('id', 'BLCaptain'))} | 诊断驱动"
        if style.get("execution_role") == "foundation-only"
        else (
            f"调色后 / GRADED | {style.get('name', style.get('id', 'BLCaptain'))} | "
            f"{strength}%"
        )
    )
    return {
        "layout": "top-bottom" if top_bottom else "left-right",
        "original_position": "top" if top_bottom else "left",
        "graded_position": "bottom" if top_bottom else "right",
        "original_label": original,
        "graded_label": graded,
        "bar_height": max(36, min(320, int(round(height * 0.055)))),
        "font_size": max(18, min(128, int(round(height * 0.025)))),
        "font_path": str(comparison_label_font() or ""),
        "visible_in_media": True,
    }


def _drawtext_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _comparison_panel_filter(label: str, spec: dict, accent: str) -> str:
    font = spec["font_path"]
    font_option = f"fontfile='{_drawtext_escape(font)}':" if font else ""
    bar = int(spec["bar_height"])
    size = int(spec["font_size"])
    pad_x = max(12, size)
    return (
        f"setsar=1,pad=iw:ih+{bar}:0:{bar}:color=0x111318,"
        f"drawbox=x=0:y={bar - 3}:w=iw:h=3:color={accent}:t=fill,"
        f"drawtext={font_option}text='{_drawtext_escape(label)}':"
        f"expansion=none:fontcolor=white:fontsize={size}:x={pad_x}:y=({bar}-text_h)/2"
    )


def comparison_panel_filters(plan: dict) -> tuple[str, str, dict]:
    spec = comparison_label_spec(plan)
    return (
        _comparison_panel_filter(spec["original_label"], spec, "0x788291"),
        _comparison_panel_filter(spec["graded_label"], spec, "0x39b6a5"),
        spec,
    )


def comparison_stack(plan: dict) -> tuple[str, str]:
    """对比图的拼接方向与它的中文说明。

    一律左右并排会把横构图拉成极端比例：2000×1333 的源并排后是 4000×1333，
    接近 3:1，在手机上看两张都只剩一条。横构图上下叠、竖构图左右排，
    结果都落在接近常见画幅的比例里。

    布局要写进回执——不然用户拿到一张没有任何标注的拼图，
    得自己猜哪半是原片。
    """
    width = int(plan["source"].get("width") or 0)
    height = int(plan["source"].get("height") or 0)
    if width and height and width >= height:
        return "vstack", "上＝原片，下＝调色后（横构图上下叠，避免拉成极宽）"
    return "hstack", "左＝原片，右＝调色后（竖构图左右排）"


def render_photo(plan: dict, vf: str, baseline_vf: str | None = None) -> tuple[list[str], list[str], dict, dict, Path, Path]:
    ffmpeg = require_tool("ffmpeg")
    source = Path(plan["source"]["path"])
    output = Path(plan["output_path"])
    comparison = Path(plan["comparison_path"])
    temp_output, temp_comparison = partial_path(output), partial_path(comparison)
    temp_baseline = output.with_name(f"{output.stem}.foundation-baseline.partial{output.suffix}")
    if baseline_vf is not None and temp_baseline.exists():
        raise SkillError(f"基础校正暂存文件已存在，不会覆盖：{temp_baseline}", 3)
    tagged_filter = photo_output_filter(plan, vf)
    confirmed = (plan.get("local_grade") or {}).get("confirmed")
    if confirmed:
        # 语义局部（L2）：全局链照常算，但只在蒙版内施加，蒙版外保持原样。
        # 走 filter_complex 而不是 -vf，因为要吃两个输入（原片 + 蒙版）。
        import local_grade
        graph = local_grade.build_local_filter_complex(
            confirmed["strategy"], vf,
            int(plan["source"]["width"]), int(plan["source"]["height"]),
            float(confirmed["strength"]),
            photo_output_filter(plan, "") or "")
        output_cmd = [ffmpeg, "-v", "error", "-n", "-i", str(source),
                      "-i", confirmed["mask_path"], "-filter_complex", graph, "-frames:v", "1"]
    else:
        output_cmd = [ffmpeg, "-v", "error", "-n", "-i", str(source), "-frames:v", "1", "-vf", tagged_filter]
    output_cmd += ["-c:v", "png", "-pix_fmt", plan["color_pipeline"]["pixel_format"]]
    output_cmd.append(str(temp_output))
    if plan.get('korean_cool_protection'):
        import korean_cool_execution
        output_cmd, vf = korean_cool_execution.command(
            plan, baseline_vf, temp_output)
    if plan.get('signature_regions'):
        import signature_regions
        output_cmd, vf = signature_regions.command(plan, baseline_vf, temp_output)
    foundation_readiness_report = None
    try:
        if baseline_vf is not None:
            baseline_filter = photo_output_filter(plan, baseline_vf)
            if confirmed:
                import local_grade
                baseline_graph = local_grade.build_local_filter_complex(
                    confirmed["strategy"], baseline_vf,
                    int(plan["source"]["width"]), int(plan["source"]["height"]),
                    float(confirmed["strength"]),
                    photo_output_filter(plan, "") or "")
                baseline_cmd = [
                    ffmpeg, "-v", "error", "-n", "-i", str(source),
                    "-i", confirmed["mask_path"], "-filter_complex", baseline_graph,
                    "-frames:v", "1",
                ]
            else:
                baseline_cmd = [
                    ffmpeg, "-v", "error", "-n", "-i", str(source),
                    "-frames:v", "1", "-vf", baseline_filter,
                ]
            baseline_cmd += [
                "-c:v", "png", "-pix_fmt", plan["color_pipeline"]["pixel_format"],
                str(temp_baseline),
            ]
            if plan.get('korean_cool_protection'):
                baseline_cmd, _ = korean_cool_execution.command(
                    plan, baseline_vf, temp_baseline, strength=0.0)
            if plan.get('signature_regions'):
                baseline_cmd, _ = signature_regions.command(plan, baseline_vf, temp_baseline, strength=0.0)
            run(baseline_cmd)
            before_foundation = plan["source"].get("foundation_diagnosis")
            readiness_targets = (
                (plan.get("foundation_grade") or plan.get("primary_grade") or {})
                .get("readiness_targets") or {})
            if before_foundation and readiness_targets.get("required"):
                import diagnose as diagnose_module
                working = ("display-p3" if plan["source"]["color"]["profile"] == "display-p3"
                           else "srgb")
                composition = composition_filter(plan) if plan.get("composition") else ""
                if composition:
                    # Readiness 必须比较同一构图区域；否则裁切改变的色彩占比会被
                    # 错算成 Foundation 自己造成的增彩或褪色。
                    with tempfile.TemporaryDirectory(prefix="blcaptain-composition-baseline-") as folder:
                        composed_source = Path(folder) / "source.png"
                        run([
                            ffmpeg, "-v", "error", "-n", "-i", str(source),
                            "-frames:v", "1", "-vf", photo_output_filter(plan, composition),
                            "-c:v", "png", "-pix_fmt", plan["color_pipeline"]["pixel_format"],
                            str(composed_source),
                        ])
                        before_foundation = diagnose_module.diagnose(
                            composed_source, working, use_semantic=False)
                after_foundation = diagnose_module.diagnose(
                    temp_baseline, working, use_semantic=False)
                foundation_readiness_report = foundation_readiness(
                    before_foundation, after_foundation,
                    active_axes=readiness_targets.get("active_axes") or [])
                if foundation_readiness_report["status"] == "blocked":
                    names = "、".join(foundation_readiness_report["blocking_failures"])
                    raise SkillError(
                        f"Foundation Readiness 未通过（{names}），拒绝进入 Creative Look。"
                        "原文件未改动；请先修复基础校正，不要通过提高风格强度绕过。", 6)
            elif before_foundation:
                foundation_readiness_report = {
                    "status": "not-required",
                    "blocking_failures": [],
                    "reason": "素材诊断没有激活 Foundation 校正轴，不为制造变化而套基础校正",
                }
        run(output_cmd)
        color_validation = validate_photo_color(temp_output, plan)
        visual_validation = validate_visual_impact(
            source, temp_output, plan, vf,
            creative_baseline=temp_baseline if baseline_vf is not None else None)
        visual_validation["foundation_readiness"] = foundation_readiness_report or {
            "status": "not-run",
            "reason": "计划没有完整 Foundation 诊断证据",
        }
        if confirmed:
            # 局部策略必须在蒙版内外产生可分辨的差异，否则它没有真的发生。
            separation = local_grade.measure_inside_outside(
                source, temp_output, Path(confirmed["mask_path"]),
                strategy=confirmed["strategy"])
            visual_validation["local_grade"] = {
                **{k: v for k, v in confirmed.items() if k != "mask_path"},
                "mask_path": confirmed["mask_path"],
                "separation": separation,
                "passed": separation["separation_ratio"] >= 1.3,
                "boundary": "只验证局部确实生效，不代表蒙版边缘可信——"
                            "发丝、树枝、电线处的边缘必须由你亲自看过",
            }
            if not visual_validation["local_grade"]["passed"]:
                raise SkillError(
                    f"局部策略未产生可分辨的区内外差异（分离比 "
                    f"{separation['separation_ratio']}，需 ≥ 1.3）："
                    "要么蒙版覆盖了几乎整幅，要么强度太低。局部改动不成立，已回退。", 6)
    except Exception:
        temp_output.unlink(missing_ok=True)
        temp_baseline.unlink(missing_ok=True)
        raise
    temp_baseline.unlink(missing_ok=True)
    source_comp = composition_filter(plan)
    original_panel, graded_panel, _label_spec = comparison_panel_filters(plan)
    original_chain = f"{source_comp},{original_panel}" if source_comp else original_panel
    stack_mode, _stack_note = comparison_stack(plan)
    compare_cmd = [
        ffmpeg, "-v", "error", "-n", "-i", str(source), "-i", str(temp_output),
        "-filter_complex", f"[0:v]{original_chain}[a];[1:v]{graded_panel}[b];[a][b]{stack_mode}=inputs=2,{photo_output_filter(plan, '')}",
        "-frames:v", "1", "-c:v", "png", "-pix_fmt", plan["color_pipeline"]["pixel_format"], str(temp_comparison),
    ]
    run(compare_cmd)
    try:
        validate_photo_color(temp_comparison, plan)
    except SkillError:
        temp_output.unlink(missing_ok=True)
        temp_comparison.unlink(missing_ok=True)
        raise
    return (output_cmd, compare_cmd, color_validation, visual_validation,
            temp_output, temp_comparison)


def render_video(plan: dict, vf: str, baseline_vf: str | None = None) -> tuple[list[str], list[str], dict, dict, Path, Path]:
    ffmpeg = require_tool("ffmpeg")
    source = Path(plan["source"]["path"])
    output = Path(plan["output_path"])
    comparison = Path(plan["comparison_path"])
    temp_output, temp_comparison = partial_path(output), partial_path(comparison)
    temp_baseline = output.with_name(f"{output.stem}.creative-baseline.partial{output.suffix}")
    if baseline_vf is not None and temp_baseline.exists():
        raise SkillError(f"创意基线暂存文件已存在，不会覆盖：{temp_baseline}", 3)
    baseline_cmd = None
    if baseline_vf is not None:
        baseline_cmd = [
            ffmpeg, "-v", "error", "-n", "-i", str(source), "-map", "0:v:0", "-an",
            "-vf", baseline_vf + ",setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-movflags", "+faststart", str(temp_baseline),
        ]
    output_cmd = [
        ffmpeg, "-v", "error", "-n", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
        "-vf", vf + ",setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "copy", "-movflags", "+faststart", str(temp_output),
    ]
    if plan.get('korean_cool_protection'):
        import korean_cool_execution
        output_cmd, vf = korean_cool_execution.command(
            plan, baseline_vf, temp_output, include_audio=True)
        baseline_cmd, _ = korean_cool_execution.command(
            plan, baseline_vf, temp_baseline, strength=0.0)
    if plan.get('signature_regions'):
        import signature_regions
        output_cmd, vf = signature_regions.command(plan, baseline_vf, temp_output, include_audio=True)
        baseline_cmd, _ = signature_regions.command(plan, baseline_vf, temp_baseline, strength=0.0)
    foundation_readiness_report = None
    try:
        if baseline_cmd is not None:
            run(baseline_cmd)
            before_foundation = plan["source"].get("foundation_diagnosis")
            readiness_targets = (
                (plan.get("foundation_grade") or plan.get("primary_grade") or {})
                .get("readiness_targets") or {})
            if plan.get("shot_grade") is not None:
                shot_grade_plan = plan["shot_grade"]
                if shot_grade_plan.get("foundation_contract") == "per-shot-foundation-v2c":
                    import shot_grade as shot_grade_module
                    working = ("display-p3" if plan["source"]["color"]["profile"] == "display-p3"
                               else "srgb")
                    after_profiles = [
                        shot_grade_module.profile_shot(temp_baseline, shot, working)
                        for shot in shot_grade_plan["shots"]
                    ]
                    foundation_readiness_report = shot_grade_module.evaluate_foundation_readiness(
                        shot_grade_plan, after_profiles)
                    if foundation_readiness_report["status"] == "blocked":
                        names = "、".join(foundation_readiness_report["blocking_failures"])
                        raise SkillError(
                            f"逐镜头 Foundation Readiness 未通过（{names}），拒绝进入 Creative Look。"
                            "原文件未改动；请先修复镜头级基础校正，不要通过提高风格强度绕过。", 6)
                else:
                    foundation_readiness_report = {
                        "status": "not-applicable",
                        "blocking_failures": [],
                        "reason": "旧版逐镜头计划没有 v2-C Foundation 合同；请重新生成计划后验收",
                    }
            elif before_foundation and readiness_targets.get("required"):
                import diagnose as diagnose_module
                after_foundation = diagnose_module.diagnose(
                    temp_baseline, use_semantic=False)
                foundation_readiness_report = foundation_readiness(
                    before_foundation, after_foundation,
                    active_axes=readiness_targets.get("active_axes") or [])
                if foundation_readiness_report["status"] == "blocked":
                    names = "、".join(foundation_readiness_report["blocking_failures"])
                    raise SkillError(
                        f"Foundation Readiness 未通过（{names}），拒绝进入 Creative Look。"
                        "原文件未改动；请先修复基础校正，不要通过提高风格强度绕过。", 6)
            elif before_foundation:
                foundation_readiness_report = {
                    "status": "not-required",
                    "blocking_failures": [],
                    "reason": "素材诊断没有激活 Foundation 校正轴，不为制造变化而套基础校正",
                }
        run(output_cmd)
        rendered_probe = probe(temp_output)
        rendered_video = next(
            item for item in rendered_probe.get("streams", [])
            if item.get("codec_type") == "video")
        rendered_tags = {
            "matrix": rendered_video.get("color_space"),
            "transfer": rendered_video.get("color_transfer"),
            "primaries": rendered_video.get("color_primaries"),
        }
        if rendered_tags != {"matrix": "bt709", "transfer": "bt709", "primaries": "bt709"}:
            raise SkillError(f"输出 Rec.709 标签验收失败：{rendered_tags}", 6)
        visual_validation = validate_visual_impact(
            source, temp_output, plan, vf,
            creative_baseline=temp_baseline if baseline_vf is not None else None)
        visual_validation["foundation_readiness"] = foundation_readiness_report or {
            "status": "not-run",
            "reason": "计划没有整段 Foundation 诊断证据",
        }
    except Exception:
        temp_output.unlink(missing_ok=True)
        temp_baseline.unlink(missing_ok=True)
        raise
    temp_baseline.unlink(missing_ok=True)
    source_comp = composition_filter(plan)
    original_panel, graded_panel, _label_spec = comparison_panel_filters(plan)
    original_chain = f"{source_comp},{original_panel}" if source_comp else original_panel
    stack_mode, _stack_note = comparison_stack(plan)
    compare_cmd = [
        ffmpeg, "-v", "error", "-n", "-i", str(source), "-i", str(temp_output), "-t", "8",
        "-filter_complex", f"[0:v]{original_chain}[a];[1:v]{graded_panel}[b];[a][b]{stack_mode}=inputs=2",
        "-an", "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(temp_comparison),
    ]
    run(compare_cmd)
    return (output_cmd, compare_cmd,
            {"status": "passed", "expected": {"matrix": "bt709", "transfer": "bt709", "primaries": "bt709"}, "actual": rendered_tags},
            visual_validation, temp_output, temp_comparison)




# 「黑位偏高，画面发灰」是 diagnose 自己的判语（p01 > 0.04）。
# 这里不引入任何新阈值，只是把这套判据第一次用在**输出**上。
_GREY_VERDICT = "黑位偏高，画面发灰"
# 标签没跨界但抬了这么多倍，仍然要如实说出来：
# 实测风景那张 p01 0.0125 → 0.0374，观感是明显起雾，而标签还停在「黑位偏软」。
NOTABLE_BLACK_LIFT_RATIO = 2.5


def tonal_health_regression(source_tone: dict, output_tone: dict) -> dict:
    """渲染完，用同一套诊断复诊自己的输出。

    上一轮真实素材验收里，四张照片被判「通过」，而它们的输出用产品自己的判据
    读出来是「黑位偏高，画面发灰」——系统从不诊断输出，所以永远说不出这句话。

    只在**这次调色造成了退化**时报：源本来就发灰的不算在这次头上。
    """
    src_health = str(source_tone.get("black_point_health", ""))
    out_health = str(output_tone.get("black_point_health", ""))
    src_p01 = float(source_tone.get("p01") or 0.0)
    out_p01 = float(output_tone.get("p01") or 0.0)

    became_grey = out_health == _GREY_VERDICT and src_health != _GREY_VERDICT
    lift_notable = (src_p01 > 0.0
                    and out_p01 / max(src_p01, 1e-6) >= NOTABLE_BLACK_LIFT_RATIO
                    and out_p01 > src_p01)
    message = ""
    if became_grey:
        message = (f"调色把黑位从 {src_p01:.4f}（{src_health}）抬到 {out_p01:.4f}，"
                   f"按本流程自己的判据，输出现在是「{out_health}」——画面被雾化了。"
                   "降低强度，或换一套不压暗部的方案。")
    elif lift_notable:
        message = (f"黑位从 {src_p01:.4f} 抬到 {out_p01:.4f}（{out_p01/max(src_p01,1e-6):.1f} 倍）。"
                   f"判语仍是「{out_health}」，但暗部确实变浅了，请自己看一眼有没有起雾。")
    return {
        "source_p01": round(src_p01, 6), "source_health": src_health,
        "output_p01": round(out_p01, 6), "output_health": out_health,
        "regressed": became_grey,
        "lift_notable": became_grey or lift_notable,
        "message": message,
        "basis": "阈值沿用 diagnose.black_point_health（p01 > 0.04 判为发灰），不是本门新增的判据",
    }


def tonal_health_should_block(report: dict | None) -> bool:
    """只阻断本次调色新造成的发灰；显著抬黑但未跨诊断线仍交人工复核。"""
    return bool((report or {}).get("regressed"))


def directional_enforcement(semantic_claimed: bool, failed_dimensions: list[str]) -> dict:
    """让方向审计的 enforcement 与真实阻断行为保持一致。"""
    all_failures = sorted(set(failed_dimensions))
    should_block = len(all_failures) >= 2
    if should_block:
        enforcement = "blocking"
        reason = "至少两项可量化结果与配方自己声明的物理方向相反"
    elif all_failures:
        enforcement = "warning"
        reason = "一项配方声明方向未达成；保留正式结果并明确提示人工复核"
    else:
        enforcement = "review-only"
        reason = "所有已测物理方向均与配方声明一致；这不代表审美通过"
    return {
        "enforcement": enforcement,
        "enforcement_reason": reason,
        "should_block": should_block,
        "blocking_failures": all_failures if should_block else [],
    }


def render_result_state(visual_status: str, output_published: bool) -> dict:
    """生成单一顶层状态，并拒绝“已阻断但仍发布”的矛盾状态。"""
    allowed = {"passed", "passed_with_findings", "blocked"}
    if visual_status not in allowed:
        raise SkillError(f"未知渲染状态：{visual_status}", 6)
    if visual_status == "blocked" and output_published:
        raise SkillError("阻断结果不能发布正式成片；请删除临时文件并保持原文件不变。", 6)
    return {"status": visual_status, "output_published": bool(output_published)}


def aesthetic_state(status: str = "pending", *, explicit: bool = False,
                    reason: str | None = None) -> dict:
    """审美状态不从自动指标推导；human_* 必须来自显式人工输入。"""
    allowed = {"pending", "self_reviewed", "human_accepted", "human_rejected"}
    if status not in allowed:
        raise SkillError(f"未知审美状态：{status}", 6)
    if status.startswith("human_") and not explicit:
        raise SkillError("人工审美状态必须来自显式记录的人工输入。", 6)
    return {
        "status": status,
        "source": "explicit-human-input" if status.startswith("human_") else "not-evaluated",
        "reason": reason,
        "boundary": "自动技术指标不能把审美状态改为人工通过或拒绝。",
    }


def result_status_bundle(technical_status: str, output_published: bool = True) -> dict:
    return {
        "technical": render_result_state(technical_status, output_published),
        "aesthetic": aesthetic_state(),
    }


def receipt_verdict(failed: list, skipped: list, tonal_regressed: bool,
                    tonal_lift_notable: bool = False) -> str:
    """回执第一句。

    雾化了就不能以「通过」开头：上一轮四张照片全都以「通过」开头，
    而它们的输出按本流程自己的判据是发灰的。
    """
    if tonal_regressed:
        return "未通过影调健康复诊：输出被本流程自己的判据判为画面发灰"
    if tonal_lift_notable:
        return "通过，但暗部明显变浅，需要你确认是否起雾"
    if failed:
        return "候选已生成，但配方声明的部分方向未达成，尚未通过审美确认"
    if skipped:
        return "候选已生成，但有未测项目，尚未完成确认"
    return "通过"


def receipt_summary(plan: dict, visual: dict, palettes: dict | None,
                    output: Path, comparison: Path) -> dict:
    """回执最前面的一段人话。

    回执有 900 多行，其中色卡占六成、滤镜链是一条 900 字符的裸 ffmpeg 串。
    用户要的三件事——做了什么、效果怎样、有什么风险——散在里面找不到。
    这一段不新增任何判断，只把已有结论按人的读法排好。
    """
    style = plan["style"]
    strength = int(round(float(plan["strength"]) * 100))
    directional = visual.get("directional_audit") or {}
    failed = directional.get("failed_dimensions") or []
    grammar = visual.get("visual_grammar") or {}
    gates = grammar.get("gates") or {}
    skipped = [name for name, item in gates.items() if item.get("status") == "skipped"]
    local = visual.get("local_grade")

    did = [f"{style['name']}，强度 {strength}%"]
    if local:
        did.append(f"并在「{local['strategy_name']}」蒙版内做了局部调整")
    if plan.get("composition", {}).get("action") == "crop":
        did.append("按确认过的构图裁切")

    risks = []
    for item in (style.get("warnings") or [])[:2]:
        risks.append(item)
    if failed:
        names = {"tone_span": "影调跨度", "colorfulness": "综合色彩",
                 "subject_separation": "主体分离", "local_contrast": "局部对比",
                 "texture": "材质"}
        risks.append("方向审计未达成：" + "、".join(names.get(x, x) for x in failed)
                     + "——配方声明的方向没有在这张素材上实现，换素材或调强度可能更合适")
    if skipped:
        risks.append("以下门没能测（不等于通过）：" + "、".join(skipped))
    for note in (palettes or {}).get("source", {}).get("risks", [])[:1]:
        risks.append(note)

    tonal = visual.get("tonal_health") or {}
    if tonal.get("message"):
        risks.insert(0, tonal["message"])
    if tonal.get("unavailable"):
        risks.append(f"影调健康复诊没能跑（不等于通过）：{tonal.get('reason','')}")
    verdict = receipt_verdict(
        failed, skipped, bool(tonal.get("regressed")),
        bool(tonal.get("lift_notable")),
    )

    def swatches(key: str) -> list[str]:
        palette = (palettes or {}).get(key) or {}
        return [item.get("hex") for item in palette.get("colors", []) if item.get("hex")]

    strength_input = plan.get("strength_input") or {}
    shown_strength = strength_input.get("raw", plan["strength"])
    refine_command = (
        f"python3 scripts/blcaptain_color.py refine --input "
        f"{shlex.quote(plan['source']['path'])} --current-style "
        f"{shlex.quote(style['id'])} --feedback {shlex.quote('更多色彩')} "
        f"--strength {shlex.quote(str(shown_strength))}"
    )

    return {
        "做了什么": "；".join(did),
        "结果": f"{verdict}：感知变化 {visual.get('mean_delta_e_ok')}"
                f"（下限 {visual.get('required_min_delta_e')}，上限 {visual.get('allowed_max_delta_e')}）",
        "要你看的": risks or ["无"],
        "原图": plan["source"]["path"],
        "成品": str(output),
        "前后对比": str(comparison),
        "对比图布局": comparison_stack(plan)[1],
        "对比图标签": comparison_label_spec(plan),
        "色卡": {
            "原图": swatches("source"),
            "目标": swatches("target"),
            "结果": swatches("result"),
            "状态": "已生成" if palettes and palettes.get("available", True) else "未生成",
        },
        "继续修改": {
            "可选反馈": ["更多色彩", "更有氛围", "更有情绪", "更自然", "肤色回退", "暗部提亮"],
            "示例命令": refine_command,
            "说明": "refine 只生成新候选，仍需再次确认后才会渲染。",
        },
        # 原文是「自动指标只保证『确实改了且没改坏』」——这句话被真实素材证伪：
        # 四张照片技术门全过，而输出按本流程自己的判据是「黑位偏高，画面发灰」、
        # 肤色彩度腰斩。自动门从来没有检查过「有没有改坏」，不能这样承诺。
        "下一步": "打开对比图看一眼；不满意就换风格或调强度重新 plan。"
                  "自动门只检查了：变化幅度是否落在区间内、通道与亮度是否过度剪切、"
                  "输出黑位是否被抬到发灰、以及本次实际跑过的那几道视觉语法门。"
                  "它没有检查好不好看，也不保证没有改坏——"
                  "「要你看的」里列出的门就是它这次没能测的部分。",
    }


def format_cli_error(error: SkillError, args: argparse.Namespace) -> str:
    """把所有用户可见失败统一成可恢复、可复制的四段式提示。"""
    source = getattr(args, "input", None)
    if not source and getattr(args, "plan", None):
        try:
            payload = json.loads(Path(args.plan).expanduser().resolve().read_text(encoding="utf-8"))
            source = payload.get("source", {}).get("path")
        except (OSError, json.JSONDecodeError, TypeError):
            source = None
    if source:
        next_command = (
            "python3 scripts/blcaptain_color.py inspect --input "
            + shlex.quote(str(source))
        )
    else:
        command = getattr(args, "command", "") or "inspect"
        next_command = f"python3 scripts/blcaptain_color.py {command} --help"
    artifact_status = error.artifact_status or (
        "安全；原文件不会被覆盖，失败时本次临时成片、对比图和回执会清理。")
    recovery_step = error.recovery_step or (
        f"先运行 `{next_command}` 重新检查素材，再按新的计划确认。")
    return (
        "发生了什么：本次命令未完成，未发布正式结果。\n"
        f"为什么：{error}\n"
        f"原文件是否安全：{artifact_status}\n"
        f"下一步：{recovery_step}"
    )


def render_plan(args: argparse.Namespace) -> dict:
    try:
        plan = json.loads(Path(args.plan).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"无法读取计划：{error}", 3) from error
    if not args.confirm_plan:
        raise SkillError("尚未收到用户确认；请提供当前计划的 --confirm-plan。", 2)

    if args.confirm_plan != plan.get("plan_id"):
        raise SkillError("确认令牌与当前计划不匹配，拒绝渲染。", 2)
    try:
        if plan.get("schema_version") != "4.0.0":
            raise SkillError("计划版本已更新，请重新生成计划并再次确认。", 2)
        if plan_fingerprint(plan) != plan.get("plan_id"):
            raise SkillError("计划指纹不匹配：计划版本或内容已变化，请重新生成计划并再次确认。", 2)
        validate_parameters(plan["parameters"], "已确认计划")
        if plan.get("tone_curve"):
            validate_curve(plan["tone_curve"], "已确认计划")
        validate_hsl(plan.get("hsl_bands", []), "已确认计划")
        recipe = find_recipe(
            load_catalog(), plan["style"]["id"], plan["source"]["media_type"],
            allow_manual=True)
        recipe = recipe_for_media(recipe, plan["source"]["media_type"])
    except (KeyError, TypeError) as error:
        raise SkillError("计划结构无效，请重新生成并确认。", 2) from error
    expected_style = style_payload(recipe, plan["source"]["media_type"])
    if plan["style"] != expected_style:
        raise SkillError("计划风格信息与正式配方库不一致，请重新生成并确认。", 2)
    if recipe['id'] != 'korean-cool' and any(
            key in plan for key in ('korean_cool_protection', 'korean_execution_sha256')):
        raise SkillError("非韩系计划不能携带韩系保护协议，请重新生成并确认。", 2)
    import signature_regions as signature_module
    if recipe['id'] not in signature_module.ROLES and any(
            key in plan for key in ('signature_regions', 'signature_execution_sha256')):
        raise SkillError('非签名配方不能携带人工区域协议。', 2)
    expected_snapshot = recipe_execution_snapshot(
        recipe, plan.get("adjustments") or {})
    if any(plan.get(key) != value for key, value in expected_snapshot.items()):
        raise SkillError("计划调色参数与当前媒体配方不一致，请重新生成并确认。", 2)
    if plan.get("highlight_protection") != highlight_protection_for(plan["source"], recipe):
        raise SkillError("计划高光保护策略与素材分位或 Style Bible 不一致，请重新生成并确认。", 2)
    expected_effective, expected_mix, expected_pipeline = strength_execution_for(
        recipe, float(plan["strength"]))
    if plan["effective_strength"] != expected_effective:
        raise SkillError("计划感知强度与正式配方不一致，请重新生成并确认。", 2)
    if plan.get("render_mix") != expected_mix or plan.get("strength_pipeline") != expected_pipeline:
        raise SkillError("计划最终混合强度与用户选择不一致，请重新生成并确认。", 2)
    shot_grade_plan = plan.get("shot_grade")
    expected_truth = input_truth_for(
        plan["source"], bool(plan.get("assume_sdr", False)))
    if plan.get("input_truth") != expected_truth:
        raise SkillError("计划输入真相与素材标签或显式假设不一致，请重新生成并确认。", 2)
    if plan.get("foundation_grade") != plan.get("primary_grade"):
        raise SkillError("计划兼容一级校正与 Foundation 不一致，请重新生成并确认。", 2)
    if plan.get("foundation_hash") != foundation_hash(plan["foundation_grade"]):
        raise SkillError("计划 Foundation 指纹不匹配，请重新生成并确认。", 2)
    if shot_grade_plan is None:
        if plan["foundation_grade"] != foundation_grade(plan["source"]):
            raise SkillError("计划基础校正与素材诊断不一致，请重新生成并确认。", 2)
    else:
        if plan["source"]["media_type"] != "video":
            raise SkillError("逐镜头校正只适用于视频，请重新生成并确认。", 2)
        if plan["primary_grade"].get("mode") != "per-shot":
            raise SkillError("计划声明了逐镜头校正但基础校正字段不匹配，请重新生成并确认。", 2)
        if shot_grade_plan.get("needs_human_review"):
            raise SkillError("计划仍有未复核的镜头边界，拒绝渲染。", 2)
        timeline = shot_grade_plan.get("timeline_filter")
        import shot_grade as shot_grade_module
        rebuilt_timeline = shot_grade_module.build_timeline_filter(shot_grade_plan)
        # 半开区间 gte(t,a)*lt(t,b)：between 两端都闭，相邻段共享端点那一帧会被调色两次
        if not isinstance(timeline, str):
            raise SkillError("逐镜头时间轴滤镜无效，请重新生成并确认。", 2)
        if not timeline:
            reason = shot_grade_plan.get("timeline_noop_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise SkillError("空时间轴缺少可核验的 no-op 原因，请重新生成并确认。", 2)
        elif "enable='gte(t," not in timeline:
            raise SkillError("逐镜头时间轴滤镜无效，请重新生成并确认。", 2)
        # 结构不变量：任何参数段都不得跨过硬切点，否则就是跨镜头平滑。
        cuts = [item["time"] for item in shot_grade_plan.get("boundaries", [])
                if item.get("kind") != "dissolve"]
        # 窗口格式从 between(t,a,b) 改成半开的 gte(t,a)*lt(t,b) 时，
        # 这条正则必须跟着改——否则这道「不得跨硬切」的守卫会静默匹配不到任何东西，
        # 变成一道永远通过的空门。
        for start, end in re.findall(
                r"gte\(t,([0-9.]+)\)\*lt\(t,([0-9.]+)\)",
                timeline + "," + shot_grade_plan.get("trim_filter", "")):
            for cut in cuts:
                if float(start) < cut - 1e-6 and float(end) > cut + 1e-6:
                    raise SkillError(f"参数段 {start}~{end} 跨过硬切点 {cut}，拒绝渲染。", 2)
        if timeline != rebuilt_timeline:
            raise SkillError("逐镜头时间轴与一级校正参数不一致，请重新生成并确认。", 2)
    brief = plan.get("visual_brief")
    if not isinstance(brief, dict) or not {"subject", "story", "emotion", "viewing_path", "visual_hierarchy", "semantic_analysis_claimed", "source"} == set(brief):
        raise SkillError("计划视觉意图无效，请重新生成并确认。", 2)
    composition = plan.get("composition")
    if not isinstance(composition, dict) or composition.get("action") not in {"none", "crop", "crop_rotate"}:
        raise SkillError("计划构图动作无效，请重新生成并确认。", 2)
    try:
        expected_attention = attention_for(None) if plan.get("attention_map", {}).get("mode") == "none" else plan["attention_map"]
        if plan.get("attention_map") != expected_attention:
            raise SkillError("计划注意力方案无效，请重新生成并确认。", 2)
        attention_for_payload = plan["attention_map"]
        required_attention = {"mode", "center_x", "center_y", "radius", "strength", "rationale"}
        if set(attention_for_payload) != required_attention:
            raise SkillError("计划注意力方案无效，请重新生成并确认。", 2)
    except (AttributeError, TypeError):
        raise SkillError("计划注意力方案无效，请重新生成并确认。", 2)
    if plan["color_pipeline"] != color_pipeline_for(plan["source"]):
        raise SkillError("计划色彩处理管线与素材色域不一致，请重新生成并确认。", 2)
    if plan.get("capability_profile") != capability_profile_for(
            plan["source"]["media_type"], plan.get("shot_grade") is not None,
            plan.get("local_grade")):
        raise SkillError("计划能力声明与当前执行器不一致，请重新生成并确认。", 2)
    # 语义局部必须单独确认：它只作用于蒙版内，而蒙版边缘在发丝、树枝、
    # 电线处不可靠——那是必须由人看过才能签字的东西，不能随全局方案一起过。
    chosen_local = getattr(args, "confirm_local", None)
    if recipe['id'] in signature_module.ROLES:
        signature_module.confirm(plan, chosen_local)
    elif recipe['id'] == 'korean-cool':
        import korean_cool_execution
        korean_cool_execution.confirm(plan, chosen_local)
    elif chosen_local:
        pool = (plan.get("local_grade") or {}).get("candidates") or []
        match = next((c for c in pool if c["strategy"] == chosen_local), None)
        if match is None:
            names = "、".join(c["strategy"] for c in pool) or "（本计划没有任何局部候选）"
            raise SkillError(
                f"--confirm-local 的取值不在本计划的候选里：{chosen_local}。可选：{names}。"
                "候选来自 plan 的 local_grade.candidates；换素材或换配方后需要重新 plan。", 2)
        preflight = match.get("execution_preflight") or {}
        if preflight.get("status") != "executable":
            raise SkillError(
                f"局部策略 {chosen_local} 未通过计划预演："
                f"{preflight.get('reason', '没有可执行证据')}。原文件未改动。"
                "下一步：从 local_grade.recommended_candidates 选择，或重新 plan --detect-local。",
                4,
            )
        if not Path(match["mask_path"]).is_file():
            raise SkillError(
                f"蒙版文件不在了：{match['mask_path']}。请重新 plan --detect-local 生成蒙版。", 2)
        ensure_mask_integrity(match)
        plan["local_grade"]["confirmed"] = {**match, "strength": match["default_strength"]}
    source = Path(plan["source"]["path"])
    output = Path(plan["output_path"])
    comparison = Path(plan["comparison_path"])
    receipt = Path(plan["receipt_path"])
    ensure_new([output, comparison, receipt], source)
    before = sha256(source)
    if before != plan["source"]["sha256"]:
        raise SkillError("原文件在确认后发生变化，请重新生成并确认计划。", 2)
    vf = build_filter(
        plan["parameters"], plan.get("tone_curve"), plan.get("hsl_bands"),
        None if shot_grade_plan is not None else plan.get("primary_grade"),
        composition_filter(plan), plan["render_mix"], plan["attention_map"],
        timeline_primary=(shot_grade_plan or {}).get("timeline_filter", ""),
        timeline_trim=(shot_grade_plan or {}).get("trim_filter", ""),
        highlight_protection=plan.get("highlight_protection"),
    )
    baseline_vf = None
    baseline_vf = build_filter(
        plan["parameters"], plan.get("tone_curve"), plan.get("hsl_bands"),
        None if shot_grade_plan is not None else plan.get("primary_grade"),
        composition_filter(plan), 0.0, plan["attention_map"],
        timeline_primary=(shot_grade_plan or {}).get("timeline_filter", ""),
        timeline_trim=(shot_grade_plan or {}).get("trim_filter", ""),
        highlight_protection=plan.get("highlight_protection"),
    )
    if recipe['id'] == 'korean-cool':
        baseline_vf = korean_cool_execution.foundation_filter(plan)
        vf = korean_cool_execution.build_graph(plan, baseline_vf)
    if recipe['id'] in signature_module.ROLES:
        baseline_vf = signature_module.foundation_filter(plan)
        vf = signature_module.build_graph(plan, baseline_vf)
    if plan["source"]["media_type"] == "photo":
        (output_cmd, compare_cmd, color_validation, visual_validation,
         staged_output, staged_comparison) = render_photo(plan, vf, baseline_vf)
    else:
        (output_cmd, compare_cmd, color_validation, visual_validation,
         staged_output, staged_comparison) = render_video(plan, vf, baseline_vf)
    after = sha256(source)
    if after != before:
        cleanup_staged([staged_output, staged_comparison])
        raise SkillError("原文件指纹发生变化，验收失败。", 6)
    if recipe['id'] == 'korean-cool':
        try:
            korean_cool_execution.validate(plan)
        except Exception:
            cleanup_staged([staged_output, staged_comparison])
            raise
    if recipe['id'] in signature_module.ROLES:
        try:
            signature_module.validate(plan)
        except Exception:
            cleanup_staged([staged_output, staged_comparison])
            raise

    directional = visual_validation.get("directional_audit") or {}
    if directional.get("should_block"):
        preview = publish_directional_preview(
            plan, visual_validation, staged_output, staged_comparison)
        raise SkillError(
            "配方声明方向未达成（"
            + "、".join(directional.get("blocking_failures") or [])
            + "），已降级为预览候选而非正式成片。"
              f"预览：{preview['preview_output_path']}；"
              "请重新 plan，换一个视觉目标不同的方向。",
            6,
            artifact_status=(
                "安全；原文件未改动，正式成片与正式对比图均未发布；"
                f"仅保留带阻断回执的预览候选：{preview['preview_receipt_path']}"),
        )

    # 三色卡：原图 / 目标 / 结果。目标由滤镜链逐点变换得出（预测），
    # 结果由真实输出重新提取（事实），两者比对把预测变成可证伪的。
    palette_block = None
    if recipe['id'] == 'korean-cool':
        palette_block = {'available': False,
                         'reason': '本次人物保护随位置变化，不以全局色块预测冒充局部目标；请看真实预演和成片。'}
    elif not getattr(args, "no_palette", False):
        try:
            import palette as palette_module
            working = "display-p3" if plan["source"]["color"]["profile"] == "display-p3" else "srgb"
            # person_present 此前在这条路径上硬编码成 None，从不探测——
            # 结果一张纯天空的日落照也会拿到「存在肤色似然区间的颜色（合计面积 100%）；
            # 语义未启用」。那是几何启发在暖色相上的必然误报，而语义后端本来就能否掉它。
            # 真正的风险提示被这种噪声淹没，比不提示更糟。
            # 优先用 plan 里已探测的结果（--detect-local 时已经跑过），避免重复调用后端。
            person_present = None
            detected = (plan.get("local_grade") or {}).get("candidates")
            if detected is not None:
                person_present = any(c["class"] == "person" for c in detected)
            elif plan["source"]["media_type"] == "photo":
                try:
                    import semantic_backend
                    if semantic_backend.person_classes_available():
                        payload = semantic_backend.resolve_backend().analyze(source)
                        if hasattr(payload, "__dict__"):
                            payload = payload.__dict__
                        person_present = semantic_backend.class_presence(payload, "person")
                except Exception:  # noqa: BLE001
                    person_present = None
            source_palette = palette_module.extract_from_media(
                source, working, int(plan["source"]["width"]), int(plan["source"]["height"]),
                media_type=plan["source"]["media_type"], label="原图色卡",
                person_present=person_present,
            )
            target_palette = palette_module.predict_target_palette(
                source_palette, vf, working, person_present
            )
            result_palette = palette_module.extract_from_media(
                staged_output, working, int(plan["source"]["width"]), int(plan["source"]["height"]),
                media_type=plan["source"]["media_type"], label="结果色卡",
                person_present=person_present,
            )
            palette_block = {
                "source": source_palette,
                "target": target_palette,
                "result": result_palette,
                "target_vs_result_by_nearest": palette_module.compare_palettes(
                    target_palette, result_palette),
                "target_prediction_verification": (
                    palette_module.verify_target_by_regions(
                        source, staged_output, source_palette, target_palette, working,
                        int(plan["source"]["width"]), int(plan["source"]["height"]),
                    ) if plan["composition"]["action"] == "none"
                    else {"available": False, "reason": "执行了构图裁切，源与输出不再逐点对应"}
                ),
                "boundary": "色卡描述颜色构成与面积；它不是 LUT，也不代表审美通过",
            }
            if plan["source"]["media_type"] == "video" and plan.get("shot_grade"):
                palette_block["video"] = palette_module.video_palettes(
                    staged_output, working, int(plan["source"]["width"]), int(plan["source"]["height"]),
                    plan["shot_grade"]["shots"], person_present=person_present,
                )
        except Exception as error:  # noqa: BLE001
            # 色卡失败不能连累已经完成且验收通过的渲染；但必须如实记录原因。
            palette_block = {"available": False, "reason": f"{type(error).__name__}: {error}"}
    result = {
        "schema_version": "4.0.0",
        **render_result_state(visual_validation["status"], output_published=True),
        **result_status_bundle(visual_validation["status"], output_published=True),
        # 摘要放最前面。回执 900 多行，用户要的三件事不该埋在第 400 行。
        "summary": receipt_summary(plan, visual_validation, palette_block, output, comparison),
        "plan_id": plan["plan_id"],
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "original_path": str(source),
        "output_path": str(output),
        "comparison_path": str(comparison),
        "comparison_labels": comparison_label_spec(plan),
        "receipt_path": str(receipt),
        "original_sha256": before,
        "output_sha256": sha256(staged_output),
        "original_unchanged": True,
        "audio_preserved": bool(plan["source"]["has_audio"] and plan["source"]["media_type"] == "video"),
        "style": plan["style"],
        "strength": plan["strength"],
        "effective_strength": plan["effective_strength"],
        "render_mix": plan["render_mix"],
        "visual_brief": plan["visual_brief"],
        "composition": plan["composition"],
        "attention_map": plan["attention_map"],
        "primary_grade": plan["primary_grade"],
        "input_truth": plan["input_truth"],
        "foundation_grade": plan["foundation_grade"],
        "foundation_hash": plan["foundation_hash"],
        "shot_grade": (
            {
                "shot_count": shot_grade_plan["shot_count"],
                "anchor": shot_grade_plan["anchor"],
                "shots": shot_grade_plan["shots"],
                "primaries": shot_grade_plan["primaries"],
                "dissolve_ramps": shot_grade_plan["dissolve_ramps"],
                "trims": shot_grade_plan.get("trims"),
                "guarantees": shot_grade_plan["guarantees"],
            } if shot_grade_plan else None
        ),
        "tone_curve": plan.get("tone_curve"),
        "hsl_bands": plan.get("hsl_bands", []),
        "highlight_protection": plan.get("highlight_protection"),
        "adjustments": plan.get("adjustments", {}),
        "color_pipeline": plan["color_pipeline"],
        "capability_profile": plan["capability_profile"],
        "color_validation": color_validation,
        "visual_validation": visual_validation,
        "palettes": palette_block,
        "filtergraph": vf,
        "commands": [output_cmd, compare_cmd],
    }
    if recipe['id'] == 'korean-cool':
        result['korean_cool_protection'] = plan['korean_cool_protection']
        result['korean_execution_sha256'] = plan['korean_execution_sha256']
        result['local_confirmation'] = chosen_local
    if recipe['id'] in signature_module.ROLES:
        result['signature_regions'] = plan['signature_regions']
        result['signature_execution_sha256'] = plan['signature_execution_sha256']
        result['local_confirmation'] = chosen_local
    staged_receipt = partial_path(receipt)
    try:
        staged_receipt.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        publish_artifact_group([
            (staged_output, output),
            (staged_comparison, comparison),
            (staged_receipt, receipt),
        ])
    except Exception:  # noqa: BLE001
        cleanup_staged([staged_output, staged_comparison, staged_receipt])
        raise
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    experiment = commands.add_parser('experiment-plan', help='显式实验视频计划；不开放普通研究门')
    experiment.add_argument('--input', required=True)
    experiment.add_argument('--style', choices=['film-soft', 'warm-cozy', 'food-vivid', 'cream-soft', 'sunset-warm', 'landscape-crisp', 'korean-cool', 'teal-orange', 'cinematic-muted', 'blue-hour', 'rainy-blue-green', 'cool-gray-sea', 'forest-cyan', 'captain-deep-sea'], required=True)
    experiment.add_argument('--strength', type=float, default=55)
    experiment.add_argument('--output-dir', required=True)
    experiment.add_argument('--plan-out', required=True)
    experimental_render = commands.add_parser('experiment-render', help='确认并执行受限实验视频方案')
    experimental_render.add_argument('--plan', required=True)
    experimental_render.add_argument('--confirm-plan', required=True)
    validate = commands.add_parser("validate-catalog")
    validate.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    listing = commands.add_parser("list-styles")
    listing.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    listing.add_argument("--media-type", choices=["photo", "video"])
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--input", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--input", required=True)
    plan.add_argument("--style", required=True)
    plan.add_argument("--strength", default="0.55")
    plan.add_argument("--review-focus", default="整体观感")
    plan.add_argument("--output-dir", required=True)
    plan.add_argument("--plan-out")
    plan.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    plan.add_argument("--assume-sdr", action="store_true")
    plan.add_argument("--variant")
    plan.add_argument("--adjustments-file")
    plan.add_argument("--visual-brief-file")
    plan.add_argument("--composition-file")
    plan.add_argument("--attention-file")
    plan.add_argument('--signature-regions', help='六套签名的本次人工区域/逐帧区间 JSON；不是自动语义识别')
    plan.add_argument("--detect-local", action="store_true",
                      help="探测语义类别并给出候选局部策略（L2）。约需 1-2 秒；"
                           "探测出的策略仍需在 render 时用 --confirm-local 单独确认")
    plan.add_argument("--shot-grade", action="store_true",
                      help="视频专用：先做镜头检测与逐镜头一级校正，再叠加创意风格")
    plan.add_argument("--match-strength", type=float, default=1.0)
    plan.add_argument("--anchor", type=int, help="指定锚点镜头序号；不指定则按技术健康度自动选取")
    plan.add_argument("--confirm-shot-boundaries", action="store_true",
                      help="确认 shots 中待复核的候选边界按当前检测使用；不等于选择锚点")
    render = commands.add_parser("render")
    render.add_argument("--plan", required=True)
    render.add_argument("--confirm-plan")
    render.add_argument("--confirm-local",
                        help="单独确认执行某个语义局部策略（L2）。取值来自 plan 的 "
                             "local_grade.candidates[].strategy；局部改动不随全局方案一起生效，"
                             "必须由用户看过蒙版边缘后单独确认")
    render.add_argument("--no-palette", action="store_true",
                        help="跳过原图／目标／结果三色卡提取（默认提取）")

    export_lut = commands.add_parser("export-lut", help="从已确认计划导出全局色彩链 .cube LUT")
    export_lut.add_argument("--plan", required=True)
    export_lut.add_argument("--confirm-plan", required=True)
    export_lut.add_argument("--output", required=True)
    export_lut.add_argument("--size", type=int, default=33, choices=[17, 33, 65])
    export_lut.add_argument("--calibration-manifest",
                            help="可选：用不少于 40 张许可照片做 28/12 标定与留出精度报告")

    # v4 子命令。全部为只读分析或计划产出，不渲染文件、不绕过确认门。
    # 它们按需惰性导入，保证核心链路在没有任何可选后端时依然可用。
    suggest = commands.add_parser("suggest", help="智能推荐／灵感组合／全部风格 + 配色色卡")
    suggest.add_argument("--input", required=True)
    suggest.add_argument("--mode", default="smart", choices=["smart", "inspire", "all"])
    suggest.add_argument("--count", type=int, default=3)
    suggest.add_argument("--seed", type=int)
    suggest.add_argument("--strength", default="0.55")
    suggest.add_argument("--no-semantic", action="store_true")
    suggest.add_argument("--user-confirmed-center", action="store_true")
    suggest.add_argument("--display-only", action="store_true")
    suggest.add_argument("--ledger", help="按素材内容哈希读取反馈账本；默认 ~/.blcaptain/feedback-ledger.json")
    suggest.add_argument("--avoid", help="仅本次推荐避开的方向，逗号分隔；不写入画像")

    refine = commands.add_parser("refine", help="根据用户反馈生成可确认的新方向")
    refine.add_argument("--input", required=True)
    refine.add_argument("--current-style", required=True)
    refine.add_argument("--feedback", required=True)
    refine.add_argument("--strength", default="0.55")
    refine.add_argument("--count", type=int, default=3)
    refine.add_argument("--consecutive-failures", type=int, default=0)
    refine.add_argument("--no-semantic", action="store_true")
    refine.add_argument("--ledger", help="按素材内容哈希读取反馈账本；默认 ~/.blcaptain/feedback-ledger.json")

    feedback = commands.add_parser("record-feedback", help="显式记录某素材对某配方强度的审美接受或否决")
    feedback.add_argument("--input", required=True)
    feedback.add_argument("--style", required=True)
    feedback.add_argument("--strength", required=True)
    feedback.add_argument("--verdict", required=True, choices=["accepted", "rejected"])
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--ledger")

    clear_feedback = commands.add_parser("clear-feedback", help="清空反馈账本；不改配方库和素材")
    clear_feedback.add_argument("--ledger")

    result_status = commands.add_parser(
        "result-status", help="只读聚合某次成片回执的最新人工状态；不回写回执")
    result_status.add_argument("--receipt", required=True)
    result_status.add_argument("--ledger")

    palette = commands.add_parser("palette", help="从真实像素提取配色色卡")
    palette.add_argument("--input", required=True)
    palette.add_argument("--label", default="原图色卡")
    palette.add_argument("--max-colors", type=int, default=7)
    palette.add_argument("--at-time", type=float)
    palette.add_argument("--svg-out")
    palette.add_argument("--png-out")
    palette.add_argument("--per-shot", action="store_true",
                         help="视频专用：输出逐镜头色卡与全片共享色卡")

    diagnose_cmd = commands.add_parser("diagnose", help="真实像素诊断与视觉简报")
    diagnose_cmd.add_argument("--input", required=True)
    diagnose_cmd.add_argument("--no-semantic", action="store_true")
    diagnose_cmd.add_argument("--mask-dir")

    shots_cmd = commands.add_parser("shots", help="镜头检测与转场分类")
    shots_cmd.add_argument("--input", required=True)
    shots_cmd.add_argument("--min-shot-seconds", type=float, default=0.6)

    shot_grade_cmd = commands.add_parser("shot-grade", help="逐镜头一级校正与镜头匹配计划")
    shot_grade_cmd.add_argument("--input", required=True)
    shot_grade_cmd.add_argument("--match-strength", type=float, default=1.0)
    shot_grade_cmd.add_argument("--anchor", type=int)

    commands.add_parser("capabilities", help="语义后端能力探测与逐类状态")

    # failure_detect 此前只有自己的 argparse 入口，不在主 CLI 的子命令表里，
    # 等于用户和代理都看不见它。工具存在但没人能用，与不存在没有区别。
    detect_cmd = commands.add_parser(
        "detect-failures", help="失败模式自动检测（11 条可自动化，7 条留人工）")
    detect_cmd.add_argument("--source", required=True)
    detect_cmd.add_argument("--rendered", required=True)
    detect_cmd.add_argument("--declared", help="配方声明的取舍 JSON")
    return root


def _silence_model_library_chatter() -> None:
    """把模型库的无关提示挡在 CLI 之外。

    huggingface_hub 在联网校验缓存时会用 WARNING 打一句「建议设置 HF_TOKEN」，
    它会抢占 stderr 的第一行——用户看到的第一句话变成一个与他无关的 token 建议，
    真正的错误被挤到下面。这个 Skill 用的是公开权重，不需要 token。
    在入口统一静音，覆盖所有子命令路径；只压提示级别，不吞异常。
    """
    import logging

    prefixes = ("huggingface_hub", "transformers", "torch", "filelock", "urllib3", "httpx")

    class _DropChatter(logging.Filter):
        """按内容丢弃与用户无关的提示。

        只设 setLevel 不够：huggingface_hub 的子 logger 自己设了级别，
        父级的 ERROR 拦不住它。这里用 filter 精确丢掉那一句 token 建议，
        其余记录一律放行——不做无差别静音，异常与真错误必须能出来。
        """

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                text = record.getMessage()
            except Exception:  # noqa: BLE001
                return True
            return "HF_TOKEN" not in text and "unauthenticated requests" not in text

    chatter = _DropChatter()
    for name in prefixes:
        logging.getLogger(name).setLevel(logging.ERROR)
    # 已注册的子 logger 也要覆盖，它们可能自己设过级别。
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(prefixes):
            logger = logging.getLogger(name)
            logger.setLevel(logging.ERROR)
            logger.addFilter(chatter)
    logging.getLogger().addFilter(chatter)


def main() -> int:
    _silence_model_library_chatter()
    args = parser().parse_args()
    try:
        if args.command == 'experiment-plan':
            import experimental_video
            plan_path = Path(args.plan_out).expanduser().resolve()
            if plan_path.is_relative_to(Path(args.output_dir).expanduser().resolve()):
                raise SkillError('计划文件必须位于实验输出目录之外，避免保存计划后无法渲染', 3)
            plan = experimental_video.make_plan(args.input, args.style, args.strength, args.output_dir)
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            with plan_path.open('x', encoding='utf-8') as handle:
                json.dump(plan, handle, ensure_ascii=False, indent=2)
            emit(plan)
        elif args.command == 'experiment-render':
            import experimental_video
            plan = json.loads(Path(args.plan).expanduser().read_text(encoding='utf-8'))
            emit(experimental_video.execute(plan, args.confirm_plan))
        elif args.command == "validate-catalog":
            catalog = load_catalog(Path(args.catalog))
            emit({"valid": True, "recipe_count": len(catalog["recipes"]), "media_types": sorted({kind for item in catalog["recipes"] for kind in item["media_types"]})})
        elif args.command == "list-styles":
            catalog = load_catalog(Path(args.catalog))
            styles = []
            for item in catalog["recipes"]:
                available = executable_media(item)
                if not available or (args.media_type and args.media_type not in available):
                    continue
                style = {key: item[key] for key in ["id", "name", "aliases", "media_types", "scenes", "summary", "warnings"]}
                style["collection"] = item.get("collection", "Core")
                style["media_types"] = [args.media_type] if args.media_type else available
                styles.append(style)
            emit({"styles": styles})
        elif args.command == "inspect":
            emit(inspect_media(args.input))
        elif args.command == "plan":
            emit(make_plan(args))
        elif args.command == "render":
            emit(render_plan(args))
        elif args.command == "export-lut":
            import lut_export
            plan_path = Path(args.plan).expanduser().resolve()
            try:
                lut_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SkillError(f"无法读取计划：{error}", 2) from error
            if args.confirm_plan != lut_plan.get("plan_id"):
                raise SkillError("确认编号与计划不一致；不会导出 LUT。", 3)
            if plan_fingerprint(lut_plan) != lut_plan.get("plan_id"):
                raise SkillError("计划版本或内容已变化，请重新生成计划后再导出 LUT。", 3)
            # 逐镜头计划的一级校正随时间轴逐段变化，单个全局 LUT 表达不了它；
            # 该结构性限制应先于目录漂移检查，避免把明确的不支持误报成配方变化。
            if (lut_plan.get("shot_grade") is not None
                    or (lut_plan.get("primary_grade") or {}).get("mode") == "per-shot"):
                raise SkillError(
                    "该计划使用逐镜头一级校正，参数随时间轴逐段变化，"
                    "无法烘焙为单个全局 .cube LUT。LUT 导出目前只支持"
                    "非逐镜头计划（照片或整段统一校正的视频）。", 3)
            recipe = find_recipe(
                load_catalog(), lut_plan["style"]["id"], lut_plan["source"]["media_type"],
                allow_manual=True,
            )
            recipe = recipe_for_media(recipe, lut_plan["source"]["media_type"])
            if lut_plan["style"] != style_payload(recipe, lut_plan["source"]["media_type"]):
                raise SkillError("当前媒体准入或配方信息已变化，请重新制定并确认计划后导出 LUT。", 3)
            source = Path(lut_plan["source"]["path"])
            if sha256(source) != lut_plan["source"]["sha256"]:
                raise SkillError("原文件在确认后发生变化，请重新生成并确认计划。", 2)
            output = Path(args.output).expanduser().resolve()
            if output.suffix.lower() != ".cube":
                raise SkillError("LUT 输出必须使用 .cube 后缀。", 3)
            try:
                emit(lut_export.export(
                    sys.modules[__name__], lut_plan, output, args.size,
                    Path(args.calibration_manifest).expanduser().resolve()
                    if args.calibration_manifest else None,
                ))
            except (ValueError, FileExistsError, RuntimeError) as error:
                raise SkillError(str(error), 3) from error
        elif args.command == "suggest":
            import suggest as suggest_module
            payload = suggest_module.build(
                Path(args.input).expanduser().resolve(), args.mode, args.count, args.seed,
                args.strength, use_semantic=not args.no_semantic,
                user_confirmed_center=args.user_confirmed_center,
                ledger_path=Path(args.ledger).expanduser().resolve() if args.ledger else None,
                avoid=args.avoid,
            )
            emit(payload["display"] if args.display_only else payload)
        elif args.command == "refine":
            import suggest as suggest_module
            emit(suggest_module.refine(
                Path(args.input).expanduser().resolve(), args.current_style, args.feedback,
                args.strength, args.count, use_semantic=not args.no_semantic,
                consecutive_failures=max(0, args.consecutive_failures),
                ledger_path=Path(args.ledger).expanduser().resolve() if args.ledger else None,
            ))
        elif args.command == "record-feedback":
            import feedback_ledger
            path = Path(args.input).expanduser().resolve()
            source = inspect_media(path)
            recipe = find_recipe(
                load_catalog(), args.style, source["media_type"], allow_manual=True)
            strength = normalize_strength(args.strength)["normalized"]
            try:
                result = feedback_ledger.record(
                    Path(args.ledger).expanduser().resolve() if args.ledger else None,
                    path, recipe["id"], strength, args.verdict, args.reason,
                )
            except ValueError as error:
                raise SkillError(str(error), 3) from error
            result["aesthetic"] = aesthetic_state(
                "human_accepted" if args.verdict == "accepted" else "human_rejected",
                explicit=True, reason=args.reason,
            )
            emit(result)
        elif args.command == "clear-feedback":
            import feedback_ledger
            emit(feedback_ledger.clear(
                Path(args.ledger).expanduser().resolve() if args.ledger else None
            ))
        elif args.command == "result-status":
            # 只读聚合：回执永远记录「生成当时」的状态（D7 禁止回写历史回执），
            # 渲染之后的人工结论只存在于反馈账本。这里把两者并排给出，
            # 且只匹配同源哈希＋配方＋强度，不把单素材结论外推到别处。
            import feedback_ledger
            receipt_path = Path(args.receipt).expanduser().resolve()
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SkillError(f"无法读取回执：{error}", 2) from error
            required = {"plan_id", "style", "strength", "original_sha256", "aesthetic"}
            if not isinstance(receipt, dict) or not required <= set(receipt) \
                    or not isinstance(receipt.get("style"), dict) \
                    or "id" not in receipt["style"]:
                raise SkillError("回执结构缺少聚合所需字段，无法查询人工状态。", 2)
            state = feedback_ledger.load(
                Path(args.ledger).expanduser().resolve() if args.ledger else None)
            source_sha = receipt["original_sha256"]
            recipe_id = receipt["style"]["id"]
            strength = round(float(receipt["strength"]), 6)
            scope = {"source_sha256": source_sha, "recipe_id": recipe_id,
                     "strength": strength}
            hits = [
                entry for entry in state.get("entries", [])
                if entry.get("source_sha256") == source_sha
                and entry.get("recipe_id") == recipe_id
                and abs(float(entry.get("strength", -1)) - strength) < 1e-9
            ]
            latest = hits[-1] if hits else None
            emit({
                "schema_version": "1.0.0",
                "receipt": str(receipt_path),
                "plan_id": receipt.get("plan_id"),
                "technical_at_render_time": receipt.get("technical"),
                "aesthetic_at_render_time": receipt.get("aesthetic"),
                "latest_human_state": (
                    {
                        "status": ("human_accepted"
                                   if latest["verdict"] == "accepted"
                                   else "human_rejected"),
                        "source": "feedback-ledger",
                        "recorded_at": latest["recorded_at"],
                        "reason": latest["reason"],
                        "scope": scope,
                    }
                    if latest else
                    {"status": "pending", "source": "no-explicit-feedback",
                     "scope": scope}
                ),
                "receipt_unchanged": True,
                "ledger": state["path"],
                "ledger_warning": state["warning"],
                "boundary": (
                    "只读聚合：回执记录生成当时的状态，永不回写；最新人工状态"
                    "仅来自显式 record-feedback 账本，且只绑定同源哈希＋配方＋强度，"
                    "不外推到其他素材、其他强度或其他配方。"
                ),
            })
        elif args.command == "palette":
            import palette as palette_module
            path = Path(args.input).expanduser().resolve()
            source = inspect_media(path)
            profile = "display-p3" if source["color"]["profile"] == "display-p3" else "srgb"
            person_present = None
            try:
                import semantic_backend
                backend = semantic_backend.resolve_backend()
                if semantic_backend.person_classes_available() and source["media_type"] == "photo":
                    payload = backend.analyze(path)
                    person_present = semantic_backend.class_presence(payload, "person")
            except Exception:  # noqa: BLE001
                person_present = None
            result = palette_module.extract_from_media(
                path, profile, int(source["width"]), int(source["height"]),
                media_type=source["media_type"], at_time=args.at_time, label=args.label,
                max_colors=max(3, min(7, args.max_colors)), person_present=person_present,
            )
            if args.per_shot:
                if source["media_type"] != "video":
                    raise SkillError("逐镜头色卡只适用于视频", 3)
                import shots as shots_module
                detection = shots_module.detect(path)
                result["video"] = palette_module.video_palettes(
                    path, profile, int(source["width"]), int(source["height"]),
                    detection["shots"], person_present=person_present,
                )
            if args.png_out:
                result["png"] = palette_module.palette_png(
                    result, Path(args.png_out).expanduser().resolve(), profile
                )
            if args.svg_out:
                svg_path = Path(args.svg_out).expanduser().resolve()
                if svg_path.exists():
                    raise SkillError(f"色卡文件已存在，不会覆盖：{svg_path}", 3)
                svg_path.parent.mkdir(parents=True, exist_ok=True)
                svg_path.write_text(palette_module.palette_svg(result, args.label), encoding="utf-8")
                result["svg_path"] = str(svg_path)
            emit(result)
        elif args.command == "diagnose":
            import diagnose as diagnose_module
            path = Path(args.input).expanduser().resolve()
            source = inspect_media(path)
            profile = "display-p3" if source["color"]["profile"] == "display-p3" else "srgb"
            emit(diagnose_module.diagnose(
                path, profile, use_semantic=not args.no_semantic,
                mask_dir=Path(args.mask_dir).expanduser().resolve() if args.mask_dir else None,
            ))
        elif args.command == "shots":
            import shots as shots_module
            emit(shots_module.detect(Path(args.input).expanduser().resolve(), args.min_shot_seconds))
        elif args.command == "shot-grade":
            import shot_grade as shot_grade_module
            path = Path(args.input).expanduser().resolve()
            source = inspect_media(path)
            profile = "display-p3" if source["color"]["profile"] == "display-p3" else "srgb"
            emit(shot_grade_module.plan_shot_grade(
                path, profile, max(0.0, min(1.0, args.match_strength)), args.anchor,
            ))
        elif args.command == "capabilities":
            import semantic_backend
            emit(semantic_backend.capability_report())
        elif args.command == "detect-failures":
            import failure_detect
            declared = (json.loads(Path(args.declared).read_text(encoding="utf-8"))
                        if args.declared else {})
            report = failure_detect.detect(
                Path(args.source).expanduser().resolve(),
                Path(args.rendered).expanduser().resolve(), declared)
            emit(report)
            # 检出高危失败模式时退 6，与渲染验收的拒绝码一致。
            return 0 if report["passes"] else 6
        return 0
    except SkillError as error:
        print(format_cli_error(error, args), file=sys.stderr)
        return error.code
    except Exception as error:  # noqa: BLE001
        # v4 子命令的失败必须显式报错，不得静默降级成「看起来成功」。
        print(f"命令执行失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
