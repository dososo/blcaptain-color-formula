#!/usr/bin/env python3
"""受约束灵感组合语法。

「随机」不是随机搅拌滑杆。一个组合只能由四条轴上互相兼容的选项构成：
    一个一级影调策略 + 一个主色彩语法 + 一个材质策略 + 一组局部策略
并且：
- 组合的执行体永远来自正式目录，微调限制在既有安全机制内；
- 同一输入 + 同一随机种子必须复现；
- 三组之间必须有可测差异；
- 冲突率必须为 0，冲突由显式禁止表判定，不靠运气。

轴标签由正式配方的既有结构化字段确定性派生，不是人工贴标或凭空发明。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
if __package__:
    from .recipe_access import apply_execution_overrides, executable_media
else:
    from recipe_access import apply_execution_overrides, executable_media

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "references" / "recipes.json"
GRAMMAR_SCHEMA_VERSION = "4.0.0"

TONE_STRATEGIES = {
    "foundation-preserve": {"name": "保留基础影调", "desc": "沿用已建立的基础明暗，不为风格二次改变影调"},
    "airy-lift": {"name": "通透抬亮", "desc": "抬中间调、控高光滚降，让画面呼吸"},
    "neutral-restore": {"name": "中性还原", "desc": "只恢复可读性，不强加情绪"},
    "density-press": {"name": "密度压暗", "desc": "压趾建立黑位重量与体积"},
    "filmic-shoulder": {"name": "胶片肩趾", "desc": "抬黑压白，柔化两端，弱化冲突"},
    "hard-contrast": {"name": "硬对比", "desc": "拉开明暗跨度，强化结构与冲击"},
}
COLOR_SYNTAXES = {
    "conditional-deep-blue": {"name": "条件化深蓝", "desc": "按低中调环境与记忆色证据分区，非全局染蓝或通用LUT"},
    "warm-cool-split": {"name": "暖冷分立", "desc": "暖生命色与冷环境对立，建立空间"},
    "warm-analogous": {"name": "暖邻近", "desc": "色相收拢在暖侧，统一而亲密"},
    "cool-analogous": {"name": "冷邻近", "desc": "色相收拢在冷侧，安静而疏离"},
    "green-world": {"name": "绿系世界", "desc": "以植被与湿气为主色语法"},
    "restrained-neutral": {"name": "克制低彩", "desc": "让事件与结构优先于颜色"},
    "monochrome": {"name": "无彩", "desc": "移除色相，只靠明度关系叙事"},
}
TEXTURE_STRATEGIES = {
    "clean": {"name": "干净", "desc": "低噪、克制锐化，保留材质差异"},
    "soft": {"name": "柔化", "desc": "柔和高光与微对比，减少硬边"},
    "grain": {"name": "颗粒", "desc": "加入空间稳定的颗粒，制造密度与年代感"},
    "crisp": {"name": "清脆", "desc": "提高微对比，强调结构与边缘"},
}
LOCAL_STRATEGIES = {
    "none": {
        "name": "不做局部", "desc": "只做全局调色，最安全",
        "requires": [], "level": "L0",
    },
    "radial-attention": {
        "name": "几何注意力", "desc": "用连续径向明暗场把视线引向已确认的重心；不是人脸识别",
        "requires": ["user_confirmed_center"], "level": "L1",
    },
    "person-protect": {
        "name": "人物保护", "desc": "全局风格照常执行，但人物区的影调与彩度变化被限幅，避免肤色被带走",
        "requires": ["semantic.person"], "level": "L2",
    },
    "person-lift": {
        "name": "人物塑光", "desc": "只在人物区做提亮与彩度恢复，环境保持不动",
        "requires": ["semantic.person"], "level": "L2",
    },
    "sky-depth": {
        "name": "天空层次", "desc": "只压天空亮部并恢复云层层次，地面不动；天空是大面积平滑渐变，压过头会出色带",
        "requires": ["semantic.sky"], "level": "L2",
    },
    "sky-protect": {
        "name": "天空保护", "desc": "全局风格照常执行，天空区变化被限幅，避免蓝天被风格染色",
        "requires": ["semantic.sky"], "level": "L2",
    },
    "foliage-tune": {
        "name": "植被分色", "desc": "只在植被区调整黄绿倾向与彩度，肤色与环境不受影响；秋叶可能被误当植被",
        "requires": ["semantic.vegetation"], "level": "L2",
    },
    "building-structure": {
        "name": "建筑结构", "desc": "只在建筑区增强中频局部对比突出石材结构；玻璃幕墙会被算作建筑",
        "requires": ["semantic.building"], "level": "L2",
    },
    "product-neutral": {
        "name": "商品中性化", "desc": "只在商品区做中性校正让商品颜色可信；透明与高反光商品蒙版不可靠",
        "requires": ["semantic.product"], "level": "L2",
    },
}

# 显式禁止表。每条都必须写明理由，禁止「凭感觉不搭」。
FORBIDDEN = [
    ("airy-lift", "density-press", "同时抬亮与压暗中间调，互相抵消后只剩灰"),
    ("monochrome", "warm-cool-split", "已移除色相就不存在暖冷对立"),
    ("monochrome", "warm-analogous", "已移除色相就没有色相邻近关系"),
    ("monochrome", "cool-analogous", "已移除色相就没有色相邻近关系"),
    ("monochrome", "green-world", "已移除色相就没有绿系主色"),
    ("restrained-neutral", "warm-cool-split", "克制低彩与强暖冷对立是两个相反的色彩主张"),
    ("filmic-shoulder", "hard-contrast", "胶片肩趾的目的就是削弱两端冲突，与硬对比直接矛盾"),
    ("clean", "grain", "同一画面不能既声明干净又声明颗粒"),
    ("soft", "crisp", "柔化与清脆是同一材质维度的相反方向"),
]
# 局部策略与其他轴的禁止关系
FORBIDDEN_LOCAL = [
    ("person-lift", "monochrome", "人物塑光的收益主要来自肤色与环境的色彩分离；无彩下退化为单纯提亮"),
]


def _pairs_forbidden(selection: dict) -> list[str]:
    values = {selection["tone"], selection["color"], selection["texture"]}
    reasons = []
    for a, b, reason in FORBIDDEN:
        if a in values and b in values:
            reasons.append(f"{a} × {b}：{reason}")
    for local, other, reason in FORBIDDEN_LOCAL:
        if selection["local"] == local and other in values:
            reasons.append(f"{local} × {other}：{reason}")
    return reasons


def derive_tags(recipe: dict) -> dict:
    """优先使用配方自己声明的 combo_tags，不由标签合法性推断已审核。

    纯参数派生对「暖石材 vs 冷天空」这类以面积和叙事定义的色彩主张会失真，
    因此标签保留在配方内；历史review文案未绑定证据，审核状态仍待核验。
    派生逻辑保留为新配方的起草工具与回归对照。
    """
    explicit = recipe.get("combo_tags")
    if isinstance(explicit, dict) and {"tone", "color", "texture"} <= set(explicit):
        if (explicit["tone"] in TONE_STRATEGIES and explicit["color"] in COLOR_SYNTAXES
                and explicit["texture"] in TEXTURE_STRATEGIES):
            return {"tone": explicit["tone"], "color": explicit["color"],
                    "texture": explicit["texture"], "source": "declared",
                    "review_status": "unverified"}
        raise ValueError(f"配方 combo_tags 取值不在轴定义内：{recipe['id']}")
    return _derive_tags_from_parameters(recipe)


def _derive_tags_from_parameters(recipe: dict) -> dict:
    impact = recipe["impact"]
    policy = impact["tone_policy"]
    targets = recipe["visual_targets"]
    parameters = recipe["parameters"]
    midpoint = float(policy["midpoint_ev"])
    intent = policy["contrast_intent"]
    span = targets["tone_span"]
    shadow_bias = float(policy["shadow_bias"])

    if intent == "soften" and shadow_bias > 0.01:
        tone = "filmic-shoulder"
    elif span == "compress":
        tone = "filmic-shoulder"
    elif midpoint <= -0.10:
        tone = "density-press"
    elif midpoint >= 0.14 and intent != "increase":
        tone = "airy-lift"
    elif intent == "increase" and span == "increase" and float(parameters["contrast"]) >= 1.14:
        tone = "hard-contrast"
    elif midpoint >= 0.10:
        tone = "airy-lift"
    else:
        tone = "neutral-restore"

    saturation = float(parameters["saturation"])
    temperature = float(parameters["temperature"])
    # build_filter 中 teal_orange 正值 = 阴影偏蓝 + 高光偏红，即经典暖冷分立。
    teal = float(parameters.get("teal_orange", 0.0))
    bands = {item["band"]: float(item["saturation"]) for item in recipe.get("hsl_bands", [])}
    green_push = bands.get("g", 0.0)
    cyan_push = max(bands.get("c", 0.0), bands.get("b", 0.0))
    scenes = "".join(recipe.get("scenes", []))
    green_scene = any(word in scenes for word in ("森林", "植被", "草原", "绿", "露营", "山林", "雨后"))

    if saturation <= 0.35:
        color = "monochrome"
    elif teal >= 0.02:
        color = "warm-cool-split"
    elif green_push >= 0.02 and green_push >= cyan_push:
        color = "green-world"
    elif green_scene and saturation >= 1.0:
        color = "green-world"
    elif cyan_push >= 0.02 or temperature <= -0.03:
        color = "cool-analogous"
    elif temperature >= 0.02:
        color = "warm-analogous"
    elif targets["colorfulness"] == "decrease" or saturation < 0.95:
        color = "restrained-neutral"
    else:
        color = "restrained-neutral"

    texture = targets["texture"]
    if texture not in TEXTURE_STRATEGIES:
        texture = "clean"
    return {"tone": tone, "color": color, "texture": texture, "source": "derived"}


def load_tagged_catalog(catalog_path: Path = CATALOG,
                        *, include_research: bool = False,
                        media_type: str | None = None) -> list[dict]:
    """读取用户可执行目录；include_research 仅保留参数兼容，不改变结果。"""
    payload = apply_execution_overrides(
        json.loads(catalog_path.read_text(encoding="utf-8")))
    recipes = []
    for recipe in payload["recipes"]:
        available = executable_media(recipe)
        if not available or (media_type is not None and media_type not in available):
            continue
        tags = derive_tags(recipe)
        recipes.append({
            "id": recipe["id"],
            "name": recipe["name"],
            "collection": recipe.get("collection", "Core"),
            "media_types": available,
            "summary": recipe["summary"],
            "scenes": recipe["scenes"],
            "art_direction": recipe["art_direction"],
            "suitability": recipe["suitability"],
            "visual_targets": recipe["visual_targets"],
            "impact": recipe["impact"],
            "parameters": recipe["parameters"],
            "warnings": recipe["warnings"],
            "match_conditions": recipe.get("match_conditions", {"light_context": ["any"], "forbidden_scene_families": []}),
            "seasonal_affinity": recipe["seasonal_affinity"],
            "tags": tags,
        })
    return recipes


class SeededRandom:
    """确定性线性同余；不使用 random 模块，保证跨版本跨平台复现。"""

    def __init__(self, seed: int):
        self.state = (seed ^ 0x9E3779B9) & 0xFFFFFFFF or 1

    def next(self) -> float:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state / 0x100000000

    def pick(self, items: list):
        if not items:
            raise ValueError("没有可选项")
        return items[int(self.next() * len(items)) % len(items)]


def stable_seed(*parts: object) -> int:
    blob = "|".join(str(item) for item in parts).encode("utf-8")
    return int(hashlib.sha256(blob).hexdigest()[:8], 16)


def capability_filter(local_id: str, capabilities: dict) -> tuple[bool, str]:
    strategy = LOCAL_STRATEGIES[local_id]
    for requirement in strategy["requires"]:
        if requirement == "user_confirmed_center":
            if not capabilities.get("user_confirmed_center"):
                return False, "径向注意力必须由用户单独确认重心，不能自动施加"
            continue
        if requirement.startswith("semantic."):
            klass = requirement.split(".", 1)[1]
            info = capabilities.get("semantic_classes", {}).get(klass) or {}
            status = info.get("status")
            if status not in ("supported", "derived"):
                return False, f"语义类别 {klass} 当前后端不支持（{status or 'unavailable'}），不得用固定 HSL 冒充"
            if not info.get("present"):
                return False, f"本张素材中未检测到{klass}，该局部策略无对象可施加"
    return True, ""


def combination_distance(first: dict, second: dict) -> int:
    """四轴上的差异数量。用于保证三组之间不是轻微改值。"""
    return sum(
        1 for axis in ("tone", "color", "texture", "local")
        if first["selection"][axis] != second["selection"][axis]
    )


def build_combinations(
    diagnosis: dict,
    catalog: list[dict],
    capabilities: dict,
    seed: int,
    count: int = 3,
    media_type: str = "photo",
) -> dict:
    """生成受约束组合。返回结构中必须包含冲突计数，且该计数必须为 0。"""
    analysis = diagnosis["analysis"]
    tone = analysis["tone"]
    color = analysis["color"]

    # 由真实诊断得出的硬约束，不是偏好。
    blocked_tone: dict[str, str] = {}
    blocked_color: dict[str, str] = {}
    if tone["highlight_clip_ratio"] > 0.02:
        blocked_tone["airy-lift"] = f"亮部已有 {tone['highlight_clip_ratio']:.1%} 剪切，继续抬亮只会扩大不可恢复区"
    if tone["shadow_clip_ratio"] > 0.06:
        blocked_tone["density-press"] = f"暗部已有 {tone['shadow_clip_ratio']:.1%} 死黑，继续压暗会丢结构"
    if tone["tone_span"] < 0.18:
        blocked_tone["filmic-shoulder"] = f"明暗跨度只有 {tone['tone_span']:.3f}，再柔化两端会变成灰片"
    if color["chromatic_pixel_ratio"] < 0.06:
        blocked_color["warm-cool-split"] = f"有彩像素仅 {color['chromatic_pixel_ratio']:.1%}，不存在可用的暖冷对立"
        blocked_color["green-world"] = "画面几乎没有绿系信息，强推会造出假色"
    dominant_families = {item["family"] for item in color["dominant_hues"]}
    if dominant_families and not (dominant_families & {"绿", "黄绿", "青绿"}):
        blocked_color.setdefault("green-world", f"主导色相是 {'、'.join(sorted(dominant_families))}，没有绿系可强化")

    tone_options = [k for k in TONE_STRATEGIES if k not in blocked_tone]
    color_options = [k for k in COLOR_SYNTAXES if k not in blocked_color]
    texture_options = list(TEXTURE_STRATEGIES)
    local_options = []
    local_rejected = {}
    for key in LOCAL_STRATEGIES:
        allowed, reason = capability_filter(key, capabilities)
        if allowed:
            local_options.append(key)
        else:
            local_rejected[key] = reason

    random = SeededRandom(seed)

    # 只从目录里真实存在的 (影调, 色彩, 材质) 三元组采样。
    # 早期实现是三条轴独立随机再去找配方，找不到就退化成只匹配色彩——
    # 结果标签写着「密度压暗·清脆」而执行体其实是「胶片肩趾·干净」。
    # 那等于用文案冒充能力，必须让标签永远等于执行体的自我声明。
    available = [
        item for item in catalog
        if media_type in item["media_types"]
        and item["tags"]["tone"] not in blocked_tone
        and item["tags"]["color"] not in blocked_color
    ]
    empty_reason = (
        "没有符合本次素材诊断与候选范围的灵感组合；不回填已被排除的方向。"
        if not available else None
    )

    combos: list[dict] = []
    conflicts_detected = 0
    attempts = 0
    max_attempts = 1200
    while available and len(combos) < count and attempts < max_attempts:
        attempts += 1
        recipe = random.pick(sorted(available, key=lambda item: item["id"]))
        selection = {
            "tone": recipe["tags"]["tone"],
            "color": recipe["tags"]["color"],
            "texture": recipe["tags"]["texture"],
            "local": random.pick(local_options),
        }
        reasons = _pairs_forbidden(selection)
        if reasons:
            conflicts_detected += 1
            continue
        combo = {
            "selection": selection,
            "recipe_id": recipe["id"],
            "recipe_name": recipe["name"],
            "collection": recipe["collection"],
        }
        if any(existing["recipe_id"] == recipe["id"] for existing in combos):
            continue
        if any(combination_distance(combo, existing) < 2 for existing in combos):
            continue
        combos.append(combo)

    # 采样耗尽仍不足时，改用确定性扫描补齐，避免因运气不足而少给用户方案。
    if len(combos) < count:
        for recipe in sorted(available, key=lambda item: item["id"]):
            if len(combos) >= count:
                break
            if any(existing["recipe_id"] == recipe["id"] for existing in combos):
                continue
            for local in local_options:
                selection = {
                    "tone": recipe["tags"]["tone"],
                    "color": recipe["tags"]["color"],
                    "texture": recipe["tags"]["texture"],
                    "local": local,
                }
                if _pairs_forbidden(selection):
                    continue
                candidate = {
                    "selection": selection,
                    "recipe_id": recipe["id"],
                    "recipe_name": recipe["name"],
                    "collection": recipe["collection"],
                }
                if all(combination_distance(candidate, existing) >= 2 for existing in combos):
                    combos.append(candidate)
                    break

    enriched = []
    for index, combo in enumerate(combos, start=1):
        selection = combo["selection"]
        recipe = next(item for item in catalog if item["id"] == combo["recipe_id"])
        strength = 0.55 if selection["tone"] == "neutral-restore" else (0.7 if selection["tone"] == "hard-contrast" else 0.62)
        enriched.append({
            "index": index,
            "title": (
                f"{TONE_STRATEGIES[selection['tone']]['name']}·"
                f"{COLOR_SYNTAXES[selection['color']]['name']}·"
                f"{TEXTURE_STRATEGIES[selection['texture']]['name']}"
            ),
            "selection": selection,
            "readable": {
                "影调策略": f"{TONE_STRATEGIES[selection['tone']]['name']}——{TONE_STRATEGIES[selection['tone']]['desc']}",
                "色彩语法": f"{COLOR_SYNTAXES[selection['color']]['name']}——{COLOR_SYNTAXES[selection['color']]['desc']}",
                "材质策略": f"{TEXTURE_STRATEGIES[selection['texture']]['name']}——{TEXTURE_STRATEGIES[selection['texture']]['desc']}",
                "局部策略": f"{LOCAL_STRATEGIES[selection['local']]['name']}——{LOCAL_STRATEGIES[selection['local']]['desc']}",
            },
            "executes_as": {
                "recipe_id": recipe["id"],
                "recipe_name": recipe["name"],
                "collection": recipe["collection"],
                "suggested_strength": strength,
                "capability_level": LOCAL_STRATEGIES[selection["local"]]["level"],
            },
            "why_it_holds": recipe["art_direction"]["thesis"],
            "main_visual_action": recipe["art_direction"]["light"],
            "risk": recipe["suitability"]["avoid_when"],
            "fallback": recipe["suitability"]["fallback"],
            "conflict_check": "passed",
            "label_matches_executor": (
                recipe["tags"]["tone"] == selection["tone"]
                and recipe["tags"]["color"] == selection["color"]
                and recipe["tags"]["texture"] == selection["texture"]
            ),
        })

    distances = [
        combination_distance({"selection": a["selection"]}, {"selection": b["selection"]})
        for i, a in enumerate(enriched) for b in enriched[i + 1:]
    ]
    return {
        "schema_version": GRAMMAR_SCHEMA_VERSION,
        "mode": "constrained-inspiration",
        "seed": seed,
        "reproducible": "同一素材诊断 + 同一 seed 必得同一组结果",
        "requested": count,
        "produced": len(enriched),
        "empty_reason": empty_reason,
        "combinations": enriched,
        "conflict_rate": 0.0,
        "label_executor_mismatch": sum(1 for item in enriched if not item["label_matches_executor"]),
        "conflicts_rejected_during_search": conflicts_detected,
        "min_axis_distance": min(distances) if distances else None,
        "diagnosis_derived_blocks": {
            "tone": blocked_tone,
            "color": blocked_color,
            "local": local_rejected,
        },
        "boundary": (
            (empty_reason or "") +
            "组合只在正式目录与既有安全边界内取值；不发明新参数。"
            "每组仍需生成计划并由用户确认 plan_id 后才会渲染。"
        ),
    }


if __name__ == "__main__":
    import argparse
    import diagnose as diagnose_module

    parser = argparse.ArgumentParser(description="生成受约束灵感组合")
    parser.add_argument("--input", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--media-type", default="photo", choices=["photo", "video"])
    parser.add_argument("--profile", default="srgb")
    args = parser.parse_args()
    path = Path(args.input).expanduser().resolve()
    result = diagnose_module.diagnose(path, args.profile)
    capabilities = {"semantic_classes": {}, "user_confirmed_center": False}
    semantic = result.get("semantic") or {}
    if semantic.get("available"):
        capabilities["semantic_classes"] = {
            key: {"status": value.get("status"), "present": bool(value.get("present"))}
            for key, value in semantic["classes"].items()
        }
    seed = args.seed if args.seed is not None else stable_seed(path.name, result["analysis"]["tone"]["p50"])
    print(json.dumps(
        build_combinations(result, load_tagged_catalog(), capabilities, seed, args.count, args.media_type),
        ensure_ascii=False, indent=2,
    ))
