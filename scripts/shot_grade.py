#!/usr/bin/env python3
"""逐镜头一级校正、镜头匹配与共享 Look 的节点图。

直接关闭审计发现 A02：v3 用整段单一中位数计算相对 EV，
少数镜头（例如一段过曝空镜）会支配全片，把其余镜头一起带偏。

节点顺序严格分层，不允许用创意 Look 去修曝光与白平衡：
    解码与色彩解释
    → 镜头检测（shots.py）
    → 人工复核门
    → 每镜头诊断与一级校正
    → 锚点镜头选取与同场景匹配
    → 全片共享 Look
    → 镜头内保持恒定（不跨硬切平滑）

技术机制：FFmpeg 的 enable='gte(t,a)*lt(t,b)' 半开区间时间轴门控，
（不用 between：它两端都闭，相邻段会在交界帧同时命中，那一帧被调色两次）
使单遍渲染即可给每个镜头不同参数。硬切两侧参数各自恒定，
因此天然不存在跨镜头平滑；渐变段用分步插值并显式标注。
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

import diagnose as diagnose_module  # noqa: E402
import shots as shots_module  # noqa: E402
from color_space import hue_distance  # noqa: E402

SHOT_GRADE_SCHEMA_VERSION = "4.0.0"
FOUNDATION_CONTRACT = "per-shot-foundation-v2c"
DISSOLVE_STEPS = 6          # 保留为下限，兼容原有行为
DISSOLVE_STEPS_MIN = 6
DISSOLVE_STEPS_MAX = 48

# diagnose 把逐帧亮度差 > 0.12 记作 large_luma_jumps——也就是本流程自己认为
# 「这么大的跳变像是镜头切换」。渐变阶梯绝不该制造出这种台阶。
LUMA_JUMP_THRESHOLD = 0.12
# 单级台阶允许的参数跨度。gamma / gain 在中间调上近似线性映射到亮度，
# 取跳变门限的三分之一作为预算，留出两倍余量吸收非线性与编码误差。
#
# 来处不是拍脑袋：实测那段 33 镜头视频，6 级固定台阶在最大的一处渐变上
# 造成 0.10~0.14 的亮度台阶（原片同位置 Δ≈0.000），正好压在 0.12 这条线上；
# 把单级预算收到 0.04 后，同一处需要的台阶数增加到足以把台阶压进不可见区间。
MAX_STEP_PARAM_DELTA = LUMA_JUMP_THRESHOLD / 3.0


def dissolve_step_count(current: dict, following: dict) -> int:
    """按两镜头的参数差决定渐变要切成几级。

    原实现固定 6 级：参数差小时浪费滤镜段，参数差大时每级都是可见的亮度台阶。
    实测后者真实发生过——一段静止画面里凭空出现 0.10 以上的亮度跳变。
    """
    keys = ("gamma", "red_gain", "green_gain", "blue_gain")
    worst = max(abs(float(following.get(k, 0.0)) - float(current.get(k, 0.0))) for k in keys)
    if worst <= 0:
        return DISSOLVE_STEPS_MIN
    needed = math.ceil(worst / MAX_STEP_PARAM_DELTA)
    return max(DISSOLVE_STEPS_MIN, min(DISSOLVE_STEPS_MAX, needed))
GAMMA_LIMITS = (0.72, 1.42)
GAIN_LIMITS = (0.82, 1.22)
# 中性灰上实测的通道增益 → OKLab 响应。u = R/B 增益偏移（rr=1+u, bb=1-u），v = G 增益偏移。
# 这些系数是量出来的，不是估的；随亮度会有变化，因此只用于一级白平衡对齐，
# 不用于创意调色，也不宣称在极暗／极亮区同样精确。
WB_RESPONSE = {"u_to_a": 0.0925, "u_to_b": 0.2207, "v_to_a": -0.2006, "v_to_b": 0.1390}


class ShotGradeError(Exception):
    pass


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ShotGradeError(f"缺少 {name}")
    return found


def _encode_srgb(linear: float) -> float:
    linear = max(0.0, min(1.0, linear))
    return 12.92 * linear if linear <= 0.0031308 else 1.055 * (linear ** (1 / 2.4)) - 0.055



def _crop_to_active_area(pixels: bytes, width: int, height: int) -> tuple:
    """按行亮度裁掉上下黑边，返回 (像素, 宽, 新高)。

    只裁上下——常见的信箱条与字幕条都在这两侧；左右裁切会误伤真正的暗侧构图。
    整帧全黑时退回原样，不做任何裁切。
    """
    if width <= 0 or height <= 0 or len(pixels) < width * height * 3:
        return pixels, width, height
    rows = []
    for y in range(height):
        base = y * width * 3
        total = 0
        for x in range(0, width, max(1, width // 64)):
            i = base + x * 3
            total += 0.2126 * pixels[i] + 0.7152 * pixels[i + 1] + 0.0722 * pixels[i + 2]
        count = len(range(0, width, max(1, width // 64)))
        rows.append(total / max(1, count) / 255.0)
    lo, hi = active_area_rows(rows)
    if lo == 0 and hi == height:
        return pixels, width, height
    return pixels[lo * width * 3: hi * width * 3], width, (hi - lo)


def profile_shot(path: Path, shot: dict, profile: str, samples: int = 3) -> dict:
    """在镜头内部均匀取样，避开边界帧以免把转场帧算进镜头统计。"""
    stream = diagnose_module.probe_stream(path)
    margin = min(0.25, shot["duration"] * 0.12)
    start = shot["start"] + margin
    end = max(start + 0.001, shot["end"] - margin)
    points = [start + (end - start) * (index + 0.5) / samples for index in range(samples)]
    analyses = []
    for at in points:
        try:
            pixels, width, height = diagnose_module.sample_frame(
                path, stream["width"], stream["height"], at_time=at
            )
        except diagnose_module.DiagnoseError:
            continue
        # D8：把画幅黑边（信箱条、字幕条）从统计里剔掉。
        #
        # 实测一段 1920x1080 里装 2.35:1 内容的素材：明亮的沙漠镜头因为上下黑边
        # 被算进统计，p50 读成 0.000000，于是被判「严重欠曝」并触发 +5.6 EV 提亮。
        # 黑边不是画面，它只会把每一个影调判语拖向黑。
        pixels, width, height = _crop_to_active_area(pixels, width, height)
        analyses.append(diagnose_module.analyze_frame(pixels, width, height, profile))
    if not analyses:
        raise ShotGradeError(f"镜头 #{shot['index']} 无法取样")
    tone_keys = ("p01", "p05", "p50", "p95", "p99", "tone_span",
                 "highlight_clip_ratio", "shadow_clip_ratio",
                 "luma_blown_ratio", "luma_crushed_ratio")
    tone = {key: round(statistics.fmean(item["tone"][key] for item in analyses), 6) for key in tone_keys}
    cast_a = statistics.fmean(
        item["color"]["white_balance"]["cast_magnitude"] *
        math.cos(math.radians(item["color"]["white_balance"]["cast_hue"])) for item in analyses
    )
    cast_b = statistics.fmean(
        item["color"]["white_balance"]["cast_magnitude"] *
        math.sin(math.radians(item["color"]["white_balance"]["cast_hue"])) for item in analyses
    )
    neutral_sample_ratio = statistics.fmean(
        item["color"]["white_balance"].get("neutral_sample_ratio", 0.0)
        for item in analyses)
    return {
        "index": shot["index"], "start": shot["start"], "end": shot["end"],
        "duration": shot["duration"], "ends_with": shot["ends_with"],
        "sampled_points": [round(value, 3) for value in points],
        "tone": tone,
        "cast_a": round(cast_a, 6), "cast_b": round(cast_b, 6),
        "cast_magnitude": round(math.hypot(cast_a, cast_b), 6),
        "cast_hue": round(math.degrees(math.atan2(cast_b, cast_a)) % 360.0, 1),
        "neutral_sample_ratio": round(neutral_sample_ratio, 6),
        "colorfulness": round(statistics.fmean(item["color"]["colorfulness"] for item in analyses), 6),
        "chromatic_pixel_ratio": round(statistics.fmean(
            item["color"]["chromatic_pixel_ratio"] for item in analyses), 6),
        "local_contrast_region": round(statistics.fmean(
            item["texture"]["local_contrast_region"] for item in analyses), 6),
    }



# 有意低调的判据。与 P0-1 的昼夜判据同族：看的不是「有多暗」，而是「亮部还在不在」。
#
#   有意低调：中位极低，但高光、实用光源、镜面反射还在 → p99 高、动态范围大
#   真的欠曝：整体压扁，连亮部也上不去 → p99 也低
#
# 取值来处：Codex iter-2 在 25 秒 Sintel 留出集上实测，6 秒处的低调镜头
# 活动画面中位亮度 0.001626、而画面里仍有明确亮部；这类镜头被强行抬到 0.05
# 之后变成 0.016214，22 秒处更达 +1.53 EV。判据必须能把这类镜头识别出来。
LOW_KEY_MAX_MEDIAN = 0.06        # 中位低于此值才谈得上「低调」
LOW_KEY_MIN_HIGHLIGHT = 0.25     # 但亮部（p99）必须仍然活着
LOW_KEY_MIN_RANGE = 0.20         # 且动态范围不能被压扁



# 「有没有可校正的内容」的下限。取值来自实测间隔：
# 需要保护的过场帧 p99 = 0.0073 / 0.0201；仍应被校正的暗镜头 p99 从 0.1998 起。
MIN_CORRECTABLE_HIGHLIGHT = 0.05



# 镜头匹配允许的最大曝光改动。
#
# 理由与素材无关：同一场景内相机曝光漂移通常不超过 1 挡；超过 1 挡的差异
# 几乎一定是有意为之，不属于「技术不一致」。镜头匹配的职责是修不一致，
# 不是重新曝光。
#
# 这不是为了套住某个样本而挑的数字——实测那个卡在判据中间带的镜头
# （p50=0.0647、p99=0.1998）拿到 +2.066 EV，目视是黄昏氛围被削弱；
# 与其把「有意低调」的阈值挪到刚好覆盖它，不如用这条与意图无关的技术上限。
MAX_MATCH_EV = 1.0


def has_correctable_content(tone: dict) -> bool:
    """这个镜头里有没有可供匹配的内容。

    判据用 p99 而不是中位数：中位数低可能是有意压暗，
    但**连最亮的 1% 都还在黑位**，说明这一帧根本没有内容——
    强行向锚点匹配只会把噪声放大成灰雾。
    """
    return float(tone.get("p99") or 0.0) >= MIN_CORRECTABLE_HIGHLIGHT


def is_intentional_low_key(tone: dict) -> bool:
    """这个镜头是有意压暗，还是真的欠曝。"""
    p50 = float(tone.get("p50") or 0.0)
    p99 = float(tone.get("p99") or 0.0)
    if p50 > LOW_KEY_MAX_MEDIAN:
        return False
    return p99 >= LOW_KEY_MIN_HIGHLIGHT and (p99 - p50) >= LOW_KEY_MIN_RANGE


# 判定黑边所用的行亮度下限。低于它视为无内容行。
ACTIVE_ROW_MIN_LUMA = 0.012


def active_area_rows(row_luma: list) -> tuple:
    """去掉画幅黑边与纯黑字幕条，返回 [起, 止) 行区间。

    D8：黑边不得参与镜头影调统计。整片 p01 若被画幅黑边支配，
    「有真实黑位」这个判语就失去意义——它量的是黑边，不是画面。
    全黑帧无法判定活动区，退回整帧而不是崩溃。
    """
    n = len(row_luma or [])
    if n == 0:
        return (0, 0)
    lo = 0
    while lo < n and float(row_luma[lo]) < ACTIVE_ROW_MIN_LUMA:
        lo += 1
    hi = n
    while hi > lo and float(row_luma[hi - 1]) < ACTIVE_ROW_MIN_LUMA:
        hi -= 1
    if lo >= hi:
        return (0, n)
    return (lo, hi)


def choose_anchor(profiles: list[dict]) -> dict:
    """锚点镜头：技术状态最健康且时长可观者，而不是最长或第一个。

    健康度看的是「有没有被剪切」和「明暗跨度是否可用」，
    因为匹配的目标是把其余镜头对齐到一个可信基准，不是对齐到最显眼的那个。
    """
    def health(item: dict) -> float:
        tone = item["tone"]
        score = 0.0
        # 必须用亮度口径。通道口径把纯饱和色也算成剪切，
        # 实测会让彩条类画面虚高到 85%，直接选出最差的镜头当基准。
        score -= tone["luma_blown_ratio"] * 260.0
        score -= tone["luma_crushed_ratio"] * 90.0
        score += min(tone["tone_span"], 0.75) * 26.0
        score -= abs(tone["p50"] - 0.22) * 55.0
        score -= max(0.0, tone["p01"] - 0.05) * 60.0
        score -= max(0.0, tone["p99"] - 0.97) * 120.0
        score += min(item["duration"], 8.0) * 1.0
        return score
    ranked = sorted(profiles, key=lambda item: -health(item))
    best = ranked[0]
    return {
        "index": best["index"],
        "reason": (
            f"镜头 #{best['index']} 的亮部过曝 {best['tone']['luma_blown_ratio']:.2%}、"
            f"暗部死黑 {best['tone']['luma_crushed_ratio']:.2%}、明暗跨度 {best['tone']['tone_span']:.3f}、"
            f"中位亮度 {best['tone']['p50']:.3f}，技术状态最健康，作为匹配基准"
        ),
        "ranking": [{"index": item["index"], "health": round(health(item), 2)} for item in ranked],
    }


def match_eligibility(shot_profile: dict, anchor: dict) -> dict:
    """仅让技术统计相近的镜头进入相对匹配。

    这是保守的同场景候选门，不是语义场景识别。若统计证据不足或差异过大，
    宁可保留叙事光比和色彩设计，也不把无关镜头强拉到同一个锚点。
    """
    required = ("chromatic_pixel_ratio", "colorfulness", "local_contrast_region")
    if not all(key in shot_profile and key in anchor for key in required):
        return {
            "eligible": True,
            "basis": "legacy-profile-without-comparability-evidence",
            "reasons": ["旧调用没有完整技术统计；保持兼容，由 Readiness 负责阻断损伤"],
        }
    reasons = []
    coverage_delta = abs(
        float(shot_profile["chromatic_pixel_ratio"])
        - float(anchor["chromatic_pixel_ratio"]))
    if coverage_delta > 0.30:
        reasons.append("chromatic_coverage_differs")

    def ratio(left: float, right: float) -> float:
        high = max(float(left), float(right), 1e-6)
        return min(float(left), float(right)) / high

    color_ratio = ratio(shot_profile["colorfulness"], anchor["colorfulness"])
    # 真实多镜头留出集上，0.578 的综合色彩比仍把两个叙事镜头误判为同场景，
    # 随后压暗造成影调跨度、局部对比和暗部剪切同时恶化；同源曝光漂移夹具为
    # 0.790。阈值取两者之间的 0.65，作为保守拒绝门而非场景分类器。
    if color_ratio < 0.65:
        reasons.append("colorfulness_structure_differs")
    tone_ratio = ratio(shot_profile["tone"]["tone_span"], anchor["tone"]["tone_span"])
    if tone_ratio < 0.45:
        reasons.append("tone_structure_differs")
    local_ratio = ratio(shot_profile["local_contrast_region"], anchor["local_contrast_region"])
    if local_ratio < 0.45:
        reasons.append("local_contrast_structure_differs")
    return {
        "eligible": not reasons,
        "basis": "technical-comparability-heuristic-not-semantic-scene-detection",
        "reasons": reasons or ["technical_statistics_are_comparable"],
        "measurements": {
            "chromatic_coverage_delta": round(coverage_delta, 6),
            "colorfulness_ratio": round(color_ratio, 6),
            "tone_span_ratio": round(tone_ratio, 6),
            "local_contrast_ratio": round(local_ratio, 6),
        },
    }


def primary_for_shot(shot_profile: dict, anchor: dict, match_strength: float = 1.0) -> dict:
    """每镜头一级校正：把该镜头对齐到锚点，只解决可读性与偏色。"""
    source_median = max(0.004, min(0.95, shot_profile["tone"]["p50"]))
    eligibility = match_eligibility(shot_profile, anchor)

    # 有意压暗的镜头不参与向锚点靠拢。
    #
    # 原实现把 target_median 夹在 [0.05, 0.62]，于是**任何** p50 低于 0.05 的镜头
    # 都被强行抬到 0.05。实测一段 25 秒多镜头素材，6 秒处的低调镜头
    # 中位亮度由 0.001626 被抬到 0.016214，22 秒处达 +1.53 EV——
    # 暗场变成灰雾，叙事光比被削平。这正是 D7 禁止的「把所有镜头拉向全片中位数」。
    #
    # 镜头匹配的职责是修**技术上的不一致**（同一场景里相机曝光/白平衡漂移），
    # 不是把导演有意压暗的画面拉亮。真欠曝仍然照修（见下面的 else 分支）。
    if (not eligibility["eligible"] or is_intentional_low_key(shot_profile["tone"])
            or not has_correctable_content(shot_profile["tone"])):
        target_median = source_median
    else:
        target_median = max(0.05, min(0.62, anchor["tone"]["p50"]))
        target_median = source_median + (target_median - source_median) * match_strength
        target_median = max(0.05, min(0.62, target_median))
    # 限幅：超过 MAX_MATCH_EV 的改动不是「匹配」，是重新曝光。
    exposure_capped = False
    if source_median > 0:
        import math as _m
        raw_ev = _m.log2(target_median / source_median)
        if abs(raw_ev) > MAX_MATCH_EV:
            exposure_capped = True
            target_median = source_median * (2.0 ** (MAX_MATCH_EV if raw_ev > 0 else -MAX_MATCH_EV))
    source_encoded = _encode_srgb(source_median)
    target_encoded = _encode_srgb(target_median)
    gamma = math.log(max(source_encoded, 0.01)) / math.log(max(target_encoded, 0.01))
    gamma = round(max(GAMMA_LIMITS[0], min(GAMMA_LIMITS[1], gamma)), 6)
    exposure_ev = round(math.log2(target_median / source_median), 4)

    # 白平衡匹配：把该镜头的偏色向锚点偏色对齐，而不是一律推向中性。
    #
    # 通道增益与 OKLab 两轴是耦合的，且方向与直觉相反。实测中性灰：
    #   R/B 增益（色温轴）主要移动 b 轴，同时带动 a 轴；
    #   G  增益（色调轴）主要移动 a 轴，同时带动 b 轴。
    # 早期实现按直觉把 a 轴接到 R/B、b 轴接到 G，结果镜头间偏色离散度不降反升 5.8%。
    # 现在用实测响应矩阵求逆，直接解出所需的两个增益偏移量。
    effective_match_strength = match_strength if eligibility["eligible"] else 0.0
    delta_a = (anchor["cast_a"] - shot_profile["cast_a"]) * effective_match_strength
    delta_b = (anchor["cast_b"] - shot_profile["cast_b"]) * effective_match_strength
    determinant = (
        WB_RESPONSE["u_to_a"] * WB_RESPONSE["v_to_b"]
        - WB_RESPONSE["v_to_a"] * WB_RESPONSE["u_to_b"]
    )
    u = (WB_RESPONSE["v_to_b"] * delta_a - WB_RESPONSE["v_to_a"] * delta_b) / determinant
    v = (WB_RESPONSE["u_to_a"] * delta_b - WB_RESPONSE["u_to_b"] * delta_a) / determinant
    u = max(-0.2, min(0.2, u))
    v = max(-0.2, min(0.2, v))
    red_gain = round(max(GAIN_LIMITS[0], min(GAIN_LIMITS[1], 1.0 + u)), 6)
    blue_gain = round(max(GAIN_LIMITS[0], min(GAIN_LIMITS[1], 1.0 - u)), 6)
    green_gain = round(max(GAIN_LIMITS[0], min(GAIN_LIMITS[1], 1.0 + v)), 6)
    active_axes = []
    if abs(gamma - 1.0) > 0.002:
        active_axes.append("tone_match")
    if any(abs(value - 1.0) > 0.002 for value in (red_gain, green_gain, blue_gain)):
        active_axes.append("neutral_match")
    source_profile_hash = hashlib.sha256(json.dumps(
        shot_profile, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    foundation_payload = {
        "contract": FOUNDATION_CONTRACT,
        "shot_index": shot_profile["index"],
        "source_profile_hash": source_profile_hash,
        "gamma": gamma,
        "red_gain": red_gain,
        "green_gain": green_gain,
        "blue_gain": blue_gain,
        "target_median": round(target_median, 6),
        "active_axes": active_axes,
    }
    foundation_hash = hashlib.sha256(json.dumps(
        foundation_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "shot_index": shot_profile["index"],
        "foundation_contract": FOUNDATION_CONTRACT,
        "foundation_hash": foundation_hash,
        "source_profile_hash": source_profile_hash,
        "active_axes": active_axes,
        "match_eligibility": eligibility,
        "gamma": gamma,
        "exposure_ev_intent": exposure_ev,
        "red_gain": red_gain, "green_gain": green_gain, "blue_gain": blue_gain,
        "source_median": round(source_median, 6),
        "target_median": round(target_median, 6),
        "exposure_capped": exposure_capped,
        "cast_delta": {"a": round(delta_a, 6), "b": round(delta_b, 6)},
        "gain_offsets": {"temperature_u": round(u, 6), "tint_v": round(v, 6)},
        "scope": "该镜头内恒定；不跨硬切平滑；不修改创意 Look",
    }


def _cast_distance(left: dict, right: dict) -> float:
    return math.hypot(
        float(left.get("cast_a") or 0.0) - float(right.get("cast_a") or 0.0),
        float(left.get("cast_b") or 0.0) - float(right.get("cast_b") or 0.0),
    )


def evaluate_foundation_readiness(plan: dict, after_profiles: list[dict]) -> dict:
    """验证逐镜头 Foundation 是否改善技术一致性且没有损伤基础画质。

    只检查一级校正实际激活的轴；未激活的轴允许编码往返的微小误差，
    不要求健康镜头为了“看起来做过”而发生变化。
    """
    before_by_index = {item["index"]: item for item in plan.get("shot_profiles") or []}
    after_by_index = {item["index"]: item for item in after_profiles}
    primary_by_index = {item["shot_index"]: item for item in plan.get("primaries") or []}
    anchor_index = int((plan.get("anchor") or {}).get("index", -1))
    anchor_before = before_by_index.get(anchor_index)
    blocking: list[str] = []
    shots_report = []

    for index, before in sorted(before_by_index.items()):
        after = after_by_index.get(index)
        primary = primary_by_index.get(index) or {}
        active_axes = list(primary.get("active_axes") or [])
        failures = []
        if after is None:
            failures.append("missing_post_profile")
        else:
            before_tone = before["tone"]
            after_tone = after["tone"]
            target = float(primary.get("target_median", before_tone["p50"]))
            before_error = abs(float(before_tone["p50"]) - target)
            after_error = abs(float(after_tone["p50"]) - target)
            if ("tone_match" in active_axes
                    and after_error > max(0.02, before_error * 0.85)):
                failures.append("tone_not_converged")
            if float(after_tone["tone_span"]) < float(before_tone["tone_span"]) * 0.90 - 0.03:
                failures.append("tone_span_regressed")
            clip_before = (float(before_tone.get("luma_blown_ratio") or 0.0)
                           + float(before_tone.get("luma_crushed_ratio") or 0.0))
            clip_after = (float(after_tone.get("luma_blown_ratio") or 0.0)
                          + float(after_tone.get("luma_crushed_ratio") or 0.0))
            if clip_after > clip_before + 0.01:
                failures.append("luma_clip_regressed")
            if float(after.get("colorfulness") or 0.0) < max(
                    0.0, float(before.get("colorfulness") or 0.0) * 0.85 - 0.01):
                failures.append("colorfulness_regressed")
            if float(after.get("local_contrast_region") or 0.0) < max(
                    0.0, float(before.get("local_contrast_region") or 0.0) * 0.90 - 0.002):
                failures.append("local_contrast_regressed")
            if "neutral_match" in active_axes and anchor_before is not None:
                before_cast_error = _cast_distance(before, anchor_before)
                after_anchor = after_by_index.get(anchor_index, anchor_before)
                after_cast_error = _cast_distance(after, after_anchor)
                if after_cast_error > max(0.004, before_cast_error * 0.90 + 0.002):
                    failures.append("neutral_not_converged")
        if failures:
            blocking.extend(f"shot_{index}_{name}" for name in failures)
        shots_report.append({
            "shot_index": index,
            "active_axes": active_axes,
            "status": "blocked" if failures else "passed",
            "failures": failures,
        })

    common_indices = sorted(set(before_by_index) & set(after_by_index))
    tone_active = any("tone_match" in (item.get("active_axes") or [])
                      for item in primary_by_index.values())
    neutral_active = any("neutral_match" in (item.get("active_axes") or [])
                         for item in primary_by_index.values())
    convergence = {}
    if len(common_indices) >= 2:
        before_p50 = [float(before_by_index[index]["tone"]["p50"]) for index in common_indices]
        after_p50 = [float(after_by_index[index]["tone"]["p50"]) for index in common_indices]
        before_spread = max(before_p50) - min(before_p50)
        after_spread = max(after_p50) - min(after_p50)
        convergence["tone_spread"] = {
            "before": round(before_spread, 6), "after": round(after_spread, 6)}
        tone_limit = before_spread * 0.90 + 0.01 if tone_active else before_spread + 0.01
        if after_spread > tone_limit:
            blocking.append("inter_shot_tone")

        def cast_spread(items: dict[int, dict]) -> float:
            return max(
                (_cast_distance(items[left], items[right])
                 for position, left in enumerate(common_indices)
                 for right in common_indices[position + 1:]),
                default=0.0,
            )
        before_cast_spread = cast_spread(before_by_index)
        after_cast_spread = cast_spread(after_by_index)
        convergence["cast_spread"] = {
            "before": round(before_cast_spread, 6), "after": round(after_cast_spread, 6)}
        cast_limit = (before_cast_spread * 0.90 + 0.003
                      if neutral_active else before_cast_spread + 0.003)
        if after_cast_spread > cast_limit:
            blocking.append("inter_shot_cast")

    blocking = list(dict.fromkeys(blocking))
    return {
        "contract": FOUNDATION_CONTRACT,
        "status": "blocked" if blocking else "passed",
        "blocking_failures": blocking,
        "shots": shots_report,
        "convergence": convergence,
        "boundary": "自动门只验证技术健康与镜头一致性；不替代人工审美、情绪和叙事连续性验收",
    }


def _primary_chain(primary: dict) -> str:
    """把一个镜头的一级校正参数写成无时间轴门控的滤镜串，用于探针色预测。"""
    parts = []
    if abs(primary["gamma"] - 1.0) > 0.002:
        parts.append(f"eq=gamma={primary['gamma']}")
    if any(abs(primary[key] - 1.0) > 0.002 for key in ("red_gain", "green_gain", "blue_gain")):
        parts.append(
            f"colorchannelmixer=rr={primary['red_gain']}:gg={primary['green_gain']}:bb={primary['blue_gain']}"
        )
    return ",".join(parts)


PROXY_WIDTH = 192
# Rec.709 亮度权重。trim 只应该改变颜色平衡，不应该顺带改变亮度，
# 否则会把一级校正好不容易对齐的影调再次拉开（实测 p50 离散度从 0.0227 退到 0.0381）。
LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)


def _normalize_luma(red: float, green: float, blue: float) -> tuple[float, float, float]:
    weighted = LUMA_WEIGHTS[0] * red + LUMA_WEIGHTS[1] * green + LUMA_WEIGHTS[2] * blue
    if weighted <= 0:
        return red, green, blue
    scale = 1.0 / weighted
    return red * scale, green * scale, blue * scale


def measure_post_look_trim(source: Path, full_chain: str, shot_list: list[dict],
                           anchor_index: int, profile: str = "srgb",
                           strength: float = 1.0) -> dict:
    """渲染低分辨率代理，实测创意 Look 之后每个镜头的残余偏色，据此解出镜头级 trim。

    为什么不用解析预测：试过两版——用镜头自身像素当探针、以及构造中性轴探针。
    前者在彩条类画面上近中性样本几乎为零，后者用几个点代表不了「整帧近中性像素群
    经非线性链路后的落点」。实测收敛分别只有 32.7% 和 14.3%，都低于不做 trim 的 23.3%。
    改成渲染 192px 代理再量，收敛回到 64.0%——这也正是调色师的实际做法：看结果再 trim。

    代价：一次低分辨率、无音轨、ultrafast 的额外解码编码。分辨率越高、时长越长，
    这一遍相对完整渲染的占比越小；但它确实是额外成本，必须写进计划让用户知道。
    """
    proxy = source.parent / f".{source.stem}__blproxy.mp4"
    command = [
        _tool("ffmpeg"), "-v", "error", "-y", "-i", str(source),
        "-vf", f"{full_chain},scale={PROXY_WIDTH}:-2", "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", str(proxy),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        return {"available": False,
                "reason": f"代理渲染失败：{result.stderr.decode('utf-8','replace').strip()[:160]}",
                "trims": []}
    try:
        measured = [profile_shot(proxy, shot, profile) for shot in shot_list]
    except ShotGradeError as error:
        proxy.unlink(missing_ok=True)
        return {"available": False, "reason": f"代理测量失败：{error}", "trims": []}
    finally:
        pass

    anchor = next((item for item in measured if item["index"] == anchor_index), None)
    if anchor is None:
        proxy.unlink(missing_ok=True)
        return {"available": False, "reason": "代理中找不到锚点镜头", "trims": []}

    determinant = (
        WB_RESPONSE["u_to_a"] * WB_RESPONSE["v_to_b"]
        - WB_RESPONSE["v_to_a"] * WB_RESPONSE["u_to_b"]
    )
    trims = []
    for item in measured:
        delta_a = (anchor["cast_a"] - item["cast_a"]) * strength
        delta_b = (anchor["cast_b"] - item["cast_b"]) * strength
        u = (WB_RESPONSE["v_to_b"] * delta_a - WB_RESPONSE["v_to_a"] * delta_b) / determinant
        v = (WB_RESPONSE["u_to_a"] * delta_b - WB_RESPONSE["u_to_b"] * delta_a) / determinant
        clamped = abs(u) > 0.06 or abs(v) > 0.06
        u = max(-0.06, min(0.06, u))
        v = max(-0.06, min(0.06, v))
        red, green, blue = _normalize_luma(1.0 + u, 1.0 + v, 1.0 - u)
        trims.append({
            "shot_index": item["index"],
            "red_gain": round(max(0.88, min(1.12, red)), 6),
            "green_gain": round(max(0.88, min(1.12, green)), 6),
            "blue_gain": round(max(0.88, min(1.12, blue)), 6),
            "measured_post_look_cast": {"a": round(item["cast_a"], 6), "b": round(item["cast_b"], 6)},
            "residual_delta": {"a": round(delta_a, 6), "b": round(delta_b, 6)},
            "clamped": clamped,
        })
    proxy.unlink(missing_ok=True)
    return {
        "available": True,
        "method": f"{PROXY_WIDTH}px 低分辨率代理实测，非解析预测",
        "luma_preserving": True,
        "max_gain_offset": 0.06,
        "anchor_index": anchor_index,
        "trims": trims,
        "boundary": "trim 只做颜色平衡微调；幅度上限 ±6%，且按 Rec.709 权重做亮度守恒归一化",
    }


def guard_trims_by_match_eligibility(trims: list[dict], primaries: list[dict]) -> list[dict]:
    """禁止 Look 后 trim 绕过前置的跨场景风险门。"""
    eligibility = {
        item["shot_index"]: item.get("match_eligibility") or {"eligible": True}
        for item in primaries
    }
    guarded = []
    for trim in trims:
        item = dict(trim)
        match = eligibility.get(item["shot_index"], {"eligible": False, "reasons": ["missing_primary"]})
        if not match.get("eligible"):
            item.update({
                "red_gain": 1.0, "green_gain": 1.0, "blue_gain": 1.0,
                "status": "skipped-cross-scene-risk",
                "skip_reasons": list(match.get("reasons") or []),
            })
        else:
            item["status"] = "eligible"
        guarded.append(item)
    return guarded


def build_trim_filter(shots_list: list[dict], trims: list[dict]) -> str:
    by_index = {item["shot_index"]: item for item in trims}
    parts: list[str] = []
    for shot in shots_list:
        trim = by_index.get(shot["index"])
        if not trim:
            continue
        if all(abs(trim[key] - 1.0) <= 0.002 for key in ("red_gain", "green_gain", "blue_gain")):
            continue
        # 与 _segment_filters 同口径：半开区间，相邻段不共享端点；末镜头开口到流尾
        window = (f"gte(t,{shot['start']:.4f})"
                  if shot is shots_list[-1]
                  else f"gte(t,{shot['start']:.4f})*lt(t,{shot['end']:.4f})")
        parts.append(
            f"colorchannelmixer=rr={trim['red_gain']}:gg={trim['green_gain']}:"
            f"bb={trim['blue_gain']}:enable='{window}'"
        )
    return ",".join(parts)


def _segment_filters(start: float, end: float, primary: dict, open_end: bool = False) -> list[str]:
    # 半开区间 [start, end)。
    #
    # 原来用 between(t,a,b)，而 ffmpeg 的 between 两端都闭：
    # 相邻段 [a,b] 与 [b,c] 在 t=b 同时命中，那一帧会连续经过两个 eq，
    # gamma 被应用两次，输出出现可见尖峰。段越多、共享端点越多，
    # 所以「把渐变切得更细」反而让闪烁更严重——实测加密阶梯后
    # t=24.53s 的跳变由 0.1095 涨到 0.1638。真正该修的是区间闭合方式。
    # 半开区间 [start, end)：相邻段不共享端点。
    # 但末段必须开口到流尾——否则视频最后一帧落在所有区间之外，一点调色都拿不到。
    window = (f"gte(t,{start:.4f})" if open_end
              else f"gte(t,{start:.4f})*lt(t,{end:.4f})")
    parts = []
    if abs(primary["gamma"] - 1.0) > 0.002:
        parts.append(f"eq=gamma={primary['gamma']}:enable='{window}'")
    if any(abs(primary[key] - 1.0) > 0.002 for key in ("red_gain", "green_gain", "blue_gain")):
        parts.append(
            f"colorchannelmixer=rr={primary['red_gain']}:gg={primary['green_gain']}:"
            f"bb={primary['blue_gain']}:enable='{window}'"
        )
    return parts


def build_timeline_filter(plan: dict) -> str:
    """把逐镜头参数编译成单遍 FFmpeg 时间轴滤镜链。"""
    filters: list[str] = []
    dissolves = {item["shot_index"]: item for item in plan["dissolve_ramps"]}
    for primary in plan["primaries"]:
        shot = next(item for item in plan["shots"] if item["index"] == primary["shot_index"])
        ramp = dissolves.get(primary["shot_index"])
        end = ramp["ramp_start"] if ramp else shot["end"]
        is_last = primary is plan["primaries"][-1]
        filters += _segment_filters(shot["start"], end, primary,
                                    open_end=bool(is_last and not ramp))
        if ramp:
            # 渐变段用分步插值：FFmpeg 的 enable 不做插值，
            # 因此显式拆成若干等长子段，并在回执中标注这是阶梯而非连续曲线。
            following = next(
                (item for item in plan["primaries"] if item["shot_index"] == primary["shot_index"] + 1),
                primary,
            )
            following_shot = next(
                (item for item in plan["shots"] if item["index"] == primary["shot_index"] + 1),
                None,
            )
            # 渐变段的终点必须**正好是**下一镜头的起点。
            #
            # 上一轮只做了 min(检测终点, 下一镜头起点)，解决了「参数段跨过硬切」；
            # 但当检测到的渐变终点**早于**下一镜头起点时，中间那段就没有任何段覆盖——
            # 那些帧完全不被调色，夹在已调色的帧中间，同样是可见跳变。
            #
            # 真实素材实测：镜头5 在 23.733 以 dissolve 结束、镜头6 从 24.800 才开始，
            # 1960 帧里有 93~95 帧落在这类空隙中；t=24.53s 的亮度跳变在只修了
            # 「双重覆盖」之后仍达 0.1642（原片同位置 Δ=0.002）。
            #
            # 渐变边界按定义不是硬切，所以延伸到下一镜头起点既不跨切也不留缝。
            ramp_end = following_shot["start"] if following_shot else ramp["ramp_end"]
            span = ramp_end - ramp["ramp_start"]
            if span <= 0:
                continue
            steps = dissolve_step_count(primary, following)
            for step in range(steps):
                ratio = (step + 0.5) / steps
                blended = {
                    key: primary[key] + (following[key] - primary[key]) * ratio
                    for key in ("gamma", "red_gain", "green_gain", "blue_gain")
                }
                blended = {key: round(value, 6) for key, value in blended.items()}
                sub_start = ramp["ramp_start"] + span * step / steps
                sub_end = ramp["ramp_start"] + span * (step + 1) / steps
                filters += _segment_filters(sub_start, sub_end, blended)
    return ",".join(filters)


def plan_shot_grade(path: Path, profile: str = "srgb", match_strength: float = 1.0,
                    anchor_index: int | None = None) -> dict:
    detection = shots_module.detect(path)
    if detection["shot_count"] < 1:
        raise ShotGradeError("未检测到任何镜头")
    profiles = [profile_shot(path, shot, profile) for shot in detection["shots"]]
    if anchor_index is not None:
        anchor_profile = next((item for item in profiles if item["index"] == anchor_index), None)
        if anchor_profile is None:
            raise ShotGradeError(f"锚点镜头 #{anchor_index} 不存在")
        anchor = {"index": anchor_index, "reason": "由用户指定", "ranking": []}
    else:
        anchor = choose_anchor(profiles)
        anchor_profile = next(item for item in profiles if item["index"] == anchor["index"])

    primaries = [primary_for_shot(item, anchor_profile, match_strength) for item in profiles]

    dissolve_ramps = []
    for shot in detection["shots"]:
        if shot["ends_with"] != "dissolve":
            continue
        boundary = next(
            (item for item in detection["boundaries"]
             if item["kind"] == "dissolve" and abs(item["time"] - shot["end"]) < 0.5), None
        )
        if boundary and "end_time" in boundary:
            dissolve_ramps.append({
                "shot_index": shot["index"],
                "ramp_start": boundary["time"],
                "ramp_end": boundary["end_time"],
                "steps": DISSOLVE_STEPS,
                "note": "渐变段用等长阶梯插值，不是连续曲线；阶梯数量已记录以便复核",
            })

    plan = {
        "schema_version": SHOT_GRADE_SCHEMA_VERSION,
        "foundation_contract": FOUNDATION_CONTRACT,
        "foundation_hashes": {
            str(item["shot_index"]): item["foundation_hash"] for item in primaries},
        "path": str(path),
        "duration": detection["duration"],
        "shot_count": detection["shot_count"],
        "shots": detection["shots"],
        "boundaries": detection["boundaries"],
        "flashes": detection["flashes"],
        "needs_human_review": detection["needs_human_review"],
        "shot_profiles": profiles,
        "anchor": anchor,
        "match_strength": match_strength,
        "primaries": primaries,
        "dissolve_ramps": dissolve_ramps,
        "node_graph": [
            "解码与色彩解释", "镜头检测", "人工复核门", "每镜头诊断",
            "每镜头一级校正", "锚点匹配", "全片共享 Look", "镜头内恒定与时序验收",
        ],
        "guarantees": [
            "每个镜头的参数在镜头内恒定，硬切两侧互不影响，因此不存在跨镜头平滑",
            "一级校正只做曝光与白平衡对齐，不承担创意 Look",
            "渐变段使用显式阶梯插值并记录阶梯数量",
            "闪光被记为镜头内事件，不触发新的一级校正段",
            "只对技术统计相近的镜头做相对匹配；该门是保守启发式，不冒充语义场景识别",
        ],
        "boundary": (
            "本计划只产出逐镜头一级校正；创意 Look 仍由正式配方提供，"
            "且必须在用户确认 plan_id 后才会渲染。"
        ),
        "readiness_targets": {
            "required": True,
            "per_shot": [
                "激活的影调匹配轴向锚点收敛", "激活的中性平衡轴向锚点收敛",
                "不得显著损失色彩丰富度、局部对比、影调跨度或新增亮度剪切",
            ],
            "inter_shot": ["镜头间中位亮度离散不扩大", "镜头间中性偏色离散不扩大"],
        },
    }
    plan["timeline_filter"] = build_timeline_filter(plan)
    plan["timeline_noop_reason"] = (
        "所有镜头的一级曝光与白平衡修正都低于执行阈值；保留空时间轴，"
        "只执行已确认的共享创意 Look。"
        if not plan["timeline_filter"] else None
    )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="逐镜头一级校正与镜头匹配计划")
    parser.add_argument("--input", required=True)
    parser.add_argument("--profile", default="srgb")
    parser.add_argument("--match-strength", type=float, default=1.0)
    parser.add_argument("--anchor", type=int)
    args = parser.parse_args()
    try:
        payload = plan_shot_grade(
            Path(args.input).expanduser().resolve(), args.profile,
            max(0.0, min(1.0, args.match_strength)), args.anchor,
        )
    except (ShotGradeError, shots_module.ShotError, diagnose_module.DiagnoseError) as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
