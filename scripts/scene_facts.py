#!/usr/bin/env python3
"""场景事实：可追溯、三态、能否决分类器标签。

**为什么需要它。**

v4.5.0 把一张真实的长曝光夜景城市照判成了「白天／户外光」，Top-1 推荐日落暖调：

    light_context = {"context": "day", "day_score": 0.808, "night_score": 0.0,
                     "night_plausible": null,
                     "evidence": ["图像分类出现日光／户外类标签（0.81）"]}

两层原因：

1. 原来的夜景判据要求 `point_light_ratio >= 2.0`（大片暗 + 点光源）。
   长曝光夜景的车流拖影把点光源抹成了连续亮带，实测比值只有 1.455，
   于是落进 else 变成 None——判据漏掉了这一整类夜景。
2. 更根本的是：即使像素证据是 None，`day_score > 0.25` 就直接判 day。
   **软标签能覆盖像素事实，像素事实却无权否决标签。**

**这里改成什么。**

事实有三态：known / unknown / conflicting。
标签只是众多来源之一；当像素事实与标签互相矛盾时，结果是 `conflicting`，
不是二选一。冲突时两类专用配方都不进候选——不装懂，退回中性或让用户选。

**判据的来处（不是拍脑袋的数字）。**

8 份真实素材实测 p95（亮部第 95 百分位，线性光）：

    夜景城市(长曝) 0.3660      夜景城市2 0.1422
    白天风景       0.8735      白天山湖   0.8102
    室内咖啡       0.6911      室内酒吧   0.7030
    窗光人像       0.7986      影棚人像   0.7970

夜景 ≤ 0.366，其余 ≥ 0.691，间隔接近两倍。物理含义：
白天场景必有亮区（天空、受光面）；夜景即使有拖影，最亮的 5% 仍然很暗。

注意 p50 **不能**用来分昼夜——白天风景的 p50 是 0.0550，比那张夜景（0.0619）还低，
两者都被本流程判为「严重欠曝」。这条是实测否掉的假设，写在这里防止有人再走一遍。
"""

from __future__ import annotations

FACT_STATES = ("known", "unknown", "conflicting")

# 明确夜景上限 0.37 + 明确亮区下限 0.40，中间保留未知带。
#
# 最初取 0.50（8 份样本的间隔是 0.3660↔0.6911，取中点偏下）。
# 随后仓库里的回归素材「灰片2」把它证伪了：那是一张真实的白天城市照，
# p95 = 0.4984，刚好落在 0.50 之下，于是被判成夜景，
# 白天专用的 gilded-autumn-city 被排除——而那正是该素材有编号的正确方向
# （BLC-30-002 回归守卫）。
#
# 补上第 9 份样本后的完整分布：
#     夜景城市2      0.1422
#     夜景城市(长曝)  0.3660   ← 夜景侧最高
#     灰片2(白天城市) 0.4984   ← 非夜景侧最低
#     室内咖啡        0.6911
#     室内酒吧        0.7030
#     影棚人像        0.7970
#     窗光人像        0.7986
#     白天山湖        0.8102
#     白天风景        0.8735
#
# 0.40 落在 0.3660 与 0.4984 之间。余量比原来窄，如实记下来：
# 这条判据在 p95 0.37~0.50 区间的可靠性依赖后续更多真实样本，
# 不是一条宽裕的分界线。
NIGHT_MAX_P95 = 0.37
DAYLIGHT_MIN_P95 = 0.40

# 标签置信度低于此值时不足以单独成立一个事实，只能作为佐证。
MIN_LABEL_CONFIDENCE = 0.25


