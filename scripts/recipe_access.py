"""配方媒体声明与内部研发沿革。

v4.9.1 起，用户执行资格只由 ``media_types`` 决定。历史状态字段继续供
归档和研发审计使用，但不再限制清单、推荐、计划或渲染。
"""

STATUSES = frozenset({"active", "manual-executable", "research"})
MEDIA_TYPES = frozenset({"photo", "video"})


def media_statuses(recipe: dict) -> dict:
    declared = recipe.get("media_types")
    if (not isinstance(declared, list) or not declared
            or any(not isinstance(value, str) or value not in MEDIA_TYPES for value in declared)
            or len(set(declared)) != len(declared)):
        raise ValueError("配方声明媒体类型无效")
    if not isinstance(recipe.get("status"), str) or recipe["status"] not in STATUSES:
        raise ValueError("配方默认准入状态无效")
    if "media_status" not in recipe:
        return {media: recipe["status"] for media in declared}
    mapping = recipe["media_status"]
    if (not isinstance(mapping, dict) or set(mapping) != set(declared)
            or any(not isinstance(value, str) or value not in STATUSES for value in mapping.values())):
        raise ValueError("媒体准入必须完整对应声明媒体，且仅使用有效执行状态")
    return dict(mapping)


def status_for(recipe: dict, media_type: str) -> str:
    statuses = media_statuses(recipe)
    if media_type not in statuses:
        raise ValueError(f"配方未声明支持媒体：{media_type}")
    return statuses[media_type]


def executable_media(recipe: dict) -> list:
    """返回用户可执行媒体；同时复用旧字段校验，但不按旧状态分级。"""
    statuses = media_statuses(recipe)
    return list(statuses)


def apply_execution_overrides(payload: dict) -> dict:
    """把目录级执行参数投影到运行时，不改写研发历史快照。"""
    overrides = payload.get("execution_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("执行参数覆盖必须是对象")
    recipes = payload.get("recipes") or []
    by_id = {item.get("id"): item for item in recipes if isinstance(item, dict)}
    unknown = set(overrides) - set(by_id)
    if unknown:
        raise ValueError(f"执行参数覆盖引用未知配方：{sorted(unknown)}")
    for recipe_id, override in overrides.items():
        if (not isinstance(override, dict)
                or set(override) != {"parameters", "basis"}
                or not isinstance(override["parameters"], dict)
                or not isinstance(override["basis"], str)
                or not override["basis"].strip()):
            raise ValueError(f"配方 {recipe_id} 的执行参数覆盖无效")
        by_id[recipe_id]["parameters"].update(override["parameters"])
    return payload


def admitted_media(recipe: dict, *, automatic: bool = False) -> list:
    """旧研发准入视图，仅供归档兼容；用户入口应使用 executable_media。"""
    allowed = {"active"} if automatic else {"active", "manual-executable"}
    return [media for media, status in media_statuses(recipe).items() if status in allowed]
