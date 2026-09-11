"""深海航线异题材结构迁移：深蓝环境、象牙暖锚、人物保护。"""
import hashlib
import json
import subprocess
from pathlib import Path

SOURCE_HASH = "2f8adf9deeebbe776765314e78e141b6e1c5d0b189530bd18edcd397dac42fbb"
FRAME_COUNT = 288


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path):
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams",
        "-show_format", "-of", "json", str(path)]))


def build_filter_complex():
    return (
        "[0:v]format=gbrp,settb=1/24000,setpts=N*1001,split=2[base][world];"
        "[world]colorbalance=rm=-0.130:gm=0.006:bm=0.200:pl=1,"
        "curves=all='0/0 0.08/0.060 0.25/0.212 0.50/0.478 0.78/0.808 1/1'[look];"
        "[1:v]scale=1280:720:flags=neighbor,format=gbrp,settb=1/24000,setpts=N*1001[environment];"
        "[base][look][environment]maskedmerge=planes=7[mixed];"
        "[2:v]scale=1280:720:flags=neighbor,format=gbrp,settb=1/24000,setpts=N*1001[subject];"
        "[mixed][base][subject]maskedmerge=planes=7,format=yuv420p,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709[out]"
    )


def render(source, candidate, workdir, assets):
    try:
        from . import deep_sea_transfer_final_check as final_check
    except ImportError:
        import deep_sea_transfer_final_check as final_check
    source, candidate, workdir = Path(source), Path(candidate), Path(workdir)
    if _sha256(source) != SOURCE_HASH:
        raise ValueError("深海异题材迁移只接受绑定中性夜景原片")
    resolved = {name: Path(item["path"]) for name, item in assets.items()}
    required = {"foundation", "environment_mask", "subject_mask"}
    if set(resolved) != required or resolved["foundation"] != source:
        raise ValueError("深海迁移资产集合或零调整Foundation不匹配")
    if any(not path.is_file() or _sha256(path) != assets[name]["sha256"]
           for name, path in resolved.items()):
        raise ValueError("深海迁移原片或保护代理缺失/变化")
    workdir.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-v", "error", "-n", "-i", str(source),
        "-i", str(resolved["environment_mask"]), "-i", str(resolved["subject_mask"]),
        "-filter_complex", build_filter_complex(), "-map", "[out]", "-an",
        "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-frames:v", str(FRAME_COUNT), str(candidate),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"深海异题材迁移失败：{result.stderr.strip()}")
    measured = final_check.measure(source, candidate, resolved["environment_mask"],
                                   resolved["subject_mask"])
    gate = final_check.evaluate(measured)
    report = {"measurements": measured, "gate": gate}
    (workdir / "final-gates.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if gate["status"] != "passed":
        raise ValueError("深海异题材迁移最终门未通过，不交付")
    return {"command": command, "final_gates": report,
            "probe": _probe(candidate),
            "boundary": "绑定单源与同源代理；证明视觉语法迁移，不证明通用语义跟踪"}
