#!/usr/bin/env python3
"""统一建议入口：智能推荐 / 灵感组合 / 全部风格，并附原图与目标配色色卡。

对用户只暴露三个动作和一句话诊断；复杂度全部留在内部。
本入口不渲染任何文件，也不生成已确认计划——它只负责「让用户看懂并选择」。
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    from bootstrap import bootstrap_or_exit

    bootstrap_or_exit(Path(__file__).resolve().parents[1])

import blcaptain_color as engine  # noqa: E402
import combos as combos_module  # noqa: E402
import diagnose as diagnose_module  # noqa: E402
import fit as fit_module  # noqa: E402
import feedback_ledger  # noqa: E402
import palette as palette_module  # noqa: E402
import scene_facts  # noqa: E402

SUGGEST_SCHEMA_VERSION = "4.0.0"


def apply_feedback_memory(options: list[dict], state: dict, source_sha256: str,
                          strength: float) -> list[dict]:
    """把同素材、同配方、同强度的人工否决后置并显式标注。"""
    rejected = {
        (item["recipe_id"], round(float(item["strength"]), 6)): item
        for item in state.get("entries", [])
        if item.get("source_sha256") == source_sha256 and item.get("verdict") == "rejected"
    }
    fresh = []
    demoted = []
    for option in options:
        key = (option["recipe_id"], round(float(option.get("suggested_strength", strength)), 6))
        record = rejected.get(key)
        if record is None:
            fresh.append(option)
            continue
        annotated = dict(option)
        annotated["feedback_memory"] = {
            "status": "human_rejected",
            "recorded_at": record["recorded_at"],
            "reason": record["reason"],
            "message": (
                f"此方向曾被你否决（{record['recorded_at']}／{record['reason']}），"
                "如仍想尝试请明确选择"
            ),
        }
        demoted.append(annotated)
    return fresh + demoted


def requires_explicit_intent(recipe: dict) -> bool:
    """强不可逆风格不能把“能执行”冒充成“适合默认推荐”。"""
    parameters = recipe.get("parameters") or {}
    tags = recipe.get("combo_tags") or {}
    return (
        float(parameters.get("saturation", 1.0)) <= 0.05
        or tags.get("color") == "monochrome"
    )


def normalize_avoid(value: str | list[str] | None) -> list[str]:
    """把本次会话的避开词拆成去重短语；不写入画像或反馈账本。"""
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    terms = []
    for item in raw:
        for term in str(item).replace("，", ",").split(","):
            normalized = term.strip()
            if normalized and normalized not in terms:
                terms.append(normalized)
    return terms


# --avoid 当前能理解的口味词表。词表外的词必须显式报「未能识别」，
# 不得静默失效——否则用户以为已避开，候选里却还留着那个方向。
KNOWN_AVOID_TERMS = frozenset({
    "黄红", "暖调", "偏黄", "偏红", "暖黄", "暖红",
    "冷调", "偏蓝", "青蓝", "冷青",
    "高饱和", "浓艳", "鲜艳",
    "低饱和", "寡淡", "褪色",
})


def avoid_reasons(recipe: dict, terms: list[str]) -> list[str]:
    """把自然语言口味映射到配方的结构化色彩标签与真实参数。"""
    parameters = recipe.get("parameters") or {}
    tags = recipe.get("combo_tags") or {}
    color_tag = str(tags.get("color") or "").lower()
    temperature = float(parameters.get("temperature", 0.0))
    saturation = float(parameters.get("saturation", 1.0))
    reasons = []
    for term in terms:
        if term in {"黄红", "暖调", "偏黄", "偏红", "暖黄", "暖红"}:
            if (temperature > 0.03
                    or any(token in color_tag for token in ("warm", "amber", "gold", "orange", "red"))):
                reasons.append(f"{term}：temperature={temperature:.4f}, color={color_tag}")
        elif term in {"冷调", "偏蓝", "青蓝", "冷青"}:
            if (temperature < -0.03
                    or any(token in color_tag for token in ("cool", "blue", "cyan"))):
                reasons.append(f"{term}：temperature={temperature:.4f}, color={color_tag}")
        elif term in {"高饱和", "浓艳", "鲜艳"} and saturation > 1.12:
            reasons.append(f"{term}：saturation={saturation:.4f}")
        elif term in {"低饱和", "寡淡", "褪色"} and saturation < 0.90:
            reasons.append(f"{term}：saturation={saturation:.4f}")
    return reasons


def session_avoid_message(avoid: dict, option_count: int) -> str:
    requested = int(avoid.get("requested_count") or option_count)
    excluded = "、".join(item["recipe_name"] for item in avoid.get("excluded", [])) or "无"
    count_note = (
        f"只找到 {option_count} 个符合口味且通过当前预演的方向，少于请求的 {requested} 个；"
        "不拿被避开的方向凑数。"
        if option_count < requested else f"已找到 {option_count} 个符合本次口味的方向。"
    )
    unrecognized = avoid.get("unrecognized_terms") or []
    unrecognized_note = (
        f"未能识别的口味词：{'、'.join(unrecognized)}——未据此排除任何方向；"
        "当前可识别：黄红／暖调、冷调／偏蓝、高饱和、低饱和等冷暖与饱和词。"
        if unrecognized else ""
    )
    return (
        f"本次硬过滤：{'、'.join(avoid['terms'])}；已排除：{excluded}。{count_note}"
        f"{unrecognized_note}"
        "这些偏好不会写盘，也不会建立用户画像。"
    )


def infer_season_signal(diagnosis: dict) -> dict:
    """只从可见色相与白平衡做保守季节联想；证据不足必须 unknown。"""
    analysis = diagnosis.get("analysis") or {}
    color = analysis.get("color") or {}
    hues = {item.get("family"): float(item.get("weight", 0.0))
            for item in color.get("dominant_hues", [])}
    chromatic = float(color.get("chromatic_pixel_ratio", 0.0))
    warm = sum(hues.get(name, 0.0) for name in ("红", "橙／肤色带", "黄"))
    green = sum(hues.get(name, 0.0) for name in ("黄绿", "绿", "青绿"))
    cool = sum(hues.get(name, 0.0) for name in ("青绿", "青蓝", "蓝"))
    if chromatic >= 0.22 and warm >= 0.58:
        return {"value": "秋", "confidence": round(min(0.88, warm * chromatic + 0.45), 3),
                "basis": f"暖色簇权重 {warm:.0%}、有彩像素 {chromatic:.0%}"}
    if chromatic >= 0.20 and green >= 0.58:
        return {"value": "春／夏", "confidence": round(min(0.84, green * chromatic + 0.42), 3),
                "basis": f"绿系权重 {green:.0%}、有彩像素 {chromatic:.0%}"}
    if cool >= 0.62 and float(color.get("mean_chroma", 1.0)) <= 0.10:
        return {"value": "冬", "confidence": round(min(0.82, cool * 0.72), 3),
                "basis": f"冷色簇权重 {cool:.0%} 且平均彩度较低"}
    return {"value": "unknown", "confidence": 0.0,
            "basis": "色相与白平衡证据不足；不根据日期或题材名称硬猜季节"}


def annotate_season(options: list[dict], catalog: dict, signal: dict, media_type: str) -> list[dict]:
    for option in options:
        recipe = engine.find_recipe(catalog, option["recipe_id"], media_type)
        affinity = recipe["seasonal_affinity"]
        if signal["value"] == "unknown":
            relation = "未知：素材季节证据不足，不参与推荐排序"
        elif "全季" in affinity["seasons"]:
            relation = f"中性：素材联想到{signal['value']}，本配方为全季"
        elif any(part in affinity["seasons"] for part in signal["value"].split("／")):
            relation = f"顺势：素材联想到{signal['value']}，配方亲和{'／'.join(affinity['seasons'])}"
        else:
            relation = f"逆势：素材联想到{signal['value']}，配方亲和{'／'.join(affinity['seasons'])}；需明确接受"
        option["seasonal_context"] = {"signal": signal, "affinity": affinity, "relation": relation}
    return options


def classify_execution_preflight(
    predicted_delta: float,
    required_delta: float,
    blockers: list[str],
    local_separation_ratio: float | None = None,
    direction_failures: list[str] | None = None,
    preview_verified: bool = True,
) -> dict:
    """把推荐可执行性压成一个状态，避免“推荐后才发现执行不了”。"""
    if blockers:
        return {"status": "blocked", "code": "safety-blocked",
                "reason": "素材安全门阻断：" + "；".join(blockers)}
    failures = set(direction_failures or [])
    if {"tone_span", "colorfulness", "local_contrast"} <= failures:
        return {"status": "blocked", "code": "direction-reversed",
                "reason": "预演中三个可测方向全部与配方声明相反"}
    if local_separation_ratio is not None and local_separation_ratio < 1.3:
        return {
            "status": "blocked",
            "code": "local-separation-insufficient",
            "reason": f"局部分离比 {local_separation_ratio:.3f} 低于 1.3，不能标为可执行",
        }
    if predicted_delta + 1e-4 < required_delta:
        return {
            "status": "blocked",
            "code": "insufficient-change",
            "reason": f"预测变化 {predicted_delta:.4f} 低于当前强度变化下限 {required_delta:.4f}",
        }
    return {
        "status": "executable" if preview_verified else "risky",
        "code": "same-chain-verified" if preview_verified else "preview-unverified",
        "reason": (
            "同执行链预演达到变化下限"
            if preview_verified else "仅完成色卡估算，尚未运行同执行链低分辨率预演"
        ),
    }


def recovery_policy(consecutive_confirmed_failures: int) -> dict:
    """连续失败两次后停止自动换风格，避免让新用户重复走死路。"""
    if consecutive_confirmed_failures < 2:
        return {"action": "retry-once", "paths": ["调整强度后重新预演"]}
    return {
        "action": "stop-and-rediagnose",
        "paths": [
            "保留风格，改为只做中性一级校正并重新确认",
            "保留情绪目标，选择视觉手势真正不同的配方并预演",
            "停止自动调色，展示诊断与全部风格供用户手选",
        ],
    }


def feedback_compatibility(feedback: str, visual_targets: dict) -> dict:
    """检查用户反馈与配方目标是否正面冲突。"""
    target = str(visual_targets.get("colorfulness", "preserve"))
    if feedback in {"更多色彩", "色彩不够", "更鲜艳"} and target == "decrease":
        return {
            "compatible": False,
            "reason": "用户要求增加色彩，但该配方声明综合色彩下降，方向冲突",
        }
    return {"compatible": True, "reason": "用户反馈与配方声明没有直接冲突"}


def _render_photo_preflight_file(recipe: dict, source: dict, strength: float,
                                 output: Path) -> tuple[str, dict]:
    import signature_regions
    if recipe['id'] in signature_regions.ROLES:
        return signature_regions.preview(recipe, source, strength, output)
    if recipe['id'] == 'korean-cool':
        import korean_cool_execution
        return korean_cool_execution.preview(recipe, source, strength, output)
    filtergraph = _filtergraph_for(recipe, source, strength)
    effective = engine.perceptual_strength(recipe, strength)
    parameters = engine.scaled_parameters(recipe["parameters"], 1.0)
    tone_curve = engine.scaled_curve(recipe.get("tone_curve"), 1.0)
    hsl_bands = engine.scaled_hsl(recipe.get("hsl_bands"), 1.0)
    color_pipeline = engine.color_pipeline_for(source)
    plan_stub = {
        "source": source,
        "style": engine.style_payload(recipe, source["media_type"]),
        "effective_strength": effective,
        "render_mix": effective,
        "parameters": parameters,
        "tone_curve": tone_curve,
        "hsl_bands": hsl_bands,
        "composition": {
            "action": "none", "crop": None, "rotate_deg": 0.0,
            "output_width": source["width"], "output_height": source["height"],
        },
        "attention_map": {
            "mode": "none", "center_x": 0.5, "center_y": 0.5,
            "radius": 1.0, "strength": 0.0, "rationale": "推荐预演不做几何局部",
        },
        "visual_brief": {"semantic_analysis_claimed": False},
        "color_pipeline": color_pipeline,
        "output_path": str(output),
    }
    # 缩放必须发生在验收之后。此前先缩到 320px 会改变局部对比统计，
    # 让真实全分辨率失败的「高原寂光 55%」在预演里变成假绿灯。
    chain = engine.photo_output_filter(plan_stub, filtergraph)
    engine.run([
        engine.require_tool("ffmpeg"), "-v", "error", "-y", "-i", source["path"],
        "-frames:v", "1", "-vf", chain, "-c:v", "png", "-pix_fmt",
        color_pipeline["pixel_format"], str(output),
    ])
    engine.validate_photo_color(output, plan_stub)
    return filtergraph, plan_stub


def _mean_delta_between(left: Path, right: Path, source: dict) -> float:
    first = engine.sample_rgb(left, source["media_type"], source["duration"])
    second = engine.sample_rgb(right, source["media_type"], source["duration"])
    usable = min(len(first), len(second)) // 3 * 3
    differences = []
    profile = source["color"]["profile"]
    for index in range(0, usable, 3):
        left_lab = engine.rgb_to_oklab(first[index], first[index + 1], first[index + 2], profile)
        right_lab = engine.rgb_to_oklab(second[index], second[index + 1], second[index + 2], profile)
        differences.append(sum((a - b) ** 2 for a, b in zip(left_lab, right_lab)) ** 0.5)
    return sum(differences) / len(differences) if differences else 0.0


def _retain_korean_preview(recipe: dict, output: Path, strength: float) -> dict | None:
    """只复制本次已验收预演；独占创建，不能覆盖或冒充正式成片。"""
    import signature_regions
    directory_path = None
    if recipe.get('id') == 'korean-cool':
        directory_path = recipe.get('_korean_preview_dir')
    elif recipe.get('id') in signature_regions.ROLES and recipe.get('_signature_regions'):
        directory_path = recipe.get('_signature_preview_dir')
    if not directory_path:
        return None
    directory = Path(directory_path).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    label = 'foundation' if strength == 0 else f'strength-{strength:.6f}'
    target = directory / f'{label}-internal-preview{output.suffix}'
    with target.open('xb') as destination, output.open('rb') as source:
        shutil.copyfileobj(source, destination)
    return {'path': str(target), 'sha256': engine.sha256(target), 'strength': strength}


def _run_photo_preflight_once(recipe: dict, source: dict, strength: float,
                              baseline_path: Path | None = None) -> dict:
    """运行单一强度的真实全分辨率预演，不用固定最小变化门提前截断测量。"""
    output_profile = "Display P3" if source["color"]["profile"] == "display-p3" else "sRGB"
    with tempfile.TemporaryDirectory(prefix="blcaptain-preflight-") as folder:
        output = Path(folder) / "preview.png"
        try:
            filtergraph, plan_stub = _render_photo_preflight_file(
                recipe, source, strength, output)
            validation = engine.validate_visual_impact(
                Path(source["path"]), output, plan_stub, filtergraph, minimum_override=0.0,
                creative_baseline=baseline_path if (recipe['id'] == 'korean-cool' or recipe.get('_signature_regions')) else None,
            )
            predicted = float(validation["mean_delta_e_ok"])
            direction = validation.get("directional_audit") or {}
            failed = direction.get("failed_dimensions") or []
            report = {
                "predicted_delta": round(predicted, 6),
                "direction_failures": failed,
                "preview_size": f"{source['width']}x{source['height']} full resolution",
                "working_profile": output_profile,
            }
            if baseline_path is not None:
                report["creative_delta_e_ok"] = round(
                    _mean_delta_between(baseline_path, output, source), 6)
            preview = _retain_korean_preview(recipe, output, strength)
            if preview is not None:
                report['preview'] = preview
            return report
        except Exception as error:  # noqa: BLE001
            return {
                "status": "blocked",
                "reason": f"同执行链预演失败：{type(error).__name__}: {error}",
                "evidence": "same-chain-full-resolution-preview",
            }


def _render_video_preflight_file(recipe: dict, source: dict, strength: float,
                                 output: Path) -> tuple[str, dict]:
    import signature_regions
    if recipe['id'] in signature_regions.ROLES:
        return signature_regions.preview(recipe, source, strength, output)
    if recipe['id'] == 'korean-cool':
        import korean_cool_execution
        return korean_cool_execution.preview(recipe, source, strength, output)
    filtergraph = _filtergraph_for(recipe, source, strength)
    effective = engine.perceptual_strength(recipe, strength)
    plan_stub = {
        "source": source,
        "style": engine.style_payload(recipe, source["media_type"]),
        "effective_strength": effective,
        "render_mix": effective,
        "parameters": engine.scaled_parameters(recipe["parameters"], 1.0),
        "tone_curve": engine.scaled_curve(recipe.get("tone_curve"), 1.0),
        "hsl_bands": engine.scaled_hsl(recipe.get("hsl_bands"), 1.0),
        "composition": {"action": "none", "crop": None, "rotate_deg": 0.0,
                        "output_width": source["width"], "output_height": source["height"]},
        "attention_map": {"mode": "none", "center_x": 0.5, "center_y": 0.5,
                          "radius": 1.0, "strength": 0.0,
                          "rationale": "推荐预演不做几何局部"},
        "visual_brief": {"semantic_analysis_claimed": False},
    }
    engine.run([
        engine.require_tool("ffmpeg"), "-v", "error", "-y", "-i", source["path"],
        "-map", "0:v:0", "-an", "-vf",
        filtergraph + ",setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        str(output),
    ])
    return filtergraph, plan_stub


def _run_video_preflight_once(recipe: dict, source: dict, strength: float,
                              baseline_path: Path | None = None) -> dict:
    """用完整时间轴运行单档视频预演；仅韩系计划可指定独立留存目录。"""
    with tempfile.TemporaryDirectory(prefix="blcaptain-video-preflight-") as folder:
        output = Path(folder) / "preview.mp4"
        try:
            filtergraph, plan_stub = _render_video_preflight_file(
                recipe, source, strength, output)
            validation = engine.validate_visual_impact(
                Path(source["path"]), output, plan_stub, filtergraph, minimum_override=0.0,
                creative_baseline=baseline_path if (recipe['id'] == 'korean-cool' or recipe.get('_signature_regions')) else None)
            direction = validation.get("directional_audit") or {}
            report = {
                "predicted_delta": round(float(validation["mean_delta_e_ok"]), 6),
                "direction_failures": direction.get("failed_dimensions") or [],
                "preview_size": f"{source['width']}x{source['height']} full timeline",
                "working_profile": "Rec.709 SDR",
            }
            if baseline_path is not None:
                report["creative_delta_e_ok"] = round(
                    _mean_delta_between(baseline_path, output, source), 6)
            preview = _retain_korean_preview(recipe, output, strength)
            if preview is not None:
                report['preview'] = preview
            return report
        except Exception as error:  # noqa: BLE001
            return {"status": "blocked",
                    "reason": f"完整时间轴预演失败：{type(error).__name__}: {error}",
                    "evidence": "same-chain-full-timeline-preview"}


def relative_creative_minimum(recipe: dict, measured: dict) -> float:
    """用 30% 创意 Look 相对一级校正基线的实测变化建立逐素材下限。"""
    visibility_floor = min(0.008, float(recipe["impact"]["min_delta_e"]))
    creative_30 = float(measured[30]["creative_delta_e_ok"])
    return round(max(visibility_floor, creative_30 * 0.8), 6)


def preflight_change_for_gate(requested: dict) -> float:
    """计划门优先检查 Look 相对 Foundation 的变化，不拿总变化冒充。"""
    creative = requested.get("creative_delta_e_ok")
    return float(creative if creative is not None else requested["predicted_delta"])


def run_execution_preflight(recipe: dict, source: dict, strength: float) -> dict:
    """照片候选逐素材实测 30/55/80 三档，并用素材相对门判断。"""
    import signature_regions
    if recipe['id'] in signature_regions.ROLES and not recipe.get('_signature_regions'):
        return {'status': 'blocked', 'code': 'signature-regions-required',
                'reason': '当前签名需要本次人工区域/序列时序证据；没有证据不预演全局替代版。'}
    if recipe['id'] == 'korean-cool' and not recipe.get('_korean_protection'):
        import korean_cool_protection
        import korean_cool_execution
        with tempfile.TemporaryDirectory(prefix='blcaptain-korean-three-levels-') as folder:
            try:
                evidence = korean_cool_protection.prepare(
                    source, Path(folder) / 'evidence', korean_cool_execution.foundation_filter({'source': source}))
                return run_execution_preflight({**recipe, '_korean_protection': evidence}, source, strength)
            except Exception as error:
                return {'status': 'blocked', 'code': 'protection-unavailable',
                        'reason': f'新素材人物保护无法建立：{error}；不回落全局调色。'}
    if (source["media_type"] == "photo"
            and recipe.get("execution_role") == "foundation-only"):
        with tempfile.TemporaryDirectory(prefix="blcaptain-foundation-only-") as folder:
            baseline = Path(folder) / "foundation.png"
            try:
                _render_photo_preflight_file(recipe, source, 0.0, baseline)
                total_delta = _mean_delta_between(Path(source["path"]), baseline, source)
                foundation = engine.foundation_grade(source)
                targets = foundation.get("readiness_targets") or {}
                axes = targets.get("active_axes") or []
                if not targets.get("required") or not axes:
                    return {
                        "status": "blocked",
                        "code": "no-repair-needed",
                        "reason": "真实像素诊断没有激活 Foundation 修复轴；健康素材保持原样，不靠提高强度制造变化。",
                        "evidence": "same-chain-full-resolution-foundation-readiness",
                        "predicted_delta": round(total_delta, 6),
                        "creative_delta_e_ok": 0.0,
                        "required_delta": 0.0,
                        "direction_failures": [],
                        "monotonicity": {
                            "status": "not-applicable-foundation-only",
                            "strictly_increasing": None,
                            "relative_min_delta_e": 0.0,
                            "measurement_basis": "source-versus-foundation-repair",
                            "reason": "修复强度由素材诊断决定，不伪造 Creative 三档差异",
                        },
                    }
                working = ("display-p3" if source["color"]["profile"] == "display-p3"
                           else "srgb")
                after = diagnose_module.diagnose(baseline, working, use_semantic=False)
                readiness = engine.foundation_readiness(
                    source["foundation_diagnosis"], after, active_axes=axes)
                if readiness["status"] == "blocked":
                    return {
                        "status": "blocked",
                        "code": "foundation-readiness-failed",
                        "reason": "Foundation 修复未达成：" + "、".join(
                            readiness["blocking_failures"]),
                        "evidence": "same-chain-full-resolution-foundation-readiness",
                        "foundation_readiness": readiness,
                    }
                return {
                    "status": "executable",
                    "code": "foundation-repair-ready",
                    "reason": "Foundation 已按真实像素诊断建立健康底片；照片 Creative 层保持恒等。",
                    "evidence": "same-chain-full-resolution-foundation-readiness",
                    "predicted_delta": round(total_delta, 6),
                    "creative_delta_e_ok": 0.0,
                    "required_delta": 0.0,
                    "direction_failures": [],
                    "preview_size": f"{source['width']}x{source['height']} full resolution",
                    "working_profile": "Display P3" if working == "display-p3" else "sRGB",
                    "foundation_readiness": readiness,
                    "monotonicity": {
                        "status": "not-applicable-foundation-only",
                        "strictly_increasing": None,
                        "relative_min_delta_e": 0.0,
                        "measurement_basis": "source-versus-foundation-repair",
                        "reason": "修复强度由素材诊断决定，不伪造 Creative 三档差异",
                    },
                }
            except Exception as error:  # noqa: BLE001
                return {
                    "status": "blocked",
                    "code": "preview-failed",
                    "reason": f"Foundation 预演失败：{type(error).__name__}: {error}",
                    "evidence": "same-chain-full-resolution-foundation-readiness",
                }
    requested_level = round(strength * 100)
    if (recipe['id'] == 'korean-cool' or recipe.get('_signature_regions')) and strength != requested_level / 100:
        requested_level = strength * 100
    baseline_artifact = None
    if source["media_type"] == "photo":
        with tempfile.TemporaryDirectory(prefix="blcaptain-preflight-baseline-") as folder:
            baseline = Path(folder) / "baseline.png"
            try:
                _render_photo_preflight_file(recipe, source, 0.0, baseline)
                baseline_artifact = _retain_korean_preview(recipe, baseline, 0.0)
                measured = {
                    level: _run_photo_preflight_once(
                        recipe, source, level / 100.0, baseline_path=baseline)
                    for level in (30, 55, 80)
                }
                if (recipe['id'] == 'korean-cool' or recipe.get('_signature_regions')) and requested_level not in measured:
                    measured[requested_level] = _run_photo_preflight_once(
                        recipe, source, strength, baseline_path=baseline)
            except Exception as error:  # noqa: BLE001
                return {"status": "blocked", "code": "preview-failed",
                        "reason": f"基础校正预演失败：{type(error).__name__}: {error}",
                        "evidence": "same-chain-full-resolution-preview"}
    else:
        with tempfile.TemporaryDirectory(prefix="blcaptain-video-preflight-baseline-") as folder:
            baseline = Path(folder) / "baseline.mp4"
            try:
                _render_video_preflight_file(recipe, source, 0.0, baseline)
                baseline_artifact = _retain_korean_preview(recipe, baseline, 0.0)
                measured = {
                    level: _run_video_preflight_once(
                        recipe, source, level / 100.0, baseline_path=baseline)
                    for level in (30, 55, 80)
                }
                if (recipe['id'] == 'korean-cool' or recipe.get('_signature_regions')) and requested_level not in measured:
                    measured[requested_level] = _run_video_preflight_once(
                        recipe, source, strength, baseline_path=baseline)
            except Exception as error:  # noqa: BLE001
                return {"status": "blocked", "code": "preview-failed",
                        "reason": f"视频基础校正预演失败：{type(error).__name__}: {error}",
                        "evidence": "same-chain-full-timeline-preview"}
    if any("predicted_delta" not in item for item in measured.values()):
        reasons = [item["reason"] for item in measured.values()
                   if "predicted_delta" not in item]
        return {"status": "blocked", "code": "preview-failed",
                "reason": "三档预演失败：" + "；".join(reasons),
                "evidence": "same-chain-full-resolution-preview"}
    delta_key = "creative_delta_e_ok"
    deltas = [float(measured[level][delta_key]) for level in (30, 55, 80)]
    strictly_increasing = deltas[0] < deltas[1] < deltas[2]
    # 修复型零点允许很小但真实的变化；风格配方仍保留 0.008 的可见性底线。
    # 这里取 min 而不是直接相信配方声明，避免历史风格配方的较高全局门限
    # 反过来吞掉逐素材相对下限。
    relative_minimum = relative_creative_minimum(recipe, measured)
    monotonicity = {
        "samples": {
            str(level): {
                "mean_delta_e_ok": measured[level]["predicted_delta"],
                "creative_delta_e_ok": measured[level].get("creative_delta_e_ok"),
                "direction_failures": measured[level]["direction_failures"],
            } for level in (30, 55, 80)
        },
        "strictly_increasing": strictly_increasing,
        "relative_min_delta_e": relative_minimum,
        "measurement_basis": (
            "creative-look-versus-zero-strength-primary-baseline"),
        "evidence": "same-chain-full-resolution-per-material",
    }
    if not strictly_increasing:
        return {
            "status": "blocked", "code": "non-monotonic-strength",
            "reason": f"当前素材三档变化不单调：{deltas}",
            "evidence": "same-chain-full-resolution-preview",
            "monotonicity": monotonicity,
        }
    requested = measured.get(requested_level)
    if requested is None:
        requested = (
            _run_photo_preflight_once(recipe, source, strength)
            if source["media_type"] == "photo"
            else _run_video_preflight_once(recipe, source, strength)
        )
    if "predicted_delta" not in requested:
        return {"status": "blocked", "code": "preview-failed",
                "reason": requested["reason"], "evidence": requested["evidence"],
                "monotonicity": monotonicity}
    predicted = float(requested["predicted_delta"])
    measured_for_gate = preflight_change_for_gate(requested)
    failed = requested["direction_failures"]
    state = classify_execution_preflight(
        measured_for_gate, relative_minimum, [], direction_failures=failed,
        preview_verified=True,
    )
    if state["status"] == "executable" and failed:
        state = {"status": "risky", "code": "direction-findings",
                 "reason": "同执行链预演可运行，但部分声明方向未达成：" + "、".join(failed)}
    return {
        **state,
        "evidence": "same-chain-full-resolution-preview",
        "predicted_delta": round(predicted, 6),
        "creative_delta_e_ok": round(measured_for_gate, 6),
        "required_delta": relative_minimum,
        "direction_failures": failed,
        "preview_size": requested["preview_size"],
        "working_profile": requested["working_profile"],
        "monotonicity": monotonicity,
        **({'preview_artifacts': {
            'status': 'internal-preview-not-final', 'baseline': baseline_artifact,
            'levels': [item['preview'] for item in measured.values()],
        }} if baseline_artifact is not None else {}),
    }


def scene_routing_note(light_fact: dict | None, scored: list[dict]) -> str | None:
    """解释为何已识别场景却仍退回通用方案。"""
    fact = light_fact or {}
    if fact.get("state") != "known" or not scene_facts.scene_keywords_for(fact.get("value")):
        return None
    matched = [item for item in scored if scene_facts.declares_scene(item, fact.get("value"))]
    if not matched or any(item.get("usable") for item in matched):
        return None
    reasons = []
    for item in matched:
        for blocker in item.get("blockers") or []:
            if blocker not in reasons:
                reasons.append(blocker)
    label = {"night": "夜景", "day": "白天", "indoor": "室内"}.get(
        fact.get("value"), str(fact.get("value")))
    return (f"已确认是{label}，但 {len(matched)} 套{label}专用配方都触发了素材安全门；"
            f"因此先给通用校正方向，不放宽门强套。主要原因："
            + "；".join(reasons[:2]))


def apply_video_topic_gate(catalog: list[dict], evidence: dict | None) -> tuple[list[dict], str | None]:
    """视频题材证据不足时不让题材风格借影调／色彩统计进入推荐。"""
    report = evidence or {}
    if report.get("status") != "insufficient":
        return catalog, None
    allowed = set(report.get("allowed_recipe_ids") or [])
    filtered = [item for item in catalog if item["id"] in allowed]
    note = (
        "视频逐镜头题材证据不足，本次只推荐基础校正；"
        "森林、风景、美食等题材风格需由可靠语义证据或你明确选择后再使用。"
    )
    return filtered, note


def _filtergraph_for(recipe: dict, source: dict, strength: float) -> str:
    if recipe['id'] == 'korean-cool':
        raise engine.SkillError('韩系清冷必须使用当前素材的独立保护执行链，不支持全局滤镜替代。', 4)
    effective = engine.perceptual_strength(recipe, strength)
    return engine.build_filter(
        engine.scaled_parameters(recipe["parameters"], 1.0),
        engine.scaled_curve(recipe.get("tone_curve"), 1.0),
        engine.scaled_hsl(recipe.get("hsl_bands"), 1.0),
        engine.adaptive_primary_grade(source, recipe),
        render_mix=effective,
        highlight_protection=engine.highlight_protection_for(source, recipe),
    )


def _option_from_score(scored: dict, catalog_raw: dict, source: dict,
                       source_palette: dict, strength: float, person_present: bool | None,
                       local_strategy: str = "none") -> dict:
    recipe = engine.find_recipe(catalog_raw, scored["recipe_id"], source["media_type"])
    recipe = engine.recipe_for_media(recipe, source["media_type"])
    art = recipe["art_direction"]
    suitability = recipe["suitability"]
    selected_strength = strength
    if scored.get("blockers"):
        target = {"error": "素材安全门已阻断，不运行颜色预测"}
        predicted_delta = 0.0
        required_delta = float(recipe["impact"]["min_delta_e"]) * engine.perceptual_strength(
            recipe, selected_strength
        )
        preflight = classify_execution_preflight(
            predicted_delta, required_delta, scored["blockers"], preview_verified=False,
        )
        preflight["evidence"] = "ranking-safety-gate"
    elif local_strategy != "none":
        target = {"error": "局部组合需先生成真实蒙版"}
        predicted_delta = 0.0
        required_delta = float(recipe["impact"]["min_delta_e"]) * engine.perceptual_strength(
            recipe, selected_strength
        )
        preflight = {
            "status": "risky",
            "reason": "局部组合必须先生成并检查真实蒙版，再做区内外分离预演",
            "evidence": "local-mask-required",
        }
    else:
        preflight = run_execution_preflight(recipe, source, selected_strength)
        if preflight.get("code") == "insufficient-change":
            for candidate_strength in (0.7, 0.85, 1.0):
                if candidate_strength <= selected_strength:
                    continue
                candidate = run_execution_preflight(recipe, source, candidate_strength)
                if candidate["status"] != "blocked":
                    selected_strength = candidate_strength
                    preflight = candidate
                    preflight["strength_adjustment"] = (
                        f"默认 {strength:.0%} 对当前素材变化不足；建议提高到 {selected_strength:.0%}，"
                        "仍需用户确认"
                    )
                    break
        try:
            if recipe['id'] == 'korean-cool':
                target = {'available': False, 'reason': '人物保护随像素位置变化，不能用全局色块预测；请查看真实预演。'}
            else:
                filtergraph = _filtergraph_for(recipe, source, selected_strength)
                target = palette_module.predict_target_palette(
                    source_palette, filtergraph, source["color"]["profile"], person_present
                )
        except palette_module.PaletteError as error:
            target = {"error": str(error)}
        predicted_delta = float(target.get("mean_delta_e_ok") or 0.0)
        required_delta = float(recipe["impact"]["min_delta_e"]) * engine.perceptual_strength(
            recipe, selected_strength
        )
    return {
        "recipe_id": recipe["id"],
        "recipe_name": recipe["name"],
        "collection": recipe.get("collection", "Core"),
        "score": scored.get("score"),
        "suggested_strength": selected_strength,
        "local_strategy": local_strategy,
        "capability_level": combos_module.LOCAL_STRATEGIES[local_strategy]["level"],
        "why_it_fits": scored.get("supports", []),
        "cautions": scored.get("penalties", []),
        "blockers": scored.get("blockers", []),
        "main_visual_action": art["light"],
        "thesis": art["thesis"],
        "palette_rule": art["palette_rule"],
        "risk": suitability["avoid_when"],
        "fallback": suitability["fallback"],
        "required_observations": suitability["required_observations"],
        "target_palette": target,
        "execution_preflight": {
            **preflight,
            "palette_estimate_delta": round(predicted_delta, 6),
            "palette_estimate_required": round(required_delta, 6),
        },
    }


def build(path: Path, mode: str = "smart", count: int = 3, seed: int | None = None,
          strength: float = 0.55, use_semantic: bool = True,
          user_confirmed_center: bool = False,
          ledger_path: Path | None = None,
          avoid: str | list[str] | None = None) -> dict:
    strength_input = engine.normalize_strength(strength)
    strength = strength_input["normalized"]
    source = engine.inspect_media(path)
    ledger_state = feedback_ledger.load(ledger_path)
    if source["color"]["support"] == "unsupported":
        # 复用 plan 路径那段带出路的提示。
        # v4.4.0 只改了 plan 的抛错点，漏了这里——而 Adobe RGB 素材在 suggest
        # 这一步就撞墙，根本走不到 plan，等于那个修复对真实用户没有生效。
        raise engine.SkillError(
            engine.unsupported_color_message(source["color"], str(path)), 3
        )
    profile = source["color"]["profile"]
    working = "display-p3" if profile == "display-p3" else "srgb"
    diagnosis = diagnose_module.diagnose(path, working, use_semantic=use_semantic)
    semantic = diagnosis.get("semantic")
    person_present = None
    if semantic and semantic.get("available"):
        import semantic_backend
        person_present = semantic_backend.class_presence(semantic, "person")

    source_palette = palette_module.extract_from_media(
        path, working, int(source["width"]), int(source["height"]),
        media_type=source["media_type"], label="原图色卡", person_present=person_present,
    )
    catalog_raw = engine.load_catalog()
    catalog = combos_module.load_tagged_catalog()
    catalog = [item for item in catalog if source["media_type"] in item["media_types"]]
    video_routing_note = None
    if source["media_type"] == "video" and mode in {"smart", "inspire"}:
        catalog, video_routing_note = apply_video_topic_gate(
            catalog, diagnosis.get("video_topic_evidence")
        )
    avoid_terms = normalize_avoid(avoid)
    excluded_by_avoid = []
    if avoid_terms:
        filtered = []
        for item in catalog:
            recipe = engine.find_recipe(catalog_raw, item["id"], source["media_type"])
            reasons = avoid_reasons(recipe, avoid_terms)
            if reasons:
                excluded_by_avoid.append({
                    "recipe_id": recipe["id"], "recipe_name": recipe["name"],
                    "reasons": reasons,
                })
            else:
                filtered.append(item)
        catalog = filtered
    media_type = source["media_type"]
    already_polished = diagnosis.get("already_polished") or {"candidate": False}
    polished_photo = media_type == "photo" and bool(already_polished.get("candidate"))
    season_signal = infer_season_signal(diagnosis)
    rejected_recipe_ids = {
        item["recipe_id"] for item in ledger_state.get("entries", [])
        if item.get("source_sha256") == source["sha256"]
        and item.get("verdict") == "rejected"
        and abs(float(item.get("strength", -1)) - strength) < 1e-9
    }

    payload = {
        "schema_version": SUGGEST_SCHEMA_VERSION,
        "mode": mode,
        "source": {
            "path": str(path), "media_type": media_type,
            "sha256": source["sha256"],
            "color_profile": source["color"]["label"],
            "duration": source["duration"], "has_audio": source["has_audio"],
        },
        "diagnosis": {
            "visual_brief": diagnosis["visual_brief"],
            "light_context": diagnosis["light_context"],
            "light_fact": diagnosis.get("light_fact"),
            "tone": diagnosis["analysis"]["tone"],
            "color": diagnosis["analysis"]["color"],
            "texture": diagnosis["analysis"]["texture"],
        },
        "semantic_status": (
            {"available": True,
             "backend": semantic["backend"]["name"],
             "license": semantic["backend"]["license"],
             "device": semantic["backend"]["device"],
             "os_build": semantic["backend"].get("os_build"),
             "classes": {k: {"status": v.get("status"), "present": v.get("present"),
                              "probe_status": v.get("probe_status")}
                         for k, v in semantic["classes"].items()}}
            if semantic and semantic.get("available")
            else {"available": False, "reason": (semantic or {}).get("degraded_reason", "语义后端未启用")}
        ),
        "source_palette": source_palette,
        "season_signal": season_signal,
        "strength_input": strength_input,
        "boundary": (
            "本入口不渲染文件、不生成已确认计划。选定方向后仍需 plan → 确认 plan_id → render 三步。"
        ),
        "feedback_memory": {
            "ledger": ledger_state["path"],
            "warning": ledger_state["warning"],
            "boundary": "只按素材内容哈希、配方与强度匹配；不建立用户画像。",
        },
        "session_avoid": {
            "terms": avoid_terms,
            "unrecognized_terms": [
                term for term in avoid_terms if term not in KNOWN_AVOID_TERMS],
            "excluded": excluded_by_avoid,
            "requested_count": count,
            "available_count": len([
                item for item in catalog if media_type in item["media_types"]]),
            "persisted": False,
            "boundary": "只在本次命令内作硬过滤，不写盘、不建立用户画像。",
        },
        "already_polished": already_polished,
    }
    if media_type == "video":
        payload["temporal"] = diagnosis.get("temporal")
        payload["per_shot"] = diagnosis.get("per_shot")
        payload["video_topic_evidence"] = diagnosis.get("video_topic_evidence")
        # 视频的判语是看某一帧得出的，必须说是哪一帧、为什么是它。
        # 不写出来，用户会以为「偏暗」是整段的性质，而它只是那一帧的性质。
        payload["diagnosis"]["primary_frame"] = diagnosis.get("primary_frame")

    if mode in ("smart", "all"):
        ranked_all = fit_module.rank(catalog, diagnosis, semantic, media_type,
                                     len(catalog), diversity=False)
        payload["routing_note"] = video_routing_note or scene_routing_note(
            diagnosis.get("light_fact"), ranked_all
        )
        options = []
        deferred = []
        explicit_only = []
        preflight_attempts = 0
        preflight_limit = smart_preflight_budget(count) if mode == "smart" else None
        for scored in ranked_all:
            recipe = engine.find_recipe(catalog_raw, scored["recipe_id"], media_type)
            if mode == "smart" and polished_photo and recipe["id"] == "natural-clean":
                continue
            if mode == "smart" and requires_explicit_intent(recipe):
                explicit_only.append({
                    "recipe_id": recipe["id"],
                    "recipe_name": recipe["name"],
                    "reason": "黑白等强不可逆风格需要用户明确选择，不参与无偏好智能推荐",
                })
                continue
            if preflight_limit is not None and preflight_attempts >= preflight_limit:
                break
            preflight_attempts += 1
            option = _option_from_score(
                scored, catalog_raw, source, source_palette,
                0.30 if polished_photo else strength, person_present
            )
            if mode == "all":
                options.append(option)
                continue
            if option["execution_preflight"]["status"] == "executable":
                options.append(option)
                fresh_count = sum(
                    1 for item in options if item["recipe_id"] not in rejected_recipe_ids
                )
                if fresh_count >= count:
                    break
            else:
                deferred.append(option)
        if mode == "smart" and len(options) < count:
            # 无法凑满可执行候选时宁可诚实给 risky，也不把 blocked 塞回首屏。
            options.extend(
                item for item in deferred
                if item["execution_preflight"]["status"] == "risky"
            )
            options = options[:count]
            payload["preflight_blocked"] = [
                {
                    "recipe_id": item["recipe_id"],
                    "recipe_name": item["recipe_name"],
                    "reason": item["execution_preflight"]["reason"],
                }
                for item in deferred if item["execution_preflight"]["status"] == "blocked"
            ]
        options = annotate_season(options, catalog_raw, season_signal, media_type)
        options = apply_feedback_memory(options, ledger_state, source["sha256"], strength)
        if mode == "smart" and options and options[0].get("feedback_memory"):
            payload["feedback_suppressed"] = options
            options = []
        payload["options"] = options[:count] if mode == "smart" else options
        if mode == "smart":
            payload["preflight_scope"] = {
                "attempted": preflight_attempts,
                "limit": preflight_limit,
                "boundary": "只预演适配排序靠前的有限候选；完整目录仍可直接点名并独立预演。",
            }
        if explicit_only:
            payload["explicit_only_deferred"] = explicit_only
        if mode == "all":
            payload["catalog_overview"] = [
                {"id": item["id"], "name": item["name"], "collection": item["collection"],
                 "tags": item["tags"], "scenes": item["scenes"], "summary": item["summary"]}
                for item in catalog if media_type in item["media_types"]
            ]

    if mode == "inspire":
        capabilities = {"semantic_classes": {}, "user_confirmed_center": user_confirmed_center}
        if semantic and semantic.get("available"):
            capabilities["semantic_classes"] = {
                key: {"status": value.get("status"), "present": bool(value.get("present"))}
                for key, value in semantic["classes"].items()
            }
        effective_seed = seed if seed is not None else combos_module.stable_seed(
            path.name, diagnosis["analysis"]["tone"]["p50"], diagnosis["analysis"]["color"]["colorfulness"]
        )
        # 灵感组合也要尊重诊断：把适配分数作为采样顺序，避免「随机」变成「不看素材」。
        scores = {item["recipe_id"]: item for item in
                  fit_module.rank(catalog, diagnosis, semantic, media_type, len(catalog), diversity=False)}
        ordered = sorted(
            (item for item in catalog if media_type in item["media_types"]),
            key=lambda item: -scores.get(item["id"], {}).get("score", 0.0),
        )
        usable = [
            item for item in ordered
            if scores.get(item["id"], {}).get("usable", True)
            and not requires_explicit_intent(
                engine.find_recipe(catalog_raw, item["id"], media_type))
        ]
        result = combos_module.build_combinations(
            diagnosis, usable or ordered, capabilities, effective_seed, count, media_type
        )
        options = []
        for combo in result["combinations"]:
            scored = scores.get(combo["executes_as"]["recipe_id"], {})
            option = _option_from_score(
                scored if scored else {"recipe_id": combo["executes_as"]["recipe_id"]},
                catalog_raw, source, source_palette,
                combo["executes_as"]["suggested_strength"], person_present,
                combo["selection"]["local"],
            )
            option["combination"] = combo["selection"]
            option["combination_readable"] = combo["readable"]
            option["title"] = combo["title"]
            options.append(option)
        options = annotate_season(options, catalog_raw, season_signal, media_type)
        options = apply_feedback_memory(options, ledger_state, source["sha256"], strength)
        payload["options"] = options[:count]
        payload["combination_report"] = {
            key: result[key] for key in
            ("seed", "reproducible", "conflict_rate", "label_executor_mismatch",
             "min_axis_distance", "conflicts_rejected_during_search", "diagnosis_derived_blocks", "boundary")
        }

    payload["display"] = _render_display(payload)
    return payload


SUPPORTED_FEEDBACK = {
    "更多色彩", "更有氛围", "更有情绪", "更自然", "肤色回退", "暗部提亮",
}


def smart_preflight_budget(count: int) -> int:
    """智能推荐只预演排序靠前的有限候选；完整目录仍可被直接点名。"""
    return max(8, count)


def partition_rejected_options(options: list[dict], state: dict,
                               source_sha256: str) -> tuple[list[dict], list[dict]]:
    """refine 专用：同素材曾被显式否决的配方（任意强度）让出推荐位。

    只按（源哈希，配方）匹配，不外推到其他素材、不建立全局画像；被让位的
    选项保留完整内容并附上否决记录，作为「仍可明确点名重试」的出口。
    否决匹配刻意不看强度——否则旧配方只换一档强度就能重新占据 Top-1。
    """
    rejected = {}
    for item in state.get("entries", []):
        if (item.get("source_sha256") == source_sha256
                and item.get("verdict") == "rejected"):
            rejected[item["recipe_id"]] = item
    front = []
    held = []
    for option in options:
        record = rejected.get(option["recipe_id"])
        if record is None:
            front.append(option)
            continue
        annotated = dict(option)
        annotated["feedback_memory"] = {
            "status": "human_rejected",
            "recorded_at": record["recorded_at"],
            "rejected_strength": record["strength"],
            "reason": record["reason"],
            "message": (
                f"此方向曾被你在本素材上否决（{record['recorded_at']}／{record['reason']}），"
                "不再自动占据推荐位；如仍想尝试请明确点名"
                "（plan --style 该配方，或 refine --current-style 该配方）"
            ),
        }
        held.append(annotated)
    return front, held


def refine(path: Path, current_style: str, feedback: str, strength: object = "0.55",
           count: int = 3, use_semantic: bool = True,
           consecutive_failures: int = 0, ledger_path: Path | None = None) -> dict:
    """把自然语言反馈转成可审查的新方向；不渲染、不静默覆盖当前方案。"""
    if feedback not in SUPPORTED_FEEDBACK:
        raise engine.SkillError(
            "不支持的反馈。可选：" + "、".join(sorted(SUPPORTED_FEEDBACK))
            + "。原文件未改动。下一步：使用 --feedback 加上述任一短语。",
            3,
        )
    source = engine.inspect_media(path)
    ledger_state = feedback_ledger.load(ledger_path)
    catalog_raw = engine.load_catalog()
    # --current-style 本身就是用户的明确点名：manual-executable 配方在这里
    # 必须可查——用户刚用它渲染过成片，refine 是它唯一的反馈闭环入口。
    # 候选推荐池依旧只来自 build() 的 active 配方，不因此扩大自动推荐面。
    current = engine.find_recipe(catalog_raw, current_style, source["media_type"],
                                 allow_manual=True)
    normalized = engine.normalize_strength(strength)
    recovery = recovery_policy(consecutive_failures)
    compatibility = feedback_compatibility(feedback, current["visual_targets"])
    recommendations = build(
        path, mode="smart", count=max(6, count * 2), strength=normalized["normalized"],
        use_semantic=use_semantic, ledger_path=ledger_path,
    )["options"]

    def matches(option: dict) -> bool:
        recipe = engine.find_recipe(catalog_raw, option["recipe_id"], source["media_type"])
        targets = recipe["visual_targets"]
        if feedback == "更多色彩":
            return targets.get("colorfulness") != "decrease"
        if feedback in {"更有氛围", "更有情绪"}:
            return recipe.get("collection") == "BLCaptain Signature"
        if feedback == "更自然":
            return recipe["id"] == "natural-clean"
        return recipe["id"] == current["id"]

    matched = [item for item in recommendations if matches(item)]
    fresh, rejected_alternatives = partition_rejected_options(
        matched, ledger_state, source["sha256"])
    if current["id"] not in {item["recipe_id"] for item in rejected_alternatives}:
        current_retry = {
            "recipe_id": current["id"],
            "recipe_name": current["name"],
            "suggested_strength": normalized["normalized"],
            "explicit_retry_only": True,
        }
        _, held_current = partition_rejected_options(
            [current_retry], ledger_state, source["sha256"])
        rejected_alternatives = held_current + rejected_alternatives
    selected = fresh[:count]
    options_note = None
    if not selected:
        options_note = (
            "本次没有可直接推荐的新方向：候选或被反馈目标过滤，或此前已被你否决。"
            "可从 rejected_alternatives 明确点名重试，或运行 next_command 重新查看方向。"
        )
    special_action = None
    if feedback == "暗部提亮":
        special_action = {
            "type": "confirmed-adjustment",
            "adjustments": {"shadow_lift": 0.04},
            "boundary": "黑场保持为黑，只提暗部中段；需生成新计划并再次确认",
        }
    elif feedback == "肤色回退":
        special_action = {
            "type": "semantic-local",
            "strategy": "skin-protect",
            "boundary": "必须先 --detect-local，并且真实蒙版预演为 executable 后才能确认",
        }
    next_command = (
        f"python3 scripts/blcaptain_color.py suggest --input {shlex.quote(str(path))} "
        f"--strength {normalized['normalized']} --count {count}"
    )
    return {
        "schema_version": SUGGEST_SCHEMA_VERSION,
        "source": str(path),
        "current_style": {"id": current["id"], "name": current["name"]},
        "feedback": feedback,
        "compatibility": compatibility,
        "recovery": recovery,
        "strength_input": normalized,
        "special_action": special_action,
        "options": selected,
        "options_note": options_note,
        "rejected_alternatives": rejected_alternatives,
        "status": "needs-user-confirmation",
        "original_safe": True,
        "next_command": next_command,
        "boundary": (
            "refine 只生成新方向，不渲染文件；用户确认新方向后再 plan → render。"
            "同素材被否决过的配方不再自动占据推荐位，只保留在 rejected_alternatives "
            "供明确点名重试；否决不外推到其他素材。"
        ),
    }


def _render_display(payload: dict) -> dict:
    lines = list(payload["diagnosis"]["visual_brief"])
    already_polished = payload.get("already_polished") or {}
    if already_polished.get("candidate"):
        lines.insert(
            0,
            "这张图完成度已高：建议保持原样，或只尝试 30% 的轻风格变体；"
            "这是技术指标候选，不是审美评分。",
        )
    palette = payload["source_palette"]
    swatches = "  ".join(
        f"{c['hex']}({c['role']} {c['area_ratio']:.0%})" for c in palette["colors"]
    )
    options = []
    for index, option in enumerate(payload.get("options", [])[:8], start=1):
        target = option.get("target_palette") or {}
        moved = target.get("colors_meaningfully_moved")
        total = target.get("color_count")
        options.append({
            "序号": index,
            "名称": option.get("title") or option["recipe_name"],
            "执行配方": f"{option['recipe_name']}（{option['recipe_id']}）",
            "为什么适合": option["why_it_fits"][:3] or [option["thesis"]],
            "季节关系": (option.get("seasonal_context") or {}).get("relation"),
            "主要视觉动作": option["main_visual_action"],
            "颜色去向": (
                f"{moved}/{total} 个主要颜色会实质改变，平均感知位移 {target.get('mean_delta_e_ok')}"
                if moved is not None else "目标色卡不可用"
            ),
            "风险": option["risk"],
            "回退": option["fallback"],
            "能力等级": option["capability_level"],
            "建议强度": f"{option['suggested_strength']:.0%}",
            "执行预演": option["execution_preflight"],
            "否决记忆": (option.get("feedback_memory") or {}).get("message"),
        })
    risky_names = [
        item["名称"] for item in options
        if (item.get("执行预演") or {}).get("status") == "risky"
    ]
    display = {
        "一句话诊断": lines,
        "推荐说明": payload.get("routing_note") or "推荐已按当前可确认的场景事实与素材安全门筛选。",
        "原图配色": swatches,
        "配色关系": f"{palette['harmony']['relation']} — {palette['harmony']['detail']}",
        "色卡风险": palette["risks"],
        "可选方向": options,
        "下一步": (
            "告诉我选第几个方向、想要多大强度（建议先 55%），我再生成方案编号 plan_id 给你确认；"
            "确认后才会生成新文件，原文件不会被改动。也可以说「看全部风格」或「再来三组灵感」。"
        ),
    }
    if not options:
        report = payload.get("combination_report") or {}
        blocked_reasons = "；".join(
            item["reason"] for item in payload.get("preflight_blocked", []) if item.get("reason")
        )
        display["暂无可选方向"] = (
            report.get("empty_reason") or blocked_reasons or report.get("boundary")
            or "没有符合当前素材、偏好与安全约束的可选方向。"
        )
        display["下一步"] = (
            "先查看上述原因；可以保持原样，或提供更适合目标风格的原素材后重新推荐。"
            "不绕过安全约束、不回填已排除的方向，也不自动提高强度。原文件未改动。"
        )
    if already_polished.get("candidate"):
        display["保持原样"] = {
            "动作": "不生成新文件",
            "原因": "原片的影调、综合色彩、局部层次与干净度已经形成完整组合。",
            "仍可选择": "如果你明确想要风格变化，可从下方 30% 轻变体中选择；系统不会替你拒绝。",
        }
    if risky_names:
        # SKILL 契约：executable 才能直接进入确认；risky 只是诚实展示，不是可直接确认的方向。
        display["risky提醒"] = (
            f"{'、'.join(risky_names)} 标为 risky，不能直接确认生成："
            "预演中列出的未达成项需要你先人工看过并接受，或选择 executable 方向／换素材。"
        )
    avoid = payload.get("session_avoid") or {}
    if avoid.get("terms"):
        available = len(payload.get("options") or [])
        display["口味避开"] = session_avoid_message(avoid, available)
    return display


def main() -> int:
    parser = argparse.ArgumentParser(description="智能推荐 / 灵感组合 / 全部风格 + 配色色卡")
    parser.add_argument("--input", required=True)
    parser.add_argument("--mode", default="smart", choices=["smart", "inspire", "all"])
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--strength", default="0.55")
    parser.add_argument("--no-semantic", action="store_true")
    parser.add_argument("--user-confirmed-center", action="store_true")
    parser.add_argument("--display-only", action="store_true")
    parser.add_argument("--ledger")
    parser.add_argument("--avoid", help="本次会话明确避开的方向，逗号分隔，例如：黄红,暖调")
    args = parser.parse_args()
    try:
        payload = build(
            Path(args.input).expanduser().resolve(), args.mode, args.count, args.seed,
            args.strength, use_semantic=not args.no_semantic,
            user_confirmed_center=args.user_confirmed_center,
            ledger_path=Path(args.ledger).expanduser().resolve() if args.ledger else None,
            avoid=args.avoid,
        )
    except (engine.SkillError, diagnose_module.DiagnoseError, palette_module.PaletteError) as error:
        print(engine.format_cli_error(error, args), file=sys.stderr)
        return getattr(error, "code", 3)
    print(json.dumps(payload["display"] if args.display_only else payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
