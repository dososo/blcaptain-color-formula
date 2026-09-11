#!/usr/bin/env python3
"""DaVinci Resolve 真实标定：把基准色卡过一遍 Resolve 的导出管线。

标定的第一问不是「我们的参数在 Resolve 里等于什么」，而是
**什么都不做的时候 Resolve 会不会改变画面**。无调整导出若已不恒等，
后面所有参数对应关系都建立在一个偏移的基线上。

前置条件（脚本会自检并如实报告，不假装成功）：
  1. DaVinci Resolve 已安装并**正在运行**——API 只在它运行时可用。
  2. Resolve 的 UI 没有被模态对话框阻塞。实测被系统权限弹窗挡住时，
     scriptapp 仍然连得上、GetCurrentPage() 却返回 None，
     SetSetting 全部返回 None，导入静默失败——所有症状都不像权限问题，
     很容易误判成 API 版本差异。所以先查 GetCurrentPage()。
  3. 运行 Resolve 的用户对素材目录有读权限。
  4. 同名项目已存在也没关系——脚本会复用并清空它。
     （曾经这里报「可能被对话框阻塞」，那是错的归因：
     Resolve 不允许删除当前打开的项目，与对话框无关。）

用法：
    python3 scripts/davinci_calibrate.py --workdir <目录>

它会生成基准包、驱动 Resolve 导出、采样对比，并在 <目录>/result.json 里
写下恒等性结论。**没有真实导出就不写 calibrated**——这是标定纪律，
仿真数据只能验证工具链本身。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RESOLVE_API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
RESOLVE_MODULES = f"{RESOLVE_API}/Modules"


class DavinciBlocked(Exception):
    """Resolve 在，但用不了。把原因带出去，不要让调用方去猜。"""


def connect():
    os.environ.setdefault("RESOLVE_SCRIPT_API", RESOLVE_API)
    if RESOLVE_MODULES not in sys.path:
        sys.path.insert(0, RESOLVE_MODULES)
    try:
        import DaVinciResolveScript as dvr
    except ImportError as error:
        raise DavinciBlocked(
            f"找不到 Resolve 的 Python 模块（{error}）。"
            f"确认 DaVinci Resolve 已安装，且 {RESOLVE_MODULES} 存在。") from error
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise DavinciBlocked(
            "Resolve 未运行或未开启外部脚本。先启动 DaVinci Resolve，"
            "并在「偏好设置 → 系统 → 通用」里把外部脚本设为 Local 或 Network。")
    page = resolve.GetCurrentPage()
    if page is None:
        raise DavinciBlocked(
            "Resolve 的 UI 没有响应——GetCurrentPage() 返回 None。"
            "最常见的原因是有模态对话框挡在前面（系统权限请求、项目管理器、"
            "许可提示）。这种状态下 API 连得上但一切 UI 操作静默失败："
            "SetSetting 返回 None、导入返回 False，症状完全不像权限问题。"
            "请切到 Resolve 把对话框处理掉，再重跑本脚本。")
    return resolve


def _image_size(path: Path) -> tuple:
    """用 ffprobe 读图像尺寸。标定的每一步都要能对齐到像素。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    if result.returncode or "," not in result.stdout:
        raise DavinciBlocked(f"读不出尺寸：{path}")
    w, h = result.stdout.strip().split(",")[:2]
    return int(w), int(h)


def assert_sizes_match(exported_size: tuple, chart_size: tuple) -> None:
    """导出了不等于对得上。

    尺寸一旦不同，色卡就被缩放或加了黑边，按相对坐标采样出来的全是错位的读数。
    实测有一版把 768×768 的方色卡渲成 1920×1080，gray-17 的 (217,217,217)
    采到黑边读成 (0,0,0)，肤色组中位 ΔE00 报 55.5——那份数字长得像一份完整的
    标定结果，比没有数字更危险。所以这里宁可不产出，也不照算。
    """
    if tuple(exported_size) != tuple(chart_size):
        raise DavinciBlocked(
            f"导出尺寸 {exported_size[0]}×{exported_size[1]} 与色卡 "
            f"{chart_size[0]}×{chart_size[1]} 不一致——"
            "色卡被缩放或加了黑边，按坐标采样会全部错位，"
            "所以不在这种情况下产出任何标定数字。"
            "请检查时间线分辨率是否生效（项目设置 → 主设置 → 时间线分辨率）。")


