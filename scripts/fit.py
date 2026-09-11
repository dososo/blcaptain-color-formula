#!/usr/bin/env python3
"""配方适配打分：只用真实测得的像素事实与配方的结构化字段。

自然语言字段（best_for / avoid_when / thesis）只用于向用户解释，
绝不参与打分——否则又会退回「按风格名称套色」的老失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import scene_facts

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIT_SCHEMA_VERSION = "4.0.0"

# Apple Vision 场景标签 → 配方 scenes 的家族映射。
# 这是真实图像分类结果，不是用户输入的关键词。
VISION_SCENE_FAMILIES = {
    "people": "人像", "adult": "人像", "child": "人像", "face": "人像", "crowd": "人像",
    "sky": "天空", "cloudy": "阴天", "sunset": "日落", "sunrise": "日落", "night": "夜景",
    "outdoor": "户外", "indoor": "室内", "room": "室内", "kitchen": "室内",
    "building": "建筑", "architecture": "建筑", "skyscraper": "建筑", "house": "建筑",
    "street": "街拍", "road": "街拍", "city": "城市", "urban": "城市", "lamppost": "城市",
    "plant": "植被", "tree": "植被", "forest": "森林", "grass": "草原", "flower": "植被",
    "beach": "海边", "sea": "海边", "ocean": "海边", "water": "水面", "lake": "湖泊",
    "mountain": "山川", "snow": "雪景", "desert": "沙漠",
    "food": "美食", "dessert": "美食", "drink": "美食", "meal": "美食",
    "product": "产品", "consumer_electronics": "产品", "machine": "产品",
    "vehicle": "汽车", "car": "汽车", "clothing": "人像", "rain": "雨夜",
}


# 语义分割类别 → 场景家族。
# 分割证据比图像分类标签强得多：它给出面积与逐像素置信度，
# 「building 占 42.7%、置信 0.93」比一个 building 标签可靠得多。
# 面积门是为了不让角落里的一点点天空把整张图判成风光。
SEGMENTATION_SCENE_FAMILIES = {
    "sky": [("天空", 0.10), ("户外", 0.10)],
    "vegetation": [("植被", 0.12), ("森林", 0.35)],
    "building": [("建筑", 0.15), ("城市", 0.25)],
    "product": [("产品", 0.10)],
}


def scene_families(semantic: dict | None) -> dict[str, float]:
    """综合图像分类标签与语义分割结果推断场景家族。

    两者角色不同：分类标签覆盖面广但只有一个置信度；
    分割给出面积与像素级置信度，对「这张图主要是什么」判断力更强。
    分割证据缺席时退回分类标签，两者都没有时返回空——空会触发窄场景配方的人工确认。
    """
    if not semantic or not semantic.get("available"):
        return {}
    families: dict[str, float] = {}
    for item in semantic.get("scene_labels", []):
        family = VISION_SCENE_FAMILIES.get(item["label"])
        if family:
            families[family] = max(families.get(family, 0.0), float(item["confidence"]))
    for klass, mappings in SEGMENTATION_SCENE_FAMILIES.items():
        info = (semantic.get("classes") or {}).get(klass) or {}
        if not info.get("present"):
            continue
        area = float(info.get("area_ratio") or 0.0)
        confidence = float(info.get("mean_confidence") or 0.0)
        if confidence < 0.45:
            continue
        for family, min_area in mappings:
            if area >= min_area:
                # 证据强度同时反映置信度与面积，面积越大越确信这是画面主体
                strength = confidence * min(1.0, 0.5 + area)
                families[family] = max(families.get(family, 0.0), strength)
    return families


def _ev_feasibility(wanted_ev: float, up: float, down: float) -> tuple[float, str | None]:
    """配方想要的相对 EV 是否落在素材真实余量之内。"""
    if wanted_ev > 0:
        if wanted_ev <= up:
            return 1.0, None
        overshoot = wanted_ev - up
        return max(0.0, 1.0 - overshoot / 0.6), f"配方想提亮 {wanted_ev:+.2f}EV，素材只剩 {up:+.2f}EV 余量"
    if wanted_ev < 0:
        if wanted_ev >= down:
            return 1.0, None
        overshoot = down - wanted_ev
        return max(0.0, 1.0 - overshoot / 0.6), f"配方想压暗 {wanted_ev:+.2f}EV，素材只剩 {down:+.2f}EV 余量"
    return 1.0, None


# 有彩像素超过这个比例时，近中性像素只剩少数派，
# 用它们求出的偏色方向不再代表这张图的色彩性格。
# 0.75 的来处：实测那张被误判的金色日落是 0.866，
# 而正常需要靠近中性判偏色的灰片、白墙室内都远在 0.6 以下。
NEUTRAL_CAST_UNREPRESENTATIVE_ABOVE = 0.75



# 候选预检的剪切限值。口径是**亮度域**（只统计真正丢细节的像素），
# 与渲染路径的门一致；沿用原有的 0.02 / 0.08 数值，只换口径不放宽数字。
BLOW_PRECHECK_LIMIT = 0.02
CRUSH_PRECHECK_LIMIT = 0.08


def _light_fact_from(diagnosis: dict) -> dict:
    """从既有诊断结构里就地推出光照事实，便于旧调用方无缝接入。"""
    tone = (diagnosis.get("analysis") or {}).get("tone") or diagnosis.get("tone") or {}
    lc = diagnosis.get("light_context") or {}
    labels = {}
    for key, name in (("day_score", "day"), ("night_score", "night"), ("indoor_score", "indoor")):
        if lc.get(key) is not None:
            labels[name] = lc[key]
    return scene_facts.derive_light_fact(tone, labels)


def score_recipe(recipe: dict, diagnosis: dict, semantic: dict | None) -> dict:
    analysis = diagnosis["analysis"]
    tone = analysis["tone"]
    color = analysis["color"]
    texture = analysis["texture"]
    tags = recipe["tags"]
    policy = recipe["impact"]["tone_policy"]
    targets = recipe["visual_targets"]

    score = 0.0
    supports: list[str] = []
    penalties: list[str] = []
    blockers: list[str] = []

    # 0. 硬性排除门：光照语境与禁止场景。
    # 这一段直接对应 BLC-30-002（黑曜金界被按「建筑」错误推荐给明亮秋日城市）。
    conditions = recipe.get("match_conditions") or {"light_context": ["any"], "forbidden_scene_families": []}
    allowed_contexts = conditions.get("light_context", ["any"])

    # 场景事实硬门。**不扣分**——原实现在 blocker 之外还写了 score -= 60，
    # 那是打分式修复：一个足够高的其它加分就能把它抵消回来。
    # 事实不满足的配方在排序之前就出局，不留任何可被抵消的数值。
    #
    # 事实的来源也换了：原来直接读 diagnosis["light_context"]["context"]，
    # 而那个值可以被一个 0.81 的分类器标签单独决定——实测一张长曝光夜景
    # 因此被判成「白天」，Top-1 推成日落暖调。现在由 scene_facts 综合像素与标签，
    # 两者矛盾时产出 conflicting，而不是二选一。
    light_fact = diagnosis.get("light_fact") or _light_fact_from(diagnosis)
    admissible, admissible_why = scene_facts.recipe_admissible(recipe, {"light": light_fact})
    if not admissible:
        blockers.append(admissible_why)
    detected_families = scene_families(semantic)
    hit_forbidden = [s for s in conditions.get("forbidden_scene_families", []) if s in detected_families]
    if hit_forbidden:
        blockers.append(
            f"图像分类检测到该配方的禁止场景：{'、'.join(hit_forbidden)}（{conditions.get('basis','')}）"
        )
        score -= 60.0

    # 配方自己声明「不做满屏橙黄」时，把这条审美禁令落实为可复核的面积门，
    # 不能只留在 warning 里等人踩坑。warm_area 由有彩像素占比 × 暖色簇权重得到，
    # 表示整张图已有多少面积落在红／橙／黄；这是面积因果门，不是调分。
    max_warm_area = conditions.get("max_existing_warm_area_ratio")
    if max_warm_area is not None:
        warm_families = {"红", "橙／肤色带", "黄"}
        warm_weight = sum(
            float(item.get("weight", 0.0)) for item in color.get("dominant_hues", [])
            if item.get("family") in warm_families
        )
        warm_area = float(color.get("chromatic_pixel_ratio", 0.0)) * warm_weight
        if warm_area >= float(max_warm_area):
            blockers.append(
                f"画面已有 {warm_area:.1%} 面积落在红／橙／黄，超过该配方“不得满屏橙黄”的"
                f" {float(max_warm_area):.0%} 上限"
            )

    # 1. 相对 EV 可行性（最高权重：方向不可行就一切免谈）
    feasibility, note = _ev_feasibility(
        float(policy["midpoint_ev"]), float(tone["headroom_ev_up"]), float(tone["headroom_ev_down"])
    )
    score += 30.0 * feasibility
    if note:
        penalties.append(note)
    else:
        supports.append(f"相对 EV {float(policy['midpoint_ev']):+.2f} 落在素材余量内")

    # 2. 剪切安全
    #
    # 口径必须与渲染路径的门一致。那里自己写着：
    #   「通道口径含纯饱和色，阈值宽；亮度口径只含真正丢细节的像素，阈值严」
    # 而这道预检原本用的是**通道口径**。实测一张长曝光夜景城市：
    #   通道口径 shadow_clip_ratio = 0.1267
    #   亮度口径 luma_crushed_ratio = 0.0217   ← 高估 5.8 倍
    # 霓虹与车流这些纯饱和色被通道口径算成了「死黑」，
    # 于是 8 套明确声明夜景的配方全部被判不可用，理由是「暗部已死黑 12.7%」——
    # 而那 12.7% 里绝大部分根本没有丢失细节。
    #
    # 这不是放宽阈值，是把同一个判断对齐到本流程自己认定更严格、更正确的口径。
    if tone["luma_blown_ratio"] > BLOW_PRECHECK_LIMIT and float(policy["midpoint_ev"]) > 0.05:
        blockers.append(f"亮部已过曝 {tone['luma_blown_ratio']:.1%}（亮度口径），该配方仍要提亮")
    if tone["luma_crushed_ratio"] > CRUSH_PRECHECK_LIMIT and float(policy["midpoint_ev"]) < -0.05:
        blockers.append(f"暗部已丢细节 {tone['luma_crushed_ratio']:.1%}（亮度口径），该配方仍要压暗")

    # 3. 影调跨度意图与素材现状是否匹配
    span = tone["tone_span"]
    if targets["tone_span"] == "increase":
        if span < 0.55:
            score += 16.0
            supports.append(f"当前明暗跨度只有 {span:.3f}，还有拉开空间")
        elif span > 0.85:
            score -= 10.0
            penalties.append(f"明暗跨度已达 {span:.3f}，再拉开容易两端剪切")
    elif targets["tone_span"] == "compress":
        if span > 0.7:
            score += 14.0
            supports.append(f"明暗跨度 {span:.3f} 偏大，压缩能换来柔和")
        elif span < 0.3:
            score -= 16.0
            penalties.append(f"明暗跨度只有 {span:.3f}，再压缩会变灰片")

    # 4. 色彩语法可行性
    chromatic = color["chromatic_pixel_ratio"]
    families = {item["family"] for item in color["dominant_hues"]}
    syntax = tags["color"]
    if syntax == "monochrome":
        score += 8.0
        supports.append("去色方案对任何素材都技术可行，成败取决于明度关系")
        # 「技术可行」不是「合适」。色彩本身就是内容时，去色是把内容拿掉，
        # 而不是把影调调好。实测一张金黄麦片被推成黑白，出来是一盘灰糊。
        #
        # 这里**不改分**：实测 5 张素材里，那张坏结果的有彩比 0.750，
        # 而黑白排名正常偏低的夜景是 0.729——两者贴得太近，
        # 在中间划一条线只是让某一张好看，不是判据。
        # 真正缺的是「这是一盘食物」这种主体识别，现有语义后端给不出，
        # 不靠调权重掩盖。能诚实做到的是把代价量化摆出来，让人自己判断。
        if chromatic >= 0.5:
            penalties.append(
                f"画面 {chromatic:.0%} 的像素带色相（综合色彩 {color['colorfulness']:.3f}）；"
                "去色会把这部分内容整体拿掉——这是一次改变主题的干预，不是影调优化。"
                "颜色本身就是主题时（食物、日落、霓虹），先确认你真的要黑白"
            )
    elif syntax == "green-world":
        if families & {"绿", "黄绿", "青绿"}:
            score += 14.0
            supports.append("画面确实存在绿系主导色相，可以被强化")
        else:
            blockers.append(f"画面主导色相是 {'、'.join(sorted(families)) or '无'}，没有绿系可强化")
            score -= 22.0
    elif syntax == "warm-cool-split":
        warm = families & {"红", "橙／肤色带", "黄"}
        cool = families & {"青蓝", "蓝", "青绿"}
        if warm and cool:
            score += 18.0
            supports.append("画面同时存在暖侧与冷侧色相，暖冷对立可以真实成立")
        elif warm or cool:
            # 灰片的低彩是「待恢复」而不是「不存在」。只要还有可辨的主导色相，
            # 暖冷分立就是恢复而非凭空造色；真正无彩才该罚。
            score += 11.0
            missing = "冷侧" if warm else "暖侧"
            supports.append(
                f"画面仍保留 {'、'.join(sorted(warm or cool))} 的主导色相，{missing}需要靠调色建立，属于恢复而非凭空造色"
            )
        else:
            score -= 14.0
            penalties.append(f"画面没有任何可辨主导色相（有彩像素 {chromatic:.1%}），暖冷分立会变成凭空造色")
    elif syntax in {"warm-analogous", "cool-analogous"}:
        # 判这张图偏暖还是偏冷，先要问「用哪些像素判」。
        #
        # white_balance 的 cast 是只在近中性像素上求的。图里绝大多数像素都带色相时，
        # 那点近中性残留代表的是少数派，不是这张图的色彩性格。
        # 实测一张金色日落：87% 像素带色相，近中性样本几乎全落在水面与天空阴影里，
        # 于是 cast 报 250°「轻微偏冷」，cool-analogous 拿到 +12「顺势强化」，
        # 冷灰海被推成 top1 把金色抽干，而 sunset-warm 掉到第 3。
        # 这跟「用人物蒙版求肤色簇中心」是同一个错：统计量求在了错的子集上。
        #
        # 所以有彩像素占绝大多数时，改用主导色相家族判断——那才是多数派的证据。
        warm_families = families & {"红", "橙／肤色带", "黄"}
        cool_families = families & {"青蓝", "蓝", "青绿"}
        if chromatic >= NEUTRAL_CAST_UNREPRESENTATIVE_ABOVE:
            is_warm_cast = bool(warm_families) and not cool_families
            is_cool_cast = bool(cool_families) and not warm_families
            basis = f"主导色相（有彩像素 {chromatic:.1%}，近中性样本不足以代表这张图）"
        else:
            cast_hue = color["white_balance"]["cast_hue"]
            cast_magnitude = color["white_balance"]["cast_magnitude"]
            is_warm_cast = cast_magnitude > 0.006 and 20.0 <= cast_hue <= 110.0
            is_cool_cast = cast_magnitude > 0.006 and 190.0 <= cast_hue <= 300.0
            basis = "近中性像素的偏色方向"
        if (syntax == "warm-analogous" and is_warm_cast) or (syntax == "cool-analogous" and is_cool_cast):
            score += 12.0
            supports.append(f"素材的冷暖方向与该色彩语法一致，属于顺势强化（依据：{basis}）")
        elif (syntax == "warm-analogous" and is_cool_cast) or (syntax == "cool-analogous" and is_warm_cast):
            score += 2.0
            penalties.append(f"素材的冷暖方向与该色彩语法相反，需要先中和再重建，干预较大（依据：{basis}）")
        else:
            score += 7.0
    elif syntax == "restrained-neutral":
        score += 9.0
        if chromatic < 0.15:
            supports.append("画面本就低彩，克制方案不需要凭空造色")

    # 5. 综合色彩方向
    if targets["colorfulness"] == "increase" and color["colorfulness"] < 0.10:
        score += 10.0
        supports.append(f"综合色彩仅 {color['colorfulness']:.3f}，有提升空间")
    elif targets["colorfulness"] == "decrease" and color["colorfulness"] < 0.07:
        score -= 12.0
        penalties.append(f"综合色彩已经只有 {color['colorfulness']:.3f}，继续降会坍缩为脏灰")

    # 6. 材质与噪点
    if tags["texture"] in {"crisp"} and texture["noise_estimate"] > 0.009:
        score -= 8.0
        penalties.append(f"素材噪点估计 {texture['noise_estimate']:.4f}，清脆材质会放大噪点")
    if tags["texture"] == "grain" and texture["noise_estimate"] > 0.012:
        score -= 6.0
        penalties.append("素材本身已有明显噪点，再加颗粒会显脏")

    # 7. 真实场景匹配（来自图像分类模型，不是用户关键词）
    families_detected = scene_families(semantic)
    required_families = conditions.get("requires_any_scene_family") or []
    # 加分要看两套词表的并集。
    # recipe["scenes"] 是给人看的题材标签（日落／旅行／逆光），
    # requires_any_scene_family 才是与检测器同词表的场景族（日落／天空／户外）。
    # 早先只用前者：一张日落照检出「天空 0.94、户外 0.94」，
    # 而 sunset-warm 的题材标签与之零交集，白白少了 12 分的场景加成，
    # 结果日落配方在日落照片上排到第 5，进不了 smart 模式的前三。
    # 它同时还通过了 required_families 的硬性门——同一个字段能拦不能奖，本身就说明用漏了。
    scene_vocabulary = list(dict.fromkeys(list(recipe["scenes"]) + list(required_families)))
    matched_scenes = [s for s in scene_vocabulary if s in families_detected]
    if matched_scenes:
        boost = 12.0 * max(families_detected[s] for s in matched_scenes)
        score += boost
        supports.append(f"图像分类结果与配方适用场景吻合：{'、'.join(matched_scenes)}")
    if conditions.get("scene_specificity") == "narrow" and required_families:
        if not families_detected:
            penalties.append("图像分类没有给出可用场景，该配方只在特定场景成立，需要人工确认场景")
        elif not (set(required_families) & set(families_detected)):
            # 「只按风格名称推荐会错路由」是已记录的失败。窄场景配方在场景完全不符时必须重罚。
            score -= 28.0
            penalties.append(
                f"该配方只在 {'／'.join(required_families)} 场景成立，"
                f"但图像分类给出的是 {'、'.join(sorted(families_detected))}"
            )

    # 8. 人物与肤色
    import semantic_backend
    person_present = semantic_backend.class_presence(semantic, "person")
    if person_present is True:
        if "人像" in recipe["scenes"]:
            score += 8.0
            supports.append("素材中确有人物，且该配方声明适用于人像")
        if targets["colorfulness"] == "decrease" and tags["color"] != "monochrome":
            penalties.append("素材中有人物，降低综合色彩需要人工确认肤色是否变灰")
        if tags["color"] == "green-world":
            penalties.append("素材中有人物，绿系强化容易让肤色偏青")

    return {
        "recipe_id": recipe["id"],
        "recipe_name": recipe["name"],
        "collection": recipe["collection"],
        "tags": tags,
        "score": round(score, 2),
        "supports": supports,
        "penalties": penalties,
        "blockers": blockers,
        "usable": not blockers,
    }


def rank(catalog: list[dict], diagnosis: dict, semantic: dict | None,
         media_type: str = "photo", count: int = 3, diversity: bool = True) -> list[dict]:
    scored = [
        score_recipe(recipe, diagnosis, semantic)
        for recipe in catalog if media_type in recipe["media_types"]
    ]
    scored.sort(key=lambda item: (-item["score"], item["recipe_id"]))
    # 事实确定时，把「自己声明适用于这个场景」的候选排到通用候选之前。
    # 不改任何分数，只换呈现顺序——通用配方否则会永远挤掉专为该场景声明的配方。
    _fact = diagnosis.get("light_fact") or _light_fact_from(diagnosis)
    if _fact.get("state") == "known":
        _by_id = {r["id"]: r for r in catalog}
        for item in scored:
            item.setdefault("scenes", _by_id.get(item["recipe_id"], {}).get("scenes") or [])
        scored = scene_facts.scene_matched_first(scored, _fact.get("value"))
    if not diversity:
        return scored[:count]
    # 多样性约束：三个推荐不能是同一色彩语法的三个近亲。
    chosen: list[dict] = []
    for item in scored:
        if not item["usable"] and len(scored) > count:
            continue
        if any(existing["tags"]["color"] == item["tags"]["color"]
               and existing["tags"]["tone"] == item["tags"]["tone"] for existing in chosen):
            continue
        chosen.append(item)
        if len(chosen) >= count:
            break
    for item in scored:
        if len(chosen) >= count:
            break
        if item not in chosen:
            chosen.append(item)
    return chosen[:count]
