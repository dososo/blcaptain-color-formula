#!/usr/bin/env python3
"""审美盲测工具：生成随机化的对比页面，由真人给出选择。

存在的理由：项目里所有自动指标都只能证明「变化发生了、没有技术破坏、彼此分得开」，
没有一个能证明「更动人」。lessons.md 反复记录过用自动指标冒充审美的失败。
这个工具不参与判断，只负责把判断交还给人，并把人的选择记录成可复核的证据。

设计上刻意做了几件事来防止自我欺骗：
- 选项顺序随机，标签隐藏，鼠标悬停也不显示来源；
- 同一对会以相反顺序重复出现，用来测量评审者自身的一致性；
- 记录「说不出差别」这个选项——强迫二选一会制造虚假偏好；
- 同时收集情绪词与拒绝原因，因为「选了哪张」信息量远小于「为什么」。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import subprocess
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

BLIND_REVIEW_SCHEMA_VERSION = "4.0.0"
THUMB_WIDTH = 900
EMOTION_WORDS = ["安静", "孤独", "亲密", "辽阔", "锋利", "温暖", "疏离",
                 "危险", "怀旧", "清醒", "慵懒", "庄严", "脆弱", "生命力"]


class BlindReviewError(Exception):
    pass


def _thumb_data_uri(path: Path, width: int = THUMB_WIDTH) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise BlindReviewError("缺少 ffmpeg")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path),
         "-vf", f"scale={width}:-2:flags=lanczos", "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
        capture_output=True)
    if result.returncode or not result.stdout:
        raise BlindReviewError(f"无法生成缩略图：{path}")
    return "data:image/jpeg;base64," + base64.b64encode(result.stdout).decode("ascii")


def build_trials(candidates: list[dict], seed: int, repeat_reversed: bool = True) -> list[dict]:
    """构造成对比较。

    repeat_reversed 会把每一对以相反顺序再出一次。这不是凑数——
    它是唯一能测出「评审者是否只是随手点左边」的办法。
    """
    rng = random.Random(seed)
    pairs = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            pairs.append((candidates[i], candidates[j]))
    rng.shuffle(pairs)
    trials = []
    for index, (first, second) in enumerate(pairs):
        flipped = rng.random() < 0.5
        left, right = (second, first) if flipped else (first, second)
        trials.append({"trial_id": f"T{index:03d}", "left": left, "right": right,
                       "consistency_probe": False})
    if repeat_reversed and pairs:
        probes = rng.sample(pairs, max(1, len(pairs) // 4))
        for index, (first, second) in enumerate(probes):
            trials.append({"trial_id": f"P{index:03d}", "left": second, "right": first,
                           "consistency_probe": True})
    rng.shuffle(trials)
    return trials


def build_page(trials: list[dict], title: str, meta: dict) -> str:
    payload = []
    for trial in trials:
        payload.append({
            "trial_id": trial["trial_id"],
            "left_id": trial["left"]["id"],
            "right_id": trial["right"]["id"],
            "left_src": trial["left"]["data_uri"],
            "right_src": trial["right"]["data_uri"],
            "probe": trial["consistency_probe"],
        })
    data = json.dumps(payload, ensure_ascii=False)
    words = json.dumps(EMOTION_WORDS, ensure_ascii=False)
    info = json.dumps(meta, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; --bg:#0d0d0f; --fg:#e8e6e1; --dim:#8a8781; --line:#2a2a2e; --accent:#c9a227; }}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang SC",sans-serif}}
header{{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}}
h1{{font-size:17px;margin:0;font-weight:600;letter-spacing:.02em}}
.progress{{color:var(--dim);font-size:13px;font-variant-numeric:tabular-nums}}
main{{max-width:1400px;margin:0 auto;padding:24px}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
figure{{margin:0;cursor:pointer;border:2px solid transparent;border-radius:6px;overflow:hidden;transition:border-color .12s}}
figure:hover{{border-color:var(--dim)}}
figure.sel{{border-color:var(--accent)}}
img{{width:100%;display:block}}
.cap{{padding:8px 10px;font-size:13px;color:var(--dim);text-align:center;user-select:none}}
.row{{margin-top:20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
button{{background:#17171a;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:8px 14px;font:inherit;font-size:14px;cursor:pointer}}
button:hover{{border-color:var(--dim)}}
button.primary{{background:var(--accent);color:#0d0d0f;border-color:var(--accent);font-weight:600}}
button.chip{{padding:5px 11px;font-size:13px;border-radius:14px}}
button.chip.on{{background:var(--accent);color:#0d0d0f;border-color:var(--accent)}}
textarea{{width:100%;min-height:60px;background:#141417;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:9px;font:inherit;font-size:14px}}
.hint{{color:var(--dim);font-size:13px;margin:14px 0 6px}}
.done{{padding:40px;text-align:center}}
pre{{background:#141417;border:1px solid var(--line);border-radius:6px;padding:14px;overflow:auto;text-align:left;font-size:12px;max-height:50vh}}
</style></head><body>
<header><h1>{title}</h1><div class="progress" id="prog"></div></header>
<main>
  <div id="stage">
    <div class="pair">
      <figure id="fl"><img id="il"><div class="cap">A</div></figure>
      <figure id="fr"><img id="ir"><div class="cap">B</div></figure>
    </div>
    <div class="hint">哪一张更打动你？看不出差别就选「说不出差别」——强迫二选一只会制造假偏好。</div>
    <div class="row">
      <button id="pa">选 A</button><button id="pb">选 B</button>
      <button id="pn">说不出差别</button>
    </div>
    <div class="hint">被选中的那张给你什么感觉？（可多选、可不选）</div>
    <div class="row" id="chips"></div>
    <div class="hint">没被选中的那张，问题出在哪？（可留空）</div>
    <textarea id="why" placeholder="例如：肤色发灰 / 暗部堵死 / 天空假 / 整体太平"></textarea>
    <div class="row"><button class="primary" id="next">下一组 →</button></div>
  </div>
  <div class="done" id="done" hidden>
    <h2>完成</h2>
    <p class="hint">把下面的内容整段复制给 Claude Code，或点击下载。</p>
    <div class="row" style="justify-content:center"><button class="primary" id="dl">下载 JSON</button><button id="cp">复制</button></div>
    <pre id="out"></pre>
  </div>
</main>
<script>
const TRIALS={data}, WORDS={words}, META={info};
let i=0, sel=null, chosen=new Set();
const results=[];
const el=id=>document.getElementById(id);
function renderChips(){{
  el('chips').innerHTML='';
  WORDS.forEach(w=>{{const b=document.createElement('button');b.className='chip';b.textContent=w;
    b.onclick=()=>{{chosen.has(w)?chosen.delete(w):chosen.add(w);b.classList.toggle('on');}};
    el('chips').appendChild(b);}});
}}
function show(){{
  if(i>=TRIALS.length){{finish();return;}}
  const t=TRIALS[i];
  el('il').src=t.left_src; el('ir').src=t.right_src;
  el('fl').classList.remove('sel'); el('fr').classList.remove('sel');
  sel=null; chosen=new Set(); renderChips(); el('why').value='';
  el('prog').textContent=`${{i+1}} / ${{TRIALS.length}}`;
}}
function pick(v){{sel=v;
  el('fl').classList.toggle('sel',v==='left');
  el('fr').classList.toggle('sel',v==='right');}}
el('pa').onclick=()=>pick('left');
el('pb').onclick=()=>pick('right');
el('pn').onclick=()=>{{sel='tie';el('fl').classList.remove('sel');el('fr').classList.remove('sel');}};
el('fl').onclick=()=>pick('left'); el('fr').onclick=()=>pick('right');
el('next').onclick=()=>{{
  if(!sel){{alert('先做出选择，或点「说不出差别」');return;}}
  const t=TRIALS[i];
  results.push({{trial_id:t.trial_id, probe:t.probe, left_id:t.left_id, right_id:t.right_id,
    choice:sel, winner: sel==='tie'?null:(sel==='left'?t.left_id:t.right_id),
    emotions:[...chosen], rejection_reason:el('why').value.trim()}});
  i++; show();
}};
function finish(){{
  el('stage').hidden=true; el('done').hidden=false;
  const out={{schema_version:'{BLIND_REVIEW_SCHEMA_VERSION}', meta:META,
    completed_at:new Date().toISOString(), trials:results}};
  const text=JSON.stringify(out,null,2);
  el('out').textContent=text;
  el('dl').onclick=()=>{{const b=new Blob([text],{{type:'application/json'}});
    const a=document.createElement('a');a.href=URL.createObjectURL(b);
    a.download='blind-review-result.json';a.click();}};
  el('cp').onclick=()=>navigator.clipboard.writeText(text).then(()=>el('cp').textContent='已复制');
}}
show();
</script></body></html>"""


