#!/usr/bin/env python3
"""ADE20K 场景解析语义后端：天空、植被、建筑、商品。

存在的理由：Apple Vision 只覆盖人物、人脸、显著前景，
天空／植被／建筑／商品这四类在 v3 与 v4 前期一直是完全空白。

选型过程（记录下来，因为许可证差点让我们选错）：
- SegFormer-B0-ADE 只有 15MB，看起来是最优解，但 NVIDIA Source Code License
  第 3 条明写「non-commercially means for research or evaluation purposes only」，
  非商用。本项目是 MIT，默认不能用它。许可证门在这里真的拦下了一次。
- 改用 openmmlab/upernet-convnext-tiny：MIT 许可，241MB，ADE20K 150 类。
  体积大一个量级是宽松许可的真实代价。

预处理不经 transformers 的 AutoImageProcessor（它依赖 torchvision）。
官方 preprocessor_config.json 声明的就是「512×512 双线性 + 1/255 缩放 +
ImageNet 均值方差归一化」，用 PIL + numpy 手写即可，给用户少一个依赖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 模型加载会往 stdout/stderr 打警告与进度条，会污染 CLI 的 JSON 输出。
# CLI 的契约是「只吐 JSON」，因此在导入 transformers 之前就把这些关掉。
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
# huggingface_hub 的「未认证请求」提醒走的是自己的 logger，
# TRANSFORMERS_VERBOSITY 管不到它。它会抢占 CLI 的 stderr 第一行——
# 用户看到的第一句话变成一个与他无关的 token 建议，真正的错误被挤到下面。
# 这个 Skill 不需要 HF token（用的是公开权重），所以直接静音。
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)


class _DropTokenChatter(logging.Filter):
    """丢掉「建议设置 HF_TOKEN」这一句，其余记录一律放行。

    只设 setLevel 拦不住它：huggingface_hub 的子 logger 自己设过级别，
    而且要到真正 import 时才注册——在入口提前设置对它无效。
    因此必须在 import 之后、且直接挂到它自己的 logger 上。
    这个 Skill 用的是公开权重，不需要 token；这句提示只会抢占 stderr 第一行，
    把真正的错误挤下去。不做无差别静音，异常与真错误照常输出。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        return "HF_TOKEN" not in text and "unauthenticated requests" not in text


def _hush_hub() -> None:
    """在 huggingface_hub 已经被 import 之后调用，给它的全部子 logger 装过滤器。"""
    chatter = _DropTokenChatter()
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(("huggingface_hub", "transformers")):
            logger = logging.getLogger(name)
            logger.addFilter(chatter)
            for handler in logger.handlers:
                handler.addFilter(chatter)
    for handler in logging.getLogger().handlers:
        handler.addFilter(chatter)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ADE_MODEL_ID = "openmmlab/upernet-convnext-tiny"
ADE_LICENSE = "MIT"
ADE_INPUT = 512
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

# ADE20K 150 类 → 本项目语义类别。
# 只映射有把握的类；把「像是商品」的类硬塞进来会让 product 变成垃圾桶。
ADE_CLASS_MAP = {
    "sky": [2],
    "vegetation": [4, 9, 17, 66, 72],          # tree, grass, plant, flower, palm
    "building": [1, 25, 48],                   # building, house, skyscraper
    "product": [41, 67, 98, 115, 120],         # box, book, bottle, bag, food
}
# 这些类别本身就是高风险：反射、透明、半透明。命中时必须单独警告。
ADE_RISK_CLASSES = {
    8: "windowpane（玻璃）", 27: "mirror（镜面）", 43: "signboard（发光招牌）",
    98: "bottle（可能透明）", 147: "glass（玻璃器皿）",
}
MIN_CLASS_PIXEL_RATIO = 0.005   # 低于 0.5% 面积视为噪声，不产出蒙版
MIN_MEAN_CONFIDENCE = 0.45


class AdeBackendError(Exception):
    pass


