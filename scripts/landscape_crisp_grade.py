#!/usr/bin/env python3
"""风景清透的区域密度研究链。

选区只依据逐帧亮度与 RGB 通道关系；它不识别天空、植被、水、山体或距离。
可靠语义证据缺失时，只允许称为像素代理并由真实素材人工复核。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from fractions import Fraction

import video_memory_protection as video_masks


SCHEMA_VERSION = "4.8.3-phase3d1"
MASK_NAMES = ("protect", "green", "cool", "earth", "detail")


class LandscapeCrispGradeError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_gray_mask(path: Path, width: int, height: int) -> bytes:
    return video_masks._read_raw(path, "gray", width, height)


def _mask_bytes(frame: Path, width: int, height: int) -> tuple[dict[str, bytes], dict]:
    rgb = video_masks._read_raw(frame, "rgb24", width, height)
    masks = {name: bytearray(width * height) for name in MASK_NAMES}
    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high, low = max(red, green, blue), min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0

        # 雾白与黑位不接受创意层，避免清透被做成发白或死黑。
        if luma <= 0.09 or (luma >= 0.72 and chroma <= 0.10):
            selected = "protect"
        # 首个真实灰雾山湖校准：4/2 通道差仅覆盖 0.36%，3/1 可达 0.56%；
        # 后者仍要求绿同时领先红、蓝，且继续与冷、土色代理互斥。
        elif 0.04 <= luma <= 0.62 and chroma >= 0.025 and green - red >= 3 and green - blue >= 1:
            selected = "green"
        elif 0.04 <= luma <= 0.66 and chroma >= 0.02 and blue - red >= 3:
            selected = "cool"
        elif 0.04 <= luma <= 0.66 and chroma >= 0.025 and red - blue >= 5 and green >= blue:
            selected = "earth"
        else:
            selected = "detail"
        masks[selected][index] = 255

    return {name: bytes(data) for name, data in masks.items()}, {
        "boundary": (
            "这些选区只是逐帧亮度与 RGB 通道关系的互斥像素代理，不是天空、植被、"
            "水面、岩土、远近层次或主体识别；其中冷色代理尤其不是天空语义。"
        )
    }


def write_landscape_masks(frame: Path, **paths: Path) -> dict:
    if set(paths) != set(MASK_NAMES):
        raise LandscapeCrispGradeError("必须提供完整的五类互斥蒙版路径")
    width, height = video_masks._dimensions(frame)
    payloads, note = _mask_bytes(frame, width, height)
    coverage = {}
    total = max(1, width * height)
    for name in MASK_NAMES:
        video_masks._write_pgm(paths[name], width, height, payloads[name])
        coverage[name] = round(sum(value >= 128 for value in payloads[name]) / total, 6)
    return {
        "paths": {name: str(paths[name]) for name in MASK_NAMES},
        "coverage": coverage,
        "meaning": "互斥的保护、绿、冷、土色与中性结构像素代理",
        **note,
    }


def build_landscape_mask_sequence(frames: list[Path], mask_dir: Path) -> dict:
    if not frames:
        raise LandscapeCrispGradeError("没有输入帧")
    dimensions = video_masks._dimensions(frames[0])
    for name in MASK_NAMES:
        (mask_dir / name).mkdir(parents=True, exist_ok=True)
    coverages = {name: [] for name in MASK_NAMES}
    hashes = {name: [] for name in MASK_NAMES}
    for index, frame in enumerate(frames):
        if video_masks._dimensions(frame) != dimensions:
            raise LandscapeCrispGradeError("输入帧尺寸不一致")
        paths = {name: mask_dir / name / f"mask-{index:06d}.pgm" for name in MASK_NAMES}
        result = write_landscape_masks(frame, **paths)
        for name in MASK_NAMES:
            coverages[name].append(result["coverage"][name])
            hashes[name].append(_sha256(paths[name]))

    # 检查全段平均覆盖；这不等于每帧都有足够样本。
    reasons = []
    for name in ("green", "cool", "earth", "detail"):
        if sum(coverages[name]) / len(frames) < 0.005:
            reasons.append(f"{name} 像素代理覆盖不足")
    if sum(coverages["protect"]) / len(frames) > 0.80:
        reasons.append("保护区过大，素材缺少安全创作空间")
    status = "blocked" if reasons else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "mutually-exclusive-per-frame-rgb-proxies",
        "frame_count": len(frames),
        "dimensions": list(dimensions),
        "coverage": coverages,
        "mask_sha256": hashes,
        "reasons": reasons,
        "boundary": "只证明本素材全段平均代理覆盖满足阈值，不证明逐帧充分覆盖、语义、景深或审美。",
    }


def capabilities_from_report(report: dict) -> set[str]:
    paths = [Path((report.get(f"{name}_mask_video") or {}).get("path") or "")
             for name in MASK_NAMES]
    if report.get("status") == "ready" and report.get("capability_granted") is True and all(path.is_file() for path in paths):
        return {"temporal-landscape-pixel-proxies", "protected-bright-and-black"}
    return set()


def foundation_filter() -> str:
    """素材已具备显示参照影调；Foundation 不统一抬灰或改动大气雾。"""
    return "null"


def detail_curve_points(strength: float) -> list[tuple[float, float]]:
    mix = max(0.0, min(1.0, float(strength)))
    offsets = {0.0: 0.0, 0.05: 0.0, 0.18: 0.035, 0.34: 0.04,
               0.55: 0.012, 0.82: 0.0, 1.0: 0.0}
    return [(point, round(point + offsets[point] * mix, 6))
            for point in offsets]


def detail_filter(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    points = " ".join(f"{x:.2f}/{y:.6f}" for x, y in detail_curve_points(mix))
    return f"curves=all='{points}',unsharp=5:5:{0.36 * mix:.6f}:5:5:0"


def green_filter(strength: float) -> str:
    mix = max(0.0, min(1.0, float(strength)))
    return f"colorchannelmixer=rr={1.0 + .10 * mix:.6f}:gg={1.0 - .04 * mix:.6f}:bb={1.0 + .10 * mix:.6f}"


def cool_gains(strength: float) -> tuple[float, float, float]:
    mix = max(0.0, min(1.0, float(strength)))
    return 1.0 - .06 * mix, 1.0 + .025 * mix, 1.0 + .10 * mix


def cool_filter(strength: float) -> str:
    red, green, blue = cool_gains(strength)
    return f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}"


def earth_gains(strength: float) -> tuple[float, float, float]:
    mix = max(0.0, min(1.0, float(strength)))
    return 1.0 + .13 * mix, 1.0 + .025 * mix, 1.0 - .08 * mix


def earth_filter(strength: float) -> str:
    red, green, blue = earth_gains(strength)
    return f"colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}"


def strength_advice(strength_percent: int, passed: bool) -> str:
    if passed:
        return "保持当前强度，进入完整连续人工回放。"
    if int(strength_percent) >= 80:
        return "80% 已失败：立即回退到 55% 或 Foundation，并重新检查亮雾与区域密度。"
    return "回退一档，检查亮雾、绿的主导度与结构密度后再决定。"


def synthetic_metric_fixture(*, whole_chroma: float, detail_density: float,
                             palette_separation: float, protected_luma: float,
                             green_chroma: float, cool_earth_chroma: float) -> dict:
    return {
        "whole_chroma": whole_chroma,
        "detail_density": detail_density,
        "palette_separation": palette_separation,
        "protected_luma": protected_luma,
        "green_chroma": green_chroma,
        "cool_earth_chroma": cool_earth_chroma,
    }


def evaluate_landscape_density(baseline: dict, candidate: dict,
                               strength_percent: int) -> dict:
    strength = max(1, min(100, int(strength_percent)))
    required_gain = 1.0 + 0.02 * strength / 55.0
    failures = []
    if candidate["whole_chroma"] < baseline["whole_chroma"]:
        failures.append("whole_chroma_regressed")
    if candidate["detail_density"] < baseline["detail_density"] * required_gain:
        failures.append("detail_density_regressed")
    if candidate["palette_separation"] < baseline["palette_separation"] * required_gain:
        failures.append("palette_separation_insufficient")
    if abs(candidate["protected_luma"] - baseline["protected_luma"]) > 0.03:
        failures.append("protected_bright_luma_drift")
    if candidate["green_chroma"] > candidate["cool_earth_chroma"] * 1.05:
        failures.append("green_dominance_exceeded")
    return {
        "gate": "region-density-v1",
        "status": "blocked" if failures else "passed",
        "blocking_failures": failures,
        "required_gain": round(required_gain, 6),
        "boundary": "固定像素代理的工程方向门；不证明空间深度、语义正确或审美成立。",
    }


def synthetic_light_path_fixture(*, detail_luma: float, protect_luma: float,
                                 black_luma: float, cool_luma: float,
                                 cool_chroma: float, earth_luma: float,
                                 earth_warmth: float) -> dict:
    return {
        "detail_luma": detail_luma,
        "protect_luma": protect_luma,
        "black_luma": black_luma,
        "cool_luma": cool_luma,
        "cool_chroma": cool_chroma,
        "earth_luma": earth_luma,
        "earth_warmth": earth_warmth,
    }


def evaluate_landscape_light_path(baseline: dict, candidate: dict,
                                  strength_percent: int) -> dict:
    strength = max(1, min(100, int(strength_percent)))
    required_gain = 1.0 + 0.015 * strength / 55.0
    failures = []
    if candidate["detail_luma"] < baseline["detail_luma"] * required_gain:
        failures.append("detail_midtone_readability_regressed")
    if (candidate["cool_luma"] < baseline["cool_luma"] * 0.995
            or candidate["cool_chroma"] < baseline["cool_chroma"] * required_gain):
        failures.append("cool_light_energy_regressed")
    if (candidate["earth_luma"] < baseline["earth_luma"] * 0.995
            or candidate["earth_warmth"] < baseline["earth_warmth"] * required_gain):
        failures.append("earth_light_energy_regressed")
    if (abs(candidate["protect_luma"] - baseline["protect_luma"]) > 0.01
            or abs(candidate["black_luma"] - baseline["black_luma"]) > 0.005):
        failures.append("protected_anchor_drift")
    return {
        "gate": "landscape-light-path-v1",
        "status": "blocked" if failures else "passed",
        "blocking_failures": failures,
        "required_gain": round(required_gain, 6),
        "boundary": "固定像素代理的可读性与光能方向门，不证明真实布光、空间层级或语义。",
    }


def light_path_from_metrics(metrics: dict) -> dict:
    regions = metrics["regions"]
    return synthetic_light_path_fixture(
        detail_luma=regions["detail"]["luma"],
        protect_luma=regions["protect"]["luma"],
        black_luma=metrics["protected_black_luma"],
        cool_luma=regions["cool"]["luma"],
        cool_chroma=regions["cool"]["chroma"],
        earth_luma=regions["earth"]["luma"],
        earth_warmth=regions["earth"]["warmth"],
    )


def masked_metrics(frames: list[Path], mask_paths: dict[str, list[Path]],
                   reference_frames: list[Path] | None = None) -> dict:
    reference_frames = frames if reference_frames is None else reference_frames
    if (not frames or len(reference_frames) != len(frames)
            or any(len(paths) != len(frames) for paths in mask_paths.values())):
        raise LandscapeCrispGradeError("综合色彩测量的帧与蒙版数量不一致")
    total = {name: {"chroma": 0.0, "luma": 0.0, "density": 0.0,
                    "warmth": 0.0} for name in ("whole", *MASK_NAMES)}
    count = {name: 0 for name in total}
    protected_black_luma = 0.0
    protected_black_count = 0
    for frame_index, frame in enumerate(frames):
        width, height = video_masks._dimensions(frame)
        rgb = video_masks._read_raw(frame, "rgb24", width, height)
        reference = (rgb if frame == reference_frames[frame_index] else
                     video_masks._read_raw(reference_frames[frame_index], "rgb24", width, height))
        masks = {name: read_gray_mask(mask_paths[name][frame_index], width, height)
                 for name in MASK_NAMES}
        for index in range(width * height):
            red, green, blue = rgb[index * 3:index * 3 + 3]
            chroma = (max(red, green, blue) - min(red, green, blue)) / 255.0
            luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
            values = {"chroma": chroma, "luma": luma,
                      "density": chroma * (1.0 - luma),
                      "warmth": (red - blue) / 255.0}
            for key, value in values.items():
                total["whole"][key] += value
            count["whole"] += 1
            for name in MASK_NAMES:
                if masks[name][index] >= 128:
                    for key, value in values.items():
                        total[name][key] += value
                    count[name] += 1
            source_red, source_green, source_blue = reference[index * 3:index * 3 + 3]
            source_luma = (0.2126 * source_red + 0.7152 * source_green + 0.0722 * source_blue) / 255.0
            if masks["protect"][index] >= 128 and source_luma <= 0.15:
                protected_black_luma += luma
                protected_black_count += 1
    if any(count[name] == 0 for name in MASK_NAMES):
        raise LandscapeCrispGradeError("至少一个像素代理没有可测样本")
    if protected_black_count == 0:
        raise LandscapeCrispGradeError("源片保护黑位没有可测样本，不能以零值代替通过")
    regions = {name: {key: round(value / count[name], 6)
                      for key, value in total[name].items()} for name in total}
    return {
        "regions": regions,
        "whole_chroma": regions["whole"]["chroma"],
        "detail_density": regions["detail"]["density"],
        "palette_separation": round(abs(regions["cool"]["warmth"] - regions["earth"]["warmth"]), 6),
        "protected_luma": regions["protect"]["luma"],
        "green_chroma": regions["green"]["chroma"],
        "cool_earth_chroma": round((regions["cool"]["chroma"] + regions["earth"]["chroma"]) / 2, 6),
        "protected_black_luma": round(
            protected_black_luma / max(1, protected_black_count), 6),
        "protected_black_samples": protected_black_count,
        "sample_pixels": count,
        "boundary": "固定逐帧像素代理，只用于同素材方向比较。",
    }


def build_dynamic_masks(source: Path, outputs: dict[str, Path], workdir: Path,
                        analysis_width: int = 960) -> dict:
    frames, probe = video_masks.extract_analysis_frames(source, workdir / "frames", analysis_width)
    report = build_landscape_mask_sequence(frames, workdir / "masks")
    report["source_probe"] = probe
    if report["status"] != "ready":
        return report
    for name in MASK_NAMES:
        report[f"{name}_mask_video"] = video_masks.assemble_mask_video(
            workdir / "masks" / name, outputs[name], probe["fps_fraction"], len(frames))
    return report


def render_layer(base_video: Path, output: Path, filter_chain: str) -> dict:
    if output.exists():
        raise LandscapeCrispGradeError(f"输出已存在，不会覆盖：{output}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
               "-i", str(base_video), "-vf", filter_chain, "-map", "0:v:0", "-an",
               "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
               "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", str(output)]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise LandscapeCrispGradeError(result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command}


def render_foundation(source: Path, output: Path) -> dict:
    """渲染带原音频的一级校正母版，供所有强度独立派生。"""
    if output.exists():
        raise LandscapeCrispGradeError(f"输出已存在，不会覆盖：{output}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n",
               "-i", str(source), "-vf", foundation_filter(), "-map", "0:v:0"]
    audio = _has_audio(source)
    if audio:
        command.extend(["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"])
    command.extend(["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", str(output)])
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise LandscapeCrispGradeError(result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command,
            "audio_preserved": audio}


def build_layered_filter_complex(width: int, height: int, fps_fraction: str) -> str:
    # 同源恒定帧率的层与蒙版按帧序号对齐，避免 MKV 毫秒时基造成错帧。
    rate = Fraction(fps_fraction)
    if rate <= 0:
        raise LandscapeCrispGradeError("分层合成必须提供原片的有效精确帧率")
    clock = f"settb=AVTB,setpts=N*{rate.denominator}/({rate.numerator}*TB)"
    mask = f"scale={width}:{height}:flags=bilinear,format=gbrp,{clock}"
    return (
        f"[0:v]format=gbrp,{clock},split=2[base][restore];"
        f"[1:v]format=gbrp,{clock}[detail];"
        f"[2:v]format=gbrp,{clock}[green];"
        f"[3:v]format=gbrp,{clock}[cool];"
        f"[4:v]format=gbrp,{clock}[earth];"
        f"[5:v]{mask}[mdetail];[base][detail][mdetail]maskedmerge=planes=7[x1];"
        f"[6:v]{mask}[mgreen];[x1][green][mgreen]maskedmerge=planes=7[x2];"
        f"[7:v]{mask}[mcool];[x2][cool][mcool]maskedmerge=planes=7[x3];"
        f"[8:v]{mask}[mearth];[x3][earth][mearth]maskedmerge=planes=7[x4];"
        f"[9:v]{mask}[mprotect];[x4][restore][mprotect]maskedmerge=planes=7,format=yuv420p[outv]"
    )


def render_layered_video(base_video: Path, layer_videos: dict[str, Path],
                         mask_videos: dict[str, Path], output: Path,
                         source_info: dict) -> dict:
    if output.exists():
        raise LandscapeCrispGradeError(f"输出已存在，不会覆盖：{output}")
    ordered = [base_video, *(layer_videos[name] for name in ("detail", "green", "cool", "earth")),
               *(mask_videos[name] for name in ("detail", "green", "cool", "earth", "protect"))]
    command = [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-n"]
    for path in ordered:
        command.extend(["-i", str(path)])
    command.extend(["-filter_complex", build_layered_filter_complex(source_info["width"], source_info["height"], source_info["fps_fraction"]),
                    "-map", "[outv]"])
    if _has_audio(base_video):
        command.extend(["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"])
    frame_count = int(source_info.get("nb_frames") or 0)
    if frame_count <= 0:
        raise LandscapeCrispGradeError("源视频没有可验证帧数")
    command.extend(["-frames:v", str(frame_count), "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709", "-shortest", str(output)])
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise LandscapeCrispGradeError(result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command,
            "audio_preserved": _has_audio(base_video)}


def _has_audio(path: Path) -> bool:
    result = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "a:0",
                             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], capture_output=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def probe(path: Path) -> dict:
    return video_masks._video_probe(path)


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
