#!/usr/bin/env python3
"""从未标定的平台标准起点，确定性派生三档手工指导数据。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "platform_seeds.json"
OUTPUT = ROOT / "guidance_seeds.json"


def clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def scaled(values: dict[str, float], factor: float, decimals: int) -> dict[str, float]:
    return {
        name: round(clamp(float(value) * factor), decimals)
        for name, value in values.items()
    }


def main() -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    seeds = []
    for item in source["records"]:
        policy = item["tier_policy"]
        standard = item["standard_parameters"]
        decimals = int(policy.get("rounding_decimals", 1))
        levels = {
            "conservative": scaled(standard, float(policy["conservative"]), decimals),
            "standard": scaled(standard, float(policy["standard"]), decimals),
            "bold": scaled(standard, float(policy["bold"]), decimals),
        }
        seeds.append({
            "id": f"{item['candidate_id']}:{item['platform']}",
            "candidate_id": item["candidate_id"],
            "media_type": "photo",
            "platform": item["platform"],
            "platform_version": item["platform_version"],
            "manual_only": True,
            "calibration_status": "heuristic",
            "renderable": False,
            "mapping_verified": False,
            "levels": levels,
            "warnings": item["warnings"],
        })
    payload = {
        "schema_version": "1.3.0",
        "derivation": "standard × 0.65 / 1.0 / 1.25，按平台范围限幅；不是跨 App 等价换算",
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "seeds": seeds,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "count": len(seeds)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
