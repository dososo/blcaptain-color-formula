"""肤色记忆色的 CIELAB 走廊与降档决策。"""

from __future__ import annotations

import math
import statistics


SKIN_HUE_CORRIDOR_DEG = (30.0, 50.0)
RELATIVE_HUE_SHIFT_LIMIT_DEG = 3.0
RELATIVE_CORRIDOR_REGRESSION_LIMIT_DEG = 2.0


def _linear(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def rgb_to_lab(pixel: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (_linear(channel) for channel in pixel)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(value: float) -> float:
        delta = 6 / 29
        return value ** (1 / 3) if value > delta ** 3 else value / (3 * delta ** 2) + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _summary(samples: list[tuple[int, int, int]]) -> dict:
    labs = [rgb_to_lab(pixel) for pixel in samples]
    chromas = [math.hypot(a, b) for _, a, b in labs]
    hues = [(math.degrees(math.atan2(b, a)) + 360.0) % 360.0 for _, a, b in labs]
    return {
        "sample_count": len(samples),
        "median_hue_deg": statistics.median(hues),
        "median_chroma": statistics.median(chromas),
        "corridor_ratio": sum(SKIN_HUE_CORRIDOR_DEG[0] <= hue <= SKIN_HUE_CORRIDOR_DEG[1]
                              for hue in hues) / len(hues),
    }


def _circular_distance_deg(left: float, right: float) -> float:
    delta = abs(left - right) % 360.0
    return min(delta, 360.0 - delta)


def _distance_to_corridor_deg(hue: float) -> float:
    low, high = SKIN_HUE_CORRIDOR_DEG
    if low <= hue <= high:
        return 0.0
    return min(_circular_distance_deg(hue, low), _circular_distance_deg(hue, high))


def evaluate_skin(before: list[tuple[int, int, int]],
                  after: list[tuple[int, int, int]],
                  chroma_gain_limit: float = 1.1) -> dict:
    if len(before) < 12 or len(after) < 12:
        return {"passed": None, "status": "skipped", "blocking_failures": [],
                "reason": "肤色样本不足 12 个，不能判定"}
    source, result = _summary(before), _summary(after)
    failures = []
    source_hue = source["median_hue_deg"]
    result_hue = result["median_hue_deg"]
    source_inside = SKIN_HUE_CORRIDOR_DEG[0] <= source_hue <= SKIN_HUE_CORRIDOR_DEG[1]
    hue_shift = _circular_distance_deg(source_hue, result_hue)
    source_distance = _distance_to_corridor_deg(source_hue)
    result_distance = _distance_to_corridor_deg(result_hue)
    if source_inside:
        hue_policy = "absolute-corridor"
        if result_distance > 0.0:
            failures.append("skin_hue_corridor")
    else:
        hue_policy = "relative-preservation"
        if (hue_shift > RELATIVE_HUE_SHIFT_LIMIT_DEG or
                result_distance > source_distance + RELATIVE_CORRIDOR_REGRESSION_LIMIT_DEG):
            failures.append("skin_hue_relative_drift")
    allowed_chroma = source["median_chroma"] * float(chroma_gain_limit)
    if result["median_chroma"] > allowed_chroma:
        failures.append("skin_chroma_cap")
    return {
        "gate": "skin_memory_corridor",
        "status": "measured",
        "passed": not failures,
        "blocking_failures": failures,
        "source": {key: round(value, 6) if isinstance(value, float) else value
                   for key, value in source.items()},
        "result": {key: round(value, 6) if isinstance(value, float) else value
                   for key, value in result.items()},
        "corridor_deg": list(SKIN_HUE_CORRIDOR_DEG),
        "corridor_ratio": round(result["corridor_ratio"], 6),
        "hue_policy": hue_policy,
        "hue_shift_deg": round(hue_shift, 6),
        "source_distance_to_corridor_deg": round(source_distance, 6),
        "result_distance_to_corridor_deg": round(result_distance, 6),
        "relative_hue_shift_limit_deg": RELATIVE_HUE_SHIFT_LIMIT_DEG,
        "relative_corridor_regression_limit_deg": RELATIVE_CORRIDOR_REGRESSION_LIMIT_DEG,
        "decision_statistic": "median_hue_deg",
        "chroma_gain_limit": float(chroma_gain_limit),
        "allowed_chroma": round(allowed_chroma, 6),
        "boundary": "只在可靠肤色蒙版内判定；原片位于走廊内时使用绝对走廊，原片天然位于走廊外时只限制相对漂移与继续远离；CIELAB 阈值是工程合同，不代表肤色审美评分。",
    }


def select_safe_strength(requested_strength: int, reports: dict[int, dict]) -> dict:
    """给计划阶段选择不高于用户请求的最高安全档，不在渲染后偷改。"""
    candidates = sorted((level for level in reports if level <= requested_strength), reverse=True)
    for level in candidates:
        if reports[level].get("passed") is True:
            return {
                "status": "unchanged" if level == requested_strength else "downgraded",
                "requested_strength": requested_strength,
                "selected_strength": level,
                "reason": "肤色走廊通过" if level == requested_strength else "较高档越界，计划阶段降到安全档",
            }
    return {"status": "blocked", "requested_strength": requested_strength,
            "selected_strength": None, "reason": "最低预演档仍越出肤色走廊，不得发布"}
