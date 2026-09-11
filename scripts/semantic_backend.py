#!/usr/bin/env python3
"""统一语义后端接口与能力探测。

铁律：
- 没有 supported 后端的类别一律返回 unavailable，绝不用固定 HSL 宽色带冒充语义蒙版。
- 每次输出都携带后端名、版本、许可证、设备、置信度与降级原因，进入 plan 指纹。
- 后端不可用时整条语义路径关闭并回退 L0，不静默降级。

本模块在没有 pyobjc 的机器上也必须能被导入并给出诚实的 unavailable。
"""

from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "references" / "model-registry.json"
BACKEND_SCHEMA_VERSION = "4.0.0"
LOW_CONFIDENCE = 0.55


def class_presence(payload: dict | None, class_name: str) -> bool | None:
    """保留三态：存在／不存在／本次没有足够证据判断。"""
    value = (((payload or {}).get("classes") or {}).get(class_name) or {}).get("present")
    return value if value is None else bool(value)


def derived_skin_state(mask_dir: Path | None, derived: dict | None,
                       face_count: int, person_coverage: float) -> dict:
    """区分「没生成肤色蒙版」与「肤色类别不存在」。"""
    if mask_dir is None:
        present = None
        probe_status = "prerequisites-detected-mask-not-generated"
    elif derived:
        present = True
        probe_status = "mask-generated"
    else:
        present = False
        probe_status = "mask-generation-failed"
    return {
        "status": "derived",
        "present": present,
        "probe_status": probe_status,
        "mask_path": (derived or {}).get("mask_path"),
        "coverage": (derived or {}).get("coverage", 0.0),
        "basis": "人脸框 ∩ 人物蒙版 ∩ 受限肤色似然像素",
        "face_count": face_count,
        "person_coverage": person_coverage,
        "needs_human_review": True,
        "boundary": "派生结果，不是训练得到的肤色分割；必须人工确认后才能做肤色定向调整",
    }


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def license_allowed(registry: dict, license_id: str) -> bool:
    return license_id in registry["license_gate"]["allowed"]


def _os_build() -> str:
    try:
        version = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip()
        build = subprocess.run(["sw_vers", "-buildVersion"], capture_output=True, text=True).stdout.strip()
        return f"macOS {version} ({build})"
    except OSError:
        return platform.platform()



# 肤色似然的边界。**这不是肤色识别**，只是把「人脸框 ∩ 人物蒙版」里
# 明显不可能是皮肤的像素（头发、深色衣物、背景漏进来的部分）剔掉。
# 判据用 OKLCh：暖色相带 + 有下限的彩度 + 中间明度区间。
# 取值刻意宽松——宁可多留一点衣物，也不要因为收得太紧而把深肤色整片切掉。
SKIN_HUE_RANGE = (15.0, 80.0)
SKIN_MIN_CHROMA = 0.015
SKIN_LIGHTNESS_RANGE = (0.08, 0.95)


def unique_face_instance_matches(score_matrix: list[list[float]],
                                 min_overlap: float = 0.15) -> list[dict]:
    """按人脸框与实例蒙版的重叠度做一对一归属。

    Apple Vision 的人物实例蒙版可能互相重叠。若每个实例都直接拿全部人脸框求交，
    同一张脸会进入多张所谓「逐人」肤色蒙版。这里用全局候选从高到低匹配，
    保证一个实例至多一张脸、一张脸也至多属于一个实例。
    """
    candidates = []
    for instance_index, row in enumerate(score_matrix):
        for face_index, score in enumerate(row):
            if float(score) >= min_overlap:
                candidates.append((float(score), instance_index, face_index))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_instances: set[int] = set()
    used_faces: set[int] = set()
    matches = []
    for score, instance_index, face_index in candidates:
        if instance_index in used_instances or face_index in used_faces:
            continue
        used_instances.add(instance_index)
        used_faces.add(face_index)
        matches.append({"instance_index": instance_index, "face_index": face_index,
                        "overlap": round(score, 4)})
    return sorted(matches, key=lambda item: item["instance_index"])


