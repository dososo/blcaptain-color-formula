#!/usr/bin/env python3
"""把每套 Style Bible 编译成可追踪的机器门绑定。

编译不把尚未标定的审美文字伪装成数值门：能自动检查的绑定到现有门，
暂时只能人工看的逐条列入 manual。这样既不会丢规则，也不会制造假绿灯。
"""

from __future__ import annotations


ZONES = {"black", "toe", "mid", "shoulder", "white"}
PALETTE_PARTS = {"primary", "secondary", "accent", "hue_family_budget"}
STRENGTH_LEVELS = {"30", "55", "80", "monotonicity"}


def _forbidden_binding(rule: str) -> dict:
    if "冷暖分离" in rule:
        return {"rule": rule, "gate": "teal_orange_zero", "stage": "catalog-static"}
    if rule == "暗角":
        return {"rule": rule, "gate": "vignette_zero", "stage": "catalog-static"}
    if rule == "颗粒":
        return {"rule": rule, "gate": "grain_zero", "stage": "catalog-static"}
    if "随机视频颗粒" in rule:
        return {"rule": rule, "gate": "deterministic_non_temporal_grain", "stage": "render-chain"}
    if "去雾" in rule or "清晰度" in rule:
        return {"rule": rule, "gate": "no_dehaze_or_excess_clarity", "stage": "render-chain"}
    if "抬白" in rule or "苍白" in rule:
        return {"rule": rule, "gate": "path_to_white_and_tonal_health", "stage": "post-render"}
    if "肤色" in rule or "脸" in rule:
        return {"rule": rule, "gate": "skin_memory_corridor", "stage": "post-render"}
    return {"rule": rule, "reason": "当前没有经验证的自动判据，保留为人工审片项"}


def compile_profile(recipe: dict) -> dict:
    bible = recipe.get("style_bible") or {}
    has_bible = bool(bible)
    violations = []
    five_zone = bible.get("five_zone_tone") or {}
    palette = bible.get("palette") or {}
    strengths = bible.get("strength_targets") or {}
    forbidden = bible.get("forbidden") or []

    if has_bible and set(five_zone) != ZONES:
        violations.append("five_zone_tone 必须逐一声明 black/toe/mid/shoulder/white")
    if has_bible and set(palette) != PALETTE_PARTS:
        violations.append("palette 必须逐一声明 primary/secondary/accent/hue_family_budget")
    if has_bible and set(strengths) != STRENGTH_LEVELS:
        violations.append("strength_targets 必须逐一声明 30/55/80/monotonicity")

    targets = recipe.get("visual_targets") or {}
    if targets.get("colorfulness") == "increase":
        gain = float((recipe.get("parameters") or {}).get("saturation", 1.0)) - 1.0
        gain += sum(float(item.get("saturation", 0.0)) for item in recipe.get("hsl_bands") or [])
        layered_binding = (recipe.get("capability_bindings") or {}).get("colorfulness")
        if gain <= 0 and not layered_binding:
            violations.append("声明 colorfulness=increase，但彩度参数净增益不为正")
    if targets.get("subject_separation") == "increase":
        if not (recipe.get("capability_bindings") or {}).get("subject_separation"):
            violations.append("声明 subject_separation=increase，但没有 L1/L2 动作绑定")

    automated = []
    manual = []
    for rule in forbidden if isinstance(forbidden, list) else []:
        binding = _forbidden_binding(rule)
        (automated if "gate" in binding else manual).append(binding)

    parameters = recipe.get("parameters") or {}
    if any(item["gate"] == "teal_orange_zero" for item in automated):
        if abs(float(parameters.get("teal_orange", 0.0))) > 1e-9:
            violations.append("forbidden 禁止冷暖分离，但 teal_orange 非零")
    if any(item["gate"] == "vignette_zero" for item in automated):
        if float(parameters.get("vignette", 0.0)) > 0:
            violations.append("forbidden 禁止暗角，但 vignette 非零")
    if any(item["gate"] == "grain_zero" for item in automated):
        if float(parameters.get("grain", 0.0)) > 0:
            violations.append("forbidden 禁止颗粒，但 grain 非零")

    return {
        "schema_version": "1.0.0",
        "recipe_id": recipe.get("id"),
        "calibration_status": bible.get("calibration_status"),
        "coverage": "style-bible" if has_bible else "structured-recipe",
        "bindings": {
            "five_zone_tone": {
                "source": five_zone,
                "gates": {"black": "additive_lift", "white": "path_to_white",
                          "toe_mid_shoulder": "directional_audit"},
                "boundary": "未给数值范围的分区文字保留为人工审片，不据此自动判通过。",
            },
            "palette": {
                "source": palette,
                "hue_limits": bible.get("hue_chroma_lightness") or {},
                "gate": "palette_and_memory_color_review",
                "boundary": "有数值才可自动量测；定性规则进入人工审片。",
            },
            "forbidden": {"automated": automated, "manual": manual},
            "strength_targets": {
                "source": strengths,
                "gate": "same-chain-per-material-monotonicity",
                "levels": [30, 55, 80],
            },
        },
        "violations": violations,
    }
