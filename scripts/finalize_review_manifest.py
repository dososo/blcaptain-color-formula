#!/usr/bin/env python3
"""校验评审视频并把仓库外媒体清单固化为可提交 manifest。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

VIDEO_SOURCES = {
    "v01_tears_060_092.mp4": {
        "title": "Tears of Steel 60s–92s", "source": "Blender Foundation / Xiph mirror",
        "source_url": "https://media.xiph.org/tearsofsteel/tears_of_steel_1080p.webm",
        "license": "CC BY 3.0", "license_url": "https://creativecommons.org/licenses/by/3.0/",
        "project_license_page": "https://mango.blender.org/about/",
    },
    "v02_sintel_005_037.mp4": {
        "title": "Sintel trailer 5s–37s", "source": "Blender Foundation",
        "source_url": "https://durian.blender.org/download/",
        "license": "CC BY 3.0", "license_url": "https://creativecommons.org/licenses/by/3.0/",
        "project_license_page": "https://durian.blender.org/sharing/",
    },
    "v03_sintel_015_047.mp4": {
        "title": "Sintel trailer 15s–47s", "source": "Blender Foundation",
        "source_url": "https://durian.blender.org/download/",
        "license": "CC BY 3.0", "license_url": "https://creativecommons.org/licenses/by/3.0/",
        "project_license_page": "https://durian.blender.org/sharing/",
    },
}


def probe(path: Path) -> dict:
    command = ["ffprobe", "-v", "error", "-show_entries",
               "stream=codec_type,width,height,r_frame_rate,color_space,color_transfer,color_primaries",
               "-show_entries", "format=duration", "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-dir", required=True)
    parser.add_argument("--repo-manifest", required=True)
    args = parser.parse_args()
    root = Path(args.set_dir).expanduser().resolve()
    photo_manifest = json.loads((root / "photo-manifest.json").read_text(encoding="utf-8"))
    videos = []
    for index, (name, source) in enumerate(VIDEO_SOURCES.items(), start=1):
        path = root / "videos" / name
        metadata = probe(path)
        video_stream = next(item for item in metadata["streams"] if item["codec_type"] == "video")
        has_audio = any(item["codec_type"] == "audio" for item in metadata["streams"])
        duration = float(metadata["format"]["duration"])
        if int(video_stream.get("width", 0)) != 1920 or int(video_stream.get("height", 0)) != 1080:
            raise RuntimeError(f"{name} 不是 1920×1080")
        if duration < 30 or not has_audio:
            raise RuntimeError(f"{name} 不满足 ≥30 秒且含音频")
        videos.append({
            "key": f"v{index:02d}", "category": "multi-shot-video", "path": str(path),
            **source, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size, "duration": duration, "has_audio": has_audio,
            "width": 1920, "height": 1080, "frame_rate": video_stream.get("r_frame_rate"),
            "color_space": video_stream.get("color_space"),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        })
    manifest = {
        **photo_manifest, "videos": videos,
        "counts": {"photos": len(photo_manifest["photos"]), "videos": len(videos)},
        "video_content_boundary": (
            "三段文件均为本轮新截取；v02/v03 来自同一 Sintel 官方片源且内容区间重叠，"
            "用于时序与渲染回归，不冒充三个独立影片来源。"
        ),
    }
    if manifest["counts"] != {"photos": 40, "videos": 3}:
        raise RuntimeError(f"评审集数量不符：{manifest['counts']}")
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (root / "manifest.json").write_text(text, encoding="utf-8")
    target = Path(args.repo_manifest).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