def derive_light_fact(tone: dict, label_scores: dict | None = None) -> dict:
    """由像素事实与分类器标签共同推出光照事实。

    返回 {state, value, why, evidence}；value 只在 state == "known" 时有意义。
    """
    labels = label_scores or {}
    p95 = float(tone.get("p95") or 0.0)
    p50 = float(tone.get("p50") or 0.0)
    point_light = float(tone.get("point_light_ratio") or 1.0)

    has_bright_region = p95 >= DAYLIGHT_MIN_P95
    no_bright_region = p95 <= NIGHT_MAX_P95
    region_state = ("有亮区" if has_bright_region else
                    ("没有亮区" if no_bright_region else "落在不确定带"))
    evidence = [f"p95={p95:.4f}（{region_state}；没有亮区 ≤ {NIGHT_MAX_P95}，"
                f"有亮区 ≥ {DAYLIGHT_MIN_P95}）"]

    # 像素侧的结论。没有亮区 = 不可能是白天户外；这条不依赖任何模型。
    pixel_value = None
    if no_bright_region:
        pixel_value = "night"
        evidence.append(f"最亮的 5% 也只有 {p95:.4f}，白天户外场景不会这样")
        if point_light < 1.5:
            evidence.append(f"点光比 {point_light:.2f} 不高——长曝光会把点光源抹成连续亮带，"
                            "所以点光比低不能用来否定夜景")
    elif p50 >= 0.09:
        pixel_value = "day-or-indoor"
        evidence.append(f"中位亮度 {p50:.4f} 且有亮区")

    # 标签侧的结论。
    best_label, best_score = None, 0.0
    for name, score in labels.items():
        if float(score) > best_score:
            best_label, best_score = name, float(score)
    if best_label and best_score >= MIN_LABEL_CONFIDENCE:
        evidence.append(f"图像分类标签「{best_label}」置信 {best_score:.2f}")

    # 标签被硬事实推翻的情形。
    #
    # 这里最初写成 conflicting（两边都不采信）。实测发现那样过度保守：
    # 那张长曝光夜景因此把夜景专用配方也一并排除，只剩黑白/低彩候选，
    # 用户拿到的结果反而更差。
    #
    # 正确的处理是分清「分歧」与「否定」：
    # 「画面没有亮区」对「白天户外」是**物理否定性证据**——白天户外场景不可能
    # 最亮的 5% 还这么暗。软标签在这种证据面前应当被推翻，而不是打成平手。
    # 8 份实测样本里，室内场景（有灯有窗）的 p95 也都在 0.69 以上，
    # 所以「没有亮区」不会误伤正常室内。
    label_says_day = best_label == "day" and best_score >= MIN_LABEL_CONFIDENCE
    if label_says_day and no_bright_region:
        evidence.append(f"分类标签「白天」置信 {best_score:.2f} 被像素事实推翻")
        return {
            "state": "known", "value": "night", "label_overridden": True,
            "why": (f"分类标签说白天（{best_score:.2f}），但画面没有亮区（p95={p95:.4f} < {DAYLIGHT_MIN_P95}）——"
                    "白天户外场景不会这样。以像素事实为准，判为夜景，并记录标签已被推翻。"),
            "evidence": evidence,
        }

    if pixel_value == "night":
        return {"state": "known", "value": "night",
                "why": f"画面没有亮区（p95={p95:.4f}），像素事实指向夜景",
                "evidence": evidence}

    if label_says_day and has_bright_region:
        return {"state": "known", "value": "day",
                "why": f"标签说白天（{best_score:.2f}），且画面确有亮区（p95={p95:.4f}），两者一致",
                "evidence": evidence}

    if best_label == "night" and best_score >= MIN_LABEL_CONFIDENCE and no_bright_region:
        return {"state": "known", "value": "night",
                "why": "标签与像素事实一致指向夜景", "evidence": evidence}

    if (best_label == "night" and best_score >= MIN_LABEL_CONFIDENCE
            and has_bright_region and p50 >= 0.09):
        # 这才是真正的分歧：标签说夜景，而画面既有亮区又不暗，两边都不占压倒性优势。
        return {"state": "conflicting", "value": None,
                "why": (f"分类标签说夜景（{best_score:.2f}），但画面有亮区（p95={p95:.4f}）"
                        f"且中位亮度 {p50:.4f} 不低。两者矛盾且都不具否定性，不采信任何一侧。"),
                "evidence": evidence}

    if best_label == "indoor" and best_score >= MIN_LABEL_CONFIDENCE:
        return {"state": "known", "value": "indoor",
                "why": f"标签说室内（{best_score:.2f}）", "evidence": evidence}

    # 事实未知，但仍可能有**否定性**结论。
    #
    # 「有亮区」不足以分清白天与室内（两者都有亮区），但足以**排除夜景**——
    # 这与前面「没亮区排除白天」是同一条否定性逻辑的反向。
    # 零安装路径（没有语义后端、拿不到任何标签）正是靠这一条守住夜景门：
    # 不能因为「说不清是白天还是室内」，就让夜景专用配方蒙混过关。
    return {"state": "unknown", "value": None,
            "not_night": bool(has_bright_region),
            "why": ("既没有足够强的标签，像素事实也不足以单独判定是白天还是室内；"
                    + ("但画面确有亮区，足以排除夜景。" if has_bright_region else "")
                    + "按不确定处理，不猜"),
            "evidence": evidence}


