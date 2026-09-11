#!/usr/bin/env python3
"""为52个研究风格生成三类未标定视频指导种子与12个视频评测场景。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CANDIDATES = ROOT / "research_candidates.json"
OUTPUT = ROOT / "video_guidance_seeds.json"
SCENARIOS = ROOT / "evaluation_scenarios.json"
PLATFORMS = {
    "capcut_video": "剪映",
    "premiere_video": "Premiere",
    "davinci_resolve_video": "DaVinci",
}
FACTORS = {"conservative": 0.65, "standard": 1.0, "bold": 1.25}
BASE_TEMPORAL_POLICY = [
    "先统一输入色彩空间并逐镜头校正曝光与白平衡",
    "禁止逐帧自动变化风格参数，必要时只在镜头切点使用关键帧",
    "局部选区必须跟踪并检查边缘漂移",
    "降噪先于锐化，颗粒放在降噪之后",
    "回放检查闪烁、色带、压缩噪点和镜头间跳色",
    "导出前核对帧率、时长、音频、码率与色彩标签",
]


def clamp(name: str, value: float) -> float:
    limit = 2.0 if name.endswith("_ev") or name == "exposure" else 100.0
    return round(max(-limit, min(limit, value)), 2)


def scaled(values: dict[str, float], factor: float) -> dict[str, float]:
    return {name: clamp(name, float(value) * factor) for name, value in values.items()}


def map_parameters(platform: str, intent: dict[str, float]) -> dict[str, float]:
    ev = float(intent["exposure_ev"])
    if platform == "capcut_video":
        return {
            "exposure": ev,
            "brightness_delta": ev * 18 + intent["whites"] * 0.08,
            "contrast_delta": intent["contrast"],
            "highlights_delta": intent["highlights"],
            "shadows_delta": intent["shadows"],
            "saturation_delta": intent["saturation"],
            "vibrance_delta": intent["vibrance"],
            "temperature_delta": intent["temperature_intent"],
            "tint_delta": intent["tint_intent"],
            "sharpening_delta": intent["sharpening"],
            "structure_delta": (intent["clarity"] + intent["texture"]) * 0.5,
            "dehaze_delta": intent["dehaze"],
            "noise_reduction_delta": intent["noise_reduction"],
            "grain_delta": intent["grain"],
            "vignette_delta": intent["vignette"],
            "fade_delta": intent["fade"],
        }
    if platform == "premiere_video":
        return {
            "exposure_ev": ev,
            "temperature_delta": intent["temperature_intent"],
            "tint_delta": intent["tint_intent"],
            "contrast_delta": intent["contrast"],
            "highlights_delta": intent["highlights"],
            "shadows_delta": intent["shadows"],
            "whites_delta": intent["whites"],
            "blacks_delta": intent["blacks"],
            "saturation_delta": intent["saturation"],
            "vibrance_delta": intent["vibrance"],
            "sharpening_delta": intent["sharpening"],
            "clarity_delta": intent["clarity"],
            "dehaze_delta": intent["dehaze"],
            "noise_reduction_delta": intent["noise_reduction"],
            "grain_delta": intent["grain"],
            "vignette_delta": intent["vignette"],
            "fade_delta": intent["fade"],
        }
    return {
        "offset_ev": ev,
        "lift_delta": intent["blacks"] * 0.5 + intent["shadows"] * 0.2,
        "gamma_delta": ev * 20 + intent["shadows"] * 0.25,
        "gain_delta": intent["whites"] * 0.5 + intent["highlights"] * 0.2,
        "contrast_delta": intent["contrast"],
        "temperature_delta": intent["temperature_intent"],
        "tint_delta": intent["tint_intent"],
        "saturation_delta": intent["saturation"],
        "color_boost_delta": intent["vibrance"],
        "midtone_detail_delta": (intent["clarity"] + intent["texture"]) * 0.5,
        "dehaze_delta": intent["dehaze"],
        "sharpening_delta": intent["sharpening"],
        "noise_reduction_delta": intent["noise_reduction"],
        "grain_delta": intent["grain"],
        "vignette_delta": intent["vignette"],
        "fade_delta": intent["fade"],
    }


def unsupported(candidate: dict[str, Any], platform: str) -> list[str]:
    result = ["自动人物／天空／商品语义蒙版", "跨镜头自动匹配的精确数值"]
    advanced = candidate["advanced_intent"]
    if advanced.get("hsl"):
        result.append("HSL需按实际颜色取样，不能只复制数值")
    if advanced.get("color_grading"):
        result.append("三路色轮位置需用示波器和画面确认")
    if platform == "capcut_video":
        result.append("版本相关滤镜、AI补光和自动去闪烁")
    elif platform == "premiere_video":
        result.append("Lumetri与新版Color模式不是同一参数空间")
    else:
        result.append("Lift/Gamma/Gain不是照片高光阴影滑杆的等价换算")
    return result


def temporal_risks(candidate: dict[str, Any]) -> list[str]:
    intent = candidate["intent_vector"]
    risks = ["镜头间曝光或白平衡跳变", "局部蒙版跟踪漂移"]
    if intent["noise_reduction"] or intent["sharpening"]:
        risks.append("降噪或锐化导致纹理闪烁")
    if intent["grain"]:
        risks.append("颗粒在压缩后产生跳动或蚊噪")
    if candidate["advanced_intent"].get("hsl") or intent["dehaze"]:
        risks.append("天空、墙面或肤色渐变出现色带")
    return risks


def video_scenarios() -> list[dict[str, Any]]:
    rows = [
        ("V001", "多镜头曝光白平衡不一致", "旅行Vlog", "人像+风景", ["镜头间曝光与白平衡不一致"], ["M001", "M002"]),
        ("V002", "室内黄光口播", "室内暖灯", "人像", ["脸黄", "高ISO噪点"], ["M001", "M008"]),
        ("V003", "雨夜街道噪点", "雨夜街道", "城市", ["高ISO噪点", "灯光过曝"], ["M024", "M025", "M021"]),
        ("V004", "霓虹移动人像", "霓虹街道", "人像", ["肤色被霓虹污染", "人物移动"], ["M024", "M021"]),
        ("V005", "森林跟拍", "雨后森林", "人像+绿植", ["绿植偏黄", "镜头移动"], ["M027", "M026"]),
        ("V006", "天空渐变航拍", "蓝调时刻", "天空+建筑", ["天空渐变", "压缩色带"], ["M033", "M034", "M037"]),
        ("V007", "日落人物剪影", "日落", "天空+人像", ["太阳附近接近剪切", "人物偏暗"], ["M035", "M036"]),
        ("V008", "餐厅美食短视频", "餐厅暖光", "美食", ["偏黄", "油光强"], ["M038", "M039"]),
        ("V009", "产品转台视频", "棚拍", "产品", ["品牌色需一致", "高光渐变"], ["M042", "M044", "M045"]),
        ("V010", "低码率社媒视频", "社媒视频", "人像", ["压缩噪点", "分辨率低"], ["M001", "M002"]),
        ("V011", "黑白纪录片", "街头纪实", "人像+街景", ["需要统一黑白层次"], ["M051", "M052"]),
        ("V012", "青橙叙事短片", "城市剧情", "人像", ["需要青橙分离", "多镜头一致性"], ["M020", "M015", "M021"]),
    ]
    common_required = ["先逐镜头校正再共享风格", "回放检查闪烁色带和跳色", "导出前核对音频帧率与色彩标签"]
    common_forbidden = ["逐帧自动漂移参数", "把照片滑杆当视频软件等价值"]
    return [{
        "id": sid,
        "name": name,
        "status": "research_acceptance_case",
        "media_type": "video",
        "input": {"scene": scene, "subject": subject, "problems": problems},
        "expected_candidate_ids": expected,
        "required_behaviors": [],
        "forbidden_behaviors": [],
        "required_temporal_behaviors": common_required,
        "forbidden_temporal_behaviors": common_forbidden,
        "execution_scope": "video_guidance_and_manual_review_only",
    } for sid, name, scene, subject, problems, expected in rows]


def main() -> int:
    candidate_payload = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    candidate_payload["schema_version"] = "1.3.0"
    candidates = candidate_payload["records"]
    for candidate in candidates:
        candidate["media_types"] = ["photo", "video"]
    CANDIDATES.write_text(json.dumps(candidate_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    seeds = []
    for candidate in candidates:
        for platform, application in PLATFORMS.items():
            standard = {name: clamp(name, value) for name, value in map_parameters(platform, candidate["intent_vector"]).items()}
            seeds.append({
                "id": f"{candidate['id']}:{platform}",
                "candidate_id": candidate["id"],
                "media_type": "video",
                "platform": platform,
                "application": application,
                "application_version": None,
                "parameter_space": "normalized_directional_starting_point_not_native_slider",
                "manual_only": True,
                "calibration_status": "heuristic",
                "renderable": False,
                "mapping_verified": False,
                "levels": {level: scaled(standard, factor) for level, factor in FACTORS.items()},
                "advanced_guidance": candidate["advanced_intent"],
                "temporal_policy": BASE_TEMPORAL_POLICY,
                "temporal_risks": temporal_risks(candidate),
                "unsupported_features": unsupported(candidate, platform),
                "warnings": ["不是照片参数的直接复制", "不是跨视频软件等价换算", "执行前必须确认素材色彩空间和软件版本"],
            })
    payload = {
        "schema_version": "1.3.0",
        "boundary": "52个视频风格指导×3个应用；全部未标定、仅手工、不可渲染",
        "seed_count": len(seeds),
        "tier_profile_count": len(seeds) * 3,
        "seeds": seeds,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    scenario_payload = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    scenario_payload["schema_version"] = "1.3.0"
    photo = [item for item in scenario_payload["records"] if not str(item.get("id", "")).startswith("V")]
    for item in photo:
        item["media_type"] = "photo"
    scenario_payload["records"] = photo + video_scenarios()
    SCENARIOS.write_text(json.dumps(scenario_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"video_seeds": len(seeds), "video_tiers": len(seeds) * 3, "scenarios": len(scenario_payload["records"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
