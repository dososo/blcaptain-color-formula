#!/usr/bin/env python3
"""把双版本真实渲染结果打乱成不泄露版本名的本地 A/B 盲看页。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--answer-key", required=True)
    parser.add_argument("--seed", type=int, default=4808)
    args = parser.parse_args()
    payload = json.loads(Path(args.results).read_text(encoding="utf-8"))
    output = Path(args.output_dir).expanduser().resolve()
    answer_path = Path(args.answer_key).expanduser().resolve()
    if output.exists() or answer_path.exists():
        raise SystemExit("盲看包或答案键已存在，不覆盖")
    by_key: dict[str, dict[str, dict]] = {}
    for item in payload["items"]:
        by_key.setdefault(item["key"], {})[item["version"]] = item
    if len(payload["items"]) != 86 or len(by_key) != 43:
        raise SystemExit("双版本执行记录未完整，不能生成盲看包")
    eligible = {
        key: pair for key, pair in by_key.items()
        if set(pair) == {"v4.7", "v4.8-current"}
        and all(item["outcome"] == "rendered" for item in pair.values())
    }
    excluded = {
        key: {version: {"outcome": item["outcome"], "stage": item["stage"],
                        "reason": item.get("render_stderr") or item.get("plan_stderr")}
              for version, item in pair.items()}
        for key, pair in by_key.items() if key not in eligible
    }
    assets = output / "assets"
    assets.mkdir(parents=True)
    rng = random.Random(args.seed)
    answer = {"seed": args.seed, "items": {}, "excluded": excluded}
    cards = []
    for index, key in enumerate(sorted(eligible), start=1):
        pair = eligible[key]
        versions = ["v4.7", "v4.8-current"]
        rng.shuffle(versions)
        aliases = {}
        for side, version in zip(("A", "B"), versions):
            source = Path(pair[version]["output_path"])
            suffix = source.suffix.lower()
            alias = assets / f"{index:02d}_{side}{suffix}"
            shutil.copy2(source, alias)
            aliases[side] = alias.name
        answer["items"][key] = {"A": versions[0], "B": versions[1]}
        media = pair["v4.8-current"]["media_type"]
        if media == "video":
            displays = "".join(
                f'<section><h3>{side}</h3><video controls preload="metadata" src="assets/{html.escape(name)}"></video></section>'
                for side, name in aliases.items())
        else:
            displays = "".join(
                f'<section><h3>{side}</h3><img src="assets/{html.escape(name)}"></section>'
                for side, name in aliases.items())
        cards.append(f'''<article data-key="{key}"><h2>{index:02d} · {key}</h2><div class="pair">{displays}</div>
<fieldset><legend>更偏好</legend><label><input type="radio" name="{key}" value="A">A</label>
<label><input type="radio" name="{key}" value="B">B</label><label><input type="radio" name="{key}" value="same">相同／都不选</label></fieldset>
<textarea aria-label="{key} 画面依据" placeholder="请写画面依据：主体、光影、色彩、情绪、伪影……"></textarea></article>''')
    script = """function save(){const out={reviewer:'队长',items:{}};document.querySelectorAll('article').forEach(x=>{const k=x.dataset.key;const c=x.querySelector('input:checked');out.items[k]={preference:c?c.value:null,evidence:x.querySelector('textarea').value};});const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json'}));a.download='captain-blind-review.json';a.click();}"""
    page = f'''<!doctype html><meta charset="utf-8"><title>BLCaptain v4.8 队长盲看包</title>
<style>body{{font-family:system-ui;margin:24px;background:#111;color:#eee}}article{{margin:36px 0;padding:18px;background:#1b1b1b}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}img,video{{width:100%;max-height:70vh;object-fit:contain;background:#000}}textarea{{width:100%;min-height:72px;margin-top:12px}}label{{margin-right:20px}}button{{position:sticky;top:10px;padding:12px 18px}}</style>
<h1>BLCaptain v4.8 A/B 盲看</h1><p>页面不显示版本答案。逐项选择并写画面依据；这是队长人工审美门。</p>
<p>共 43 组素材：{len(eligible)} 组双方均真实成片并进入盲看，{len(excluded)} 组因至少一侧安全拒绝而排除；排除不计输赢。</p>
<button onclick="save()">下载我的盲看结果</button>{''.join(cards)}<script>{script}</script>'''
    (output / "index.html").write_text(page, encoding="utf-8")
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_text = json.dumps(answer, ensure_ascii=False, indent=2) + "\n"
    answer_path.write_text(answer_text, encoding="utf-8")
    print(json.dumps({"items": len(eligible), "excluded": len(excluded),
                      "output": str(output / "index.html"),
                      "answer_key_sha256": hashlib.sha256(answer_text.encode()).hexdigest()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
