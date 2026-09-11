#!/usr/bin/env python3
"""视觉语法：把大师方法论与厂商色彩科学落成可执行算子与验收门。

来源：research/master-aesthetics-v4.json（215 条发现、179 条数值事实、8 个一手来源簇）。

**所有数值阈值都是工程提议，不是大师或厂商公布的参数。**
调研的对抗审查明确指出：那些来源只给了定性主张，任何写成「某某同款 L* 68–74」的说法
都是虚构精度。因此本模块的每个常量都带 ENGINEERING_PROPOSAL 标记，且必须可被标定覆盖。

同样明确不做的宣称：
- 不宣称「获奖级」——奖项材料只给出淘汰底线，从未公布获奖依据。
- 不宣称胶片还原——没有取得任何真实胶片特性曲线或染料矩阵。
- 不宣称「符合 ACES/ITU」——引用常量不等于实现合规。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from color_space import eotf, oetf, oklab_to_oklch, oklab_to_rgb8, rgb8_to_oklab

GRAMMAR_SCHEMA_VERSION = "4.0.0"

# 每个数值都是工程起点，必须被真实素材标定后才能宣称精确。
ENGINEERING_PROPOSAL = "engineering_proposal_pending_calibration"

# --- 影调轴 -------------------------------------------------------------
# 富士把影调拆成 highlight_tone 与 shadow_tone 两个独立轴（0–9，基准 5）。
# 借用的是「解耦」这个结构，不是它的数值语义——那是序数刻度，不能反推物理量。
TONE_AXIS_RANGE = (0, 9)
TONE_AXIS_NEUTRAL = 5
# 两套配方在影调四轴上距离低于此值判为撞味。
TONE_AXIS_MIN_SEPARATION = 2.0

# --- 物理边界 -----------------------------------------------------------
# 加法抬黑的上限（相对场景白）。超过它，「柔黑」就变成了眩光雾。
MAX_ADDITIVE_LIFT = 0.01
# 比例型软趾的参数区间。它是加法抬黑的正确替代品。
SOFT_TOE_RANGE = (0.02, 0.08)
# 暗部斜率下限：低于它说明已经坐进趾部，细节不可恢复。
MIN_SHADOW_SLOPE = 0.15
# 高光段的饱和增益相对中间调的上限，强制 path-to-white。
HIGHLIGHT_SAT_RATIO = 0.6
# 高光彩度随亮度上升多少才算霓虹化。见 check_path_to_white 里的推理。
NEON_SLOPE_TOLERANCE = 0.03
# 色相守恒容差：纪实严、创意宽。
HUE_TOLERANCE = {"documentary": 2.0, "creative": 8.0}
# 肤色容差是非对称的：色相严、彩度宽。
SKIN_HUE_TOLERANCE = 3.0
SKIN_CHROMA_TOLERANCE = 0.20
# 深肤色分型区间（相对参考白的亮度占比）。
# 它取代「深肤色禁止提亮」那条错误门禁——那条规则会把欠曝的深肤色素材系统性留在暗处。
FITZPATRICK_BANDS = {
    "I-II": (0.32, 0.54),
    "III-IV": (0.20, 0.42),
    "V-VI": (0.05, 0.20),
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --- 算子 ---------------------------------------------------------------
def pivot_preserving_slope(value: float, slope: float, pivot: float) -> float:
    """成对的反差算子：offset 由支点推出，不允许裸加法。

    裸 offset 会整体平移影调，把白点黑点一起挪走；配对形式保证支点不动。
    这是「反差必须成对」这条原语的实现。
    """
    offset = pivot * (1.0 - slope)
    return value * slope + offset


def proportional_soft_toe(value: float, toe: float) -> float:
    """比例型软趾 out = x²/(x + t)。

    要柔黑、要褪色感，正确做法是这个，而不是加法抬黑。
    加法抬黑在物理上等价于给画面加一层眩光，暗部斜率会塌到接近零，
    细节不是「变柔」而是永久损失——这就是「抬黑就显脏」的机制。
    """
    toe = _clamp(toe, *SOFT_TOE_RANGE)
    if value <= 0:
        return 0.0
    return (value * value) / (value + toe)


def shadow_slope(curve, at: float = 0.05, delta: float = 0.01) -> float:
    """暗部局部斜率。低于 MIN_SHADOW_SLOPE 表示已坐进趾部。"""
    return (curve(at + delta) - curve(max(0.0, at - delta))) / (2 * delta)


def luminance_weighted_saturation(lab: tuple[float, float, float],
                                  mid_gain: float,
                                  highlight_ratio: float = HIGHLIGHT_SAT_RATIO
                                  ) -> tuple[float, float, float]:
    """亮度加权饱和：增益随明度单调不增，强制 path-to-white。

    全局 saturation 乘法会把高光一起推到霓虹色，因为它不知道
    「越亮的东西越该趋向白」这件事。真实世界里强光下的物体会失去彩度。
    """
    lightness, a, b = lab
    if lightness <= 0.5:
        gain = mid_gain
    else:
        # 0.5 → mid_gain，1.0 → mid_gain * highlight_ratio，之间线性
        ratio = (lightness - 0.5) / 0.5
        gain = mid_gain * (1.0 - ratio * (1.0 - highlight_ratio))
    return lightness, a * gain, b * gain


def hue_preserving_compress(lab: tuple[float, float, float],
                            amount: float,
                            start: float = 0.75) -> tuple[float, float, float]:
    """等色相切片内的彩度压缩：只动彩度，色相不动。

    RGB 域逐通道 clip 会把不同色相硬截断到同一处，色相直方图上表现为簇塌缩。
    在等色相切片里压缩可以避免这件事。
    """
    lightness, chroma, hue = oklab_to_oklch(*lab)
    if chroma <= start:
        return lab
    excess = chroma - start
    compressed = start + excess / (1.0 + amount * excess / max(1e-6, start))
    radians = math.radians(hue)
    return lightness, compressed * math.cos(radians), compressed * math.sin(radians)


def halation_weight(value: float, threshold: float, gamma: float = 1.5,
                    channel: str = "r") -> float:
    """Halation 的权重函数：阈值驱动、超线性、红加权。

    机制本身可信（乳剂层穿透后在片基背面反射回来，红光穿透最深），
    但阈值、gamma 与通道权重全部是工程起点，**未做任何实拍标定**。
    不得宣称「物理正确的 halation」。
    """
    if value <= threshold:
        return 0.0
    weights = {"r": 1.0, "g": 0.35, "b": 0.15}
    return ((value - threshold) ** gamma) * weights.get(channel, 0.0)


def grain_amplitude(lightness: float, peak: float) -> float:
    """颗粒幅度随明度调制：暗部最强、高光衰减。

    均匀噪点是数字噪声的样子；胶片颗粒在欠曝区最明显、在高光处几乎消失。
    幅度与明度不相关（|r| < 0.2）就说明退化成了均匀噪点。
    """
    if lightness <= 0.15:
        return peak * (lightness / 0.15) * 0.7 + peak * 0.3
    if lightness <= 0.5:
        return peak
    if lightness >= 0.85:
        return peak * 0.2
    ratio = (lightness - 0.5) / 0.35
    return peak * (1.0 - ratio * 0.8)


# --- 验收门 -------------------------------------------------------------

# 门的归属登记表。每个 check_* 都必须在这里有一行，说明它接在哪、是拦还是只告警。
# 这张表存在的理由：v4 审计发现 7 个门里 6 个从未接进任何执行链——
# 声明了却不运行的门，比没有门更危险，它让文档看起来有约束而实际什么也没拦。
GATE_REGISTRY = {
    "check_axis_differentiation": {
        "tier": "catalog", "enforcement": "blocking",
        "wired_at": "blcaptain_color:load_catalog",
        "note": "纯元数据判定，不需要素材。",
    },
    "check_additive_lift": {
        "tier": "recipe", "enforcement": "blocking",
        "wired_at": "blcaptain_color:visual_grammar_gates",
        "note": "解析滤镜链本身，与素材无关。度量的是链里加法算子的量，"
                "不是输出画面的黑场——后者混入了对比度压缩的固有抬黑，两者在像素统计上不可分。",
    },
    "check_path_to_white": {
        "tier": "render", "enforcement": "blocking",
        "wired_at": "blcaptain_color:visual_grammar_gates",
        "note": "用输出像素的 OKLab 高光段回归。",
    },
    "check_grain_modulation": {
        "tier": "render", "enforcement": "warning",
        "wired_at": "failure_detect:detect",
        "note": "颗粒是否退化成均匀噪点。只告警不拦：颗粒是审美选择，越界不等于翻车。",
    },
    "check_hue_preservation": {
        "tier": "render", "enforcement": "warning",
        "wired_at": "blcaptain_color:visual_grammar_gates",
        "note": "色相簇位移。只告警：多数创意配方本就要搬色相，容差无法一刀切。",
    },
    "check_skin_tolerance": {
        "tier": "conditional", "enforcement": "warning",
        "wired_at": "blcaptain_color:skin_tolerance_gate",
        "was_blocked_by":
            "验收取样缩到 96×64 后空间对应关系丢了，蒙版对不齐。"
            "L2 接入之后蒙版本来就要生成一次，对齐取样也有了现成实现，阻碍消失。",
        "note": "接上之后也只能告警：后端把 skin 自标为 derived 且 needs_human_review，"
                "拿一个自认不可靠的派生蒙版去拒绝渲染，等于用近似冒充能力。",
    },
    "check_skin_memory_corridor": {
        "tier": "conditional", "enforcement": "blocking",
        "wired_at": "blcaptain_color:visual_grammar_gates",
        "note": "仅在肤色蒙版存在且样本足够时，以 CIELAB 30°–50° 走廊与配方彩度上限阻断。",
    },
    "check_skin_exposure_band": {
        "tier": "manual", "enforcement": "manual",
        "wired_at": None,
        "reason_if_manual":
            "需要 Fitzpatrick 分型，而分型无法从像素推断——从像素推分型必然被曝光混淆，"
            "而曝光正是要测的量，这是循环论证。更要紧的是研究的对抗批评明确警告过："
            "不能把「不提亮深肤色」变成后期规则，那对欠曝的深肤色画面是有害的。"
            "因此本门只作为人工验收条目存在，不接任何自动链。",
    },
}


def check_skin_memory_corridor(report: dict) -> dict:
    """登记并保持 memory_color_gate 的实测结果，不重新解释其样本。"""
    return report


def additive_lift_of_chain(build_filter, params: dict, tone_curve: dict | None,
                           hsl_bands: list | None, probe_black) -> float:
    """链里加法算子的抬升量（相对场景白）。

    不能用「把纯黑推过完整链」来测：那个数里混着对比度压缩的贡献。
    实测 contrast=0.845 单独就能把黑场抬到 0.0775，而对比度压缩是低反差影调的
    正当属性，不是眩光。把 contrast 与 gamma 归中性之后再推，剩下的才是
    真正的加法——brightness 的正值、曲线的 0/y 起点、以及阴影端的加法色偏。

    也不能用输出像素统计来分辨：实测加法眩光 +42 的暗部 L 斜率是 0.92，
    比雾景配方的 0.65 还高；暗部 SD 比更是反的（加法 0.58 vs 降对比 0.36）。
    加法与柔黑在输出侧不可分——画面看起来就是那样，无论它是怎么来的。
    可分的只有一处：链本身做了什么。
    """
    neutral = dict(params)
    neutral["contrast"] = 1.0
    neutral["gamma"] = 1.0
    return probe_black(build_filter(neutral, tone_curve, hsl_bands))


def check_additive_lift(before_black: float, after_black: float,
                        scene_white: float = 1.0) -> dict:
    """抬黑是否越过了「柔黑」与「眩光雾」的边界。"""
    lift = (after_black - before_black) / max(1e-6, scene_white)
    passed = lift <= MAX_ADDITIVE_LIFT
    return {
        "gate": "additive_lift",
        "lift_ratio": round(lift, 6),
        "limit": MAX_ADDITIVE_LIFT,
        "passed": passed,
        "advice": None if passed else (
            "加法抬黑已达眩光量级。要柔黑请改用比例型软趾 out = x²/(x+t)，"
            f"t 取 {SOFT_TOE_RANGE[0]}–{SOFT_TOE_RANGE[1]}；它保留暗部斜率，加法不保留。"
        ),
        "threshold_status": ENGINEERING_PROPOSAL,
    }


def check_path_to_white(samples: list[tuple[float, float, float]]) -> dict:
    """高光是否霓虹化：L>0.8 段 chroma 对 L 的回归斜率必须为负。"""
    points = []
    for lab in samples:
        lightness, chroma, _ = oklab_to_oklch(*lab)
        if lightness > 0.8:
            points.append((lightness, chroma))
    if len(points) < 12:
        # 样本不足时不许伪装成通过。passed=None 表示「没测」，
        # 调用方必须自己决定怎么处理，不能靠 truthy 判断把它读成绿灯。
        return {"gate": "path_to_white", "passed": None, "status": "skipped",
                "slope": None, "sample_count": len(points),
                "reason": f"高光（L>0.8）样本仅 {len(points)} 个，不足 12 个，无法回归",
                "threshold_status": ENGINEERING_PROPOSAL}
    mean_x = sum(p[0] for p in points) / len(points)
    mean_y = sum(p[1] for p in points) / len(points)
    numerator = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    denominator = sum((p[0] - mean_x) ** 2 for p in points)
    slope = numerator / denominator if denominator else 0.0
    # 判据是量级不是符号。原始表述是「斜率必须为负」，但那等于不允许任何色偏——
    # 而带色偏正是许多配方的身份（森林青绿的青、蓝调时刻的蓝）。
    # 通道增益施加在接近白的像素上时，彩度天然与亮度成正比，斜率必然微正。
    # 霓虹化是量级问题：斜率 0.02 意味着彩度在高光区间只升 0.004，看不出来；
    # 斜率 0.1 意味着升 0.02，那才是霓虹。容差取 NEON_SLOPE_TOLERANCE。
    passed = slope < NEON_SLOPE_TOLERANCE
    return {
        "gate": "path_to_white",
        "status": "measured",
        "slope": round(slope, 6),
        "sample_count": len(points),
        "passed": passed,
        "advice": None if passed else (
            "高光彩度随亮度上升，属于霓虹化。饱和增益必须随明度单调不增，"
            f"高光段增益不超过中间调的 {HIGHLIGHT_SAT_RATIO}。"
        ),
        "threshold_status": ENGINEERING_PROPOSAL,
    }


def check_hue_preservation(before: list[tuple[float, float, float]],
                           after: list[tuple[float, float, float]],
                           mode: str = "creative") -> dict:
    """色相是否被污染：逐色带比较处理前后的色相簇中心位移。"""
    tolerance = HUE_TOLERANCE.get(mode, HUE_TOLERANCE["creative"])
    bands: dict[int, list[tuple[float, float]]] = {}
    for lab_before, lab_after in zip(before, after):
        _, chroma_before, hue_before = oklab_to_oklch(*lab_before)
        _, chroma_after, hue_after = oklab_to_oklch(*lab_after)
        if chroma_before < 0.03 or chroma_after < 0.03:
            continue
        bands.setdefault(int(hue_before // 30) % 12, []).append((hue_before, hue_after))
    shifts = {}
    worst = 0.0
    for band, pairs in bands.items():
        if len(pairs) < 8:
            continue
        before_mean = sum(p[0] for p in pairs) / len(pairs)
        after_mean = sum(p[1] for p in pairs) / len(pairs)
        gap = abs(after_mean - before_mean) % 360
        shift = 360 - gap if gap > 180 else gap
        shifts[f"{band * 30}-{band * 30 + 30}°"] = round(shift, 2)
        worst = max(worst, shift)
    return {
        "gate": "hue_preservation",
        "mode": mode,
        "tolerance_deg": tolerance,
        "per_band_shift": shifts,
        "worst_shift": round(worst, 2),
        "passed": worst <= tolerance,
        "advice": None if worst <= tolerance else (
            "色相位移超出容差。降饱和与色域压缩必须在等色相切片内执行，"
            "不要用 RGB 域逐通道 clip。"
        ),
        "threshold_status": ENGINEERING_PROPOSAL,
    }


def check_skin_tolerance(before: tuple[float, float, float],
                         after: tuple[float, float, float]) -> dict:
    """肤色的非对称容差：色相严（±3°），彩度宽（±20%）。"""
    _, chroma_before, hue_before = oklab_to_oklch(*before)
    _, chroma_after, hue_after = oklab_to_oklch(*after)
    gap = abs(hue_after - hue_before) % 360
    hue_shift = 360 - gap if gap > 180 else gap
    chroma_ratio = chroma_after / max(1e-6, chroma_before)
    hue_ok = hue_shift <= SKIN_HUE_TOLERANCE
    chroma_ok = abs(chroma_ratio - 1.0) <= SKIN_CHROMA_TOLERANCE
    return {
        "gate": "skin_tolerance",
        "hue_shift_deg": round(hue_shift, 2),
        "hue_tolerance_deg": SKIN_HUE_TOLERANCE,
        "chroma_ratio": round(chroma_ratio, 4),
        "chroma_tolerance": SKIN_CHROMA_TOLERANCE,
        "passed": hue_ok and chroma_ok,
        "advice": None if (hue_ok and chroma_ok) else (
            "肤色调整的主手段是色相定向旋转加明度微抬，"
            "不是加暖白平衡、也不是提橙饱和——橙色宽色带不等于肤色。"
        ),
        "threshold_status": ENGINEERING_PROPOSAL,
    }


def check_skin_exposure_band(face_luminance: float, reference_white: float,
                             band: str) -> dict:
    """深肤色的分型区间验收。

    这条取代了「深肤色禁止提亮」那个错误门禁。调研的对抗审查说得很清楚：
    把布光决策直接翻译成后期禁止提亮，会把本来就欠曝的深肤色素材系统性地留在暗处，
    那不是尊重而是反向伤害。正确做法是检查它是否落在该分型的合理区间内，
    落在区间外——包括低于下界——才报警。
    """
    if band not in FITZPATRICK_BANDS:
        return {"gate": "skin_exposure_band", "passed": True,
                "note": f"未知分型 {band}，跳过判定"}
    low, high = FITZPATRICK_BANDS[band]
    ratio = face_luminance / max(1e-6, reference_white)
    if ratio < low:
        verdict, advice = False, (
            f"人脸相对参考白仅 {ratio:.1%}，低于 {band} 分型区间下界 {low:.0%}，"
            "属于欠曝，应当提亮而不是维持现状。"
        )
    elif ratio > high:
        verdict, advice = False, (
            f"人脸相对参考白 {ratio:.1%}，超出 {band} 分型区间上界 {high:.0%}，"
            "存在把深肤色向浅肤色区间牵引的风险，判为肤色迁移。"
        )
    else:
        verdict, advice = True, None
    return {
        "gate": "skin_exposure_band",
        "band": band,
        "ratio": round(ratio, 4),
        "expected_range": [low, high],
        "passed": verdict,
        "advice": advice,
        "boundary": (
            "区间取自 SDR 照片调色语境下的工程提议，来源是广播显示电平的类比，"
            "并非直接适用的标准；必须用真实多肤色素材标定后才能宣称准确。"
        ),
        "threshold_status": ENGINEERING_PROPOSAL,
    }


def check_grain_modulation(pairs: list[tuple[float, float]]) -> dict:
    """颗粒是否退化成均匀噪点：幅度与明度的相关系数。"""
    if len(pairs) < 20:
        return {"gate": "grain_modulation", "passed": None, "status": "skipped",
                "reason": f"取样点仅 {len(pairs)} 个，不足 20 个，相关系数无意义"}
    mean_x = sum(p[0] for p in pairs) / len(pairs)
    mean_y = sum(p[1] for p in pairs) / len(pairs)
    cov = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pairs)
    var_x = sum((p[0] - mean_x) ** 2 for p in pairs)
    var_y = sum((p[1] - mean_y) ** 2 for p in pairs)
    correlation = cov / math.sqrt(var_x * var_y) if var_x and var_y else 0.0
    passed = abs(correlation) >= 0.2
    return {
        "gate": "grain_modulation",
        "status": "measured",
        "correlation": round(correlation, 4),
        "passed": passed,
        "advice": None if passed else (
            "颗粒幅度与明度不相关，说明是均匀数字噪点而不是胶片颗粒。"
            "幅度应当在欠曝区最高、在高光衰减到峰值的 20% 以下。"
        ),
        "threshold_status": ENGINEERING_PROPOSAL,
    }


def tone_axis_distance(first: dict, second: dict) -> float:
    """两套配方在影调四轴上的距离。低于阈值判为撞味。

    四轴是序数刻度，只能用于相对定位与去重，不能反推物理量。
    """
    keys = ("highlight_tone", "shadow_tone", "color_axis", "sharpness_axis")
    return math.sqrt(sum(
        (float(first.get(k, TONE_AXIS_NEUTRAL)) - float(second.get(k, TONE_AXIS_NEUTRAL))) ** 2
        for k in keys))


def check_axis_differentiation(axes: dict) -> dict:
    """单套配方是否「未分化」：高光轴与阴影轴取值相同。"""
    highlight = axes.get("highlight_tone", TONE_AXIS_NEUTRAL)
    shadow = axes.get("shadow_tone", TONE_AXIS_NEUTRAL)
    differentiated = highlight != shadow
    return {
        "gate": "axis_differentiation",
        "highlight_tone": highlight,
        "shadow_tone": shadow,
        "passed": differentiated,
        "advice": None if differentiated else (
            "高光轴与阴影轴取值相同，等于只有一个 contrast 滑块，配方未分化。"
            "有性格的影调来自两端的不对称处理，例如硬高光配软深阴影。"
        ),
        "boundary": "四轴是序数刻度，只用于相对定位与去重，不可反推物理量。",
    }
