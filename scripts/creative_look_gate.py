"""创意 Look 的可执行资格门。

这里只判断结构化的现场观察与能力是否满足，不根据风格名猜场景，
也不把 research 预演误写成正式可交付输出。
"""


def evaluate(contract: dict, observed: set[str], capabilities: set[str]) -> dict:
    required = set(contract.get("required_observations", []))
    missing_observations = sorted(required - set(observed))
    if missing_observations:
        return {
            "status": "blocked_scene",
            "missing_observations": missing_observations,
            "missing_capabilities": [],
            "formal_output_allowed": False,
        }

    missing_capabilities = set()
    for condition in contract.get("capability_conditions", []):
        triggers = set(condition.get("when_any", []))
        if triggers & set(observed):
            missing_capabilities.update(
                set(condition.get("require", [])) - set(capabilities))

    if missing_capabilities:
        return {
            "status": "blocked_capability",
            "missing_observations": [],
            "missing_capabilities": sorted(missing_capabilities),
            "formal_output_allowed": False,
        }

    return {
        "status": contract.get("eligible_status", "eligible_research_preview"),
        "missing_observations": [],
        "missing_capabilities": [],
        "formal_output_allowed": bool(contract.get("formal_output_allowed", False)),
    }
