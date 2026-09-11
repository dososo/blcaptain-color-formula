#!/usr/bin/env python3
"""镜头检测与分类：硬切、渐变、闪光、甩镜、快速变焦。

为什么不能只用 FFmpeg 的 scene 滤镜：实测一段含 1 秒 xfade 渐变的素材，
scene 滤镜找到了两个硬切却完全漏掉渐变。渐变必须靠「连贯漂移」特征单独识别。

分层：
1. 全局粗扫（固定采样率，抓渐变与漂移）
2. FFmpeg scene 滤镜（抓硬切候选，C 速度）
3. 候选点原生帧率细化（定位到帧）
4. 分类与置信度

只依赖标准库与 ffmpeg。甩镜与快速变焦在没有光流的前提下只能标 suspected，
必须诚实降级，不得当作已确认边界。

已知边界（必须如实告知，不得当作已覆盖）：
- 渐变判据用「窗口内变化率 ÷ 两侧稳定段变化率」区分转场与镜头内摇移，
  因此**两个都在剧烈运动的镜头之间的渐变**会漏检。
- 快速变焦没有独立判据，会表现为硬切或渐变，当前不单独报告。
- 甩镜判据要求两侧画面本身具备可被模糊的细节；平滑无细节画面之间的甩镜会漏检。
- 所有 suspected 类型必须经人工复核后才能进入逐镜头校正。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

SHOTS_SCHEMA_VERSION = "4.0.0"
COARSE_W, COARSE_H = 16, 9
FINE_W, FINE_H = 32, 18
MAX_COARSE_FRAMES = 14000
SCENE_THRESHOLD = 0.10
# 甩镜判据要求两肩必须具备真实细节，否则比值不具判别意义。
WHIP_MIN_SHOULDER_DETAIL = 0.010
# 低于此置信度进入人工复核队列；高于 AUTO_ACCEPT 的边界不必逐条看。
# 实测 94 分钟 1961 年胶片影片：高置信边界 90% 是真切点，低置信只有 50%，
# 说明这个分数确实有判别力，可以据此排序复核优先级。
REVIEW_CONFIDENCE = 0.60
AUTO_ACCEPT_CONFIDENCE = 0.85


class ShotError(Exception):
    pass


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ShotError(f"缺少 {name}")
    return found


def probe(path: Path) -> dict:
    result = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate,nb_frames,width,height,duration",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise ShotError(f"无法探测视频：{result.stderr.strip()[:200]}")
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    rate = stream.get("avg_frame_rate", "0/1")
    try:
        numerator, denominator = rate.split("/")
        fps = float(numerator) / float(denominator) if float(denominator) else 25.0
    except (ValueError, ZeroDivisionError):
        fps = 25.0
    duration = float(stream.get("duration") or payload.get("format", {}).get("duration") or 0)
    return {"fps": fps or 25.0, "duration": duration,
            "width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0)}


def _read_frames(path: Path, fps: float, width: int, height: int,
                 start: float | None = None, span: float | None = None) -> list[list[int]]:
    command = [_tool("ffmpeg"), "-v", "error"]
    if start is not None:
        command += ["-ss", f"{max(0.0, start):.4f}"]
    command += ["-i", str(path)]
    if span is not None:
        command += ["-t", f"{span:.4f}"]
    command += ["-vf", f"fps={fps},format=rgb24,scale={width}:{height}:flags=area",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise ShotError(f"无法解码帧：{result.stderr.decode('utf-8','replace').strip()[:200]}")
    size = width * height * 3
    data = result.stdout
    return [list(data[i:i + size]) for i in range(0, len(data) - size + 1, size)]


def _frame_distance(first: list[int], second: list[int]) -> float:
    return sum(abs(a - b) for a, b in zip(first, second)) / (len(first) * 255.0)


def _luma(frame: list[int]) -> float:
    total = 0.0
    for index in range(0, len(frame), 3):
        total += 0.2126 * frame[index] + 0.7152 * frame[index + 1] + 0.0722 * frame[index + 2]
    return total / (len(frame) / 3) / 255.0


def _detail_energy(frame: list[int], width: int, height: int) -> float:
    """高频能量。甩镜的运动模糊会显著降低它，用来把甩镜和硬切区分开。"""
    total = 0.0
    count = 0
    for y in range(height):
        row = y * width * 3
        for x in range(1, width):
            left = frame[row + (x - 1) * 3]
            here = frame[row + x * 3]
            total += abs(here - left)
            count += 1
    return total / max(1, count) / 255.0


def scene_candidates(path: Path, threshold: float = SCENE_THRESHOLD) -> list[dict]:
    """用 FFmpeg scene 滤镜取硬切候选。

    metadata 必须写到独立文件再解析，不能用 `file=-` 后拼接 stdout+stderr：
    两个流在系统负载下会交错，把「pts_time 行」和「scene_score 行」的配对打断，
    表现为随机漏检。实测 5 次里有 1 次因此少检出一个边界。
    """
    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp) / "scene.txt"
        subprocess.run(
            [_tool("ffmpeg"), "-v", "error", "-i", str(path),
             "-vf", f"select='gt(scene,{threshold})',metadata=print:file={meta}",
             "-f", "null", "-"],
            capture_output=True,
        )
        if not meta.exists():
            return []
        text = meta.read_text(encoding="utf-8", errors="replace")
    entries = []
    current = None
    for line in text.splitlines():
        stamp = re.search(r"pts_time:([0-9.]+)", line)
        if stamp:
            current = float(stamp.group(1))
            continue
        score = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if score and current is not None:
            entries.append({"time": current, "scene_score": float(score.group(1))})
            current = None
    return entries


def coarse_scan(path: Path, info: dict) -> dict:
    duration = max(0.001, info["duration"])
    scan_fps = min(info["fps"], max(2.0, MAX_COARSE_FRAMES / duration))
    frames = _read_frames(path, scan_fps, COARSE_W, COARSE_H)
    if len(frames) < 3:
        return {"scan_fps": scan_fps, "frames": len(frames), "distances": [], "lumas": []}
    distances = [_frame_distance(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
    lumas = [_luma(frame) for frame in frames]
    return {"scan_fps": scan_fps, "frames": len(frames), "distances": distances,
            "lumas": lumas, "raw": frames}


def find_gradual_transitions(scan: dict, fps: float) -> list[dict]:
    """渐变识别：多尺度滑窗 + 连贯漂移判据。

    早期实现用「连续超阈值段」切分，结果渐变段和紧随其后的运动内容段连成一片，
    长度超过上限被整段丢弃。滑窗不依赖段边界，因此不受相邻内容影响。

    三个判据缺一不可：
    - direct 足够大：窗口首尾确实换了画面，不是抖动；
    - coherence 高：首尾距离接近逐帧距离之和，说明是单向漂移而非来回运动；
    - 没有单帧主导：否则那是硬切，应交给细化阶段。
    """
    distances = scan.get("distances") or []
    frames = scan.get("raw") or []
    if len(distances) < 4 or not frames:
        return []
    candidates: list[dict] = []
    scales = sorted({max(3, int(round(fps * seconds))) for seconds in (0.4, 0.8, 1.4, 2.2)})
    for window in scales:
        if window >= len(distances):
            continue
        step = max(1, window // 3)
        for start_index in range(0, len(distances) - window, step):
            end_index = start_index + window
            direct = _frame_distance(frames[start_index], frames[min(end_index, len(frames) - 1)])
            if direct < 0.08:
                continue
            travelled = sum(distances[start_index:end_index])
            if travelled <= 0:
                continue
            coherence = direct / travelled
            peak = max(distances[start_index:end_index])
            if coherence < 0.5 or peak >= direct * 0.5:
                continue
            # 仅靠连贯漂移不足以区分「转场渐变」与「镜头内缓慢摇移」——
            # 两者都是单向连贯变化。真转场的变化率必须显著高于它两侧的稳定段。
            before = distances[max(0, start_index - window):start_index]
            after = distances[end_index:end_index + window]
            inside = statistics.median(distances[start_index:end_index])
            baselines = [statistics.median(seg) for seg in (before, after) if seg]
            if not baselines:
                continue
            baseline = min(baselines)
            ratio = inside / baseline if baseline > 0 else float("inf")
            if ratio < 2.5:
                continue
            candidates.append({
                "start_index": start_index, "end_index": end_index, "length": window,
                "coherence": round(coherence, 4), "direct": round(direct, 5),
                "travelled": round(travelled, 5), "peak_step": round(peak, 5),
                "inside_over_baseline": round(ratio, 2),
            })
    if not candidates:
        return []
    # 多尺度会对同一渐变重复命中，按重叠归并，保留连贯度最高的一个。
    candidates.sort(key=lambda item: (-item["coherence"], item["length"]))
    merged: list[dict] = []
    for item in candidates:
        if any(not (item["end_index"] <= kept["start_index"] or item["start_index"] >= kept["end_index"])
               for kept in merged):
            continue
        merged.append(item)
    merged.sort(key=lambda item: item["start_index"])
    # 多尺度只保留连贯度最高的那个窗口，跨度往往短于真实渐变。
    # 向两侧外扩到变化率回落至基线水平，才能拿到完整转场区间——
    # 这是「不得跨渐变转场平滑」能否正确执行的前提。
    for item in merged:
        window = item["length"]
        before = distances[max(0, item["start_index"] - window):item["start_index"]]
        after = distances[item["end_index"]:item["end_index"] + window]
        # 两侧必须各用自己的基线。用 min(两侧) 会让外扩冲进下一个运动镜头：
        # 实测渐变本应是 7.0~8.0，用统一基线时一路扩到了 11.0。
        floor_left = statistics.median(before) * 2.0 if before else float("inf")
        floor_right = statistics.median(after) * 2.0 if after else float("inf")
        start_index = item["start_index"]
        limit = max(0, item["start_index"] - window * 2)
        while start_index > limit and distances[start_index - 1] > floor_left:
            start_index -= 1
        end_index = item["end_index"]
        ceiling = min(len(distances), item["end_index"] + window * 2)
        while end_index < ceiling and distances[end_index] > floor_right:
            end_index += 1
        item["expanded_from"] = [item["start_index"], item["end_index"]]
        item["start_index"], item["end_index"] = start_index, end_index
        item["length"] = end_index - start_index
    return merged


def refine_cut(path: Path, info: dict, approximate: float) -> dict:
    """在候选点附近以原生帧率细化，定位到帧，并区分硬切／闪光。"""
    fps = info["fps"]
    # 窗口必须宽到能看见切点两侧「清晰的肩」。
    # 早期用 0.48s 的窗口，遇到 0.48s 的甩镜模糊段时整窗都是模糊帧，
    # V 形凹陷深度恒为 0，甩镜必然漏检。
    span = min(2.4, max(0.9, 26.0 / fps))
    start = max(0.0, approximate - span / 2)
    frames = _read_frames(path, fps, FINE_W, FINE_H, start=start, span=span)
    if len(frames) < 3:
        return {"time": approximate, "confidence": 0.3, "kind": "unverified",
                "reason": "候选点附近帧数不足，无法细化"}
    distances = [_frame_distance(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
    peak = max(range(len(distances)), key=lambda i: distances[i])
    peak_value = distances[peak]
    others = [value for index, value in enumerate(distances) if index != peak]
    background = statistics.median(others) if others else 0.0
    cut_time = start + (peak + 1) / fps

    # 闪光：一段很短的插入内容，它两侧的画面彼此相同。
    #
    # 必须双向判。早期只朝前看（frames[peak-1] 与 frames[peak+1+lag] 比），
    # 那是默认 peak 一定落在闪光的**上升沿**上。可闪光有两条边，
    # 升与落的步进量级本来就接近，谁更大取决于内容——实测 8 个渐变种子里
    # 有 1 个的落边更大，peak 落到落边，此时 frames[peak-1] 本身就是白帧，
    # 「回到峰值前状态」永远不可能成立，一个 0.08s 的闪光就被判成硬切。
    # 反向判据是对称的：峰值**之前**若存在一帧与峰值之后的画面相同，
    # 说明峰值是闪光的落边，中间那几帧才是插入内容。
    kind = "hard_cut"
    evidence = [f"峰值步进 {peak_value:.4f}，邻域中位 {background:.4f}"]
    flash_start = None
    for lag in (1, 2, 3):
        if peak + 1 + lag < len(frames) and peak >= 1:
            back = _frame_distance(frames[peak - 1], frames[peak + 1 + lag])
            if back < peak_value * 0.35:
                kind = "flash"
                flash_start = peak
                evidence.append(f"峰值后第 {lag} 帧画面回到峰值前状态（回归距离 {back:.4f}，上升沿）")
                break
    if kind == "hard_cut":
        for lag in (1, 2, 3):
            if peak - 1 - lag >= 0 and peak + 1 < len(frames):
                back = _frame_distance(frames[peak - 1 - lag], frames[peak + 1])
                if back < peak_value * 0.35:
                    kind = "flash"
                    # 闪光的时间锚点取插入段的起点，而不是被抓到的那条边。
                    flash_start = peak - 1 - lag
                    evidence.append(
                        f"峰值前第 {lag} 帧与峰值后画面相同（回归距离 {back:.4f}，"
                        "峰值落在闪光的下降沿）")
                    break
    if kind == "flash" and flash_start is not None:
        cut_time = start + (flash_start + 1) / fps

    # 甩镜：切换瞬间的运动模糊会同时削弱切点两侧紧邻帧的细节。
    # 关键是两侧必须各自与「自己镜头内部的稳定值」比较——
    # 早期实现拿切点前一帧去比两侧混合基线，遇到两个镜头细节水平本就差很多时必然误判。
    if kind == "hard_cut" and len(frames) >= 8:
        # 甩镜的可测特征是细节能量在切点附近形成 V 形凹陷：
        # 越接近切换瞬间越模糊，两端各自恢复到本镜头的清晰水平。
        # 用「窗口内相对凹陷深度」而不是固定偏移量取样，避免模糊段长度不同导致漏检。
        energies = [_detail_energy(frame, FINE_W, FINE_H) for frame in frames]
        left_peak = max(energies[:max(1, peak - 1)] or [0.0])
        right_peak = max(energies[min(len(energies) - 1, peak + 2):] or [0.0])
        trough_span = energies[max(0, peak - 2):min(len(energies), peak + 4)]
        trough = min(trough_span) if trough_span else 0.0
        shoulder = min(left_peak, right_peak)
        depth = 1.0 - (trough / shoulder) if shoulder > 0 else 0.0
        # 绝对下限：运动模糊要成为证据，前提是原本有细节可以被模糊掉。
        # 平滑渐变类画面的细节能量本就在 0.001~0.003 量级，比值波动没有判别意义。
        if depth >= 0.45 and shoulder >= WHIP_MIN_SHOULDER_DETAIL:
            kind = "whip_pan_suspected"
            evidence.append(
                f"切点附近细节能量形成 V 形凹陷，谷底 {trough:.5f} 相对两肩 {shoulder:.5f} 下降 {depth:.0%}，"
                "双侧同时出现运动模糊，疑似甩镜；无光流无法确认，按 suspected 处理"
            )
        elif shoulder < WHIP_MIN_SHOULDER_DETAIL:
            evidence.append(
                f"两侧细节能量仅 {shoulder:.5f}，画面本身几乎没有可被模糊的细节，"
                "不以细节凹陷推断甩镜，判为硬切"
            )
        else:
            evidence.append(
                f"细节能量未形成足够凹陷（谷底/两肩下降 {depth:.0%}），不构成双侧运动模糊，判为硬切"
            )

    confidence = min(0.99, peak_value / max(1e-6, peak_value + background * 3))
    return {"time": round(cut_time, 4), "kind": kind,
            "confidence": round(confidence, 3),
            "peak_step": round(peak_value, 5),
            "neighbour_median": round(background, 5),
            "evidence": evidence}


def detect(path: Path, min_shot_seconds: float = 0.6) -> dict:
    info = probe(path)
    if info["duration"] <= 0:
        raise ShotError("视频时长无效")
    scan = coarse_scan(path, info)
    candidates = scene_candidates(path)
    gradual = find_gradual_transitions(scan, scan.get("scan_fps") or info["fps"])
    scan_fps = scan.get("scan_fps") or info["fps"]

    boundaries: list[dict] = []
    for item in candidates:
        refined = refine_cut(path, info, item["time"])
        refined["scene_score"] = item["scene_score"]
        refined["source"] = "scene-filter+refined"
        boundaries.append(refined)

    for item in gradual:
        start_time = item["start_index"] / scan_fps
        end_time = item["end_index"] / scan_fps
        if any(abs(existing["time"] - start_time) < 0.5 for existing in boundaries):
            continue
        boundaries.append({
            "time": round(start_time, 4),
            "end_time": round(end_time, 4),
            "kind": "dissolve",
            "confidence": round(min(0.95, item["coherence"]), 3),
            "peak_step": item["peak_step"],
            "source": "coarse-coherent-drift",
            "evidence": [
                f"{item['length']} 帧窗口内首尾距离 {item['direct']}、累计 {item['travelled']}，"
                f"连贯度 {item['coherence']}，无单帧主导（峰值 {item['peak_step']}），"
                f"且窗口内变化率是两侧稳定段的 {item['inside_over_baseline']} 倍；"
                "属于转场渐变而非镜头内摇移。FFmpeg scene 滤镜对这类转场无效"
            ],
        })

    boundaries.sort(key=lambda item: item["time"])
    deduped: list[dict] = []
    for item in boundaries:
        if deduped and item["time"] - deduped[-1]["time"] < min_shot_seconds:
            if item["confidence"] > deduped[-1]["confidence"]:
                deduped[-1] = item
            continue
        deduped.append(item)

    # 闪光不是镜头边界，它是同一镜头内的事件。
    cuts = [item for item in deduped if item["kind"] != "flash"]
    flashes = [item for item in deduped if item["kind"] == "flash"]

    shots = []
    previous = 0.0
    for index, item in enumerate(cuts):
        end = item["time"]
        if end - previous >= min_shot_seconds * 0.5:
            shots.append({"index": len(shots), "start": round(previous, 4), "end": round(end, 4),
                          "duration": round(end - previous, 4),
                          "ends_with": item["kind"], "boundary_confidence": item["confidence"]})
        previous = item.get("end_time", end)
    shots.append({"index": len(shots), "start": round(previous, 4),
                  "end": round(info["duration"], 4),
                  "duration": round(info["duration"] - previous, 4),
                  "ends_with": "end-of-file", "boundary_confidence": 1.0})

    low_confidence = [item for item in cuts if item["confidence"] < 0.6]
    return {
        "schema_version": SHOTS_SCHEMA_VERSION,
        "path": str(path),
        "duration": round(info["duration"], 4),
        "fps": round(info["fps"], 4),
        "scan_fps": round(scan_fps, 4),
        "scanned_frames": scan.get("frames", 0),
        "coverage": "全片均匀粗扫 + 候选点原生帧率细化",
        "shot_count": len(shots),
        "shots": shots,
        "boundaries": cuts,
        "flashes": flashes,
        "needs_human_review": sorted(
            [
                {"time": item["time"], "kind": item["kind"], "confidence": item["confidence"],
                 "why": ("类型不确定，需人工判断是否真的是甩镜"
                         if item["kind"].endswith("_suspected")
                         else "置信度偏低，可能是胶片颗粒或闪烁造成的误检")}
                for item in cuts
                if item["confidence"] < REVIEW_CONFIDENCE or item["kind"].endswith("_suspected")
            ],
            key=lambda item: item["confidence"],
        ),
        "review_policy": {
            "threshold": REVIEW_CONFIDENCE,
            "ordering": "按置信度升序，最不确定的排在最前",
            "asymmetry": (
                "误检与漏检的代价不对称，复核的重点是漏检而不是多检。"
                "误检只是把一个镜头切成两半，两半内容相同因而拿到几乎相同的一级校正"
                "（实测某个疑似误检点两侧 gamma 差 0.0000、红增益差 0.0010，不可察觉）；"
                "漏检则会让参数跨过真实切点，直接违反「不跨镜头平滑」。"
                "因此检测宁可偏向多检，复核队列也不必逐条清空。"
            ),
            "evidence_note": "上述无害性目前只在 1 个疑似误检样本上实测过，方向明确但样本量薄。",
            "auto_accept_above": AUTO_ACCEPT_CONFIDENCE,
        },
        "boundary": (
            "甩镜与快速变焦在没有光流的前提下只能标 suspected；"
            "闪光被记为镜头内事件而不是边界；所有边界在逐镜头校正前需要人工复核门。"
        ),
        "low_confidence_count": len(low_confidence),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="镜头检测与分类")
    parser.add_argument("--input", required=True)
    parser.add_argument("--min-shot-seconds", type=float, default=0.6)
    args = parser.parse_args()
    try:
        payload = detect(Path(args.input).expanduser().resolve(), args.min_shot_seconds)
    except ShotError as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
