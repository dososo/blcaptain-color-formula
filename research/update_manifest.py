#!/usr/bin/env python3
"""更新研究数据数量和SHA-256清单。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FILES = [
    ("research_candidates.json", "records"),
    ("recommendation_rules.json", "records"),
    ("sources.json", "records"),
    ("platform_seeds.json", "records"),
    ("guidance_seeds.json", "seeds"),
    ("video_guidance_seeds.json", "seeds"),
    ("evaluation_scenarios.json", "records"),
    ("xiaohongshu-v3-candidates.json", "notes"),
]


def main() -> int:
    files = []
    for name, key in FILES:
        path = ROOT / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        files.append({
            "path": name,
            "records": len(payload[key]),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    manifest = {
        "schema_version": "4.8.1",
        "dataset_status": "research_and_manual_guidance_only",
        "generated_from": "audited v1.1-v4.0 synthesis plus v4.8 read-only Xiaohongshu candidate observations",
        "excluded_content": [
            "60 third-party raw formulas",
            "third-party media",
            "commercial presets or LUTs",
            "unverified executable promotion of the 52 research styles",
        ],
        "files": files,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(files), "records": sum(item["records"] for item in files)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
