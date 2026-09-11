#!/usr/bin/env python3
"""BLCaptain v1.2 手工调色指导推荐器；不会生成渲染计划。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research"
PHOTO_APP_TO_PLATFORM = {
    "iPhone照片": "iphone_photos",
    "醒图": "xingtu",
    "Lightroom": "lightroom",
}
VIDEO_APP_TO_PLATFORM = {
    "剪映": "capcut_video",
    "Premiere": "premiere_video",
    "DaVinci": "davinci_resolve_video",
}
APP_TO_PLATFORM = {**PHOTO_APP_TO_PLATFORM, **VIDEO_APP_TO_PLATFORM}
LEVEL_TO_KEY = {"保守": "conservative", "标准": "standard", "大胆": "bold"}
VIDEO_WORKFLOW = [
    "确认素材是Rec.709 SDR；Log或HDR先在支持色彩管理的软件中完成技术转换",
    "先逐镜头匹配曝光和白平衡，再给整段共享风格",
    "按基础校正→曲线／色轮／HSL→局部跟踪的顺序调整",
    "先时序降噪再锐化，颗粒放在降噪之后",
    "完整回放检查闪烁、色带、跳色和蒙版漂移",
    "导出前核对帧率、时长、音频、码率和色彩标签",
]
VIDEO_TEMPORAL_REQUIRED = ["先逐镜头校正再共享风格", "回放检查闪烁色带和跳色", "导出前核对音频帧率与色彩标签"]
VIDEO_TEMPORAL_FORBIDDEN = ["逐帧自动漂移参数", "把照片滑杆当视频软件等价值"]

# 画面问题触发的人工检查策略。它只产生指导文本，不代表自动蒙版已经执行。
ACTION_POLICIES = {
    "欠曝": (["先中和白平衡", "人物局部提亮", "锐化降档"], ["直接叠加大幅暖色温", "全局高锐化"]),
    "主体偏暗": (["主体蒙版", "高光保护"], ["全局阴影拉满", "抹平逆光方向"]),
    "面部偏绿": (["面部色调向洋红微调", "压背景亮斑"], ["全局去绿导致环境枯黄"]),
    "肤色偏黄": (["背景与人物分离处理"], ["把肤色推成青灰"]),
    "肤色较深": (["保持肤色明度层次", "只做中性校正"], ["以冷白皮为目标", "全局大幅抬曝光"]),
    "毛孔明显": (["人脸纹理负值", "眼发边缘保留"], ["全局磨皮", "清晰度大幅正值"]),
    "海水偏黄": (["海水 HSL 局部处理", "人物肤色保护"], ["全局拉蓝"]),
    "天空高光强": (["天空与人物分离"], ["全局 HDR 感"]),
    "云层平": (["天空蒙版去朦胧", "建筑局部提阴影"], ["人物清晰度跟随天空"]),
    "太阳附近接近剪切": (["先降曝光保护太阳", "人物局部恢复"], ["全局色温拉满"]),
    "晚霞颜色淡": (["克制洋红分级", "检查色带"], ["高饱和造成脏紫"]),
    "绿植偏黄": (["压黄绿饱和", "局部去朦胧"], ["肤色跟随绿色偏青"]),
    "绿色过饱和": (["HSL 绿饱和回退"], ["只降全局饱和度"]),
    "灯光过曝": (["降噪优先于锐化", "灯光高光保护"], ["黑位压死"]),
    "肤色被霓虹污染": (["人物白平衡局部校正", "降噪"], ["全局去色"]),
    "额头反光": (["面部高光局部压制", "背景保持深色"], ["全局抬阴影"]),
    "油光强": (["先中和过量黄偏", "压油光高光"], ["继续大幅升色温"]),
    "白盘偏灰": (["白盘中性化", "局部纹理"], ["白盘剪切"]),
    "希望奶油感": (["柔对比", "甜品局部自然饱和"], ["全局锐度过高"]),
    "背景不够白": (["背景与主体分离", "品牌色色差检查"], ["剪掉透明或白色产品边缘"]),
    "金属高光杂乱": (["高光渐变保护", "主体纹理局部化"], ["全局过度去朦胧"]),
    "金属偏黄": (["中性灰校正", "局部纹理"], ["金属整体变蓝"]),
    "分辨率低": (["细节参数乘0.65", "禁止假细节"], ["高锐化", "高去朦胧", "重颗粒"]),
    "曝光与白平衡不一致": (["逐张中性校正后共享风格向量", "以肤色和白点做锚"], ["直接复制全部参数不复核"]),
    # 以下四条补自评测判别力检查暴露的覆盖缺口：V006／V009／V011／V012 四个视频场景
    # 原本命中不到任何规则，而它们的 required_behaviors 恰好为空，
    # 于是同义反复的通过率检查「通过」得毫无意义。规则依据见 research/primary-sources-v4.json。
    "天空渐变": (["天空区分段检查断层", "降低去朦胧与清晰度幅度", "必要时加入极轻微抖动掩盖量化"],
                 ["大幅去朦胧", "高强度锐化天空", "对渐变区做强对比"]),
    "压缩色带": (["提高码率或改用更高位深中间格式", "减少连续大面积渐变上的强对比操作"],
                 ["在低码率上叠加高对比与高锐化", "用降噪掩盖色带"]),
    "品牌色需一致": (["以中性校正建立锚点后再套风格", "品牌色区域单独取样比对", "逐镜头核对同一物体的颜色"],
                     ["整体色相偏移", "对品牌色做创意分级"]),
    "高光渐变": (["高光滚降平滑处理", "检查金属与白色物体的高光是否仍有纹理"],
                 ["把高光推到剪切", "对高光区做强饱和"]),
    "需要统一黑白层次": (["去色前先统一各镜头明度关系", "用通道权重重新设计明度而非简单去饱和",
                          "逐镜头核对黑位与白位"],
                         ["直接把饱和度归零", "各镜头分别调整对比度"]),
    "需要青橙分离": (["先逐镜头中性校正再施加双色语法", "暖冷两侧强度成对约束", "肤色单独复核"],
                     ["两侧独立拉满", "让冷侧吞掉暖侧生命色"]),
    "多镜头一致性": (["先逐镜头一级校正再共享风格", "选技术状态最健康的镜头做锚点",
                      "创意 Look 之后再做一次镜头级微调"],
                     ["用创意 Look 掩盖曝光与白平衡差异", "跨硬切平滑参数"]),
}
PARAMETER_LABELS = {
    "exposure": "曝光", "brilliance": "鲜明度", "light_sense": "光感",
    "highlights": "高光", "shadows": "阴影", "contrast": "对比度",
    "brightness": "亮度", "black_point": "黑点", "whites": "白色色阶",
    "blacks": "黑色色阶", "warmth": "暖色调", "temperature": "色温",
    "tint": "色调", "saturation": "饱和度", "vibrance": "自然饱和度",
    "clarity": "清晰度", "texture": "纹理", "definition": "精细度",
    "structure": "结构", "dehaze": "去朦胧", "sharpness": "锐度",
    "sharpening": "锐化", "noise_reduction": "降噪", "grain": "颗粒",
    "vignette": "暗角", "fade": "褪色",
    "brightness_delta": "亮度方向", "contrast_delta": "对比度方向",
    "highlights_delta": "高光方向", "shadows_delta": "阴影方向",
    "whites_delta": "白色色阶方向", "blacks_delta": "黑色色阶方向",
    "saturation_delta": "饱和度方向", "vibrance_delta": "自然饱和度方向",
    "temperature_delta": "色温方向", "tint_delta": "色调方向",
    "sharpening_delta": "锐化方向", "structure_delta": "结构方向",
    "clarity_delta": "清晰度方向", "dehaze_delta": "去雾方向",
    "noise_reduction_delta": "降噪方向", "grain_delta": "颗粒方向",
    "vignette_delta": "暗角方向", "fade_delta": "褪色方向",
    "offset_ev": "Offset曝光", "lift_delta": "Lift方向", "gamma_delta": "Gamma方向",
    "gain_delta": "Gain方向", "color_boost_delta": "Color Boost方向",
    "midtone_detail_delta": "中间调细节方向",
}


def read_records(name: str) -> list[dict[str, Any]]:
    path = RESEARCH / name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取研究数据 {path.name}：{error}") from error
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise RuntimeError(f"研究数据结构无效：{path.name}")
    return records


def read_seeds(media_type: str = "照片") -> list[dict[str, Any]]:
    filename = "video_guidance_seeds.json" if media_type == "视频" else "guidance_seeds.json"
    path = RESEARCH / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取指导种子：{error}") from error
    seeds = payload.get("seeds") if isinstance(payload, dict) else None
    if not isinstance(seeds, list):
        raise RuntimeError("指导种子结构无效")
    return seeds


def normalized(value: str) -> str:
    return re.sub(r"[\s,，、/；;|+·_\-]+", "", str(value).lower())


def hits(query: str, keywords: list[str]) -> list[str]:
    source = normalized(query)
    if not source:
        return []
    result = []
    for keyword in keywords:
        target = normalized(keyword)
        if len(target) >= 2 and (target in source or source in target):
            result.append(keyword)
    return result


def score_candidate(
    candidate: dict[str, Any],
    rule: dict[str, Any],
    scene: str,
    subject: str,
    problems: str,
    preferences: str,
) -> tuple[float, list[str]]:
    keyword_groups = rule.get("keywords", {})
    fields = [
        ("偏好", preferences, keyword_groups.get("preferences", []), 10.0),
        ("问题", problems, keyword_groups.get("problems", []), 9.0),
        ("场景", scene, keyword_groups.get("scenes", []), 8.0),
        ("主体", subject, keyword_groups.get("subjects", []), 4.0),
    ]
    score = 0.0
    reasons: list[str] = []
    for label, query, words, weight in fields:
        matched = hits(query, words)
        if matched:
            score += weight + max(0, len(matched) - 1) * weight * 0.3
            reasons.append(f"{label}匹配：{'、'.join(matched[:3])}")

    applicability = candidate.get("applicability", {})
    matched_scenes = hits(scene, applicability.get("scenes", []))
    matched_subjects = hits(subject, applicability.get("subjects", []))
    if matched_scenes:
        score += 5.0
        reasons.append(f"适合场景：{'、'.join(matched_scenes[:2])}")
    if matched_subjects and "全部" not in applicability.get("subjects", []):
        score += 3.0
        reasons.append(f"适合主体：{'、'.join(matched_subjects[:2])}")

    direct = hits(preferences, [candidate.get("name", ""), candidate.get("family", ""), *candidate.get("aliases", [])])
    if direct:
        score += 12.0
        reasons.append(f"风格直接匹配：{'、'.join(direct[:2])}")

    conflicts = hits(" ".join([scene, subject, problems, preferences]), keyword_groups.get("exclusions", []))
    if conflicts:
        score -= 15.0
        reasons.append(f"存在冲突：{'、'.join(conflicts[:2])}")
    # 原实现是 `if candidate["id"] == "M001": score += 25`，对单个候选定向加分。
    # 那等于把评测答案写进打分器——Top-1 指标因此不可信。
    # 改为按候选家族表达同一条领域规则：素材之间基础状态不一致时，
    # 任何「基础校正」家族的候选都应优先，而不是某一个特定 ID。
    if candidate.get("family") == "基础校正" and problems:
        score += 1.0
        if "不一致" in problems or "镜头匹配" in problems:
            score += 25.0
            reasons.append("多镜头基础状态不一致，先建立中性校正基线")
    if not reasons:
        reasons.append("作为保守的研究候选供人工比较")
    return round(score, 2), reasons


def rank_candidates(
    scene: str,
    subject: str,
    problems: str,
    preferences: str,
    count: int = 3,
) -> list[tuple[float, list[str], dict[str, Any]]]:
    candidates = read_records("research_candidates.json")
    rules = {item["candidate_id"]: item for item in read_records("recommendation_rules.json")}
    ranked = []
    for candidate in candidates:
        score, reasons = score_candidate(
            candidate, rules.get(candidate["id"], {}), scene, subject, problems, preferences
        )
        ranked.append((score, reasons, candidate))
    ranked.sort(key=lambda item: (-item[0], item[2]["id"]))
    return ranked[:count]


def manual_actions(parameters: dict[str, float]) -> list[str]:
    groups = [
        ("先修明暗", ["exposure", "brilliance", "highlights", "shadows", "contrast", "brightness", "black_point", "whites", "blacks"]),
        ("再校正冷暖与偏色", ["warmth", "temperature", "tint"]),
        ("再调整颜色浓淡", ["saturation", "vibrance"]),
        ("最后增加质感", ["clarity", "texture", "definition", "dehaze", "sharpness", "sharpening", "noise_reduction", "grain", "vignette", "fade"]),
    ]
    actions = []
    for title, names in groups:
        values = [
            f"{PARAMETER_LABELS.get(name, name)} {parameters[name]:+g}"
            for name in names if name in parameters and parameters[name] != 0
        ]
        if values:
            actions.append(f"{title}：{'，'.join(values)}")
    actions.append("逐项开关效果，检查肤色、高光、黑位、天空渐变和锐化光晕")
    return actions


def video_manual_actions(parameters: dict[str, float]) -> list[str]:
    values = [
        f"{PARAMETER_LABELS.get(name, name)} {value:+g}"
        for name, value in parameters.items() if value != 0
    ]
    return [
        "先完成逐镜头基础校正，不要用创意风格掩盖曝光或白平衡问题",
        f"在目标软件中按标准化方向尝试：{'，'.join(values)}" if values else "保持全局参数中性",
        "需要人物、天空或商品局部处理时建立蒙版并跟踪，逐段检查边缘",
        "最后完整回放，再用示波器检查高光、黑位、肤色和饱和度",
    ]


def review_policy(problems: str) -> tuple[list[str], list[str]]:
    required: list[str] = []
    forbidden: list[str] = []
    for keyword, (actions, bans) in ACTION_POLICIES.items():
        problem_text = normalized(problems).replace("脸黄", "肤色偏黄").replace("脸绿", "面部偏绿")
        if normalized(keyword) in problem_text:
            required.extend(actions)
            forbidden.extend(bans)
    return list(dict.fromkeys(required)), list(dict.fromkeys(forbidden))


def build_result(args: argparse.Namespace) -> dict[str, Any]:
    allowed = VIDEO_APP_TO_PLATFORM if args.媒体 == "视频" else PHOTO_APP_TO_PLATFORM
    if args.应用 not in allowed:
        raise ValueError(f"媒体与应用不匹配：{args.媒体}不能使用{args.应用}")
    platform = allowed[args.应用]
    level_key = LEVEL_TO_KEY[args.档位]
    seeds = {(seed["candidate_id"], seed["platform"]): seed for seed in read_seeds(args.媒体)}
    ranked = rank_candidates(args.场景, args.主体, args.问题, args.偏好, args.数量)
    required_actions, forbidden_actions = review_policy(args.问题)
    result = []
    for score, reasons, candidate in ranked:
        seed = seeds[(candidate["id"], platform)]
        parameters = seed["levels"][level_key]
        result.append({
            "id": candidate["id"],
            "name": candidate["name"],
            "family": candidate["family"],
            "score": score,
            "reasons": reasons,
            "application": args.应用,
            "strength": args.档位,
            "parameters": {PARAMETER_LABELS.get(name, name): value for name, value in parameters.items()},
            "manual_actions": video_manual_actions(parameters) if args.媒体 == "视频" else manual_actions(parameters),
            "fallbacks": [
                "脸或白色物体发灰：把冷暖向暖回 3～8 格，或降低一级强度",
                "高光丢细节：继续降低高光；若已经剪切，调色无法恢复原始细节",
                "颜色荧光或天空断层：降低饱和、去朦胧与锐化，并回到保守档",
            ],
            "manual_only": True,
            "calibration_status": "heuristic",
            "renderable": False,
            "review_guards": candidate.get("manual_review_guards", []),
            "temporal_risks": seed.get("temporal_risks", []) if args.媒体 == "视频" else [],
            "unsupported_features": seed.get("unsupported_features", []),
        })
    return {
        "mode": "guidance",
        "manual_only": True,
        "calibration_status": "heuristic",
        "renderable": False,
        "notice": "以下仅是未标定的手工起点，不是跨 App 精确换算，也不会生成渲染计划。",
        "input": {
            "media_type": args.媒体,
            "scene": args.场景,
            "subject": args.主体,
            "problems": args.问题,
            "preferences": args.偏好,
            "application": args.应用,
            "strength": args.档位,
        },
        "manual_review_policy": {
            "required_actions": required_actions,
            "forbidden_actions": forbidden_actions,
            "boundary": "这些是人工检查与回退规则，不表示已经执行局部蒙版。",
        },
        "video_workflow": VIDEO_WORKFLOW if args.媒体 == "视频" else [],
        "temporal_review_policy": {
            "required_actions": VIDEO_TEMPORAL_REQUIRED if args.媒体 == "视频" else [],
            "forbidden_actions": VIDEO_TEMPORAL_FORBIDDEN if args.媒体 == "视频" else [],
            "boundary": "时序规则只指导人工操作，不表示已自动跟踪蒙版或匹配镜头。",
        },
        "candidates": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="给出 2～3 个未标定的手工调色候选；不会渲染文件。")
    parser.add_argument("--媒体", "--media-type", choices=["照片", "视频"], default="照片")
    parser.add_argument("--场景", "--scene", default="")
    parser.add_argument("--主体", "--subject", default="")
    parser.add_argument("--问题", "--problem", default="")
    parser.add_argument("--偏好", "--preference", default="")
    parser.add_argument("--应用", "--app", choices=list(APP_TO_PLATFORM), default="Lightroom")
    parser.add_argument("--档位", "--strength", choices=list(LEVEL_TO_KEY), default="标准")
    parser.add_argument("--数量", "--count", type=int, choices=[2, 3], default=3)
    parser.add_argument("--输出", "--output", default="")
    args = parser.parse_args()
    try:
        payload = build_result(args)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.输出:
            output = Path(args.输出)
            if output.exists():
                print(f"拒绝覆盖已有输出：{output}", file=sys.stderr)
                return 3
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    except ValueError as error:
        print(f"指导模式输入错误：{error}", file=sys.stderr)
        return 2
    except (KeyError, RuntimeError) as error:
        print(f"指导模式数据错误：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
