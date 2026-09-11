#!/usr/bin/env python3
"""从研究目录直接调用v1.3严格数据合同校验器。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VALIDATOR = ROOT.parent / "validators" / "validate_data.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("blcaptain_validate_data", VALIDATOR)
    if spec is None or spec.loader is None:
        print(json.dumps({"errors": ["无法加载严格校验器"], "warnings": []}, ensure_ascii=False))
        return 1
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.validate(ROOT)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