def _weights_path() -> Path | None:
    root = Path(os.path.expanduser("~/.cache/huggingface/hub"))
    if not root.is_dir():
        return None
    for pattern in ("*upernet-convnext-tiny*/**/*.safetensors", "*upernet-convnext-tiny*/**/*.bin"):
        for candidate in root.rglob(pattern):
            if candidate.stat().st_size > 1_000_000:
                return candidate
    return None


def weights_hash() -> str:
    path = _weights_path()
    if path is None:
        return "not-downloaded"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def available() -> tuple[bool, str]:
    try:
        import numpy  # noqa: F401
        import torch  # noqa: F401
        from PIL import Image  # noqa: F401
        from transformers import UperNetForSemanticSegmentation  # noqa: F401
    except ImportError as error:
        return False, f"缺少依赖：{error.name}"
    _hush_hub()
    if _weights_path() is None:
        return False, "模型权重未下载"
    return True, "就绪"


class AdeSemanticBackend:
    """ADE20K 场景解析。只承诺 sky / vegetation / building / product 四类。"""

    id = "ade20k-upernet-convnext-tiny"

    def __init__(self):
        ok, reason = available()
        if not ok:
            raise AdeBackendError(reason)
        import contextlib
        import io
        import logging
        import torch
        logging.getLogger("transformers").setLevel(logging.ERROR)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            from transformers import UperNetForSemanticSegmentation
            self._torch = torch
            self._model = UperNetForSemanticSegmentation.from_pretrained(ADE_MODEL_ID)
        self._model.eval()
        # MPS 上 UperNet 的金字塔池化会踩到 adaptive_avg_pool2d 的非整除限制
        # （PyTorch issue 96056）。用一次真实前向探测，失败就回退 CPU 并如实记录，
        # 不猜、不静默。60M 参数在 M 系列 CPU 上单图仍是秒级，可以接受。
        self._device = "cpu"
        self._device_note = "CPU"
        if torch.backends.mps.is_available():
            try:
                self._model.to("mps")
                with torch.no_grad():
                    self._model(pixel_values=torch.zeros(1, 3, ADE_INPUT, ADE_INPUT).to("mps"))
                self._device = "mps"
                self._device_note = "MPS"
            except Exception as error:  # noqa: BLE001
                self._model.to("cpu")
                self._device = "cpu"
                self._device_note = f"CPU（MPS 不可用：{type(error).__name__}）"
        self._model.to(self._device)
        self._labels = self._model.config.id2label

    def describe(self) -> dict:
        return {
            "id": self.id,
            "name": "UperNet + ConvNeXt-tiny (ADE20K)",
            "model_id": ADE_MODEL_ID,
            "license": ADE_LICENSE,
            "license_note": (
                "MIT。曾评估更小的 SegFormer-B0-ADE（15MB），但其 NVIDIA Source Code License "
                "限定 non-commercial，与本项目 MIT 冲突，因此不采用。"
            ),
            "weights_hash": weights_hash(),
            "device": self._device,
            "device_note": self._device_note,
            "input_size": ADE_INPUT,
            "num_labels": len(self._labels),
            "classes": sorted(ADE_CLASS_MAP),
        }

    def _preprocess(self, path: Path):
        """手写预处理，避开 transformers 的 AutoImageProcessor（依赖 torchvision）。"""
        import numpy as np
        from PIL import Image
        image = Image.open(path).convert("RGB")
        original = image.size  # (w, h)
        resized = image.resize((ADE_INPUT, ADE_INPUT), Image.BILINEAR)
        array = np.asarray(resized, dtype=np.float32) / 255.0
        array = (array - np.array(IMAGENET_MEAN, dtype=np.float32)) / np.array(IMAGENET_STD, dtype=np.float32)
        tensor = self._torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0)
        return tensor.to(self._device), original

    def analyze(self, path: Path, mask_dir: Path | None = None) -> dict:
        import numpy as np
        tensor, (width, height) = self._preprocess(path)
        with self._torch.no_grad():
            logits = self._model(pixel_values=tensor).logits
        probs = self._torch.softmax(logits, dim=1)[0]
        best = probs.argmax(dim=0)
        confidence = probs.max(dim=0).values
        label_map = best.detach().to("cpu").numpy()
        conf_map = confidence.detach().to("cpu").numpy()
        total = float(label_map.size)

        classes: dict[str, dict] = {}
        warnings: list[str] = []
        for name, ids in ADE_CLASS_MAP.items():
            member = np.isin(label_map, ids)
            ratio = float(member.sum()) / total
            if ratio < MIN_CLASS_PIXEL_RATIO:
                classes[name] = {
                    "status": "supported", "present": False, "area_ratio": round(ratio, 6),
                    "reason": f"该类别在本张素材中面积仅 {ratio:.2%}，低于 {MIN_CLASS_PIXEL_RATIO:.1%} 阈值，不产出蒙版",
                }
                continue
            mean_conf = float(conf_map[member].mean()) if member.any() else 0.0
            entry = {
                "status": "supported",
                "present": True,
                "area_ratio": round(ratio, 6),
                "mean_confidence": round(mean_conf, 4),
                "ade_class_ids": ids,
                "ade_class_names": [self._labels[str(i)] if str(i) in self._labels
                                    else self._labels.get(i, str(i)) for i in ids],
            }
            if mean_conf < MIN_MEAN_CONFIDENCE:
                entry["status"] = "low-confidence"
                entry["needs_human_review"] = True
                entry["reason"] = (
                    f"平均置信度仅 {mean_conf:.2f}，低于 {MIN_MEAN_CONFIDENCE}；"
                    "不自动执行局部处理，需人工确认"
                )
                warnings.append(f"{name} 置信度不足（{mean_conf:.2f}），已降级为人工确认")
            if mask_dir is not None and entry["status"] == "supported":
                entry["mask_path"] = str(self._write_mask(
                    member, conf_map, mask_dir, Path(path).stem, name, width, height))
            classes[name] = entry

        for class_id, label in ADE_RISK_CLASSES.items():
            ratio = float((label_map == class_id).sum()) / total
            if ratio >= MIN_CLASS_PIXEL_RATIO:
                warnings.append(
                    f"检测到 {label} 占 {ratio:.1%}：反射与半透明区域的语义边界不可靠，"
                    "局部调色前必须人工复核"
                )

        return {
            "schema_version": "4.0.0",
            "backend": self.describe(),
            "path": str(path),
            "classes": classes,
            "warnings": warnings,
            "boundary": (
                "ADE20K 场景解析只覆盖 sky／vegetation／building／product 四类。"
                "人物、人脸、肤色仍由 Apple Vision 后端负责；两者不互相替代。"
                "分割在 512×512 上进行后再放大到原尺寸，细小结构（发丝、栏杆、树枝末梢）边界不可靠。"
            ),
        }

    def _write_mask(self, member, conf_map, mask_dir: Path, stem: str,
                    name: str, width: int, height: int) -> Path:
        import numpy as np
        from PIL import Image
        mask_dir.mkdir(parents=True, exist_ok=True)
        # 用置信度做软边：命中区域按置信度给灰度，而不是硬 0/255。
        soft = np.where(member, np.clip(conf_map, 0.0, 1.0), 0.0)
        image = Image.fromarray((soft * 255).astype("uint8"), mode="L")
        image = image.resize((width, height), Image.BILINEAR)
        out = mask_dir / f"{stem}__{name}_mask.png"
        image.save(out)
        return out


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="ADE20K 语义后端")
    parser.add_argument("--input")
    parser.add_argument("--mask-dir")
    parser.add_argument("--capabilities", action="store_true")
    args = parser.parse_args()
    if args.capabilities:
        ok, reason = available()
        payload = {"available": ok, "reason": reason, "model_id": ADE_MODEL_ID,
                   "license": ADE_LICENSE, "classes": sorted(ADE_CLASS_MAP)}
        if ok:
            payload["weights_hash"] = weights_hash()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not args.input:
        print("需要 --input 或 --capabilities", file=sys.stderr)
        return 2
    try:
        backend = AdeSemanticBackend()
        result = backend.analyze(Path(args.input).expanduser().resolve(),
                                 Path(args.mask_dir).expanduser().resolve() if args.mask_dir else None)
    except AdeBackendError as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
