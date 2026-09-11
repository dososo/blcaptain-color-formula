"""配方媒体级准入；支持类型不等于执行许可，缺新字段时沿用原状态。"""

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


def admitted_media(recipe: dict, *, automatic: bool = False) -> list:
    allowed = {"active"} if automatic else {"active", "manual-executable"}
    return [media for media, status in media_statuses(recipe).items() if status in allowed]
