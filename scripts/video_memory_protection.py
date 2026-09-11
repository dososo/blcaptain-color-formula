#!/usr/bin/env python3
"""视频动态记忆色保护。

人物区域来自 Apple Vision 的逐帧独立人物分割；近中性白位来自逐帧像素条件。
两者都不依赖固定 HSL 色带，也不复用镜头中点的静态蒙版。

这不是目标身份跟踪：不做光流、不插值、不跨镜头保持身份。逐帧分割出现孤立漏检时，
资格门直接阻断，不用猜测蒙版填洞。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from fractions import Fraction


SCHEMA_VERSION = "4.8.3-phase2d2"
WHITE_LUMA_MIN = 0.68
WHITE_CHROMA_MAX = 0.11
PERSON_MASK_MIN_COVERAGE = 0.001


class VideoMemoryProtectionError(Exception):
    pass


def capabilities_from_report(report: dict) -> set[str]:
    """只把可复核的 ready 蒙版报告转换为资格门能力令牌。"""
    mask_path = Path((report.get("mask_video") or {}).get("path") or "")
    if (report.get("status") == "ready"
            and report.get("capability_granted") is True
            and mask_path.is_file()):
        return {"temporal-memory-color-protection"}
    return set()


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise VideoMemoryProtectionError("缺少 ffmpeg")
    return found


def _dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run([
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(path),
    ], capture_output=True)
    if result.returncode:
        raise VideoMemoryProtectionError(f"无法读取画面尺寸：{path}")
    try:
        stream = json.loads(result.stdout)["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise VideoMemoryProtectionError(f"无效画面尺寸：{path}") from error


def _read_raw(path: Path, pixel_format: str, width: int, height: int) -> bytes:
    result = subprocess.run([
        _ffmpeg(), "-v", "error", "-i", str(path),
        "-vf", f"scale={width}:{height}:flags=bilinear,format={pixel_format}",
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", pixel_format, "-",
    ], capture_output=True)
    if result.returncode or not result.stdout:
        raise VideoMemoryProtectionError(f"无法读取像素：{path}")
    return result.stdout


def _write_pgm(path: Path, width: int, height: int, data: bytes) -> None:
    if len(data) != width * height:
        raise VideoMemoryProtectionError(
            f"蒙版字节数 {len(data)} 与 {width}x{height} 不一致")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P5\n{width} {height}\n255\n".encode("ascii") + data)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _neutral_white_bytes(frame: Path, width: int, height: int) -> bytes:
    rgb = _read_raw(frame, "rgb24", width, height)
    mask = bytearray(width * height)
    for index in range(width * height):
        offset = index * 3
        red, green, blue = rgb[offset:offset + 3]
        high = max(red, green, blue)
        low = min(red, green, blue)
        luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        chroma = (high - low) / 255.0
        mask[index] = 255 if luma >= WHITE_LUMA_MIN and chroma <= WHITE_CHROMA_MAX else 0
    return bytes(mask)


def write_neutral_white_mask(frame: Path, output: Path) -> dict:
    """逐帧近中性高亮安全蒙版；它不是白色物体语义识别。"""
    width, height = _dimensions(frame)
    data = _neutral_white_bytes(frame, width, height)
    _write_pgm(output, width, height, data)
    covered = sum(1 for value in data if value >= 128)
    return {
        "path": str(output),
        "coverage": round(covered / max(1, width * height), 6),
        "luma_min": WHITE_LUMA_MIN,
        "chroma_max": WHITE_CHROMA_MAX,
        "meaning": "逐帧近中性高亮像素；不是白色物体语义分割",
    }


def _payload_dict(payload) -> dict:
    if isinstance(payload, dict):
        return dict(payload)
    try:
        return dict(payload)
    except (TypeError, ValueError) as error:
        raise VideoMemoryProtectionError("语义后端返回了无法读取的结果") from error


def build_mask_sequence(frames: list[Path], backend, mask_dir: Path,
                        require_person: bool = True) -> dict:
    """为每一帧独立生成“人物 ∪ 近中性白位”蒙版并给出资格证据。"""
    if not frames:
        raise VideoMemoryProtectionError("没有输入帧")
    mask_dir.mkdir(parents=True, exist_ok=True)
    width, height = _dimensions(frames[0])
    mask_paths = []
    mask_hashes = []
    person_coverages = []
    white_coverages = []
    person_detected = []
    person_mask_sources = []
    backend_description = None

    for index, frame in enumerate(frames):
        if _dimensions(frame) != (width, height):
            raise VideoMemoryProtectionError("输入帧尺寸不一致")
        per_frame = mask_dir / f"analysis-{index:06d}"
        per_frame.mkdir(parents=True, exist_ok=True)
        payload = _payload_dict(backend.analyze(frame, per_frame))
        backend_description = backend_description or payload.get("backend")
        person = (payload.get("classes") or {}).get("person") or {}
        person_path = Path(person["mask_path"]) if person.get("mask_path") else None
        coverage = float(person.get("coverage") or 0.0)
        detected = bool(person_path and person_path.is_file()
                        and coverage >= PERSON_MASK_MIN_COVERAGE)
        person_source = "person_union" if detected else "none"
        person_bytes = None
        if detected:
            person_bytes = _read_raw(person_path, "gray", width, height)
        else:
            instances = ((payload.get("classes") or {}).get("person_instances") or {})
            instance_paths = [
                Path(item["mask_path"])
                for item in instances.get("instance_masks") or []
                if item.get("mask_path") and Path(item["mask_path"]).is_file()
                and float(item.get("coverage") or 0.0) >= PERSON_MASK_MIN_COVERAGE
            ]
            if instance_paths:
                layers = [_read_raw(path, "gray", width, height) for path in instance_paths]
                person_bytes = bytes(max(layer[pos] for layer in layers)
                                     for pos in range(width * height))
                coverage = sum(1 for value in person_bytes if value >= 128) / (width * height)
                detected = coverage >= PERSON_MASK_MIN_COVERAGE
                person_source = "person_instances_union" if detected else "none"
        person_detected.append(detected)
        person_coverages.append(round(coverage, 6))
        person_mask_sources.append(person_source)
        if person_bytes is None:
            person_bytes = bytes(width * height)
        white_bytes = _neutral_white_bytes(frame, width, height)
        white_coverages.append(round(
            sum(1 for value in white_bytes if value >= 128) / (width * height), 6))
        combined = bytes(max(person_bytes[pos], white_bytes[pos])
                         for pos in range(width * height))
        target = mask_dir / f"mask-{index:06d}.pgm"
        _write_pgm(target, width, height, combined)
        mask_paths.append(str(target))
        mask_hashes.append(_sha256(target))

    first_person = next((index for index, value in enumerate(person_detected) if value), None)
    last_person = next((index for index in range(len(person_detected) - 1, -1, -1)
                        if person_detected[index]), None)
    isolated_gaps = (
        [index for index in range(first_person, last_person + 1)
         if not person_detected[index]]
        if first_person is not None and last_person is not None else []
    )
    reasons = []
    if require_person and not any(person_detected):
        reasons.append("整段没有得到任何逐帧人物蒙版")
    if isolated_gaps:
        reasons.append("人物逐帧分割出现孤立漏检：" + "、".join(map(str, isolated_gaps)))
    status = "blocked" if reasons else "ready"
    describe = backend_description
    if not isinstance(describe, dict) and hasattr(backend, "describe"):
        describe = backend.describe()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "capability_granted": status == "ready",
        "method": "independent-per-frame-segmentation",
        "frame_count": len(frames),
        "dimensions": [width, height],
        "mask_paths": mask_paths,
        "mask_sha256": mask_hashes,
        "person_detected": person_detected,
        "person_mask_sources": person_mask_sources,
        "person_coverage": person_coverages,
        "neutral_white_coverage": white_coverages,
        "isolated_person_gaps": isolated_gaps,
        "backend": describe,
        "reasons": reasons,
        "boundary": (
            "人物蒙版逐帧独立分割；整幅人物蒙版缺失时只接受同帧逐人物实例蒙版的联合结果。"
            "近中性白位逐帧按亮度与综合色度生成。"
            "不做光流插值、不跨镜头保持身份、不把近中性白位称为白色物体语义。"
        ),
    }


def build_precomputed_protection_filter_complex(
        protected_keep: float = 0.25,
        width: int | None = None,
        height: int | None = None) -> str:
    """输入 0/1/2 为 Foundation/Look/蒙版；输出标签为 blprotected。

    Foundation 与 Look 必须先独立渲染。`build_filter()` 内含固定标签；把两条完整链
    拼进同一个 filter_complex 会发生标签碰撞，让强度档位失去真实差异。
    """
    keep = max(0.0, min(1.0, float(protected_keep)))
    mask_chain = "format=gbrp,setpts=PTS-STARTPTS"
    if width and height:
        mask_chain = f"scale={int(width)}:{int(height)}:flags=bilinear,{mask_chain}"
    return (
        "[0:v]format=gbrp,setpts=PTS-STARTPTS[blfoundation];"
        "[1:v]format=gbrp,setpts=PTS-STARTPTS,split=2[blgradedfull][blgradedmix];"
        f"[blfoundation][blgradedmix]blend=all_expr='A*(1-{keep:.4f})+B*{keep:.4f}'[bllimited];"
        f"[2:v]{mask_chain}[blmemorymask];"
        f"[blgradedfull][bllimited][blmemorymask]maskedmerge=planes=7,format=yuv420p[blprotected]"
    )


def _video_probe(path: Path) -> dict:
    result = subprocess.run([
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,color_space,color_transfer,color_primaries",
        "-show_entries", "format=duration", "-of", "json", str(path),
    ], capture_output=True)
    if result.returncode:
        raise VideoMemoryProtectionError(f"无法探测视频：{path}")
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    rate = Fraction(stream.get("avg_frame_rate") or "0/1")
    if rate <= 0:
        raise VideoMemoryProtectionError("视频帧率无效")
    return {
        "width": int(stream["width"]), "height": int(stream["height"]),
        "fps": float(rate), "fps_fraction": f"{rate.numerator}/{rate.denominator}",
        "nb_frames": int(stream["nb_frames"]) if stream.get("nb_frames") else None,
        "duration": float(payload.get("format", {}).get("duration") or 0.0),
        "color_space": stream.get("color_space"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
    }


def extract_analysis_frames(source: Path, frame_dir: Path,
                            analysis_width: int = 960) -> tuple[list[Path], dict]:
    """按源帧率逐帧抽取 Rec.709 分析帧；不把 Log 当显示参照片猜测。"""
    info = _video_probe(source)
    tags = {info["color_space"], info["color_transfer"], info["color_primaries"]}
    if tags != {"bt709"}:
        raise VideoMemoryProtectionError(
            f"本批只接受完整 Rec.709 标签，实际为 {sorted(str(x) for x in tags)}；"
            "Log／HDR 必须先走对应官方输入变换")
    frame_dir.mkdir(parents=True, exist_ok=True)
    target_height = round(info["height"] * analysis_width / info["width"] / 2) * 2
    pattern = frame_dir / "frame-%06d.png"
    command = [
        _ffmpeg(), "-v", "error", "-n", "-i", str(source),
        "-map", "0:v:0", "-vsync", "0",
        "-vf", (
            "zscale=matrixin=709:transferin=709:primariesin=709:"
            "matrix=709:transfer=709:primaries=709,"
            f"format=rgb24,scale={analysis_width}:{target_height}:flags=lanczos"
        ),
        str(pattern),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise VideoMemoryProtectionError(
            "逐帧抽取失败：" + result.stderr.decode("utf-8", "replace").strip())
    frames = sorted(frame_dir.glob("frame-*.png"))
    if not frames:
        raise VideoMemoryProtectionError("没有抽出任何分析帧")
    if info["nb_frames"] is not None and len(frames) != info["nb_frames"]:
        raise VideoMemoryProtectionError(
            f"抽帧数 {len(frames)} 与源 nb_frames={info['nb_frames']} 不一致")
    return frames, {**info, "analysis_dimensions": [analysis_width, target_height],
                    "extract_command": command}


def assemble_mask_video(mask_dir: Path, output: Path, fps_fraction: str,
                        expected_frames: int) -> dict:
    masks = sorted(mask_dir.glob("mask-*.pgm"))
    if len(masks) != expected_frames:
        raise VideoMemoryProtectionError(
            f"蒙版帧数 {len(masks)} 与预期 {expected_frames} 不一致")
    if output.exists():
        raise VideoMemoryProtectionError(f"蒙版视频已存在，不会覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _ffmpeg(), "-v", "error", "-n", "-framerate", fps_fraction,
        "-start_number", "0", "-i", str(mask_dir / "mask-%06d.pgm"),
        "-frames:v", str(expected_frames), "-c:v", "ffv1", "-pix_fmt", "gray",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise VideoMemoryProtectionError(
            "蒙版视频编码失败：" + result.stderr.decode("utf-8", "replace").strip())
    probe = _video_probe(output)
    if probe.get("nb_frames") is not None and probe["nb_frames"] != expected_frames:
        raise VideoMemoryProtectionError("蒙版视频帧数验收失败")
    return {"path": str(output), "sha256": _sha256(output), "command": command,
            "frame_count": expected_frames, "fps_fraction": fps_fraction}


def build_dynamic_mask_video(source: Path, output: Path, workdir: Path,
                             analysis_width: int = 960,
                             require_person: bool = True) -> dict:
    """用当前解释器的 Apple Vision 构建真实逐帧保护蒙版视频。"""
    try:
        import semantic_backend
        entry = next(item for item in semantic_backend.load_registry()["backends"]
                     if item["id"] == "apple-vision")
        backend = semantic_backend.AppleVisionBackend(entry)
    except Exception as error:  # noqa: BLE001
        raise VideoMemoryProtectionError(
            "BLOCKED_ENV：当前解释器无法使用 Apple Vision："
            f"{type(error).__name__}: {error}") from error
    frames, source_probe = extract_analysis_frames(
        source, workdir / "frames", analysis_width)
    report = build_mask_sequence(frames, backend, workdir / "masks", require_person)
    report["source_probe"] = source_probe
    if report["status"] != "ready":
        return report
    report["mask_video"] = assemble_mask_video(
        workdir / "masks", output, source_probe["fps_fraction"], len(frames))
    return report


def render_protected_video(foundation_video: Path, creative_video: Path,
                           mask_video: Path, audio_source: Path, output: Path,
                           protected_keep: float = 0.25) -> dict:
    if output.exists():
        raise VideoMemoryProtectionError(f"输出已存在，不会覆盖：{output}")
    if output.resolve() in {foundation_video.resolve(), creative_video.resolve(),
                            mask_video.resolve(), audio_source.resolve()}:
        raise VideoMemoryProtectionError("输出不得覆盖原片")
    info = _video_probe(audio_source)
    foundation_probe = _video_probe(foundation_video)
    creative_probe = _video_probe(creative_video)
    if (foundation_probe["width"], foundation_probe["height"]) != (
            creative_probe["width"], creative_probe["height"]):
        raise VideoMemoryProtectionError("Foundation 与 Look 画面尺寸不一致")
    graph = build_precomputed_protection_filter_complex(
        protected_keep, info["width"], info["height"])
    command = [
        _ffmpeg(), "-v", "error", "-n", "-i", str(foundation_video),
        "-i", str(creative_video), "-i", str(mask_video), "-i", str(audio_source),
        "-filter_complex", graph, "-map", "[blprotected]", "-map", "3:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "copy", "-movflags", "+faststart", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode or not output.is_file():
        raise VideoMemoryProtectionError(
            "动态保护渲染失败：" + result.stderr.decode("utf-8", "replace").strip())
    return {"path": str(output), "sha256": _sha256(output), "command": command,
            "filter_complex": graph, "protected_keep": protected_keep,
            "probe": _video_probe(output)}
