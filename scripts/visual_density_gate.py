#!/usr/bin/env python3
"""综合色彩之外的区域密度方向门。

它只比较同素材、同固定选区的工程代理；不识别语义，也不判断审美。
"""

from __future__ import annotations


def evaluate_region_density(baseline: dict, candidate: dict,
                            strength_percent: int,
                            focus_region: str = "warm") -> dict:
    """综合色彩、区域密度、色相层次与区域光比必须共同成立。"""
    base_regions = baseline["regions"]
    current_regions = candidate["regions"]
    strength = max(1, min(100, int(strength_percent)))
    required_gain = 1.0 + 0.03 * strength / 55.0
    failures = []
    if current_regions["whole"]["chroma"] < base_regions["whole"]["chroma"]:
        failures.append("whole_chroma_regressed")
    if current_regions[focus_region]["density"] < (
            base_regions[focus_region]["density"] * required_gain):
        failures.append(f"{focus_region}_density_regressed")
    if candidate["warm_cool_separation"] < (
            baseline["warm_cool_separation"] * required_gain):
        failures.append("warm_cool_separation_insufficient")
    if candidate["warm_cool_luma_contrast"] < (
            baseline["warm_cool_luma_contrast"] * 0.98):
        failures.append("regional_light_contrast_regressed")
    for name in ("red_green", "green_blue"):
        if current_regions[focus_region].get(name, 0.0) < (
                base_regions[focus_region].get(name, 0.0) * 0.95):
            failures.append(f"{focus_region}_{name}_layer_regressed")
    return {
        "gate": "region-density-v1",
        "status": "blocked" if failures else "passed",
        "blocking_failures": failures,
        "required_gain": round(required_gain, 6),
        "boundary": (
            "工程方向门只拒绝同固定选区内明显洗浅、光比坍塌或色相层次倒退；"
            "选区必须由具体配方另行证明，是否更有感染力仍由人工观看决定"
        ),
    }