def run(workdir: Path) -> dict:
    import calibration as cal

    bench = workdir / "bench"
    manifest = cal.build_benchmark_package(bench, "srgb")
    chart = bench / "benchmark-chart.png"

    resolve = connect()
    project_manager = resolve.GetProjectManager()
    name = "BLCaptainCalib"
    # 项目可能已经存在：Resolve 不允许删除当前打开的项目，
    # 此时 DeleteProject 与 CreateProject 都会返回 False，
    # 而失败信息里一句「可能被对话框阻塞」会把人带偏——它跟对话框没关系。
    # 正确顺序是先试加载，加载不到再创建。
    project = project_manager.LoadProject(name)
    if project is None:
        project_manager.DeleteProject(name)
        project = project_manager.CreateProject(name)
    if project is None:
        raise DavinciBlocked(
            f"既加载不了也创建不了项目 {name}。"
            f"当前项目列表：{project_manager.GetProjectListInCurrentFolder()}。"
            "如果列表里已有同名项目而它正被打开，先在 Resolve 里切到别的项目。")
    # 复用旧项目时要清掉上一轮的时间线与媒体，否则会叠加渲染任务。
    pool_root = project.GetMediaPool().GetRootFolder()
    stale = pool_root.GetClipList() or []
    if stale:
        project.GetMediaPool().DeleteClips(stale)
    for index in range(project.GetTimelineCount(), 0, -1):
        timeline = project.GetTimelineByIndex(index)
        if timeline:
            project.GetMediaPool().DeleteTimelines([timeline])
    project.DeleteAllRenderJobs()

    # 色彩管理必须显式设定：标定测出的偏差要能归因，不能不知道当时是什么配置。
    # 时间线分辨率必须等于色卡尺寸。默认 1920×1080 会把 768×768 的方图
    # 放进 16:9 画布并加黑边，按相对坐标采样就全部错位——
    # 实测那一版里 gray-17 采到的是黑边，(217,217,217) 读成 (0,0,0)，
    # 于是「skin 组 ΔE00 中位 55」看起来像色彩管理出了大问题，其实是采样对错了地方。
    width, height = _image_size(chart)
    settings = {
        "colorScienceMode": "davinciYRGBColorManagedv2",
        "rcmPresetMode": "Custom",
        "colorSpaceInput": "Rec.709 Gamma 2.4",
        "colorSpaceTimeline": "Rec.709 Gamma 2.4",
        "colorSpaceOutput": "Rec.709 Gamma 2.4",
        "timelineResolutionWidth": str(width),
        "timelineResolutionHeight": str(height),
    }
    applied = {key: project.SetSetting(key, value) for key, value in settings.items()}

    # superScale 在 19.0.0b.20 上通过 API 写不动：设成 1 或 2 都返回 False 且值不变。
    # 所以不假装在设它，改成校验——它要是不等于 1，色卡会被放大后再采样，
    # 那样采出来的读数不能用，宁可停在这里。
    super_scale = str(project.GetSetting("superScale"))
    if super_scale != "1":
        raise DavinciBlocked(
            f"superScale = {super_scale}（1 才是不放大），而这一项 API 改不动。"
            "放大后采样的读数不可用。请在 Resolve 里手动改回："
            "项目设置 → 图像缩放 → Super Scale 设为 No scaling。")
    if all(value in (None, False) for value in applied.values()):
        raise DavinciBlocked(
            "所有 SetSetting 都没有生效——Resolve 仍在无响应状态，标定无法进行。")

    storage = resolve.GetMediaStorage()
    clips = storage.AddItemListToMediaPool([str(chart)])
    if not clips:
        raise DavinciBlocked(
            f"导入失败：{chart}。检查运行 Resolve 的用户对该目录有无读权限，"
            "以及 Resolve 是否仍被对话框阻塞。")

    pool = project.GetMediaPool()
    timeline = pool.CreateTimelineFromClips("calib", clips)
    if timeline is None:
        raise DavinciBlocked("建立时间线失败。")
    project.SetCurrentTimeline(timeline)

    out = workdir / "export"
    out.mkdir(parents=True, exist_ok=True)
    project.SetCurrentRenderFormatAndCodec("png", "RGB16")
    project.SetRenderSettings({"TargetDir": str(out), "CustomName": "davinci_out",
                               "SelectAllFrames": True,
                               "FormatWidth": width, "FormatHeight": height})
    job = project.AddRenderJob()
    if not job:
        raise DavinciBlocked("无法建立渲染任务。")
    project.StartRendering(job)
    while project.IsRenderingInProgress():
        time.sleep(2)

    exported = sorted(p for p in out.iterdir() if p.suffix.lower() == ".png")
    if not exported:
        raise DavinciBlocked(f"渲染完成但导出目录里没有 PNG：{out}")

    exported_size = _image_size(exported[0])
    assert_sizes_match(exported_size, (width, height))

    before = cal.sample_chart(chart, manifest)
    after = cal.sample_chart(exported[0], manifest)
    from color_diff import delta_e_2000, rgb8_to_lab

    deltas = []
    per_group: dict = {}
    for a, b in zip(before["samples"], after["samples"]):
        value = delta_e_2000(rgb8_to_lab(*a["measured_rgb"]), rgb8_to_lab(*b["measured_rgb"]))
        deltas.append(value)
        per_group.setdefault(a["group"], []).append(value)
    deltas.sort()
    median = deltas[len(deltas) // 2]
    p95 = deltas[int(len(deltas) * 0.95)]
    worst_index = max(range(len(before["samples"])),
                      key=lambda i: delta_e_2000(
                          rgb8_to_lab(*before["samples"][i]["measured_rgb"]),
                          rgb8_to_lab(*after["samples"][i]["measured_rgb"])))
    worst = before["samples"][worst_index]
    group_medians = {name: round(sorted(values)[len(values) // 2], 4)
                     for name, values in per_group.items()}

    # 逐块读数全部落盘：有了它，导出的 PNG 就是冗余的，不必把图片带进开源仓库
    # （仓库有一条「不夹带任何图片」的守卫，不该为了留证据给它开例外）。
    patches = [
        {"id": a["id"], "group": a["group"],
         "reference_rgb": a["reference_rgb"],
         "before_rgb": a["measured_rgb"], "after_rgb": b["measured_rgb"],
         "delta_e00": round(delta_e_2000(rgb8_to_lab(*a["measured_rgb"]),
                                         rgb8_to_lab(*b["measured_rgb"])), 4)}
        for a, b in zip(before["samples"], after["samples"])
    ]
    return {
        "schema_version": cal.CALIBRATION_SCHEMA_VERSION,
        "app": "DaVinci Resolve",
        "app_version": resolve.GetVersionString(),
        "chart_size": [width, height],
        "exported_size": list(exported_size),
        "settings_applied": applied,
        "settings_verified": {"superScale": super_scale},
        "identity_check": {
            "question": "什么都不做的导出，画面变了没有",
            "patch_count": len(deltas),
            "delta_e00_median": round(median, 4),
            "delta_e00_p95": round(p95, 4),
            "delta_e00_max": round(deltas[-1], 4),
            "by_group_median": group_medians,
            "worst_patch": {
                "id": worst["id"], "group": worst["group"],
                "reference_rgb": worst["reference_rgb"],
                "before_rgb": worst["measured_rgb"],
                "after_rgb": after["samples"][worst_index]["measured_rgb"],
            },
            "patches": patches,
            "exported_frames": len(exported),
            "sampled_frame": exported[0].name,
            "identity": median < 1.0,
            "meaning": "median < 1.0 视为管线恒等；否则用户在 Resolve 里复现我们的参数"
                       "会带着一个系统偏移，必须先把这个偏移标定出来",
        },
        "boundary": "这是管线恒等性，不是参数对应关系。参数标定要在此基线成立之后再做。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DaVinci Resolve 真实标定")
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        result = run(workdir)
    except DavinciBlocked as error:
        payload = {"status": "blocked", "reason": str(error),
                   "boundary": "未产生真实导出，因此不写任何标定结论。"}
        (workdir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 4
    result["status"] = "completed"
    (workdir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
