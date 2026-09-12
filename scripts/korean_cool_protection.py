"""韩系清冷的新素材保护证据；不调色，不调用正式渲染或固定实验合同。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

try:
    from . import semantic_backend, video_memory_protection as memory
except ImportError:
    import semantic_backend
    import video_memory_protection as memory

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "korean-cool-protection-v1"
ENCODING = "full-range-gray8"


def neutral_white_weights(rgb_bytes: bytes) -> bytes:
    """近中性亮部权重；不把像素条件称为白色物体语义。"""
    if len(rgb_bytes) % 3:
        raise ProtectionError("亮部权重只接受完整 rgb24 像素")
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        rgb = np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(-1, 3).astype(np.float64) / 255
        # 不用矩阵乘法；避免特定 NumPy/BLAS 构建虚报浮点异常。
        luma = .2126 * rgb[:, 0] + .7152 * rgb[:, 1] + .0722 * rgb[:, 2]
        chroma = np.max(rgb, axis=1) - np.min(rgb, axis=1)
        light = np.clip((luma - .56) / (.68 - .56), 0, 1)
        neutral = np.clip((.17 - chroma) / (.17 - .11), 0, 1)
        weight = light * light * (3 - 2 * light) * neutral * neutral * (3 - 2 * neutral)
        return np.rint(255 * weight).astype(np.uint8).tobytes()

    def smooth(value):
        value = max(0.0, min(1.0, value))
        return value * value * (3 - 2 * value)

    weights = bytearray()
    for index in range(0, len(rgb_bytes), 3):
        red, green, blue = rgb_bytes[index:index + 3]
        luma = (.2126 * red + .7152 * green + .0722 * blue) / 255
        chroma = (max(red, green, blue) - min(red, green, blue)) / 255
        weights.append(round(255 * smooth((luma - .56) / (.68 - .56)) *
                             smooth((.17 - chroma) / (.17 - .11))))
    return bytes(weights)


def _expand_white_transition(frames: list[Path], report: dict, source: dict,
                             foundation_filter: str) -> None:
    """原片先做 Foundation 再缩图；只扩展白位，不重复人物推理。"""
    width, height = report["dimensions"]
    chain = [foundation_filter, "format=gbrp16le"]
    if source["color"]["profile"] == "display-p3":
        chain.append("zscale=primariesin=smpte432:transferin=iec61966-2-1:matrixin=gbr:rangein=full:"
                     "primaries=709:transfer=13:matrix=gbr:range=full")
    chain += [f"scale={width}:{height}:flags=lanczos", "format=rgb24"]
    command = ["ffmpeg", "-v", "error", "-nostdin", "-filter_complex_threads", "1", "-i", source["path"],
               "-filter_complex", "[0:v]" + ",".join(chain) + "[foundation_analysis]",
               "-map", "[foundation_analysis]", "-an", "-fps_mode", "passthrough",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    mean_weights, foundation_means, hashes = [], [], []
    frame_bytes = width * height * 3
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
        try:
            for index, frame in enumerate(frames):
                foundation_rgb = process.stdout.read(frame_bytes)
                if len(foundation_rgb) != frame_bytes:
                    raise ProtectionError("Foundation 分析缺帧或帧字节尺寸不完整")
                target = Path(report["mask_paths"][index])
                old_union = memory._read_raw(target, "gray", width, height)
                soft_white = neutral_white_weights(memory._read_raw(frame, "rgb24", width, height))
                foundation_white = neutral_white_weights(foundation_rgb)
                combined = bytes(max(a, b, c) for a, b, c in zip(old_union, soft_white, foundation_white))
                memory._write_pgm(target, width, height, combined)
                report["mask_sha256"][index] = _sha(target)
                mean_weights.append(round(sum(soft_white) / (255 * width * height), 6))
                foundation_means.append(round(sum(foundation_white) / (255 * width * height), 6))
                hashes.append(hashlib.sha256(foundation_rgb).hexdigest())
            if process.stdout.read(1):
                raise ProtectionError("Foundation 分析多出帧或尾部字节")
            if process.wait() != 0:
                errors.seek(0)
                raise ProtectionError("Foundation 分析失败：" + errors.read().decode("utf-8", "replace"))
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    report["neutral_white_mean_weight"] = mean_weights
    report["foundation_white_mean_weight"] = foundation_means
    report["foundation_analysis_sha256"] = hashes


class ProtectionError(ValueError):
    """本次没有可安全复用的真实保护证据。"""


def _sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _implementation() -> dict:
    names = ("scripts/korean_cool_protection.py", "scripts/video_memory_protection.py",
             "scripts/semantic_backend.py", "references/model-registry.json")
    return {name: _sha(ROOT / name) for name in names}


def _backend():
    try:
        registry = semantic_backend.load_registry()
        entry = next(item for item in registry["backends"] if item["id"] == "apple-vision")
        if not semantic_backend.license_allowed(registry, entry["license"]):
            raise ProtectionError("人物后端许可证未通过")
        backend = semantic_backend.AppleVisionBackend(entry)
        descriptor = backend.describe()
        descriptor["packages"] = {name: importlib.metadata.version(name) for name in
                                  ("pyobjc-core", "pyobjc-framework-Vision", "pyobjc-framework-Quartz")}
        return backend, descriptor
    except Exception as error:
        raise ProtectionError(f"当前解释器的人物后端不可用：{error}") from error


def _source_binding(source: dict) -> dict:
    path = Path(source["path"]).resolve()
    if not path.is_file() or _sha(path) != source["sha256"]:
        raise ProtectionError("原片缺失或哈希已变化")
    color = source.get("color") or {}
    media = source["media_type"]
    allowed = {"srgb", "display-p3"} if media == "photo" else {"rec709-sdr"}
    if media not in ("photo", "video") or color.get("profile") not in allowed:
        raise ProtectionError("保护证据只接受明确 sRGB/P3 照片或完整 Rec.709 视频")
    if color.get("support") != "direct":
        raise ProtectionError("先完成本次输入的明确色彩解释，再建立人物保护")
    return {"path": str(path), "sha256": source["sha256"], "media_type": media,
            "dimensions": [int(source["width"]), int(source["height"])], "color": color}


def _probe(path: Path, frames: bool = False) -> dict:
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams"]
    if frames:
        command += ["-show_frames", "-show_entries",
                    "stream=width,height,time_base,avg_frame_rate,sample_aspect_ratio,codec_name,pix_fmt:"
                    "stream_side_data=rotation:frame=best_effort_timestamp"]
    result = subprocess.run(command + ["-of", "json", str(path)], capture_output=True, text=True)
    if result.returncode:
        raise ProtectionError("无法解码保护素材：" + result.stderr.strip())
    return json.loads(result.stdout)


def _timeline(path: Path, expected_fps: str | None = None) -> dict:
    data = _probe(path, frames=True)
    stream = data["streams"][0]
    if stream.get("sample_aspect_ratio", "1:1") not in ("1:1", "N/A"):
        raise ProtectionError("非方形像素视频需先规范化")
    if any(float(item.get("rotation", 0)) % 360 for item in stream.get("side_data_list", [])):
        raise ProtectionError("带旋转的视频需先明确显示几何")
    reported_rate = stream.get("avg_frame_rate") or "0/1"
    rate = Fraction(expected_fps or reported_rate)
    base = Fraction(stream["time_base"])
    rows = data.get("frames") or []
    if rate <= 0 or base <= 0 or not rows or any("best_effort_timestamp" not in row for row in rows):
        raise ProtectionError("视频缺少完整可验证的帧时钟")
    pts = [int(row["best_effort_timestamp"]) for row in rows]
    relative = [(value - pts[0]) * base for value in pts]
    if any(b <= a for a, b in zip(relative, relative[1:])):
        raise ProtectionError("视频时间戳重复或不递增")
    # 只容许容器半个时基刻度的舍入；不能吞掉一个完整的缺帧间隔。
    tolerance = base / 2 + Fraction(1, 10**9)
    if any(abs(value - Fraction(index, 1) / rate) > tolerance for index, value in enumerate(relative)):
        raise ProtectionError("变帧率或不连续时间轴不能使用当前逐帧保护")
    return {"fps_fraction": f"{rate.numerator}/{rate.denominator}", "reported_avg_frame_rate": reported_rate,
            "time_base": str(base), "source_start_pts": pts[0],
            "relative_pts": [str(value) for value in relative], "frame_count": len(rows),
            "dimensions": [stream["width"], stream["height"]],
            "codec_name": stream.get("codec_name"), "pix_fmt": stream.get("pix_fmt")}


def _photo_frame(source: dict, target: Path) -> Path:
    width, height = source["width"], source["height"]
    scale = min(1.0, 960 / max(width, height))
    w, h = max(2, round(width * scale)), max(2, round(height * scale))
    chain = []
    if source["color"]["profile"] == "display-p3":
        # 只规范化分析代理；成片与原片仍由主执行链保持 P3。
        chain += ["format=gbrp16le", "zscale=primariesin=smpte432:transferin=iec61966-2-1:matrixin=gbr:rangein=full:"
                  "primaries=709:transfer=13:matrix=gbr:range=full"]
    chain += [f"scale={w}:{h}:flags=lanczos", "format=rgb24"]
    result = subprocess.run(["ffmpeg", "-v", "error", "-n", "-i", source["path"],
                             "-vf", ",".join(chain), "-frames:v", "1", str(target)], capture_output=True)
    if result.returncode:
        raise ProtectionError("照片分析代理生成失败：" + result.stderr.decode("utf-8", "replace"))
    return target


def prepare(source: dict, workdir: Path, foundation_filter: str) -> dict:
    """生成源绑定的人物∪近中性亮部证据；没有人物或任何漏帧都拒绝。"""
    if not isinstance(foundation_filter, str) or not foundation_filter.strip():
        raise ProtectionError("必须提供本次真实 Foundation 滤镜；无校正时明确传 null 字符串")
    binding = _source_binding(source)
    backend, descriptor = _backend()
    implementation = _implementation()
    timeline = _timeline(Path(binding["path"])) if binding["media_type"] == "video" else None
    if timeline and timeline["dimensions"] != binding["dimensions"]:
        raise ProtectionError("视频探针与源几何不一致")
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=False)
    if timeline:
        width, height = binding["dimensions"]
        analysis_width = max(2, int(min(width, 960, 960 * width / height)) // 2 * 2)
        frames, _ = memory.extract_analysis_frames(Path(binding["path"]), workdir / "frames", analysis_width)
        if len(frames) != timeline["frame_count"]:
            raise ProtectionError("分析帧与完整源时间轴数量不一致")
    else:
        frames = [_photo_frame(source, workdir / "analysis.png")]
    report = memory.build_mask_sequence(frames, backend, workdir / "masks", require_person=True)
    if report["status"] != "ready" or not all(report["person_detected"]):
        raise ProtectionError("人物保护未覆盖全部帧，停止本次处理：" + "；".join(report["reasons"]))
    _expand_white_transition(frames, report, source, foundation_filter)
    if timeline:
        artifact = memory.assemble_mask_video(workdir / "masks", workdir / "protection.mkv",
                                               timeline["fps_fraction"], len(frames))
        mask_timeline = _timeline(Path(artifact["path"]), timeline["fps_fraction"])
        if mask_timeline["frame_count"] != len(frames):
            raise ProtectionError("编码后蒙版丢帧")
        path = Path(artifact["path"])
    else:
        mask_timeline = None
        path = Path(report["mask_paths"][0])
    mask = {"path": str(path), "sha256": _sha(path), "encoding": ENCODING,
            "dimensions": report["dimensions"], "frame_count": len(frames), "timeline": mask_timeline}
    evidence = {"schema_version": SCHEMA, "status": "ready", "source_binding": binding,
                "implementation": implementation, "backend": descriptor,
                "analysis_dimensions": report["dimensions"], "mask": mask, "timeline": timeline,
                "person_coverage": report["person_coverage"],
                "neutral_white_coverage": report["neutral_white_coverage"],
                "neutral_white_mean_weight": report["neutral_white_mean_weight"],
                "foundation_filter": foundation_filter,
                "foundation_analysis_sha256": report["foundation_analysis_sha256"],
                "foundation_white_mean_weight": report["foundation_white_mean_weight"],
                "neutral_white_rule": {"method": "smoothstep-product", "luma_transition": [.56, .68],
                                       "chroma_transition": [.11, .17], "original_core_preserved": True,
                                       "domains": ["source", "foundation"]},
                "person_mask_sources": report["person_mask_sources"],
                "method": "independent-per-frame-segmentation", "human_review": "pending",
                "boundary": "逐帧独立人物分割与近中性亮部阈值；非身份跟踪、非白色物体语义。需检查发丝、手指、遮挡和完整时序。"}
    evidence["fingerprint"] = _fingerprint(evidence)
    validate(evidence, source)
    (workdir / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence


def validate(evidence: dict, source: dict) -> None:
    """重核源、实现、后端、编码与完整帧时间轴；存在一个文件不等于证据有效。"""
    if evidence.get("schema_version") != SCHEMA or evidence.get("status") != "ready":
        raise ProtectionError("保护证据未就绪")
    if evidence.get("fingerprint") != _fingerprint({k: v for k, v in evidence.items() if k != "fingerprint"}):
        raise ProtectionError("保护证据内容发生变化")
    if evidence.get("source_binding") != _source_binding(source):
        raise ProtectionError("保护证据不是本次源素材")
    if evidence.get("implementation") != _implementation():
        raise ProtectionError("保护模块或模型登记已变化，需重建证据")
    _, descriptor = _backend()
    if evidence.get("backend") != descriptor:
        raise ProtectionError("人物后端或系统版本已变化，需重建证据")
    mask = evidence["mask"]
    path = Path(mask["path"])
    if mask.get("encoding") != ENCODING or not path.is_file() or _sha(path) != mask["sha256"]:
        raise ProtectionError("蒙版文件缺失、改变或编码语义不可信")
    stream = _probe(path)["streams"][0]
    if stream.get("pix_fmt") != "gray" or [stream["width"], stream["height"]] != mask["dimensions"]:
        raise ProtectionError("蒙版必须是保持原权重的单通道灰度且几何一致")
    if evidence["analysis_dimensions"] != mask["dimensions"]:
        raise ProtectionError("分析几何与蒙版不一致")
    if (not isinstance(evidence.get("foundation_filter"), str) or not evidence["foundation_filter"].strip()
            or len(evidence.get("foundation_analysis_sha256", [])) != mask["frame_count"]):
        raise ProtectionError("Foundation 滤镜或逐帧分析绑定不完整")
    if source["media_type"] == "video":
        if evidence.get("timeline") != _timeline(Path(source["path"])):
            raise ProtectionError("原片完整时间轴发生变化")
        actual = _timeline(path, evidence["timeline"]["fps_fraction"])
        if stream.get("codec_name") != "ffv1" or actual != mask["timeline"]:
            raise ProtectionError("视频蒙版不是绑定的无损完整时间轴")
        if actual["frame_count"] != evidence["timeline"]["frame_count"] or actual["fps_fraction"] != evidence["timeline"]["fps_fraction"]:
            raise ProtectionError("视频蒙版与原片帧数或帧率不一致")
    elif mask["frame_count"] != 1 or evidence.get("timeline") is not None:
        raise ProtectionError("照片保护必须是单帧")


def validate_output_timeline(evidence: dict, output: Path) -> None:
    """检查预演/成片的全部视频帧；不检查音频，不用时长推算帧数。"""
    source = evidence.get("timeline")
    if not source:
        raise ProtectionError("照片没有视频时间轴合同")
    actual = _timeline(Path(output), source["fps_fraction"])
    if actual["source_start_pts"] != 0 or actual["frame_count"] != source["frame_count"]:
        raise ProtectionError("输出视频首帧不是零时刻或完整帧数不一致")
    tolerance = (Fraction(actual["time_base"]) + Fraction(source["time_base"])) / 2 + Fraction(1, 10**9)
    if any(abs(Fraction(a) - Fraction(b)) > tolerance
           for a, b in zip(actual["relative_pts"], source["relative_pts"])):
        raise ProtectionError("输出视频完整相对时间轴与原片不一致")
