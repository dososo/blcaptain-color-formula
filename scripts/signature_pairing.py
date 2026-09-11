#!/usr/bin/env python3
"""生成 Signature 随机盲配对包并按独立答案键计分。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path


def prepare(manifest_path: Path, output_dir: Path, seed: int,
            answer_key_path: Path | None = None) -> dict:
    if output_dir.exists():
        raise FileExistsError(f"输出目录已存在，不覆盖：{output_dir}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if len(items) != 11 or len({item["recipe_id"] for item in items}) != 11:
        raise ValueError("盲配对必须恰好包含 11 套不同 Signature 的真实渲染")
    rng = random.Random(seed)
    shuffled = list(items)
    rng.shuffle(shuffled)
    output_dir.mkdir(parents=True)
    assets_dir = output_dir / "assets"
    assets_dir.mkdir()
    answer_key_path = answer_key_path or output_dir.parent / f"{output_dir.name}-answer-key.json"
    if answer_key_path.exists():
        raise FileExistsError(f"答案键已存在，不覆盖：{answer_key_path}")
    public_items = []
    answer_key = {}
    for index, item in enumerate(shuffled, start=1):
        blind_id = f"S{index:02d}"
        source = Path(item["output_path"])
        suffix = source.suffix.lower() or ".png"
        public_path = assets_dir / f"{blind_id}{suffix}"
        shutil.copy2(source, public_path)
        public_items.append({
            "blind_id": blind_id, "image_path": f"assets/{public_path.name}",
            "source_sha256": item["source_sha256"],
        })
        answer_key[blind_id] = item["recipe_id"]
    (output_dir / "blind-items.json").write_text(
        json.dumps({"seed": seed, "items": public_items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    answer_key_path.parent.mkdir(parents=True, exist_ok=True)
    answer_key_path.write_text(
        json.dumps({"seed": seed, "answers": answer_key}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    cards = "\n".join(
        f'<figure><img src="{html.escape(item["image_path"])}">'
        f'<figcaption>{item["blind_id"]}：<input aria-label="{item["blind_id"]} 配方 ID"></figcaption></figure>'
        for item in public_items
    )
    page = f"""<!doctype html><meta charset="utf-8"><title>Signature 盲配对</title>
<style>body{{font-family:system-ui;margin:24px;background:#111;color:#eee}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}}img{{width:100%;height:auto}}input{{width:16em}}</style>
<h1>Signature 盲配对</h1><p>只依据画面与 visual_signature 描述填写配方 ID；页面不含答案。</p><main>{cards}</main>"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")
    return {"count": 11, "seed": seed, "output_dir": str(output_dir),
            "answer_key_path": str(answer_key_path),
            "answer_key_sha256": hashlib.sha256(answer_key_path.read_bytes()).hexdigest()}


def score(answer_key_path: Path, response_path: Path) -> dict:
    expected = json.loads(answer_key_path.read_text(encoding="utf-8"))["answers"]
    actual = json.loads(response_path.read_text(encoding="utf-8"))["answers"]
    rows = [{"blind_id": key, "expected": value, "actual": actual.get(key),
             "correct": actual.get(key) == value} for key, value in expected.items()]
    correct = sum(row["correct"] for row in rows)
    return {
        "scored_at": datetime.now(timezone.utc).isoformat(), "correct": correct, "total": len(rows),
        "accuracy": round(correct / max(1, len(rows)), 4), "target": 0.75,
        "target_met": correct / max(1, len(rows)) >= 0.75, "rows": rows,
        "boundary": "这是描述反向配对辨识度，不等于作品审美通过。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_cmd = commands.add_parser("prepare")
    prepare_cmd.add_argument("--manifest", required=True)
    prepare_cmd.add_argument("--output-dir", required=True)
    prepare_cmd.add_argument("--seed", type=int, default=480)
    prepare_cmd.add_argument("--answer-key")
    score_cmd = commands.add_parser("score")
    score_cmd.add_argument("--answer-key", required=True)
    score_cmd.add_argument("--responses", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(
            Path(args.manifest), Path(args.output_dir), args.seed,
            Path(args.answer_key) if args.answer_key else None,
        )
    else:
        result = score(Path(args.answer_key), Path(args.responses))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
