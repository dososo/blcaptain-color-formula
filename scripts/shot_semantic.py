#!/usr/bin/env python3
"""L3 视频语义：镜头级蒙版，以及它到底稳不稳。

真正的跨帧跟踪需要光流或专门的跟踪器，这里没有。
能诚实做到的是**镜头级恒定蒙版**——每个镜头取一帧分割，蒙版在镜头内不变，
跨镜头重新分割。这与已有的「参数镜头内恒定、不跨硬切平滑」是同一条纪律。

但恒定蒙版有个前提：镜头内主体不能大幅移动。这个前提**必须被测量而不是假设**。
所以每个镜头都在首／中／末三处各分割一次，用 IoU 量它们的一致性：
一致性够高才承认这个镜头的蒙版可用，否则如实标为不稳定并拒绝使用。

不做的事：不跨镜头平滑蒙版、不插值、不用前一帧的蒙版去猜后一帧。
猜出来的蒙版看起来平滑，但它在边缘上是错的，而边缘正是局部调色唯一露馅的地方。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SHOT_SEMANTIC_SCHEMA_VERSION = "4.3.0"

# 三处采样的 IoU 下限。低于它就说明主体在镜头内移动到了恒定蒙版盖不住的程度。
MIN_SHOT_MASK_IOU = 0.70
# 类别在画面里的最小占比；太小的目标蒙版边缘不可靠，局部调色没有意义。
MIN_CLASS_AREA = 0.02


class ShotSemanticError(Exception):
    pass


def _extract_frame(video: Path, at_time: float, out: Path) -> None:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{at_time:.3f}", "-i", str(video),
         "-frames:v", "1", "-c:v", "png", str(out)], capture_output=True)
    if result.returncode or not out.exists():
        raise ShotSemanticError(
            f"抽帧失败：{video.name} @ {at_time:.3f}s —— "
            f"{result.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:] or ['未知']}")


def _mask_iou(first: Path, second: Path) -> float:
    """两张灰度蒙版的交并比。用 ffmpeg 直接算，不引入新依赖。"""
    def load(path: Path) -> list[int]:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", "format=gray,scale=96:54:flags=area", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
        if result.returncode or not result.stdout:
            raise ShotSemanticError(f"读不出蒙版：{path}")
        return list(result.stdout)

    a, b = load(first), load(second)
    size = min(len(a), len(b))
    intersection = union = 0
    for index in range(size):
        left, right = a[index] >= 128, b[index] >= 128
        if left and right:
            intersection += 1
        if left or right:
            union += 1
    return intersection / union if union else 0.0


def analyze_shot(video: Path, shot: dict, klass: str, workdir: Path) -> dict:
    """一个镜头、一个类别：给出蒙版与它的稳定性证据。"""
    import semantic_backend

    backend = semantic_backend.resolve_backend()
    start, end = float(shot["start"]), float(shot["end"])
    span = max(0.0, end - start)
    if span < 0.2:
        return {"shot": shot["index"], "class": klass, "usable": False,
                "reason": f"镜头只有 {span:.2f}s，短于 0.2s，不做语义局部"}

    # 首／中／末三处。避开边界 8%，防止抽到转场帧。
    # 避开边界防止抽到转场帧，但必须夹回视频范围内：
    # 末尾镜头的 end 常常等于时长，end-margin 仍可能落在最后一帧之后，
    # 抽帧会直接失败。留半帧的余量。
    margin = min(span * 0.08, 0.25)
    safe_end = max(start + 0.02, end - max(margin, 0.04))
    times = [min(start + margin, safe_end), (start + end) / 2, safe_end]
    masks: list[Path] = []
    coverages: list[float] = []
    detected = []
    for order, at in enumerate(times):
        frame = workdir / f"shot{shot['index']}_{order}.png"
        _extract_frame(video, at, frame)
        mask_dir = workdir / f"masks_{shot['index']}_{order}"
        mask_dir.mkdir(parents=True, exist_ok=True)
        payload = backend.analyze(frame, mask_dir)
        if hasattr(payload, "__dict__"):
            payload = payload.__dict__
        info = (payload.get("classes") or {}).get(klass) or {}
        detected.append(bool(info.get("present") and info.get("mask_path")))
        if info.get("present") and info.get("mask_path"):
            masks.append(Path(info["mask_path"]))
            coverages.append(float(info.get("coverage") or info.get("area_ratio") or 0.0))

    if not any(detected):
        # 三处都没检出，和「时有时无」是两回事，别混为一谈。
        # 实测最常见的原因不是画面里没有该类别，而是后端够不着：
        # Apple Vision 的人像分割是为近景人像做的，一整屏远景小人群它一个都不给。
        hint = ("三个采样点都没检出——这通常不是画面里没有该类别，"
                "而是后端够不着。人物类走 Apple Vision，它的分割是为近景人像设计的，"
                "远景密集人群、极小目标、强遮挡都检不出。"
                if klass in ("person", "face", "skin") else
                "三个采样点都没检出，该类别在这个镜头里不成立。")
        return {"shot": shot["index"], "class": klass, "usable": False,
                "detected_at": detected, "reason": hint}
    if not all(detected):
        missing = [i + 1 for i, ok in enumerate(detected) if not ok]
        return {"shot": shot["index"], "class": klass, "usable": False,
                "detected_at": detected,
                "reason": f"第 {'、'.join(map(str, missing))} 个采样点没检出 {klass}，"
                          "而其余采样点检出了——类别在镜头内时有时无，恒定蒙版不成立"}

    if min(coverages) < MIN_CLASS_AREA:
        return {"shot": shot["index"], "class": klass, "usable": False,
                "reason": f"{klass} 最小占比仅 {min(coverages):.1%}，低于 {MIN_CLASS_AREA:.0%}，"
                          "目标太小时蒙版边缘不可靠"}

    pairs = [("首→中", _mask_iou(masks[0], masks[1])),
             ("中→末", _mask_iou(masks[1], masks[2])),
             ("首→末", _mask_iou(masks[0], masks[2]))]
    worst_label, worst = min(pairs, key=lambda item: item[1])
    usable = worst >= MIN_SHOT_MASK_IOU
    return {
        "shot": shot["index"], "class": klass, "usable": usable,
        "mask_path": str(masks[1]),          # 用中点的蒙版，它离两端都最近
        "sampled_at": [round(t, 3) for t in times],
        "coverage": [round(c, 4) for c in coverages],
        "iou": {label: round(value, 4) for label, value in pairs},
        "worst_pair": worst_label,
        "worst_iou": round(worst, 4),
        "threshold": MIN_SHOT_MASK_IOU,
        "reason": None if usable else (
            f"{worst_label} 的蒙版 IoU 只有 {worst:.2f}，低于 {MIN_SHOT_MASK_IOU}——"
            f"{klass} 在这个镜头内移动得太多，恒定蒙版会错位。"
            "本工具不做跨帧跟踪，遇到这种镜头如实拒绝，不用插值糊过去"),
        "boundary": "蒙版在镜头内恒定，跨镜头重新分割；不跨镜头平滑、不插值、"
                    "不用前一帧去猜后一帧",
    }


def analyze_video(video: Path, klass: str, workdir: Path,
                  min_shot_seconds: float = 0.6) -> dict:
    import shots as shot_module

    detection = shot_module.detect(video, min_shot_seconds)
    results = [analyze_shot(video, shot, klass, workdir) for shot in detection["shots"]]
    usable = [item for item in results if item.get("usable")]
    return {
        "schema_version": SHOT_SEMANTIC_SCHEMA_VERSION,
        "path": str(video),
        "class": klass,
        "shot_count": detection["shot_count"],
        "usable_shots": len(usable),
        "shots": results,
        "capability": "L3-shot-constant",
        "boundary":
            "这是镜头级恒定蒙版，不是跨帧跟踪。每个镜头取中点分割，"
            "并用首／中／末三处的蒙版 IoU 验证它在镜头内是否站得住；"
            "站不住的镜头如实标为不可用，不做插值。"
            f"当前 {len(usable)}/{detection['shot_count']} 个镜头可用。"
            "另有一层限制来自后端本身：人物类走 Apple Vision 的人像分割，"
            "它面向近景人像，远景密集人群与极小目标一个都检不出——"
            "这类镜头会被如实标为未检出，而不是给一个空蒙版假装可用。",
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="L3 视频语义：镜头级蒙版与稳定性验证")
    parser.add_argument("--input", required=True)
    parser.add_argument("--class", dest="klass", required=True,
                        help="person / sky / vegetation / building / product")
    parser.add_argument("--workdir")
    args = parser.parse_args()
    video = Path(args.input).expanduser().resolve()
    if args.workdir:
        workdir = Path(args.workdir).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        report = analyze_video(video, args.klass, workdir)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            report = analyze_video(video, args.klass, Path(tmp))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["usable_shots"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
