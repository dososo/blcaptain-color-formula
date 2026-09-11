#!/usr/bin/env python3
"""运行 24 个规则召回与人工动作覆盖场景；不冒充真实审美评测。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RECOMMENDER = ROOT / "scripts" / "recommend.py"


def load_recommender() -> Any:
    spec = importlib.util.spec_from_file_location("blcaptain_guidance", RECOMMENDER)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载指导推荐器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_scenarios(research_root: Path) -> list[dict[str, Any]]:
    payload = json.loads((research_root / "evaluation_scenarios.json").read_text(encoding="utf-8"))
    scenarios = payload.get("records")
    if not isinstance(scenarios, list):
        raise RuntimeError("评测场景结构无效")
    return scenarios


def metric(passed: int, total: int) -> dict[str, Any]:
    return {"passed": passed, "total": total, "rate": round(passed / total, 4) if total else 0.0}


def evaluate(research_root: Path) -> dict[str, Any]:
    recommender = load_recommender()
    # 推荐器默认读取技能内 research；显式替换目录，支持故障注入和独立副本评测。
    recommender.RESEARCH = research_root
    scenarios = read_scenarios(research_root)
    results = []
    top1_passed = top3_passed = required_passed = forbidden_passed = 0
    media_passed = temporal_passed = temporal_total = 0
    for scenario in scenarios:
        inputs = scenario.get("input", {})
        problems = "，".join(inputs.get("problems", []))
        ranked = recommender.rank_candidates(
            inputs.get("scene", ""),
            inputs.get("subject", ""),
            problems,
            "，".join(inputs.get("preferences", [])),
            3,
        )
        recommended = [item[2]["id"] for item in ranked]
        media_type = scenario.get("media_type", "photo")
        expected = scenario.get("expected_candidate_ids", [])
        top1_hit = bool(recommended and recommended[0] in expected)
        top3_hit = bool(set(recommended) & set(expected))
        generated_required, generated_forbidden = recommender.review_policy(problems)
        required_ok = set(scenario.get("required_behaviors", [])).issubset(generated_required)
        forbidden_ok = set(scenario.get("forbidden_behaviors", [])).issubset(generated_forbidden)
        media_ok = all(media_type in item[2].get("media_types", []) for item in ranked)
        generated_temporal_required = recommender.VIDEO_TEMPORAL_REQUIRED if media_type == "video" else []
        generated_temporal_forbidden = recommender.VIDEO_TEMPORAL_FORBIDDEN if media_type == "video" else []
        temporal_ok = True
        if media_type == "video":
            temporal_total += 1
            temporal_ok = (
                set(scenario.get("required_temporal_behaviors", [])).issubset(generated_temporal_required)
                and set(scenario.get("forbidden_temporal_behaviors", [])).issubset(generated_temporal_forbidden)
            )
            temporal_passed += int(temporal_ok)
        top1_passed += int(top1_hit)
        top3_passed += int(top3_hit)
        required_passed += int(required_ok)
        forbidden_passed += int(forbidden_ok)
        media_passed += int(media_ok)
        results.append({
            "id": scenario["id"],
            "name": scenario["name"],
            "media_type": media_type,
            "recommended": recommended,
            "expected": expected,
            "top1_hit": top1_hit,
            "top3_hit": top3_hit,
            "required_actions_passed": required_ok,
            "forbidden_actions_passed": forbidden_ok,
            "media_routing_passed": media_ok,
            "temporal_policy_passed": temporal_ok,
            "generated_required_actions": generated_required,
            "generated_forbidden_actions": generated_forbidden,
        })
    total = len(scenarios)
    # required／forbidden 的期望值来自场景的 problems 字符串，而这些字符串正是
    # ACTION_POLICIES 的字典键——所以那两项 36/36 在结构上必然成立，不携带信息。
    # 下面三个指标是真正有判别力的：规则引擎是否能对不同场景给出不同结论。
    action_sets = {frozenset(item["generated_required_actions"]) for item in results}
    forbidden_sets = {frozenset(item["generated_forbidden_actions"]) for item in results}
    recommendation_sets = {tuple(item["recommended"]) for item in results}
    empty_action_scenarios = [item["id"] for item in results if not item["generated_required_actions"]]
    return {
        "evaluation_boundary": "照片／视频关键词召回、媒体路由、人工动作与时序规则评测，属于非审美、非真实媒体、非跨 App 标定验证。",
        "tautology_warning": (
            "required_actions 与 forbidden_actions 的期望值直接来自场景 problems 字符串，"
            "而该字符串就是规则表的键，因此这两项的通过率在结构上必然为 100%，不构成质量证据。"
            "temporal_policy 比对的是两个模块级常量，同理。"
            "请以 discrimination 段的指标判断规则引擎是否真的有判别力。"
        ),
        "discrimination": {
            "distinct_required_action_sets": len(action_sets),
            "distinct_forbidden_action_sets": len(forbidden_sets),
            "distinct_recommendation_sets": len(recommendation_sets),
            "scenario_count": total,
            "required_action_diversity": round(len(action_sets) / total, 4),
            "recommendation_diversity": round(len(recommendation_sets) / total, 4),
            "scenarios_with_no_required_action": empty_action_scenarios,
            "coverage_gate_passed": not empty_action_scenarios,
            "coverage_gate_meaning": (
                "每个评测场景都必须至少命中一条人工检查规则。"
                "命中不到规则却因为场景期望为空而「通过」，是同义反复检查最典型的假阳性。"
            ),
            "meaning": (
                "多样性接近 1 说明规则能区分不同场景；接近 0 说明无论输入是什么都给同一套结论，"
                "那样即使通过率 100% 也毫无价值。"
            ),
        },
        "metrics": {
            "top1": metric(top1_passed, total),
            "top3": metric(top3_passed, total),
            "required_actions": metric(required_passed, total),
            "forbidden_actions": metric(forbidden_passed, total),
            "media_routing": metric(media_passed, total),
            "temporal_policy": metric(temporal_passed, temporal_total),
        },
        "scenarios": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 BLCaptain v1.2 结构化研究评测")
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        print(f"拒绝覆盖已有评测报告：{args.output}", file=sys.stderr)
        return 3
    try:
        report = evaluate(args.research_root.resolve())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, json.JSONDecodeError, RuntimeError) as error:
        print(f"评测失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
