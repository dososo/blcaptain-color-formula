#!/usr/bin/env python3
"""按素材内容哈希保存显式审美反馈；不建立用户画像。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
VERDICTS = {"accepted", "rejected"}


def default_path() -> Path:
    return Path.home() / ".blcaptain" / "feedback-ledger.json"


def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version 不受支持")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries 不是数组")
    required = {"source_sha256", "recipe_id", "strength", "verdict", "reason", "recorded_at"}
    for entry in entries:
        if not isinstance(entry, dict) or not required <= set(entry):
            raise ValueError("反馈记录字段不完整")
        if entry["verdict"] not in VERDICTS:
            raise ValueError("反馈结论无效")
        if not isinstance(entry["source_sha256"], str) or len(entry["source_sha256"]) != 64:
            raise ValueError("素材哈希无效")
    return payload


def load(path: Path | None = None) -> dict:
    ledger = Path(path) if path is not None else default_path()
    if not ledger.exists():
        return {**_empty(), "path": str(ledger), "warning": None}
    try:
        payload = _validate(json.loads(ledger.read_text(encoding="utf-8")))
        return {**payload, "path": str(ledger), "warning": None}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return {
            **_empty(),
            "path": str(ledger),
            "warning": f"反馈账本损坏，本次已降级为无记忆：{type(error).__name__}: {error}",
        }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def record(path: Path | None, source: Path, recipe_id: str, strength: float,
           verdict: str, reason: str) -> dict:
    ledger = Path(path) if path is not None else default_path()
    state = load(ledger)
    if state["warning"]:
        raise ValueError(state["warning"] + "；为避免覆盖可恢复数据，拒绝写入，请先清空账本")
    if verdict not in VERDICTS:
        raise ValueError("verdict 只能是 accepted 或 rejected")
    if not reason.strip():
        raise ValueError("必须记录具体反馈原因")
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"素材不存在：{source}")
    entry = {
        "source_sha256": _sha256(source),
        "recipe_id": recipe_id,
        "strength": round(float(strength), 6),
        "verdict": verdict,
        "reason": reason.strip(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    entries = [
        item for item in state["entries"]
        if not (
            item["source_sha256"] == entry["source_sha256"]
            and item["recipe_id"] == recipe_id
            and abs(float(item["strength"]) - entry["strength"]) < 1e-9
        )
    ]
    entries.append(entry)
    _write(ledger, {"schema_version": SCHEMA_VERSION, "entries": entries})
    status = "human_accepted" if verdict == "accepted" else "human_rejected"
    return {
        "recorded": entry,
        "ledger": str(ledger),
        "aesthetic": {"status": status, "source": "explicit-user-feedback", "reason": entry["reason"]},
        "boundary": "反馈只绑定素材内容哈希、配方和强度；不建立用户画像，不改动配方库。",
    }


def clear(path: Path | None = None) -> dict:
    ledger = Path(path) if path is not None else default_path()
    state = load(ledger)
    count = len(state["entries"])
    _write(ledger, _empty())
    return {"ledger": str(ledger), "cleared_entries": count, "status": "cleared"}
