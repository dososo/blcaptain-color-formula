#!/usr/bin/env python3
"""开源前的个人标识与令牌检查。

不用 grep 关键词——正文里提到「author_id」这个词和真的带着一个 author_id
是两回事，关键词匹配分不开，会给出一堆假阳性，然后人就开始忽略它。

这里只看三样确定的东西：
  1. JSON 里 author_id / author 字段是不是还带着值；
  2. URL 上有没有挂访问令牌（xsec_token 之类）；
  3. 文本里有没有联系方式形态（微信号、手机号、平台 UID/mid 后跟数字）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("research", "references", "tasks", "evals", "agents")
TOKEN_IN_URL = re.compile(r"https?://[^\s\"']*[?&](xsec_token|access_token|sig|signature)=")
CONTACT = re.compile(r"(微信\s*[:：]?\s*[A-Za-z0-9_-]{5,})|(\b1[3-9]\d{9}\b)|((UID|mid)\s*[:：]?\s*\d{6,})")
IDENTITY_FIELDS = ("author_id", "author", "uploader", "nickname", "user_id")
ALLOWED_VALUES = re.compile(r"^(unknown|)$|已移除|已按开源策略|个人标识|作者A\d+|UID-\d+")


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, node


def main() -> int:
    findings = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in {".json", ".md", ".txt"}:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            rel = p.relative_to(ROOT)
            for m in TOKEN_IN_URL.finditer(text):
                findings.append(f"{rel}: URL 上挂着访问令牌 {m.group(1)}")
            for m in CONTACT.finditer(text):
                findings.append(f"{rel}: 疑似联系方式／平台 UID «{m.group(0)[:40]}»")
            if p.suffix.lower() != ".json":
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            for key_path, value in walk(data):
                leaf = key_path.rsplit(".", 1)[-1].split("[")[0]
                if leaf in IDENTITY_FIELDS and isinstance(value, str):
                    if not ALLOWED_VALUES.search(value):
                        findings.append(f"{rel}: 字段 {key_path} 仍带个人标识 «{value[:40]}»")

    print(json.dumps({
        "checked_dirs": list(SCAN_DIRS),
        "findings": findings,
        "clean": not findings,
        "boundary": "只查确定形态：身份字段值、URL 访问令牌、联系方式形态。"
                    "不查自由文本里的人名——化名替换后正文提到「作者A34」是允许的，"
                    "而一个没被替换过的真名这里查不出来，需要靠替换时的全量扫描保证。",
    }, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
