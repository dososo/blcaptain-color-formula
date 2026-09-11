#!/usr/bin/env python3
"""严格校验研究层；只证明数据合同，不证明视觉效果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import style_bible_gates  # noqa: E402


EXPECTED = {
    "research_candidates.json": ("records", 52),
    "recommendation_rules.json": ("records", 52),
    "sources.json": ("records", 36),
    "platform_seeds.json": ("records", 156),
    "evaluation_scenarios.json": ("records", 36),
    "guidance_seeds.json": ("seeds", 156),
    "video_guidance_seeds.json": ("seeds", 156),
    "xiaohongshu-v3-candidates.json": ("notes", 225),
    "graduated_candidates.json": ("candidates", 29),
    "promotion_queue.json": ("queue", 28),
}


def load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"缺少文件：{path.name}")
        return {}
    except json.JSONDecodeError as error:
        errors.append(f"JSON 无法解析：{path.name} / {error}")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"根节点必须是对象：{path.name}")
        return {}
    return payload


def duplicate_values(records: list[dict[str, Any]], key: str) -> list[str]:
    values = [str(item.get(key, "")) for item in records]
    return sorted({value for value in values if value and values.count(value) > 1})


STYLE_BIBLE_REQUIRED = {
    "id", "name", "version", "calibration_status", "worldview",
    "one_line_thesis", "emotional_target", "fit", "viewing_path",
    "light_philosophy", "five_zone_tone", "palette",
    "hue_chroma_lightness", "memory_color_protection", "spatial_layering",
    "texture", "forbidden", "strength_targets", "photo_video_delta",
    "fallback", "evidence",
}


def validate_style_bible(value: Any, owner_id: str) -> list[str]:
    """校验 Style Bible 的结构完整性；不把草案数值当成标定结果。"""
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{owner_id} 缺少 Style Bible"]
    missing = sorted(STYLE_BIBLE_REQUIRED - set(value))
    if missing:
        errors.append(f"{owner_id} Style Bible 缺字段：{missing}")
        return errors
    if value.get("id") != owner_id:
        errors.append(f"{owner_id} Style Bible ID 不一致")
    if value.get("calibration_status") not in {
            "pending-calibration", "pending-engine-recalibration", "calibrated"}:
        errors.append(f"{owner_id} Style Bible 标定状态非法")
    for key in ("worldview", "one_line_thesis", "viewing_path",
                "light_philosophy", "spatial_layering", "photo_video_delta", "fallback"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            errors.append(f"{owner_id} Style Bible.{key} 不能为空")
    for key in ("emotional_target", "forbidden"):
        values = value.get(key)
        if not isinstance(values, list) or not values or not all(
                isinstance(item, str) and item.strip() for item in values):
            errors.append(f"{owner_id} Style Bible.{key} 必须是非空字符串数组")
    fit = value.get("fit")
    if not isinstance(fit, dict) or not all(key in fit for key in (
            "light_requirements", "subjects", "counter_examples", "skin_tone_policy")):
        errors.append(f"{owner_id} Style Bible.fit 不完整")
    for key in ("five_zone_tone", "palette", "hue_chroma_lightness",
                "memory_color_protection", "texture", "strength_targets", "evidence"):
        if not isinstance(value.get(key), dict) or not value[key]:
            errors.append(f"{owner_id} Style Bible.{key} 必须是非空对象")
    return errors


def validate(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    loaded: dict[str, list[dict[str, Any]]] = {}
    for filename, (key, count) in EXPECTED.items():
        payload = load_json(root / filename, errors)
        records = payload.get(key, [])
        if not isinstance(records, list):
            errors.append(f"{filename}.{key} 必须是数组")
            records = []
        if len(records) != count:
            errors.append(f"{filename} 记录数应为 {count}，实际 {len(records)}")
        loaded[filename] = records

    xhs_candidates = loaded["xiaohongshu-v3-candidates.json"]
    xhs_payload = load_json(root / "xiaohongshu-v3-candidates.json", errors)
    if xhs_payload.get("dataset_status") != "research_candidate_layer_only":
        errors.append("小红书 v3 数据只能保持研究候选层状态")
    xhs_serialized = json.dumps(xhs_payload, ensure_ascii=False)
    for forbidden_key in ("xsec_token", '"author"', '"uid"'):
        if forbidden_key in xhs_serialized:
            errors.append(f"小红书候选层包含禁止字段：{forbidden_key}")
    if any(item.get("query_attribution_status") != "transition_unverified"
           for item in xhs_candidates):
        errors.append("未逐条核实的小红书查询归因必须标为 transition_unverified")

    graduated = loaded["graduated_candidates.json"]
    graduated_payload = load_json(root / "graduated_candidates.json", errors)
    if graduated_payload.get("status") not in {
            "research-only-not-executable", "mixed-manual-and-research"}:
        errors.append("降级配方归档状态必须明确区分研究禁用与人工可执行分级")
    if graduated_payload.get("candidate_count") != len(graduated):
        errors.append("降级配方归档声明数量与实际记录数不一致")
    if duplicate_values(graduated, "id"):
        errors.append("降级配方归档 ID 重复")
    for item in graduated:
        label = item.get("id", "未知")
        snapshot = item.get("recipe_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("id") != label:
            errors.append(f"降级配方归档快照缺失或 ID 不一致：{label}")
        elif snapshot.get("status") not in {"research", "manual-executable"}:
            errors.append(f"降级配方归档快照状态无效：{label}")
        elif (snapshot.get("status") == "manual-executable"
              and item.get("disposition") != "manual-executable-explicit-only"):
            errors.append(f"人工可执行配方缺少明确点名边界：{label}")
        if not str(item.get("demotion_reason", "")).strip():
            errors.append(f"降级配方归档缺少降级理由：{label}")
        criteria = item.get("promotion_criteria")
        if not isinstance(criteria, list) or not criteria or not all(
                isinstance(value, str) and value.strip() for value in criteria):
            errors.append(f"降级配方归档缺少完整晋级标准：{label}")

    catalog = load_json(root.parent / "references" / "recipes.json", errors)
    executable_recipes = [item for item in catalog.get("recipes", [])
                          if isinstance(item, dict)
                          and item.get("status") in {"active", "manual-executable"}]
    for recipe in executable_recipes:
        errors.extend(validate_style_bible(recipe.get("style_bible"), recipe.get("id", "未知")))
        compiled = style_bible_gates.compile_profile(recipe)
        errors.extend(
            f"{recipe.get('id', '未知')} Style Bible 门编译违反合同：{item}"
            for item in compiled["violations"]
        )

    # 晋级队列合同：28 套 research 必须逐套排队，禁止再次整体放开或整体禁用。
    queue_payload = load_json(root / "promotion_queue.json", errors)
    queue = loaded["promotion_queue.json"]
    research_ids = {item.get("id") for item in catalog.get("recipes", [])
                    if isinstance(item, dict) and item.get("status") == "research"}
    queue_ids = {item.get("recipe_id") for item in queue if isinstance(item, dict)}
    if queue_ids != research_ids:
        errors.append("晋级队列必须恰好覆盖全部 research 配方，不多不少")
    queue_priorities = sorted(item.get("priority") for item in queue
                              if isinstance(item, dict))
    if queue_priorities != list(range(1, len(queue) + 1)):
        errors.append("晋级队列 priority 必须是 1..N 的唯一排序")
    queue_required = {
        "recipe_id", "name", "priority", "topic_value", "capability_readiness",
        "primary_risks", "min_real_photos", "min_real_videos",
        "memory_color_gates", "human_signoff",
        "promotion_to_manual", "promotion_to_active",
    }
    for item in queue:
        label = item.get("recipe_id", "未知") if isinstance(item, dict) else "未知"
        if not isinstance(item, dict) or not queue_required <= set(item):
            errors.append(f"晋级队列字段不完整：{label}")
            continue
        for key in ("primary_risks", "memory_color_gates",
                    "promotion_to_manual", "promotion_to_active"):
            values = item.get(key)
            if (not isinstance(values, list) or not values
                    or not all(str(value).strip() for value in values)):
                errors.append(f"晋级队列 {key} 必须是非空列表：{label}")
    if "逐套" not in str(queue_payload.get("boundary", "")):
        errors.append("晋级队列缺少逐套推进边界声明")
    drafts = catalog.get("style_bible_drafts")
    if not isinstance(drafts, list):
        errors.append("目录缺少 style_bible_drafts 数组")
    else:
        for draft in drafts:
            label = draft.get("id", "未知") if isinstance(draft, dict) else "未知"
            if not isinstance(draft, dict) or draft.get("executable") is not False:
                errors.append(f"Style Bible 草案不得可执行：{label}")
                continue
            if draft.get("calibration_status") != "pending-calibration":
                errors.append(f"Style Bible 草案必须标为 pending-calibration：{label}")
            errors.extend(validate_style_bible(draft.get("style_bible"), label))

    candidates = loaded["research_candidates.json"]
    candidate_ids = {item.get("id") for item in candidates}
    if len(candidate_ids) != len(candidates) or None in candidate_ids:
        errors.append("研究候选 ID 缺失或重复")
    for item in candidates:
        if item.get("executable") is not False or item.get("status") != "unverified":
            errors.append(f"研究候选必须保持不可执行且未验证：{item.get('id')}")
        if set(item.get("media_types", [])) != {"photo", "video"}:
            errors.append(f"研究候选必须明确覆盖照片与视频指导：{item.get('id')}")

    rules = loaded["recommendation_rules.json"]
    if duplicate_values(rules, "rule_id"):
        errors.append("推荐规则 ID 重复")
    if {item.get("candidate_id") for item in rules} != candidate_ids:
        errors.append("推荐规则与 52 个研究候选未闭合")

    sources = loaded["sources.json"]
    source_ids = {item.get("id") for item in sources}
    referenced_sources = {
        source_id
        for item in candidates
        for source_id in item.get("evidence", {}).get("source_ids", [])
    }
    missing_sources = sorted(referenced_sources - source_ids)
    if missing_sources:
        errors.append(f"候选引用了不存在的来源：{missing_sources}")

    platform_seeds = loaded["platform_seeds.json"]
    platform_keys = [(item.get("candidate_id"), item.get("platform")) for item in platform_seeds]
    if len(set(platform_keys)) != len(platform_keys):
        errors.append("平台种子复合键重复")
    for item in platform_seeds:
        if item.get("candidate_id") not in candidate_ids:
            errors.append(f"平台种子引用不存在候选：{item.get('candidate_id')}")
        if item.get("usage_mode") != "manual_only" or item.get("executable") is not False:
            errors.append(f"平台种子越过人工指导边界：{item.get('candidate_id')}")

    guidance = loaded["guidance_seeds.json"]
    guidance_keys = [(item.get("candidate_id"), item.get("platform")) for item in guidance]
    if len(set(guidance_keys)) != len(guidance_keys):
        errors.append("指导种子复合键重复")
    for seed in guidance:
        label = seed.get("id", "未知")
        if seed.get("manual_only") is not True or seed.get("renderable") is not False:
            errors.append(f"指导种子必须 manual_only 且不可渲染：{label}")
        if seed.get("calibration_status") != "heuristic":
            errors.append(f"指导种子必须标记 heuristic：{label}")
        if seed.get("media_type") != "photo":
            errors.append(f"照片指导种子媒体类型无效：{label}")
        levels = seed.get("levels")
        if not isinstance(levels, dict) or set(levels) != {"conservative", "standard", "bold"}:
            errors.append(f"指导种子三档不完整：{label}")
            continue
        for level, values in levels.items():
            if not isinstance(values, dict) or not values:
                errors.append(f"指导种子参数为空：{label}/{level}")
                continue
            for name, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"参数必须是数值 numeric：{label}/{level}/{name}")
                elif not -100 <= value <= 100:
                    errors.append(f"参数超出安全范围：{label}/{level}/{name}={value}")

    video_guidance = loaded["video_guidance_seeds.json"]
    video_keys = [(item.get("candidate_id"), item.get("platform")) for item in video_guidance]
    if len(set(video_keys)) != len(video_keys):
        errors.append("视频指导种子复合键重复")
    platforms = {"capcut_video", "premiere_video", "davinci_resolve_video"}
    for platform in platforms:
        count = sum(item.get("platform") == platform for item in video_guidance)
        if count != 52:
            errors.append(f"视频平台 {platform} 应有52条，实际{count}条")
    for seed in video_guidance:
        label = seed.get("id", "未知")
        if seed.get("candidate_id") not in candidate_ids:
            errors.append(f"视频指导种子引用不存在候选：{label}")
        if seed.get("media_type") != "video":
            errors.append(f"视频指导种子媒体类型无效：{label}")
        if seed.get("manual_only") is not True or seed.get("renderable") is not False:
            errors.append(f"视频指导种子必须 manual_only 且不可渲染：{label}")
        if seed.get("calibration_status") != "heuristic":
            errors.append(f"视频指导种子必须标记 heuristic：{label}")
        if not isinstance(seed.get("temporal_policy"), list) or not seed.get("temporal_policy"):
            errors.append(f"视频指导种子缺少时序 temporal 策略：{label}")
        levels = seed.get("levels")
        if not isinstance(levels, dict) or set(levels) != {"conservative", "standard", "bold"}:
            errors.append(f"视频指导种子三档不完整：{label}")
            continue
        for level, values in levels.items():
            if not isinstance(values, dict) or not values:
                errors.append(f"视频指导种子参数为空：{label}/{level}")
                continue
            for name, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"视频参数必须是数值 numeric：{label}/{level}/{name}")
                elif not -100 <= value <= 100:
                    errors.append(f"视频参数超出安全范围：{label}/{level}/{name}={value}")

    scenarios = loaded["evaluation_scenarios.json"]
    if duplicate_values(scenarios, "id"):
        errors.append("评测场景 ID 重复")
    for scenario in scenarios:
        unknown = set(scenario.get("expected_candidate_ids", [])) - candidate_ids
        if unknown:
            errors.append(f"评测场景引用不存在候选：{scenario.get('id')} / {sorted(unknown)}")

    media_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".mkv"}
    bundled_media = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and path.suffix.lower() in media_extensions]
    if bundled_media:
        errors.append(f"研究层不得包含第三方媒体：{bundled_media}")
    forbidden = re.compile(r"原始调色公式库|raw60|\"原始(?:调色)?公式\"\s*:\s*60", re.I)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".txt", ".csv", ".py"}:
            if forbidden.search(path.read_text(encoding="utf-8")):
                errors.append(f"研究层包含禁止分发的原始公式声明：{path.name}")

    manifest = load_json(root / "manifest.json", errors)
    manifest_files = manifest.get("files", []) if isinstance(manifest, dict) else []
    if not isinstance(manifest_files, list):
        errors.append("manifest.files必须是数组")
    else:
        expected_manifest = set(EXPECTED)
        actual_manifest = {item.get("path") for item in manifest_files if isinstance(item, dict)}
        if actual_manifest != expected_manifest:
            errors.append("manifest文件集合与严格数据合同不一致")
        for item in manifest_files:
            if not isinstance(item, dict) or item.get("path") not in EXPECTED:
                continue
            path = root / item["path"]
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if item.get("sha256") != digest:
                    errors.append(f"manifest SHA-256不匹配：{item['path']}")
                expected_count = EXPECTED[item["path"]][1]
                if item.get("records") != expected_count:
                    errors.append(f"manifest记录数不匹配：{item['path']}")

    return {
        "validator": "BLCaptain research contract 1.5.0",
        "boundary": "照片／视频结构、类型、引用与时序合同校验；不等同于视觉、审美、跨 App 标定或法律意见",
        "counts": {name: len(records) for name, records in loaded.items()},
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 BLCaptain 研究层数据合同")
    parser.add_argument("--research-root", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.research_root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