def recipe_admissible(recipe: dict, facts: dict) -> tuple:
    """配方是否可以进入候选。

    **这是硬门，不是打分。** 返回 (bool, 理由)。
    不满足的配方在排序之前就出局，不产生任何可被其它加分抵消的数值。
    """
    conditions = (recipe.get("match_conditions") or {})
    allowed = list(conditions.get("light_context") or ["any"])
    if "any" in allowed:
        return True, "该配方不限定光照语境"

    light = (facts.get("light") or {})
    state = light.get("state", "unknown")
    value = light.get("value")

    if state == "conflicting":
        return False, (f"光照事实冲突，无法确认语境；{recipe.get('id','该配方')} "
                       f"只适用于 {'／'.join(allowed)}，冲突时不进入候选")
    if state == "unknown":
        # 配方自己声明能容忍 unknown 时放行——这是它声明的适用范围的一部分。
        # 最初这里无条件排除，结果把 food-vivid 这类明确写着
        # light_context: [indoor, day, unknown] 的配方也挡了，属于过度排除。
        # 即便事实未知，否定性结论仍然生效：画面有亮区就排除夜景专用配方。
        # 条件取 {"night","unknown"}：只在夜景或未知下成立的配方，
        # 在「画面确有亮区」这条否定性证据面前就该出局。
        # 写成 {"night"} 太窄——声明为 ["night","unknown"] 的配方会漏网。
        if light.get("not_night") and set(allowed) <= {"night", "unknown"}:
            return False, (f"画面确有亮区，可以排除夜景；{recipe.get('id','该配方')} "
                           "只在夜景成立，不进入候选")
        if "unknown" in allowed:
            return True, f"光照事实未知，但 {recipe.get('id','该配方')} 声明可用于 unknown"
        return False, (f"光照事实未知；{recipe.get('id','该配方')} "
                       f"只适用于 {'／'.join(allowed)}，证据不足时不进入候选")
    if value not in allowed:
        return False, (f"素材光照语境是「{value}」，而 {recipe.get('id','该配方')} "
                       f"只适用于 {'／'.join(allowed)}")
    return True, f"光照语境「{value}」满足该配方的要求"


# 事实值 → 配方 scenes 里可能出现的关键词。
# 这些词直接取自 references/recipes.json 现有的 scenes 声明，不是新造的词表。
_SCENE_KEYWORDS = {
    "night": ("夜景", "夜", "霓虹", "车流", "灯光", "日落后", "夜店"),
    "day": ("日落", "日出", "黄昏", "秋日", "旅行", "风光", "逆光", "户外"),
    "indoor": ("室内", "居家", "咖啡", "餐厅", "静物", "产品"),
}


def scene_keywords_for(fact_value) -> tuple:
    """事实值对应的场景关键词。未知事实返回空元组。"""
    return _SCENE_KEYWORDS.get(fact_value, ())


def declares_scene(recipe: dict, fact_value) -> bool:
    """该配方是否**自己声明**适用于这个场景事实。"""
    keywords = scene_keywords_for(fact_value)
    if not keywords:
        return False
    declared = " ".join(str(x) for x in (recipe.get("scenes") or []))
    declared += " " + " ".join(str(x) for x in
                               ((recipe.get("match_conditions") or {}).get("requires_any_scene_family") or []))
    return any(word in declared for word in keywords)


def scene_matched_first(items: list, fact_value) -> list:
    """把「声明匹配本场景」的候选排到通用候选之前。

    **这不是加分。** 分数一个都不改，组内顺序也不动，只是分成两组先后呈现。

    为什么需要：通用配方（不限定场景）永远能进候选，于是总能挤掉
    为该场景专门声明的配方。实测那张长曝光夜景，事实门修好之后
    top3 仍是 documentary-low-color / bw-documentary / paper-moon-bw，
    而配方表里有 8 套明确声明夜景的（night-black-gold「夜景/建筑/车流」等）
    一个都没进前三。用加分去纠正这件事会污染其它场景；
    结构上分组既解决问题，又不产生任何可被抵消的数值。
    """
    if not scene_keywords_for(fact_value):
        return list(items)
    matched = [x for x in items if declares_scene(x, fact_value)]
    generic = [x for x in items if not declares_scene(x, fact_value)]
    return matched + generic