def _face_overlap_scores(mask_path: Path, face_boxes: list, grid: int = 512) -> list[float]:
    """读取一张实例蒙版，计算它在每个人脸框内部的覆盖比例。"""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(mask_path)],
        capture_output=True, text=True,
    )
    try:
        sw, sh = [int(x) for x in probe.stdout.strip().split(",")[:2]]
    except Exception:
        return [0.0 for _ in face_boxes]
    scale = grid / max(sw, sh)
    w, h = max(16, int(round(sw * scale))), max(16, int(round(sh * scale)))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mask_path), "-vf",
         f"scale={w}:{h}:flags=area,format=gray", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        capture_output=True,
    ).stdout
    if len(raw) < w * h:
        return [0.0 for _ in face_boxes]
    scores = []
    for box in face_boxes:
        x0 = max(0, min(w - 1, int(box["x"] * w)))
        x1 = max(x0 + 1, min(w, int((box["x"] + box["w"]) * w)))
        y0 = max(0, min(h - 1, int(box["y"] * h)))
        y1 = max(y0 + 1, min(h, int((box["y"] + box["h"]) * h)))
        total = (x1 - x0) * (y1 - y0)
        covered = sum(1 for y in range(y0, y1) for x in range(x0, x1)
                      if raw[y * w + x] >= 128)
        scores.append(covered / max(1, total))
    return scores


def _match_faces_to_instance_masks(instance_masks: list[dict], face_boxes: list) -> list[dict]:
    matrix = [_face_overlap_scores(Path(item["mask_path"]), face_boxes)
              for item in instance_masks]
    return unique_face_instance_matches(matrix)


def _write_skin_mask(source: Path, person_mask_png: Path, face_boxes: list,
                     out_path: Path, grid: int = 512) -> dict | None:
    """人脸框 ∩ 人物蒙版 ∩ 受限肤色似然像素 → 一张真实的肤色蒙版 PNG。

    此前 skin 类只是一个「derived」的**声明**，从来没有任何代码把蒙版写出来；
    于是 skin_tolerance 门与局部候选生成都因为拿不到 mask_path 而永远跳过。
    这里补上真正的求交。

    刻意不做的事：不按固定橙色 HSL 宽带圈选（D4），
    不做任何肤型或族裔分类（D5）——只在几何与实例给定的区域内筛像素。
    """
    import subprocess
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from color_space import rgb8_to_oklab, oklab_to_oklch

    def _raw(path, w, h):
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                            f"format=rgb24,scale={w}:{h}:flags=area", "-frames:v", "1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                           capture_output=True)
        return r.stdout

    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0",
                            str(source)], capture_output=True, text=True)
    try:
        sw, sh = [int(x) for x in probe.stdout.strip().split(",")[:2]]
    except Exception:
        return None
    scale = (grid / max(sw, sh))
    w = max(16, int(round(sw * scale)))
    h = max(16, int(round(sh * scale)))

    src = _raw(source, w, h)
    person = _raw(person_mask_png, w, h)
    if len(src) < w * h * 3 or len(person) < w * h * 3:
        return None

    out = bytearray(w * h)
    kept = 0
    lo_h, hi_h = SKIN_HUE_RANGE
    lo_l, hi_l = SKIN_LIGHTNESS_RANGE
    for y in range(h):
        ny = (y + 0.5) / h
        in_any_face = False
        rows = [(b["x"], b["y"], b["w"], b["h"]) for b in face_boxes]
        for i in range(w):
            idx = (y * w + i)
            if person[idx * 3] < 128:          # 不在人物/实例蒙版内
                continue
            nx = (i + 0.5) / w
            # 人脸框放大 1.6 倍，把颈部与耳侧也纳入；仍受人物蒙版约束。
            inside = False
            for bx, by, bw, bh in rows:
                cx, cy = bx + bw / 2.0, by + bh / 2.0
                if abs(nx - cx) <= bw * 0.8 and abs(ny - cy) <= bh * 0.8:
                    inside = True
                    break
            if not inside:
                continue
            r, g, b = src[idx * 3], src[idx * 3 + 1], src[idx * 3 + 2]
            L, a, bb = rgb8_to_oklab(r, g, b)
            light, chroma, hue = oklab_to_oklch(L, a, bb)
            if chroma < SKIN_MIN_CHROMA:
                continue
            if not (lo_h <= hue <= hi_h):
                continue
            if not (lo_l <= light <= hi_l):
                continue
            out[idx] = 255
            kept += 1
    if kept < 64:
        return None
    coverage = kept / float(w * h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                           "-pix_fmt", "gray", "-s", f"{w}x{h}", "-i", "pipe:0",
                           "-vf", f"scale={sw}:{sh}:flags=neighbor",
                           "-pix_fmt", "gray", str(out_path)],
                          input=bytes(out), capture_output=True)
    if proc.returncode or not out_path.exists():
        return None
    return {"mask_path": str(out_path), "coverage": round(coverage, 6), "pixels": kept}


class SemanticResult(dict):
    """始终包含 available / backend / degraded_reason 三个字段。"""


class NullBackend:
    id = "null"

    def __init__(self, reason: str):
        self.reason = reason

    def describe(self) -> dict:
        return {
            "backend": "null",
            "available": False,
            "reason": self.reason,
            "class_support": {},
        }

    def analyze(self, path: Path) -> SemanticResult:
        return SemanticResult({
            "schema_version": BACKEND_SCHEMA_VERSION,
            "available": False,
            "backend": self.describe(),
            "degraded_reason": self.reason,
            "classes": {},
            "capability_level": "L0-only",
        })


