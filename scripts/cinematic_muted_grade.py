#!/usr/bin/env python3
"""电影低饱和的 Foundation-first 研究执行链。

输入必须是已完成一级校正的 BT.709 显示参照视频，并附带同帧率的
动态保护蒙版。Creative Look 只收窄非暖色环境的色彩宽度并建立趾肩密度；
不注入青橙、不全局洗灰、不声称蒙版具备跨素材语义跟踪能力。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from fractions import Fraction
import colorsys
import math


SCHEMA_VERSION = "4.8.3-phase3g1"


class CinematicMutedGradeError(Exception):
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
        shutil.which("ffprobe") or "ffprobe", "-v", "error",
        "-count_frames",
        "-show_entries",
        "stream=index,codec_type,width,height,avg_frame_rate,nb_frames,nb_read_frames,"
        "color_space,color_transfer,color_primaries:format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise CinematicMutedGradeError(result.stderr.strip())
    return json.loads(result.stdout)


def _video_stream(probe: dict) -> dict:
    stream = next((item for item in probe.get("streams", [])
                   if item.get("codec_type") == "video"), None)
    if not stream:
        raise CinematicMutedGradeError("没有可验证的视频流")
    return stream


def _frame_count(stream: dict) -> int:
    """MOV 常在头部给 nb_frames，FFV1/Matroska 需读取计数字段。"""
    for key in ("nb_frames", "nb_read_frames"):
        value = stream.get(key)
        if value not in (None, "", "N/A"):
            return int(value)
    return 0


def build_filter_complex(strength: float, width: int, height: int, fps_fraction: str = '24') -> str:
    """只对绿／青／蓝系环境做减法；人物与白位由蒙版恢复。"""
    mix = _clamp(strength)
    fps = Fraction(fps_fraction)
    if fps <= 0:
        raise CinematicMutedGradeError('帧率必须为正')
    clock = f'settb=AVTB,setpts=N*{fps.denominator}/({fps.numerator}*TB)'
    if mix == 0:
        return (f'[0:v]format=gbrp,{clock},split=2[a][b];'
                f'[1:v]scale={width}:{height},format=gbrp,{clock}[m];'
                '[a][b][m]maskedmerge=planes=7,format=yuv420p[blmuted]')
    toe_08 = 0.08 - 0.040 * mix
    low_22 = 0.22 - 0.080 * mix
    mid_50 = 0.50 - 0.030 * mix
    shoulder_78 = 0.78 + 0.028 * mix
    saturation_shift = -0.55 * mix
    return (
        f"[0:v]format=gbrp,{clock},split=2[blbase][blworldin];"
        "[blworldin]"
        f"curves=all='0/0 0.08/{toe_08:.6f} 0.22/{low_22:.6f} "
        f"0.50/{mid_50:.6f} 0.78/{shoulder_78:.6f} 1/1',"
        f"huesaturation=colors=g+c+b:saturation={saturation_shift:.6f}:"
        "strength=100:lightness=1,format=gbrp[blworld];"
        f"[1:v]scale={int(width)}:{int(height)}:flags=bilinear,"
        "format=gray,lut=y='clip(val*1.5,0,255)',"
        f"format=gbrp,{clock}[blprotect];"
        "[blworld][blbase][blprotect]maskedmerge=planes=7,"
        "format=yuv420p[blmuted]"
    )


def strength_advice(strength_percent: int, passed: bool) -> str:
    if passed:
        return "保持当前强度并进入人工连续回放。"
    if int(strength_percent) >= 80:
        return "80% 已失败：回退到 55% 或 Foundation，本轮停止加力。"
    return "回退一档，先检查环境密度、记忆色和黑白位，再决定是否重做。"


def evaluate_signature(baseline: dict, candidate: dict,
                       strength_percent: int) -> dict:
    """统计门只验证方向和安全，不替代人工审美。"""
    scale = max(0.3, min(1.0, float(strength_percent) / 55.0))
    axes = []
    if candidate["world_chroma"] <= baseline["world_chroma"] * (1.0 - 0.08 * scale):
        axes.append("selective_world_chroma_convergence")
    if candidate["world_hue_spread"] <= baseline["world_hue_spread"] * (1.0 - 0.07 * scale):
        axes.append("weighted_world_hue_span_convergence")
    if candidate["world_tone_separation"] >= baseline["world_tone_separation"] * (1.0 + 0.055 * scale):
        axes.append("directional_tone_density")
    if (candidate["protected_chroma"] >= baseline["protected_chroma"] * 1.003
            and candidate["world_chroma"] < baseline["world_chroma"]):
        axes.append("memory_color_separation")

    safety = []
    if candidate["protected_chroma"] < baseline["protected_chroma"] * 0.96:
        safety.append("protected_memory_color_regressed")
    if abs(candidate["protected_luma"] - baseline["protected_luma"]) > 0.018:
        safety.append("protected_luma_drifted")
    if candidate["world_luma"] < baseline["world_luma"] - 0.055:
        safety.append("world_crushed")
    if candidate["neutral_white_chroma"] > baseline["neutral_white_chroma"] + 0.010:
        safety.append("neutral_white_drifted")
    if candidate["black_luma"] > baseline["black_luma"] + 0.010:
        safety.append("black_lifted")
    if candidate["highlight_clip_ratio"] > baseline["highlight_clip_ratio"] + 0.003:
        safety.append("highlight_clip_regressed")
    if candidate["global_chroma"] < baseline["global_chroma"] * 0.82:
        safety.append("global_chroma_collapsed")

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
        "boundary": "统计只检查方向与上限；蒙版意义和审美均限当前真实素材。",
    }


def measure_signature(foundation, candidate, mask):
    """全时间轴160×90固定源成员；RGB跨度彩度，影调P90-P10，色相圆方差。"""
    arrays = []
    for path, pixfmt in ((foundation,'rgb24'),(candidate,'rgb24'),(mask,'gray')):
        arrays.append(subprocess.check_output(['ffmpeg','-v','error','-i',str(path),
            '-vf','scale=160:90','-pix_fmt',pixfmt,'-f','rawvideo','-']))
    base,out,weights = arrays
    if not weights or len(weights) % (160*90) or len(base) != len(out) or len(base) != len(weights)*3:
        raise CinematicMutedGradeError('最终测量帧数不完整或不一致')
    totals = [{name:[0,0.,0.] for name in ('world','protected','white','black','whole')} for _ in range(2)]
    histograms, circular, clips = [[0]*256 for _ in range(2)], [[0.,0.,0.] for _ in range(2)], [0,0]
    for i,weight in enumerate(weights):
        a = tuple(v/255 for v in base[i*3:i*3+3])
        by = .2126*a[0]+.7152*a[1]+.0722*a[2]
        regions = ['whole']
        if min(255, weight*1.5) >= 230: regions.append('protected')
        if min(255, weight*1.5) <= 25: regions.append('world')
        if by >= .75 and max(a)-min(a) <= .10: regions.append('white')
        if by <= .15: regions.append('black')
        for side,data in enumerate((base,out)):
            r,g,b = (v/255 for v in data[i*3:i*3+3])
            y,c = .2126*r+.7152*g+.0722*b,max(r,g,b)-min(r,g,b)
            clips[side] += max(r,g,b) >= 254/255
            for region in regions:
                row=totals[side][region];row[0]+=1;row[1]+=y;row[2]+=c
            if 'world' in regions:
                histograms[side][round(y*255)]+=1
                angle=colorsys.rgb_to_hsv(r,g,b)[0]*2*math.pi
                circular[side][0]+=c*math.cos(angle);circular[side][1]+=c*math.sin(angle);circular[side][2]+=c
    results=[]
    for side,sums in enumerate(totals):
        if any(row[0] == 0 for row in sums.values()):
            raise CinematicMutedGradeError('缺少固定源区域样本')
        means={name:[v/row[0] for v in row[1:]] for name,row in sums.items()}
        values=[];total=0;targets=[sums['world'][0]*.1,sums['world'][0]*.9]
        for level,count in enumerate(histograms[side]):
            total+=count
            while len(values)<2 and total>=targets[len(values)]: values.append(level/255)
        x,y,w=circular[side]
        results.append(dict(world_luma=means['world'][0],world_chroma=means['world'][1],
            world_tone_separation=values[1]-values[0],world_hue_spread=1-math.hypot(x,y)/max(w,1e-12),
            protected_luma=means['protected'][0],protected_chroma=means['protected'][1],
            neutral_white_chroma=means['white'][1],black_luma=means['black'][0],
            global_chroma=means['whole'][1],highlight_clip_ratio=clips[side]/len(weights),
            sample_counts={name:row[0] for name,row in sums.items()}))
    return {'baseline':results[0],'candidate':results[1],'frames':len(weights)//(160*90),
            'boundary':'全帧汇总而非每帧独立门；固定源成员与val*1.5保护权重；不声称语义与审美验收'}


def render(foundation: Path, mask: Path, output: Path, strength: float) -> dict:
    if output.exists():
        raise CinematicMutedGradeError(f"输出已存在，不会覆盖：{output}")
    if output.resolve() in {foundation.resolve(), mask.resolve()}:
        raise CinematicMutedGradeError("输出不得覆盖 Foundation 或蒙版")
    source_probe = _probe(foundation)
    mask_probe = _probe(mask)
    source_video = _video_stream(source_probe)
    mask_video = _video_stream(mask_probe)
    source_frames = _frame_count(source_video)
    mask_frames = _frame_count(mask_video)
    if source_frames <= 0 or mask_frames <= 0 or source_frames != mask_frames:
        raise CinematicMutedGradeError("蒙版与 Foundation 帧数不一致")
    if abs(float(Fraction(source_video['avg_frame_rate'])) - float(Fraction(mask_video['avg_frame_rate']))) > .01:
        raise CinematicMutedGradeError('蒙版与Foundation帧率不一致')
    tags = {source_video.get("color_space"), source_video.get("color_transfer"),
            source_video.get("color_primaries")}
    if tags != {"bt709"}:
        raise CinematicMutedGradeError("Foundation 不是完整 BT.709 显示参照")
    width, height = int(source_video["width"]), int(source_video["height"])
    graph = build_filter_complex(strength, width, height, source_video['avg_frame_rate'])
    output.parent.mkdir(parents=True, exist_ok=True)
    has_audio = any(item.get("codec_type") == "audio"
                    for item in source_probe.get("streams", []))
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
        "-i", str(foundation), "-i", str(mask),
        "-filter_complex", graph, "-map", "[blmuted]",
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
        raise CinematicMutedGradeError(result.stderr.strip())
    rendered_probe = _probe(output)
    rendered_frames = _frame_count(_video_stream(rendered_probe))
    if rendered_frames != source_frames:
        raise CinematicMutedGradeError("成片帧数验收失败")
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
