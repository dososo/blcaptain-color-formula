#!/usr/bin/env python3
"""从 Openverse 官方 API 索引的 StockSnap CC0 素材构建仓库外照片评审集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QUERIES = [
    ("landscape", 6, "mountain lake landscape"),
    ("portrait", 6, "portrait woman"),
    ("city", 6, "city street architecture"),
    ("night", 6, "night city lights"),
    ("food", 6, "food table restaurant"),
    ("product", 5, "product"),
    ("lowkey", 5, "dark portrait"),
]
API_ROOT = "https://api.openverse.org/v1/images/"
SOURCE_LICENSE = "https://stocksnap.io/license"
CC0_LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"
USER_AGENT = "BLCaptainReviewSet/1.1 (local evaluation; contact: local-user)"


def _open(url: str, timeout: int = 60):
    last_error = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in {429, 500, 502, 503, 504}:
                raise
            time.sleep(2 * (attempt + 1))
    raise last_error


def _download(url: str, target: Path) -> None:
    temporary = target.with_suffix(target.suffix + ".partial")
    with _open(url, 120) as source, temporary.open("wb") as output:
        while block := source.read(1024 * 1024):
            output.write(block)
    temporary.replace(target)


def _candidates(query: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "q": query, "license": "cc0", "source": "stocksnap",
        # Openverse 匿名调用的稳妥页大小；每类最多只需 6 张。
        "page_size": 20, "mature": "false",
    })
    with _open(f"{API_ROOT}?{params}") as response:
        payload = json.load(response)
    return payload.get("results") or []


def build(output_dir: Path) -> dict:
    photo_dir = output_dir / "photos"
    photo_dir.mkdir(parents=True, exist_ok=False)
    used: set[str] = set()
    items: list[dict] = []
    for category, required, query in QUERIES:
        for candidate in _candidates(query):
            if sum(item["category"] == category for item in items) >= required:
                break
            identifier = str(candidate.get("id") or "")
            landing = str(candidate.get("foreign_landing_url") or "")
            if not identifier or identifier in used or not landing.startswith("https://stocksnap.io/photo/"):
                continue
            if candidate.get("source") != "stocksnap" or candidate.get("license") != "cc0":
                continue
            # StockSnap CDN 会拒绝自动化直链；使用 Openverse 官方缩略图代理取得评审副本。
            download_url = candidate.get("thumbnail") or candidate.get("url")
            if not download_url:
                continue
            index = len(items) + 1
            target = photo_dir / f"{index:02d}_{category}.jpg"
            try:
                _download(download_url, target)
            except (OSError, urllib.error.URLError):
                continue
            signature = target.read_bytes()[:8]
            if not (signature.startswith(b"\xff\xd8\xff") or signature.startswith(b"\x89PNG")):
                target.unlink()
                continue
            used.add(identifier)
            items.append({
                "key": f"p{index:02d}", "category": category,
                "title": candidate.get("title") or identifier,
                "creator": candidate.get("creator"), "creator_url": candidate.get("creator_url"),
                "source": "StockSnap", "source_page": landing,
                "discovery_provider": "Openverse API", "discovery_id": identifier,
                "download_url": download_url, "original_url": candidate.get("url"),
                "license": "CC0 1.0",
                "license_url": candidate.get("license_url") or CC0_LICENSE,
                "source_license_url": SOURCE_LICENSE,
                "license_evidence": "Openverse 条目声明 cc0；StockSnap 官方许可页声明站内全部照片采用 CC0。",
                "path": str(target), "reported_width": candidate.get("width"),
                "reported_height": candidate.get("height"), "bytes": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "acquired_at": datetime.now(timezone.utc).isoformat(), "api_query": query,
            })
            time.sleep(0.15)
        actual = sum(item["category"] == category for item in items)
        if actual != required:
            raise RuntimeError(f"{category} 只取得 {actual}/{required} 张合规素材")

    manifest = {
        "schema_version": "1.1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
        "boundary": "媒体像素位于仓库外；仓库只保存许可、来源、哈希与评审记录。",
        "license_policy": {
            "accepted": ["CC0 1.0"], "source_license": SOURCE_LICENSE,
            "cc0_legalcode": CC0_LICENSE,
            "openverse_boundary": "Openverse 仅作发现索引；每条同时保留 StockSnap 原始落地页与官方站点许可页。",
        },
        "photos": items, "videos": [], "counts": {"photos": len(items), "videos": 0},
    }
    (output_dir / "photo-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output = Path(args.output_dir).expanduser().resolve()
    if output.exists():
        raise SystemExit(f"输出目录已存在，不覆盖：{output}")
    manifest = build(output)
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
