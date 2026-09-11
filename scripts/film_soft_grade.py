#!/usr/bin/env python3
"""柔和胶片的 Foundation-first 研究执行链。

本模块只处理已经完成一级校正的 BT.709 视频。它不识别人、肤色或物体；
所谓暗部、高光和记忆色保护均是亮度/RGB 统计代理，最终审美仍需人工确认。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path


SCHEMA_VERSION = "4.8.3-phase3f1"


class FilmSoftGradeError(Exception):
    pass


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_filter_complex(strength: float) -> str:
    """构建同一条 30/55/80 单调链；颗粒只在暗部占主要权重。"""
    mix = _clamp(strength)
    toe_08 = 0.08 - 0.036 * mix
    toe_22 = 0.22 - 0.027 * mix
    mid = 0.50 + 0.045 * mix
    shoulder = 0.78 + 0.073 * mix
    white = 1.0 - 0.064 * mix
    saturation = 1.0 + 0.055 * mix
    grain = 2.18 * mix
    rs, gs, bs = 0.10 * mix, 0.022 * mix, -0.073 * mix
    rm, bm = 0.011 * mix, -0.007 * mix
    rh, bh = -0.022 * mix, 0.018 * mix
    return (
        "[0:v]"
        f"curves=all='0/0 0.08/{toe_08:.6f} 0.22/{toe_22:.6f} "
        f"0.50/{mid:.6f} 0.78/{shoulder:.6f} 1/{white:.6f}',"
        f"colorbalance=rs={rs:.6f}:gs={gs:.6f}:bs={bs:.6f}:"
        f"rm={rm:.6f}:gm=0:bm={bm:.6f}:"
        f"rh={rh:.6f}:gh=0:bh={bh:.6f}:pl=1,"
        f"eq=saturation={saturation:.6f},format=gbrp,split=3"
        "[blclean][blgrainin][blmaskin];"
        f"[blgrainin]noise=alls={grain:.6f}:allf=t+u:all_seed=1487[blgrain];"
        "[blmaskin]format=gray,lut=y='clip((140-val)*2,0,255)',"
        "format=gbrp[blgrainmask];"
        "[blclean][blgrain][blgrainmask]maskedmerge=planes=7,"
        "format=yuv420p[blfilm]"
    )


def strength_advice(strength_percent: int, passed: bool) -> str:
    if passed:
        return "保持当前强度并进入人工连续回放。"
    if int(strength_percent) >= 80:
        return "80% 已失败：回退到 55% 或 Foundation，本轮停止加力。"
    return "回退一档，先检查高光中性、暗部琥珀和记忆色，再决定是否重做。"


def evaluate_signature(baseline: dict, candidate: dict,
                       strength_percent: int) -> dict:
    """验证可见签名与安全底线；统计门不能替代人工审美。"""
    scale = max(0.3, min(1.0, float(strength_percent) / 55.0))
    axes = []
    if candidate["shadow_warmth"] >= baseline["shadow_warmth"] + 0.010 * scale:
        axes.append("amber_toe")
    if candidate["highlight_chroma"] <= baseline["highlight_chroma"] - 0.003 * scale:
        axes.append("neutral_highlight_shoulder")
    if candidate["midtone_separation"] >= baseline["midtone_separation"] * (1.035 * scale):
        axes.append("filmic_tone_separation")
    if (candidate["shadow_grain"] >= baseline["shadow_grain"] + 0.003 * scale
            and candidate["highlight_grain"] <= candidate["shadow_grain"] * 0.45):
        axes.append("luma_modulated_grain")
    if candidate["accent_chroma"] >= baseline["accent_chroma"] * 1.02:
        axes.append("memory_color_retained")

    safety = []
    if candidate["global_chroma"] < baseline["global_chroma"] * 0.92:
        safety.append("global_chroma_regressed")
    if candidate["accent_chroma"] < baseline["accent_chroma"] * 0.96:
        safety.append("memory_color_regressed")
    if candidate["neutral_white_chroma"] > baseline["neutral_white_chroma"] + 0.012:
        safety.append("neutral_white_drifted")
    if candidate["black_luma"] > baseline["black_luma"] + 0.012:
        safety.append("black_lifted")
    if candidate["highlight_clip_ratio"] > baseline["highlight_clip_ratio"] + 0.003:
        safety.append("highlight_clip_regressed")
    if abs(candidate["skin_proxy_warmth"] - baseline["skin_proxy_warmth"]) > 0.025:
        safety.append("skin_proxy_drifted")
    if candidate["highlight_grain"] > candidate["shadow_grain"] * 0.55:
        safety.append("grain_not_luma_modulated")

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
        "boundary": "RGB/亮度统计代理不等于肤色、物体或语义蒙版；人工审美待确认。",
    }


def _probe(path: Path) -> dict:
    command = [shutil.which("ffprobe") or "ffprobe", "-v", "error",
               "-show_entries", "stream=index,codec_type,width,height,r_frame_rate,nb_frames,color_space,color_transfer,color_primaries:format=duration",
               "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise FilmSoftGradeError(result.stderr.strip())
    return json.loads(result.stdout)


def render(foundation: Path, output: Path, strength: float) -> dict:
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise FilmSoftGradeError("强度必须是 0 到 1 之间的有限数值")
    if output.exists():
        raise FilmSoftGradeError(f"输出已存在，不会覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_probe = _probe(foundation)
    video = next((item for item in source_probe.get("streams", [])
                  if item.get("codec_type") == "video"), None)
    if not video or int(video.get("nb_frames") or 0) <= 0:
        raise FilmSoftGradeError("Foundation 视频没有可验证的逐帧信息")
    if any(video.get(field) != "bt709" for field in (
            "color_space", "color_transfer", "color_primaries")):
        raise FilmSoftGradeError("Foundation 视频必须具有完整的 BT.709 色彩标签；不会猜测或静默转换")
    has_audio = any(item.get("codec_type") == "audio"
                    for item in source_probe.get("streams", []))
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
               "-i", str(foundation), "-filter_complex", build_filter_complex(strength),
               "-map", "[blfilm]"]
    if has_audio:
        command.extend(["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"])
    command.extend(["-frames:v", str(video["nb_frames"]), "-c:v", "libx264",
                    "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                    "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709", "-shortest", str(output)])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode or not output.is_file():
        raise FilmSoftGradeError(result.stderr.strip())
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "rendered_pending_human",
        "foundation": str(foundation),
        "foundation_sha256": _sha256(foundation),
        "output": str(output),
        "output_sha256": _sha256(output),
        "strength_percent": round(_clamp(strength) * 100),
        "audio_preserved": has_audio,
        "probe": _probe(output),
        "command": command,
        "aesthetic_status": "pending_human",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foundation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strength", type=float, required=True,
                        help="0 到 1，例如 0.55")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.receipt and args.receipt.resolve() == args.output.resolve():
        raise FilmSoftGradeError("回执与成片路径不能相同，不会覆盖成片")
    if args.receipt and args.receipt.exists():
        raise FilmSoftGradeError(f"回执已存在，不会覆盖：{args.receipt}")
    receipt = render(args.foundation, args.output, args.strength)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
