#!/usr/bin/env python3
"""把已知色彩空间的基准素材转换为显示参考；未知 Log/RAW/HDR 一律拒绝猜测。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


TRANSFORMS = {
    ("rec709-scene-linear", "bt709-display"): {
        "input_primaries": "bt709",
        "input_transfer": "linear",
        "input_matrix": "bt709",
        "output_primaries": "bt709",
        "output_transfer": "bt709",
        "output_matrix": "bt709",
        "ffmpeg_filter": (
            "zscale=pin=bt709:tin=linear:min=bt709:"
            "p=bt709:t=bt709:m=bt709"
        ),
    },
    ("rec709-scene-linear", "srgb-display"): {
        "input_primaries": "bt709",
        "input_transfer": "linear",
        "input_matrix": "bt709",
        "output_primaries": "bt709",
        "output_transfer": "iec61966-2-1",
        "output_matrix": "bt709",
        "output_icc": "sRGB Profile.icc",
        "ffmpeg_filter": (
            "zscale=pin=bt709:tin=linear:min=bt709:"
            "p=bt709:t=iec61966-2-1:m=bt709"
        ),
    },
}

SRGB_ICC = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")


def resolve_transform(input_space: str, output_space: str) -> dict:
    transform = TRANSFORMS.get((input_space, output_space))
    if transform is None:
        raise ValueError(
            f"不支持或未声明的输入变换：{input_space} -> {output_space}；"
            "禁止把未知 Log/RAW/HDR 当作 Rec.709 处理"
        )
    return dict(transform)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_sequence(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def build_receipt(
    *,
    source_id: str,
    source_sha256: str,
    output_sha256: str,
    output_path: Path,
    transform: dict,
    ffmpeg_version: str,
) -> dict:
    return {
        "schema_version": "1.0.0",
        "source_id": source_id,
        "source_sha256": source_sha256,
        "output_sha256": output_sha256,
        "output_path": str(output_path.resolve()),
        "transform": transform,
        "tool": {"name": "ffmpeg", "version": ffmpeg_version},
        "overwrote_source": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def sequence_paths(pattern: str, start: int, count: int) -> list[Path]:
    if "%" not in pattern:
        raise ValueError("序列输入必须包含 printf 风格帧号占位符，例如 %05d")
    paths = [Path(pattern % frame).expanduser().resolve() for frame in range(start, start + count)]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"序列缺少 {len(missing)} 帧，首个缺失文件：{missing[0]}")
    return paths


def ffmpeg_version() -> str:
    result = subprocess.run(
        ["ffmpeg", "-version"], check=True, capture_output=True, text=True
    )
    return result.stdout.splitlines()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input")
    source.add_argument("--input-pattern")
    parser.add_argument("--start-number", type=int, default=0)
    parser.add_argument("--frame-count", type=int)
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--input-space", required=True)
    parser.add_argument("--output-space", default="bt709-display")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--receipt")
    args = parser.parse_args()

    transform = resolve_transform(args.input_space, args.output_space)
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"输出已存在，拒绝覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-n"]
    if args.input_pattern:
        if not args.frame_count or args.frame_count < 1:
            raise ValueError("序列输入必须提供正整数 --frame-count")
        paths = sequence_paths(args.input_pattern, args.start_number, args.frame_count)
        command += [
            "-framerate", str(args.fps),
            "-start_number", str(args.start_number),
            "-i", str(Path(args.input_pattern).expanduser().resolve()),
            "-frames:v", str(args.frame_count),
            "-vf", transform["ffmpeg_filter"],
            "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", "-movflags", "+faststart", str(output),
        ]
        source_sha256 = sha256_sequence(paths)
    else:
        input_path = Path(args.input).expanduser().resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"输入不存在：{input_path}")
        if input_path == output:
            raise ValueError("输出不得与输入相同")
        command += [
            "-i", str(input_path), "-frames:v", "1",
            "-vf", transform["ffmpeg_filter"],
            "-pix_fmt", "rgb48be", "-color_primaries", "bt709",
            "-color_trc", transform["output_transfer"], "-colorspace", "bt709", str(output),
        ]
        source_sha256 = sha256_file(input_path)

    subprocess.run(command, check=True)
    if transform.get("output_icc"):
        if not SRGB_ICC.is_file():
            raise FileNotFoundError(f"缺少系统 sRGB ICC：{SRGB_ICC}")
        profiled = output.with_name(output.stem + ".profiled" + output.suffix)
        subprocess.run(
            ["magick", str(output), "-profile", str(SRGB_ICC), str(profiled)],
            check=True,
        )
        profiled.replace(output)
    receipt_path = Path(args.receipt).expanduser().resolve() if args.receipt else output.with_suffix(output.suffix + ".json")
    if receipt_path.exists():
        raise FileExistsError(f"回执已存在，拒绝覆盖：{receipt_path}")
    receipt = build_receipt(
        source_id=args.source_id,
        source_sha256=source_sha256,
        output_sha256=sha256_file(output),
        output_path=output,
        transform=transform,
        ffmpeg_version=ffmpeg_version(),
    )
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
