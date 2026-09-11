#!/usr/bin/env python3
"""校验风格图谱素材许可账本；不下载、不复制、也不修改任何媒体。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "research" / "style_atlas_sources.json"
STATUSES = {"approved-public", "approved-internal", "candidate-license-review", "blocked-license"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_manifest(payload: dict, mode: str = "local") -> list[str]:
    errors: list[str] = []
    if mode not in {"local", "public-export"}:
        return ["素材账本模式无效"]
    public = mode == "public-export"
    if public and payload.get("distribution_mode") != "public-export":
        errors.append("公开候选必须明确标记distribution_mode=public-export")
    if not public and payload.get("distribution_mode") == "public-export":
        errors.append("外置素材公开候选不能当本地素材账本；请显式使用public-export模式")
    if payload.get("schema_version") != "1.0.0":
        errors.append("schema_version 必须为 1.0.0")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        return errors + ["sources 必须是非空数组"]

    seen: set[str] = set()
    for index, item in enumerate(sources):
        source_id = item.get("id") or f"sources[{index}]"
        if source_id in seen:
            errors.append(f"{source_id}: id 重复")
        seen.add(source_id)
        if item.get("status") not in STATUSES:
            errors.append(f"{source_id}: status 无效")
        for field in ("media_type", "input_class", "landing_page", "provider"):
            if not item.get(field):
                errors.append(f"{source_id}: 缺少 {field}")
        license_block = item.get("license") or {}
        if not license_block.get("name"):
            errors.append(f"{source_id}: 缺少 license.name")
        if not isinstance(item.get("rights_risks"), list):
            errors.append(f"{source_id}: rights_risks 必须是数组")
        if item.get("stored_in_git") is not False:
            errors.append(f"{source_id}: stored_in_git 必须为 false")
        if public and (item.get("local_path") is not None
                       or item.get("asset_binding") != item.get("id")
                       or not item.get("asset_binding")
                       or item.get("availability") != "external-not-bundled"):
            errors.append(f"{source_id}: 公开候选必须无本机路径、绑定原id并声明素材外置未随包分发")

        status = item.get("status")
        if status == "approved-public":
            if not SHA256.fullmatch(str(item.get("source_sha256") or "")):
                errors.append(f"{source_id}: approved-public 缺少有效 source_sha256")
            if not license_block.get("url"):
                errors.append(f"{source_id}: approved-public 缺少 license.url")
            if not item.get("attribution_text"):
                errors.append(f"{source_id}: approved-public 缺少 attribution_text")
            if not item.get("modification_notice"):
                errors.append(f"{source_id}: approved-public 缺少 modification_notice")
            if item.get("release_allowed") is not True:
                errors.append(f"{source_id}: approved-public 的 release_allowed 必须为 true")
        elif item.get("release_allowed") is not False:
            errors.append(f"{source_id}: 非 approved-public 的 release_allowed 必须为 false")

        if status == "approved-internal":
            if not SHA256.fullmatch(str(item.get("source_sha256") or "")):
                errors.append(f"{source_id}: approved-internal 缺少有效 source_sha256")
            if not public and not item.get("local_path"):
                errors.append(f"{source_id}: approved-internal 缺少 local_path")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--mode", choices=("local", "public-export"), default="local")
    args = parser.parse_args()
    path = Path(args.manifest).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_manifest(payload, args.mode)
    print(json.dumps({
        "manifest": str(path),
        "source_count": len(payload.get("sources") or []),
        "errors": errors,
        "status": "passed" if not errors else "failed",
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