def tally(result: dict) -> dict:
    """统计盲测结果，并把一致性探针单列。

    一致性低时，胜率就没有解释力——这一点必须写在报告里，
    否则会拿一份随手点出来的数据当审美证据。
    """
    trials = result.get("trials", [])
    wins: dict[str, int] = {}
    appearances: dict[str, int] = {}
    ties = 0
    emotions: dict[str, dict[str, int]] = {}
    rejections: list[dict] = []
    for trial in trials:
        for key in ("left_id", "right_id"):
            appearances[trial[key]] = appearances.get(trial[key], 0) + 1
        if trial["choice"] == "tie":
            ties += 1
            continue
        winner = trial["winner"]
        wins[winner] = wins.get(winner, 0) + 1
        for word in trial.get("emotions", []):
            emotions.setdefault(winner, {})
            emotions[winner][word] = emotions[winner].get(word, 0) + 1
        if trial.get("rejection_reason"):
            loser = trial["left_id"] if winner == trial["right_id"] else trial["right_id"]
            rejections.append({"recipe": loser, "reason": trial["rejection_reason"]})

    # 一致性：探针试次与其原始试次的选择是否一致
    normal = {frozenset((t["left_id"], t["right_id"])): t for t in trials if not t.get("probe")}
    agree = total = 0
    for trial in trials:
        if not trial.get("probe"):
            continue
        key = frozenset((trial["left_id"], trial["right_id"]))
        original = normal.get(key)
        if not original:
            continue
        total += 1
        if original["choice"] == "tie" and trial["choice"] == "tie":
            agree += 1
        elif original.get("winner") and original["winner"] == trial.get("winner"):
            agree += 1
    consistency = round(agree / total, 4) if total else None

    rates = {
        key: round(wins.get(key, 0) / appearances[key], 4)
        for key in sorted(appearances) if appearances[key]
    }
    return {
        "schema_version": BLIND_REVIEW_SCHEMA_VERSION,
        "trial_count": len(trials),
        "tie_count": ties,
        "tie_rate": round(ties / len(trials), 4) if trials else 0.0,
        "win_rate": dict(sorted(rates.items(), key=lambda item: -item[1])),
        "appearances": appearances,
        "emotion_words": emotions,
        "rejection_reasons": rejections,
        "consistency_probe": {
            "probe_count": total,
            "agreement": consistency,
            "meaning": (
                "同一对以相反顺序重复出现时选择是否一致。低于 0.7 说明评审者本身不稳定，"
                "此时胜率没有解释力，不得据此下审美结论。"
            ),
        },
        "boundary": (
            "这是一份真人偏好记录，不是客观质量排名。样本量小、评审者单一时"
            "只能作为方向参考；拒绝原因的信息量通常大于胜率本身。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="审美盲测页面生成与结果统计")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--images", nargs="+", required=True,
                       help="候选图片，文件名 stem 作为标识（对评审者隐藏）")
    build.add_argument("--out", required=True)
    build.add_argument("--title", default="BLCaptain 审美盲测")
    build.add_argument("--seed", type=int, default=20260821)
    build.add_argument("--source", default="", help="原始素材路径，会作为一个候选加入")
    score = sub.add_parser("tally")
    score.add_argument("--result", required=True)
    args = parser.parse_args()

    if args.command == "tally":
        payload = json.loads(Path(args.result).expanduser().resolve().read_text(encoding="utf-8"))
        print(json.dumps(tally(payload), ensure_ascii=False, indent=2))
        return 0

    paths = [Path(p).expanduser().resolve() for p in args.images]
    if args.source:
        paths.insert(0, Path(args.source).expanduser().resolve())
    if len(paths) < 2:
        raise BlindReviewError("至少需要两个候选")
    candidates = []
    for path in paths:
        if not path.is_file():
            raise BlindReviewError(f"文件不存在：{path}")
        candidates.append({"id": path.stem, "path": str(path),
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
                           "data_uri": _thumb_data_uri(path)})
    trials = build_trials(candidates, args.seed)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "candidate_count": len(candidates),
        "candidates": [{"id": c["id"], "sha256": c["sha256"]} for c in candidates],
        "trial_count": len(trials),
        "probe_count": sum(1 for t in trials if t["consistency_probe"]),
    }
    out = Path(args.out).expanduser().resolve()
    if out.exists():
        raise BlindReviewError(f"输出已存在，不会覆盖：{out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_page(trials, args.title, meta), encoding="utf-8")
    print(json.dumps({"page": str(out), **meta,
                      "note": "选项顺序随机、标签隐藏；四分之一的对会以相反顺序重复出现用于测一致性"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
