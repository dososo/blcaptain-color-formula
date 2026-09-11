#!/usr/bin/env python3
"""韩系清冷的 Foundation-first 单素材视频研究执行链。

输入必须是完成一级校正的 BT.709 显示参照视频，并附带同帧率动态保护
蒙版。Creative Look 只冷化保护区外环境；人物联合与近中性白位恢复
Foundation。本脚本不声称蒙版具备跨素材肤色识别或身份跟踪能力。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from fractions import Fraction


SCHEMA_VERSION = "4.8.3-phase3h1"


class KoreanCoolGradeError(Exception):
    pass


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path) -> dict:
    command = [
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-count_frames",
        "-show_entries",
        "stream=index,codec_type,width,height,avg_frame_rate,nb_frames,nb_read_frames,"
        "color_space,color_transfer,color_primaries,codec_name,channels,channel_layout,sample_rate:format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise KoreanCoolGradeError(result.stderr.strip())
    return json.loads(result.stdout)


def _video_stream(probe: dict) -> dict:
    stream = next((item for item in probe.get("streams", [])
                   if item.get("codec_type") == "video"), None)
    if not stream:
        raise KoreanCoolGradeError("没有可验证的视频流")
    return stream


def _frame_count(stream: dict) -> int:
    for key in ("nb_frames", "nb_read_frames"):
        value = stream.get(key)
        if value not in (None, "", "N/A"):
            return int(value)
    return 0


def build_filter_complex(strength: float, width: int, height: int, fps_fraction: str = '24') -> str:
    """环境跨全明度带冷化，保护区逐帧恢复 Foundation。"""
    mix = _clamp(strength)
    low_20 = 0.20 - 0.022 * mix
    mid_50 = 0.50 - 0.018 * mix
    high_80 = 0.80 - 0.008 * mix
    red = -0.075 * mix
    green = -0.012 * mix
    blue = 0.105 * mix
    fps = Fraction(fps_fraction)
    if fps <= 0:
        raise KoreanCoolGradeError('帧率必须为正')
    clock = f'settb=AVTB,setpts=N*{fps.denominator}/({fps.numerator}*TB)'
    if mix == 0:
        return (f'[0:v]format=gbrp,{clock},split=2[kcbase][kcworld];'
                f'[1:v]scale={width}:{height},format=gbrp,{clock}[kcprotect];'
                '[kcworld][kcbase][kcprotect]maskedmerge=planes=7,format=yuv420p[kcout]')
    return (
        f"[0:v]format=gbrp,{clock},split=2[kcbase][kcworldin];"
        "[kcworldin]"
        f"curves=all='0/0 0.20/{low_20:.6f} 0.50/{mid_50:.6f} "
        f"0.80/{high_80:.6f} 1/1',"
        f"colorbalance=rs={red:.6f}:gs={green:.6f}:bs={blue:.6f}:"
        f"rm={red:.6f}:gm={green:.6f}:bm={blue:.6f}:"
        f"rh={red:.6f}:gh={green:.6f}:bh={blue:.6f}:pl=1,"
        "format=gbrp[kcworld];"
        f"[1:v]scale={int(width)}:{int(height)}:flags=bilinear,"
        "format=gray,lut=y='clip((val-16)*3,0,255)',"
        f"format=gbrp,{clock}[kcprotect];"
        "[kcworld][kcbase][kcprotect]maskedmerge=planes=7,"
        "format=yuv420p[kcout]"
    )


def strength_advice(strength_percent: int, passed: bool) -> str:
    if passed:
        return "保持当前强度并进入人工连续回放。"
    if int(strength_percent) >= 80:
        return "80% 已失败：回退到 55% 或 Foundation，本轮停止加力。"
    return "回退一档，先检查环境冷化、人物体温与白位，再决定是否重做。"


def evaluate_signature(baseline: dict, candidate: dict,
                       strength_percent: int) -> dict:
    """统计门只验证空间方向和安全上限，不替代人工审美。"""
    scale = max(0.3, min(1.0, float(strength_percent) / 55.0))
    axes = []
    if candidate["world_cool_bias"] >= baseline["world_cool_bias"] + 0.015 * scale:
        axes.append("cool_world_across_luminance")
    if candidate["spatial_cool_partition"] >= baseline["spatial_cool_partition"] + 0.018 * scale:
        axes.append("protected_subject_world_separation")
    if (candidate["world_luma"] <= baseline["world_luma"] - 0.006 * scale
            and candidate["world_luma"] >= baseline["world_luma"] - 0.035):
        axes.append("restrained_world_tone_separation")

    safety = []
    if abs(candidate["protected_cool_bias"] - baseline["protected_cool_bias"]) > 0.012:
        safety.append("protected_region_cooled")
    if candidate["protected_chroma"] < baseline["protected_chroma"] * 0.96:
        safety.append("protected_memory_color_regressed")
    if abs(candidate["protected_luma"] - baseline["protected_luma"]) > 0.018:
        safety.append("protected_luma_drifted")
    if candidate["world_luma"] < baseline["world_luma"] - 0.045:
        safety.append("world_crushed")
    if candidate["neutral_white_chroma"] > baseline["neutral_white_chroma"] + 0.010:
        safety.append("neutral_white_drifted")
    if candidate["black_luma"] > baseline["black_luma"] + 0.010:
        safety.append("black_lifted")
    if candidate["highlight_clip_ratio"] > baseline["highlight_clip_ratio"] + 0.003:
        safety.append("highlight_clip_regressed")

    blocking = []
    if len(axes) < 2:
        blocking.append("fewer_than_two_signature_axes")
    blocking.extend(safety)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if not blocking else "blocked",
        "strength_percent": int(strength_percent),
        "passed_signature_axes": axes,
        "safety_failures": safety,
        "blocking_failures": blocking,
        "boundary": "统计只检查空间冷化方向与安全；蒙版意义和审美限当前素材。",
    }


def measure_signature(foundation: Path, candidate: Path, mask: Path) -> dict:
    """全帧320×180固定源选区；RGB代理非语义，缺样本不作通过。"""
    def decode(path, gray=False):
        filt = 'scale=320:180:flags=bilinear'
        if gray:
            filt += ",format=gray,lut=y='clip((val-16)*3,0,255)'"
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
            '-vf', filt, '-fps_mode', 'passthrough', '-f', 'rawvideo',
            '-pix_fmt', 'gray' if gray else 'rgb24', '-'])
    base, out, weights = decode(foundation), decode(candidate), decode(mask, True)
    if len(base) != len(out) or len(base) != 3 * len(weights) or not weights:
        raise KoreanCoolGradeError('最终测量帧数或像素数不一致')
    totals = [{name: [0, 0.0, 0.0, 0.0] for name in ('world', 'protected', 'white', 'black', 'whole')} for _ in range(2)]
    clips = [0, 0]
    for i, weight in enumerate(weights):
        offset = i * 3
        br, bg, bb = (v / 255 for v in base[offset:offset + 3])
        by = .2126 * br + .7152 * bg + .0722 * bb
        bc = max(br, bg, bb) - min(br, bg, bb)
        regions = ['whole']
        if weight >= 230:
            regions.append('protected')
        if weight <= 25:
            regions.append('world')
        if by >= .75 and bc <= .10:
            regions.append('white')
        if by <= .15:
            regions.append('black')
        for side, data in enumerate((base, out)):
            r, g, b = (v / 255 for v in data[offset:offset + 3])
            y, c, cool = .2126*r + .7152*g + .0722*b, max(r,g,b)-min(r,g,b), b-r
            clips[side] += max(r,g,b) >= 254/255
            for region in regions:
                row = totals[side][region]
                row[0] += 1
                row[1] += y
                row[2] += c
                row[3] += cool
    results = []
    for side, sums in enumerate(totals):
        if any(row[0] == 0 for row in sums.values()):
            raise KoreanCoolGradeError('最终签名测量缺少固定源区域样本')
        means = {name: [v/row[0] for v in row[1:]] for name,row in sums.items()}
        world, protected = means['world'], means['protected']
        results.append(dict(world_luma=world[0], world_chroma=world[1], world_cool_bias=world[2],
            protected_luma=protected[0], protected_chroma=protected[1], protected_cool_bias=protected[2],
            spatial_cool_partition=world[2]-protected[2], neutral_white_chroma=means['white'][1],
            black_luma=means['black'][0], highlight_clip_ratio=clips[side]/len(weights),
            sample_counts={name:row[0] for name,row in sums.items()}))
    return {'baseline': results[0], 'candidate': results[1], 'frames': len(weights)//(320*180),
            'definition': '全帧320×180；恢复权重>=230保护区、<=25环境；Foundation亮度>=.75且RGB跨度<=.10为白位，<=.15为黑位，固定成员；非语义'}


def render(foundation: Path, mask: Path, output: Path, strength: float) -> dict:
    if output.exists():
        raise KoreanCoolGradeError(f"输出已存在，不会覆盖：{output}")
    if output.resolve() in {foundation.resolve(), mask.resolve()}:
        raise KoreanCoolGradeError("输出不得覆盖 Foundation 或蒙版")
    source_probe = _probe(foundation)
    mask_probe = _probe(mask)
    source_video = _video_stream(source_probe)
    mask_video = _video_stream(mask_probe)
    source_frames = _frame_count(source_video)
    mask_frames = _frame_count(mask_video)
    if source_frames <= 0 or mask_frames <= 0 or source_frames != mask_frames:
        raise KoreanCoolGradeError("蒙版与 Foundation 帧数不一致")
    tags = {source_video.get("color_space"), source_video.get("color_transfer"),
            source_video.get("color_primaries")}
    if tags != {"bt709"}:
        raise KoreanCoolGradeError("Foundation 不是完整 BT.709 显示参照")
    width, height = int(source_video["width"]), int(source_video["height"])
    graph = build_filter_complex(strength, width, height, source_video['avg_frame_rate'])
    output.parent.mkdir(parents=True, exist_ok=True)
    has_audio = any(item.get("codec_type") == "audio"
                    for item in source_probe.get("streams", []))
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(foundation), "-i", str(mask),
        "-filter_complex", graph, "-map", "[kcout]",
    ]
    if has_audio:
        command.extend(["-map", "0:a:0?", "-c:a", "copy"])
    command.extend([
        "-frames:v", str(source_frames), "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-movflags", "+faststart", str(output),
    ])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode or not output.is_file():
        raise KoreanCoolGradeError(result.stderr.strip())
    rendered_probe = _probe(output)
    if _frame_count(_video_stream(rendered_probe)) != source_frames:
        raise KoreanCoolGradeError("成片帧数验收失败")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "rendered_pending_human",
        "foundation": str(foundation),
        "foundation_sha256": _sha256(foundation),
        "mask": str(mask),
        "mask_sha256": _sha256(mask),
        "output": str(output),
        "output_sha256": _sha256(output),
        "strength_percent": round(_clamp(strength) * 100),
        "audio_preserved": has_audio,
        "probe": rendered_probe,
        "filter_complex": graph,
        "command": command,
        "aesthetic_status": "pending_human",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foundation", type=Path, required=True)
    parser.add_argument("--mask", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strength", type=float, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    receipt = render(args.foundation, args.mask, args.output, args.strength)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
