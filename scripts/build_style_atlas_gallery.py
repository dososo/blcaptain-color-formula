#!/usr/bin/env python3
"""从研究回归 results.json 生成本地诊断页；不复制或修改媒体。"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Optional
from urllib.parse import quote


def published_attempt(job: dict) -> Optional[dict]:
    for attempt in job["attempts"]:
        if attempt.get("status") == "rendered" and attempt.get("output_published") is True:
            return attempt
    return None


def rejection_reason(job: dict) -> str:
    for attempt in reversed(job["attempts"]):
        log_path = Path(attempt["plan_path"]).parent / "render.log"
        if not log_path.is_file():
            continue
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("为什么："):
                return line.removeprefix("为什么：").strip()
    return "未生成正式媒体；请查看该项日志。"


def media_uri(index_dir: Path, media_path: str) -> str:
    relative = Path(media_path).resolve().relative_to(index_dir.resolve())
    return quote(relative.as_posix())


def card(index_dir: Path, job: dict) -> str:
    attempt = published_attempt(job)
    title = html.escape(f"{job['style_name']} · {job['style_id']}")
    status = html.escape(job["catalog_status"])
    if attempt:
        strength = attempt["strength"]
        source = media_uri(index_dir, attempt["comparison_path"])
        if job["media_type"] == "photo":
            media = f'<img loading="lazy" src="{source}" alt="{title} 对比图">'
        else:
            media = f'<video controls preload="metadata" src="{source}"></video>'
        result = f'<span class="ok">研究诊断预览 {strength}% · 非生产发布</span>'
    else:
        media = f'<div class="rejected">安全门拒绝<br><small>{html.escape(rejection_reason(job))}</small></div>'
        result = '<span class="blocked">80% 与 55% 均未生成研究预览</span>'
    return f'''<article>
      <h3>{title}</h3>
      <p>{result}<code>{status}</code></p>
      {media}
    </article>'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results_path = args.results.expanduser().resolve()
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    output = args.output.expanduser().resolve() if args.output else results_path.parent / "index.html"
    photos = [job for job in payload["jobs"] if job["media_type"] == "photo"]
    videos = [job for job in payload["jobs"] if job["media_type"] == "video"]
    sections = []
    for heading, jobs in (("照片配方（31）", photos), ("视频配方（30）", videos)):
        sections.append(f'<h2>{heading}</h2><div class="grid">' + "".join(card(output.parent, job) for job in jobs) + "</div>")
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BLCaptain 全配方研究诊断图谱</title>
<style>body{{margin:0;background:#0d0f12;color:#f5f6f7;font:16px system-ui;padding:32px}}h1{{margin-bottom:8px}}h2{{margin-top:48px}}.note{{color:#aeb5bf}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:24px}}article{{background:#171a1f;border:1px solid #2a3038;border-radius:14px;padding:16px}}h3{{margin:0 0 8px}}p{{display:flex;gap:12px;align-items:center}}code{{color:#9aa3ae}}img,video{{width:100%;background:#070809;border-radius:8px}}.ok{{color:#62d6b2}}.blocked{{color:#ffaf66}}.rejected{{min-height:220px;display:grid;place-content:center;text-align:center;background:#241a13;border:1px solid #72451f;border-radius:8px;color:#ffb36e;padding:24px}}small{{display:block;max-width:620px;color:#ded1c5;margin-top:12px}}</style></head><body>
<h1>BLCaptain 全配方研究诊断图谱</h1><p class="note">同一真实来源分别应用研究配方，用于暴露风格指纹、错场景和执行缺陷。80% 被安全门拒绝时才单列 55%；未生成项不以模拟图代替。</p>
<p class="note"><strong>本页全部媒体都是研究回归预览，不是生产发布、配方晋级或人工审美通过。</strong>生产配方状态未改变；所有审美状态仍为 pending，人工接受／拒绝须以独立反馈证据为准。</p>{''.join(sections)}</body></html>'''
    output.write_text(document, encoding="utf-8")
    print(json.dumps({"output": str(output), "photos": len(photos), "videos": len(videos)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