class AppleVisionBackend:
    id = "apple-vision"

    def __init__(self, entry: dict):
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
        self.entry = entry
        self._Vision = Vision
        self._Quartz = Quartz

    # ---- 基础工具 -------------------------------------------------------
    def _load(self, path: Path):
        from Foundation import NSURL
        Quartz = self._Quartz
        url = NSURL.fileURLWithPath_(str(path))
        source = Quartz.CGImageSourceCreateWithURL(url, None)
        if source is None:
            raise RuntimeError(f"无法解码图像：{path}")
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        return image, Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)

    def _request(self, name: str):
        cls = getattr(self._Vision, name)
        return cls.alloc().initWithCompletionHandler_(None)

    def _mask_stats(self, pixel_buffer, grid: int = 48) -> dict:
        """网格化统计覆盖率与置信分布，避免逐像素 Python 循环。"""
        Quartz = self._Quartz
        Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)
        try:
            width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
            height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
            stride = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
            base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
            data = memoryview(base.as_buffer(stride * height))
            step_x = max(1, width // grid)
            step_y = max(1, height // grid)
            total = 0
            hard = 0
            soft = 0
            accum = 0
            edge = 0
            previous_row = None
            for y in range(0, height, step_y):
                row_values = []
                row_base = y * stride
                for x in range(0, width, step_x):
                    value = data[row_base + x]
                    row_values.append(value)
                    accum += value
                    total += 1
                    if value >= 200:
                        hard += 1
                    elif value >= 60:
                        soft += 1
                if previous_row and len(previous_row) == len(row_values):
                    edge += sum(1 for a, b in zip(previous_row, row_values) if abs(a - b) > 90)
                previous_row = row_values
            if not total:
                return {"coverage": 0.0, "soft_edge_ratio": 0.0, "mean": 0.0, "mask_size": [width, height]}
            return {
                "coverage": round(hard / total, 4),
                "soft_edge_ratio": round(soft / total, 4),
                "boundary_activity": round(edge / total, 4),
                "mean": round(accum / total / 255.0, 4),
                "mask_size": [width, height],
                "sample_grid": [len(previous_row or []), total // max(1, len(previous_row or [1]))],
            }
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)

    def export_mask_png(self, pixel_buffer, out_path: Path) -> bool:
        """用 CoreImage 把 CVPixelBuffer 落盘为灰度 PNG，供 ffmpeg 作为蒙版使用。"""
        Quartz = self._Quartz
        from Foundation import NSURL
        try:
            ci_image = Quartz.CIImage.imageWithCVImageBuffer_(pixel_buffer)
            if ci_image is None:
                return False
            context = Quartz.CIContext.contextWithOptions_(None)
            cg_image = context.createCGImage_fromRect_(ci_image, ci_image.extent())
            if cg_image is None:
                return False
            url = NSURL.fileURLWithPath_(str(out_path))
            destination = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
            if destination is None:
                return False
            Quartz.CGImageDestinationAddImage(destination, cg_image, None)
            return bool(Quartz.CGImageDestinationFinalize(destination))
        except Exception:  # noqa: BLE001
            return False

    # ---- 描述 -----------------------------------------------------------
    def describe(self) -> dict:
        return {
            "backend": self.entry["id"],
            "name": self.entry["name"],
            "available": True,
            "license": self.entry["license"],
            "license_note": self.entry["license_note"],
            "weights_hash": self.entry["weights_hash"],
            "weights_hash_note": self.entry["weights_hash_note"],
            "os_build": _os_build(),
            "device": self.entry["device"],
            "offline": self.entry["offline"],
            "class_support": self.entry["class_support"],
        }

    # ---- 分析 -----------------------------------------------------------
    def analyze(self, path: Path, mask_dir: Path | None = None) -> SemanticResult:
        Vision = self._Vision
        Quartz = self._Quartz
        image, width, height = self._load(path)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        classes: dict[str, dict] = {}
        warnings: list[str] = []

        # 人脸
        face_request = self._request("VNDetectFaceRectanglesRequest")
        handler.performRequests_error_([face_request], None)
        faces = list(face_request.results() or [])
        face_boxes = []
        for face in faces:
            box = face.boundingBox()
            # Vision 原点在左下；转换为图像坐标（原点左上）。
            face_boxes.append({
                "x": round(box.origin.x, 4),
                "y": round(1.0 - box.origin.y - box.size.height, 4),
                "w": round(box.size.width, 4),
                "h": round(box.size.height, 4),
                "confidence": round(float(face.confidence()), 3),
            })
        min_face_conf = min((b["confidence"] for b in face_boxes), default=1.0)
        classes["face"] = {
            "status": "supported",
            # present 表示「这张图里确实有」，与 status（后端是否支持该类别）严格区分。
            # 早期实现只看 status，导致一张没有人的城市照片也被允许选「人物塑光」。
            "present": bool(face_boxes) and min_face_conf >= LOW_CONFIDENCE,
            "count": len(face_boxes),
            "boxes": face_boxes,
            "min_confidence": round(min_face_conf, 3),
            "needs_human_review": bool(face_boxes) and min_face_conf < LOW_CONFIDENCE,
        }
        if len(face_boxes) > 1:
            warnings.append(f"检测到 {len(face_boxes)} 张人脸；多人肤色差异必须逐人确认，不能用一套肤色偏移")

        # 人物分割
        person_request = self._request("VNGeneratePersonSegmentationRequest")
        person_request.setQualityLevel_(Vision.VNGeneratePersonSegmentationRequestQualityLevelAccurate)
        person_request.setOutputPixelFormat_(Quartz.kCVPixelFormatType_OneComponent8)
        handler.performRequests_error_([person_request], None)
        person_results = list(person_request.results() or [])
        if person_results:
            buffer = person_results[0].pixelBuffer()
            stats = self._mask_stats(buffer)
            mask_path = None
            if mask_dir is not None and stats["coverage"] > 0.001:
                mask_dir.mkdir(parents=True, exist_ok=True)
                candidate = mask_dir / f"{path.stem}__person_mask.png"
                if self.export_mask_png(buffer, candidate):
                    mask_path = str(candidate)
            classes["person"] = {
                "status": "supported",
                "present": stats["coverage"] >= 0.02,
                "presence_threshold": 0.02,
                "mask_path": mask_path,
                "needs_human_review": stats["coverage"] < 0.005 and bool(face_boxes),
                **stats,
            }
            if stats.get("boundary_activity", 0) > 0.22:
                warnings.append("人物蒙版边界活跃度较高，可能存在头发或遮挡；局部调色前需检查边缘污染")
        else:
            classes["person"] = {"status": "no-detection", "present": False, "coverage": 0.0}

        # 人物实例：多人时每人一个实例，是逐人肤色处理的前提。
        # 注意：本机现有的全部授权素材最多只有 1 人，因此多人路径**未经真实验证**。
        # 未验证的能力不得自动执行——检测到多人时必须降级为人工确认。
        try:
            instance_request = self._request("VNGeneratePersonInstanceMaskRequest")
            handler.performRequests_error_([instance_request], None)
            instance_results = list(instance_request.results() or [])
            if instance_results:
                observation = instance_results[0]
                instances = observation.allInstances()
                count = int(instances.count())

                # 逐实例蒙版。此前这里只取了实例**数量**，从未调用
                # generateScaledMaskForImageForInstances 把单个实例的蒙版取出来，
                # 于是多人路径一直停在「有几个人」，做不了「分别处理每个人」。
                # 实测在一张真实三人合影上，三个实例各自返回 1800x1800 的独立蒙版。
                instance_masks = []
                if mask_dir is not None and count >= 1:
                    from Foundation import NSIndexSet
                    mask_dir.mkdir(parents=True, exist_ok=True)
                    for slot, idx in enumerate(list(instances), start=1):
                        try:
                            one = NSIndexSet.indexSetWithIndex_(int(idx))
                            buf, _err = (observation
                                         .generateScaledMaskForImageForInstances_fromRequestHandler_error_(
                                             one, handler, None))
                            if buf is None:
                                continue
                            stats_i = self._mask_stats(buf)
                            target = mask_dir / f"{path.stem}__person_instance_{slot}.png"
                            if self.export_mask_png(buf, target):
                                instance_masks.append({
                                    "instance": slot,
                                    "mask_path": str(target),
                                    "coverage": stats_i.get("coverage"),
                                    "centroid_x": stats_i.get("centroid_x"),
                                    "centroid_y": stats_i.get("centroid_y"),
                                })
                        except Exception:  # noqa: BLE001
                            continue

                validated = count <= 1 or bool(instance_masks)
                classes["person_instances"] = {
                    "status": "supported" if validated else "unvalidated-multi-person",
                    "present": count > 0,
                    "count": count,
                    "instance_masks": instance_masks,
                    "needs_human_review": count > 1,
                    "validation_note": (
                        f"多人：已逐实例取出 {len(instance_masks)} 个独立蒙版，"
                        "可分别处理每个人；仍需用户确认后才执行"
                        if count > 1 and instance_masks else
                        ("多人但逐实例蒙版取不到（未提供 mask_dir 或取蒙版失败），"
                         "本次不自动分人处理" if count > 1 else "单人，已验证路径")
                    ),
                }
                if count > 1 and not instance_masks:
                    warnings.append(
                        f"多人画面：检测到 {count} 个人物实例，但本次未能取出逐实例蒙版"
                        "（探测阶段没有输出目录时不生成蒙版）。"
                        "不同肤色、不同受光的人不能共用一套肤色偏移，本次不自动分人处理。"
                    )
                elif count > 1:
                    warnings.append(
                        f"多人画面：检测到 {count} 个人物实例，已分别取出各自蒙版，"
                        "可以逐人分别处理。逐人肤色处理仍需你确认后才执行。"
                    )
            else:
                classes["person_instances"] = {"status": "no-detection", "present": False, "count": 0}
        except Exception as error:  # noqa: BLE001
            classes["person_instances"] = {
                "status": "unavailable", "present": False,
                "reason": f"人物实例分割不可用：{type(error).__name__}",
            }

        # 显著前景实例
        foreground_request = self._request("VNGenerateForegroundInstanceMaskRequest")
        handler.performRequests_error_([foreground_request], None)
        foreground_results = list(foreground_request.results() or [])
        if foreground_results:
            observation = foreground_results[0]
            instance_count = int(observation.allInstances().count())
            classes["salient_foreground"] = {
                "status": "supported",
                "present": instance_count > 0,
                "instance_count": instance_count,
            }
        else:
            classes["salient_foreground"] = {"status": "no-detection", "present": False, "instance_count": 0}

        # 注意力显著性
        saliency_request = self._request("VNGenerateAttentionBasedSaliencyImageRequest")
        handler.performRequests_error_([saliency_request], None)
        saliency_results = list(saliency_request.results() or [])
        if saliency_results:
            observation = saliency_results[0]
            objects = list(observation.salientObjects() or [])
            regions = []
            for item in objects:
                box = item.boundingBox()
                regions.append({
                    "x": round(box.origin.x, 4),
                    "y": round(1.0 - box.origin.y - box.size.height, 4),
                    "w": round(box.size.width, 4),
                    "h": round(box.size.height, 4),
                    "confidence": round(float(item.confidence()), 3),
                })
            classes["attention"] = {"status": "supported", "present": bool(regions), "regions": regions}
        else:
            classes["attention"] = {"status": "no-detection", "present": False, "regions": []}

        # 水平线（供 L1 水平校正建议使用）
        horizon_request = self._request("VNDetectHorizonRequest")
        handler.performRequests_error_([horizon_request], None)
        horizon_results = list(horizon_request.results() or [])
        horizon = None
        if horizon_results:
            horizon = round(float(horizon_results[0].angle()) * 57.29577951308232, 3)

        # 场景分类（只做上下文，不作为蒙版）
        classify_request = self._request("VNClassifyImageRequest")
        handler.performRequests_error_([classify_request], None)
        labels = sorted(list(classify_request.results() or []), key=lambda o: -o.confidence())[:10]
        scene_labels = [
            {"label": item.identifier(), "confidence": round(float(item.confidence()), 3)}
            for item in labels if item.confidence() >= 0.05
        ]

        # 肤色：派生，不是独立分割模型
        instance_count = classes.get("person_instances", {}).get("count", 0)
        multi_person = instance_count > 1 or len(face_boxes) > 1
        # 「画面里有多个人」这件事独立于「人物蒙版是否成功」。
        # 实测 30~40 人的超大群像上，人物分割会退化（实例只认出 4 个、人脸只认出 2 张），
        # 早期实现把多人判定嵌在「蒙版成功」分支里，于是这类素材落到了普通 unavailable——
        # 结果虽然同样安全，但给用户的理由是错的。
        if multi_person:
            _inst = classes.get("person_instances") or {}
            _masks = _inst.get("instance_masks") or []
            if _masks:
                # 逐实例蒙版可用时，多人不再整体停用，而是逐人派生。
                # 关键是每个人的参考值取自他自己——共用一套偏移会把
                # 不同肤色、不同受光的人一起拉向同一个中间值。
                _skin_inst = []
                _matches = _match_faces_to_instance_masks(_masks, face_boxes)
                _by_instance = {item["instance_index"]: item for item in _matches}
                if mask_dir is not None:
                    for mask_index, m in enumerate(_masks):
                        match = _by_instance.get(mask_index)
                        if match is None:
                            continue
                        face_index = match["face_index"]
                        got = _write_skin_mask(
                            path, Path(m["mask_path"]), [face_boxes[face_index]],
                            mask_dir / f"{path.stem}__skin_instance_{m['instance']}.png")
                        if got:
                            _skin_inst.append({**m, "skin_mask_path": got["mask_path"],
                                               "skin_coverage": got["coverage"],
                                               "face_index": face_index,
                                               "face_instance_overlap": match["overlap"]})
                complete = (len(_skin_inst) == min(len(_masks), len(face_boxes)))
                classes["skin"] = {
                    "status": ("derived-per-instance" if complete
                               else "derived-per-instance-partial"),
                    "present": bool(_skin_inst),
                    "per_instance": True,
                    "instance_count": len(_skin_inst),
                    "instances": _skin_inst or _masks,
                    "mask_path": (_skin_inst[0]["skin_mask_path"] if _skin_inst else None),
                    "coverage": (sum(x["skin_coverage"] for x in _skin_inst) if _skin_inst else 0.0),
                    "face_count": len(face_boxes),
                    "matched_face_count": len(_matches),
                    "unmatched_face_count": max(0, len(face_boxes) - len(_matches)),
                    "unmatched_instance_count": max(0, len(_masks) - len(_matches)),
                    "basis": "人脸与人物实例先按覆盖度一对一归属，再做逐实例人物蒙版 ∩ 人脸几何 ∩ 受限肤色似然像素；"
                             "每个人的参考值取自他自己，不与其他人合并",
                    "needs_human_review": True,
                    "boundary": "派生结果，不是训练得到的肤色分割；"
                                "必须人工确认后才能做肤色定向调整；不做任何肤型或族裔分类",
                }
                if not complete:
                    warnings.append(
                        f"逐人肤色蒙版只完成 {len(_skin_inst)}/{min(len(_masks), len(face_boxes))} 个唯一匹配；"
                        "未唯一匹配的人不执行肤色定向调整。"
                    )
            else:
                    classes["skin"] = {
                    "status": "unavailable-multi-person",
                    "present": None if mask_dir is None else False,
                    "probe_status": ("prerequisites-detected-mask-not-generated"
                                     if mask_dir is None else "instance-mask-generation-failed"),
                    "face_count": len(face_boxes),
                    "instance_count": instance_count,
                    "reason": (
                    f"画面中检测到多个人物（实例 {instance_count}、人脸 {len(face_boxes)}）。"
                    "不同肤色与不同受光的人不能共用一套肤色偏移，"
                    "而逐人肤色路径尚未在真实多人素材上验证，因此本次不提供肤色语义处理。"
                ),
                    "unblock_requirement": "需要真实多人（含不同肤色）素材完成验证后才能启用",
            }
        elif classes["face"].get("present") and classes["person"].get("present"):
            _single = None
            if mask_dir is not None and classes["person"].get("mask_path"):
                _single = _write_skin_mask(
                    path, Path(classes["person"]["mask_path"]), face_boxes,
                    mask_dir / f"{path.stem}__skin_mask.png")
            classes["skin"] = derived_skin_state(
                mask_dir, _single, len(face_boxes), classes["person"]["coverage"])
        else:
            classes["skin"] = {
                "status": "unavailable",
                "present": False,
                "probe_status": "prerequisites-not-detected",
                "reason": "未同时检测到人脸与人物蒙版；不得用固定橙色 HSL 冒充肤色",
            }

        for missing in ("sky", "vegetation", "building", "product"):
            classes[missing] = {
                "status": "unavailable",
                "present": None,
                "probe_status": "backend-unavailable",
                "reason": "Apple Vision 不提供该类别；需要已登记且许可证通过的语义分割后端",
            }

        return SemanticResult({
            "schema_version": BACKEND_SCHEMA_VERSION,
            "available": True,
            "backend": self.describe(),
            "degraded_reason": None,
            "image_size": [width, height],
            "classes": classes,
            "horizon_angle_deg": horizon,
            "scene_labels": scene_labels,
            "warnings": warnings,
            "capability_level": "L2-partial",
            "boundary": "语义结果只用于建立蒙版与人工验收重点；天空／植被／建筑／商品当前不可用。",
        })


class CompositeBackend:
    """把 Apple Vision 与 ADE20K 两个后端合成一个统一视图。

    分工是硬的，不重叠也不互相替代：
    - Apple Vision：人物、人脸、显著前景、肤色（derived）、人物实例
    - ADE20K：天空、植被、建筑、商品

    任何一侧不可用时，另一侧照常工作，缺的类别如实标 unavailable 并写明原因。
    绝不用另一侧的结果去猜缺失类别。
    """

    id = "composite"

    def __init__(self, vision=None, ade=None, ade_reason: str = ""):
        self._vision = vision
        self._ade = ade
        self._ade_reason = ade_reason

    def describe(self) -> dict:
        parts = []
        if self._vision is not None:
            parts.append(self._vision.describe())
        if self._ade is not None:
            parts.append(self._ade.describe())
        # capability_report 读的是 class_support，组合后端必须把成员的支持情况汇总上来，
        # 否则逐类状态会全部显示 unavailable——明明能用却报不能用。
        support: dict[str, str] = {}
        if self._vision is not None:
            support.update(self._vision.describe().get("class_support", {}))
        if self._ade is not None:
            for name in self._ade.describe().get("classes", []):
                support[name] = "supported"
        # 组合后端必须保留成员的**完整顶层契约**，而不是挑几个字段搬过来。
        # 这条教训已经付过一次学费：当时只补了 analyze 载荷的 scene_labels 与
        # image_size，没管 describe——结果 suggest 三种模式全部 KeyError: 'license'
        # 崩在用户最常用的入口上，而单元测试全绿，因为测试只调 analyze。
        # 逐个补字段是治标；正确做法是把成员有而组合没有的字段一律带上来。
        merged: dict = {}
        for member in parts:
            for key, value in member.items():
                if key in ("class_support", "classes", "members", "id", "name", "available"):
                    continue
                if key in merged:
                    # 多个成员都有的字段（license、device 等）拼成可读的聚合值，
                    # 不静默丢掉任何一个——许可证尤其不能丢。
                    if str(value) not in str(merged[key]):
                        merged[key] = f"{merged[key]} + {value}"
                else:
                    merged[key] = value
        return {
            "id": self.id,
            "name": "组合后端（Apple Vision + ADE20K）",
            # available 是各后端共同的契约字段，组合后端也必须给，
            # 否则调用方要为组合情况写特例。
            "available": bool(self._vision is not None or self._ade is not None),
            **merged,
            "members": parts,
            "class_support": support,
            "person_classes_from": ("apple-vision" if self._vision
                                    else f"unavailable：{getattr(self, '_vision_reason', '') or '未知'}"),
            "scene_classes_from": self._ade.id if self._ade else f"unavailable：{self._ade_reason}",
        }

    def analyze(self, path, mask_dir=None) -> dict:
        classes: dict = {}
        warnings: list[str] = []
        payloads = {}
        scene_labels: list = []
        passthrough: dict = {}
        if self._vision is not None:
            vision_payload = self._vision.analyze(path, mask_dir)
            payloads["apple-vision"] = vision_payload["backend"]
            classes.update(vision_payload["classes"])
            warnings += vision_payload.get("warnings", [])
            # scene_labels 是 Apple Vision 的图像分类结果，下游的场景路由靠它。
            # 组合后端如果不透传，所有场景特异配方都会被误判为「没有场景证据」，
            # 直接触发已记录过的 BLC-30-002 错误路由。
            scene_labels = vision_payload.get("scene_labels", [])
            # 逐个补字段补了两次（scene_labels、image_size）才发现问题在方法上：
            # 组合后端必须保留成员载荷的完整顶层契约，而不是挑几个字段搬过来。
            # 漏掉任何一个，下游都会静默走进错误分支。
            for key, value in vision_payload.items():
                if key not in ("classes", "warnings", "backend", "schema_version", "path"):
                    passthrough.setdefault(key, value)
        else:
            for name in ("person", "face", "skin", "salient_foreground", "person_instances"):
                classes[name] = {"status": "unavailable", "present": False,
                                 "reason": "Apple Vision 后端不可用"}
        if self._ade is not None:
            ade_payload = self._ade.analyze(path, mask_dir)
            payloads[self._ade.id] = ade_payload["backend"]
            classes.update(ade_payload["classes"])
            warnings += ade_payload.get("warnings", [])
        else:
            for name in ("sky", "vegetation", "building", "product"):
                classes[name] = {"status": "unavailable", "present": False,
                                 "reason": f"场景解析后端不可用：{self._ade_reason}"}
        return {
            **passthrough,
            "schema_version": "4.0.0",
            "available": True,
            "backend": self.describe(),
            "member_backends": payloads,
            "path": str(path),
            "classes": classes,
            "scene_labels": scene_labels,
            "warnings": warnings,
            "boundary": (
                "人物类与场景类由两个不同模型给出，各自的置信度与权重哈希分别记录。"
                "任何一类不可用时如实降级，不用另一个模型的输出去猜。"
            ),
        }


def resolve_backend(prefer: str | None = None):
    """解析可用后端。人物类走 Apple Vision，场景类走 ADE20K，两者合成一个视图。

    prefer 指定后端 id 时必须严格校验：请求一个不存在的后端应当明确失败，
    不能静默回落到组合后端——那会让「我要用 X」和「随便给我一个」变成同一件事。
    """
    registry = load_registry()
    if prefer:
        known = {item["id"] for item in registry["backends"]} | {"composite"}
        if prefer not in known:
            return NullBackend(f"请求的后端不存在：{prefer}；已登记的是 {sorted(known)}")
    vision = None
    vision_reason = ""
    try:
        entry = next(item for item in registry["backends"] if item["id"] == "apple-vision")
        if license_allowed(registry, entry["license"]):
            vision = AppleVisionBackend(entry)
    except Exception as error:  # noqa: BLE001
        # 不静默吞掉——把原因带出去，否则「明明能用却报不能用」查不出根因。
        vision = None
        vision_reason = f"{type(error).__name__}: {error}"

    ade = None
    ade_reason = "未知"
    try:
        import ade_backend
        ok, reason = ade_backend.available()
        ade_reason = reason
        if ok:
            if license_allowed(registry, ade_backend.ADE_LICENSE):
                ade = ade_backend.AdeSemanticBackend()
            else:
                ade_reason = f"许可证 {ade_backend.ADE_LICENSE} 未通过许可证门"
    except Exception as error:  # noqa: BLE001
        ade_reason = f"{type(error).__name__}: {error}"

    if vision is None and ade is None:
        # 两个原因都要带出去。此前只拼了 ade_reason，vision_reason 被算出来又扔掉——
        # 用户看到「场景解析：缺少依赖 numpy」会以为装个 numpy 就好了，
        # 而人物类走的是完全不同的一套依赖（pyobjc-framework-Vision / -Quartz）。
        # 少说一半的降级原因，比不说更容易把人带偏。
        parts = []
        if vision_reason:
            parts.append(f"人物类（Apple Vision）：{vision_reason}")
        else:
            parts.append("人物类（Apple Vision）：需要 pyobjc-framework-Vision 与 "
                         "pyobjc-framework-Quartz，且只在 macOS 上可用")
        parts.append(f"场景类（ADE20K）：{ade_reason}")
        return NullBackend(degraded_reason_for(parts))
    composite = CompositeBackend(vision, ade, ade_reason)
    composite._vision_reason = vision_reason
    return composite



def degraded_reason_for(parts: list) -> str:
    """降级说明必须写清是**哪个解释器**缺依赖，并给出绑定到它的安装命令。

    上一轮的文案是「装法见 README 的「可选依赖」一节」，前面还带着
    「本机未安装可用的语义模型后端」这种机器级断言。实测同一台机器上
    三个 python3 里两个缺 numpy/Quartz、一个全都有——问题从来不在机器，
    在于跑这条命令的是哪个解释器。那句话把审计方引向了错误的
    「不可解除的环境阻塞」结论。
    """
    import sys as _sys

    detail = "；".join(parts) if parts else "当前解释器缺少可选依赖"
    return (f"没有可用的语义后端。{detail}。"
            f"注意这是**当前解释器**的依赖状况，不是这台机器的能力上限——"
            f"当前解释器：{_sys.executable}。"
            f"给这个解释器装依赖：\n"
            f"  {_sys.executable} -m pip install -r requirements-optional.txt\n"
            f"换一个已经装好依赖的 python3 也可以。"
            f"不装也能用，只是全程走 L0 全局链路，不做语义局部。")


def person_classes_available() -> bool:
    """人物类是否真的可用。

    组合后端接入后，isinstance(backend, AppleVisionBackend) 不再成立——
    测试若继续这样判断会被静默跳过，覆盖率无声流失。统一走这个函数。
    """
    try:
        description = resolve_backend().describe()
    except Exception:  # noqa: BLE001
        return False
    return description.get("person_classes_from") == "apple-vision"


def scene_classes_available() -> bool:
    try:
        description = resolve_backend().describe()
    except Exception:  # noqa: BLE001
        return False
    source = description.get("scene_classes_from", "")
    return bool(source) and not source.startswith("unavailable")


def capability_report() -> dict:
    registry = load_registry()
    backend = resolve_backend()
    description = backend.describe()
    supported = {
        item["id"]: description.get("class_support", {}).get(item["id"], "unavailable")
        for item in registry["semantic_classes"]
    }
    return {
        "schema_version": BACKEND_SCHEMA_VERSION,
        "active_backend": description,
        "class_status": supported,
        "high_risk_labels": registry["high_risk_labels"],
        "degradation_policy": registry["degradation_policy"],
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="语义后端能力探测与真实分析")
    parser.add_argument("--input")
    parser.add_argument("--mask-dir")
    parser.add_argument("--capabilities", action="store_true")
    args = parser.parse_args()
    if args.capabilities or not args.input:
        print(json.dumps(capability_report(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    backend = resolve_backend()
    payload = backend.analyze(
        Path(args.input).expanduser().resolve(),
        Path(args.mask_dir).expanduser().resolve() if args.mask_dir else None,
    ) if person_classes_available() else backend.analyze(Path(args.input))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
