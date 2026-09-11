#!/usr/bin/env python3
"""由历史双版本渲染证据生成单图绝对质量人工评审包。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_tag(alias: str, label: str) -> str:
    safe_alias = html.escape(alias, quote=True)
    safe_label = html.escape(label)
    if Path(alias).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}:
        return f'<video controls preload="metadata" src="assets/{safe_alias}"></video>'
    return f'<img loading="lazy" src="assets/{safe_alias}" alt="{safe_label}">'


def render_page(items: list[dict], excluded_count: int) -> str:
    cards = []
    for index, item in enumerate(items, 1):
        key = html.escape(item["key"])
        emotion = html.escape(item["emotion"])
        media = "视频" if item["media_type"] == "video" else "照片"
        choices = "".join(
            f'<label><input type="radio" name="status-{key}" value="{status}"> {status}</label>'
            for status in ("可直接交付", "需要修改", "不可接受")
        )
        cards.append(f"""
        <article data-key="{key}">
          <h2>{index:02d} · {key} · {media}</h2>
          <p class="emotion"><strong>情绪命题：</strong>{emotion}</p>
          <div class="media-grid">
            <figure>{media_tag(item['source_alias'], '原图')}<figcaption>原图</figcaption></figure>
            <figure>{media_tag(item['result_alias'], '成片')}<figcaption>成片</figcaption></figure>
            <figure>{media_tag(item['comparison_alias'], '并排对比')}<figcaption>并排对比</figcaption></figure>
          </div>
          <fieldset><legend>绝对质量结论</legend>{choices}</fieldset>
          <label class="reason">具体理由<textarea rows="3" placeholder="请保留原话，例如：不够通透、肤色偏黄、节奏跳色。"></textarea></label>
        </article>""")
    payload = json.dumps([item["key"] for item in items], ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BLCaptain v4.8.1 绝对质量评审</title>
<style>
body{{margin:0;background:#111;color:#eee;font:16px/1.6 system-ui;padding:24px}} main{{max-width:1500px;margin:auto}}
.notice{{padding:16px;border:1px solid #d6a84b;background:#2d2618}} article{{margin:32px 0;padding:20px;background:#1c1c1c}}
.media-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}} figure{{margin:0}} img,video{{width:100%;max-height:560px;object-fit:contain;background:#080808}} figcaption{{text-align:center}}
fieldset{{border:0;padding:12px 0}} fieldset label{{margin-right:24px}} .reason,textarea{{display:block;width:100%;box-sizing:border-box}} textarea{{margin-top:6px}}
button{{padding:12px 18px;font-size:16px}} @media(max-width:900px){{.media-grid{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>BLCaptain v4.8.1 绝对质量评审</h1>
<p class="notice"><strong>评审边界：</strong>本轮 v4.7 与 v4.8 渲染输出逐字节相同，因此不存在版本差异需要判别。本页只评价成片本身是否可交付，不做 A/B 偏好选择。另有 {excluded_count} 个未形成双版本有效成片的样本不进入本页。</p>
{''.join(cards)}
<button id="download">下载评审结果 JSON</button>
</main><script>
const keys={payload};
document.querySelector('#download').onclick=()=>{{
 const items=keys.map(key=>{{const card=document.querySelector(`[data-key="${{key}}"]`);return {{key,status:card.querySelector('input:checked')?.value||'pending',reason:card.querySelector('textarea').value}}}});
 const blob=new Blob([JSON.stringify({{schema_version:'1.0',review_type:'absolute-quality',reviewer:'captain',items}},null,2)],{{type:'application/json'}});
 const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='captain-absolute-quality-review.json';link.click();
}};
</script></body></html>"""


def build(args: argparse.Namespace) -> dict:
    output_dir = args.output_dir.resolve()
    response_template = args.response_template.resolve()
    if output_dir.exists() or response_template.exists():
        raise FileExistsError("拒绝覆盖既有评审包或回执模板，请使用新路径")

    results = json.loads(args.results.read_text(encoding="utf-8"))["items"]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    recipes = {
        item["id"]: item
        for item in json.loads(args.recipes.read_text(encoding="utf-8"))["recipes"]
    }
    sources = {item["key"]: Path(item["path"]) for item in manifest["photos"] + manifest["videos"]}
    grouped: dict[str, dict[str, dict]] = {}
    for item in results:
        grouped.setdefault(item["key"], {})[item["version"]] = item

    eligible = []
    for key, versions in sorted(grouped.items()):
        if set(versions) != {"v4.7", "v4.8-current"}:
            continue
        old, current = versions["v4.7"], versions["v4.8-current"]
        if old.get("outcome") != "rendered" or current.get("outcome") != "rendered":
            continue
        old_output, current_output = Path(old["output_path"]), Path(current["output_path"])
        if sha256(old_output) != sha256(current_output):
            raise ValueError(f"{key} 的双版本成片并非逐字节相同，不能进入本绝对质量包")
        eligible.append(current)

    excluded_count = len(grouped) - len(eligible)
    assets = output_dir / "assets"
    assets.mkdir(parents=True)
    page_items = []
    response_items = []
    for index, item in enumerate(eligible, 1):
        source = sources[item["key"]]
        result = Path(item["output_path"])
        comparison = Path(item["comparison_path"])
        aliases = {
            "source_alias": f"{index:02d}_original{source.suffix.lower()}",
            "result_alias": f"{index:02d}_result{result.suffix.lower()}",
            "comparison_alias": f"{index:02d}_comparison{comparison.suffix.lower()}",
        }
        for origin, alias in ((source, aliases["source_alias"]), (result, aliases["result_alias"]), (comparison, aliases["comparison_alias"])):
            if not origin.is_file():
                raise FileNotFoundError(origin)
            shutil.copy2(origin, assets / alias)
        emotion = recipes[item["style"]]["art_direction"]["emotion"]
        page_items.append({**aliases, "key": item["key"], "media_type": item["media_type"], "emotion": emotion})
        response_items.append({"key": item["key"], "status": "pending", "reason": ""})

    (output_dir / "index.html").write_text(render_page(page_items, excluded_count), encoding="utf-8")
    response_template.parent.mkdir(parents=True, exist_ok=True)
    response_template.write_text(json.dumps({
        "schema_version": "1.0",
        "review_type": "absolute-quality",
        "reviewer": "captain",
        "status": "pending",
        "items": response_items,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"reviewed_count": len(page_items), "excluded_count": excluded_count, "output_dir": str(output_dir), "response_template": str(response_template)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--recipes", type=Path, default=Path(__file__).resolve().parents[1] / "references" / "recipes.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--response-template", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
