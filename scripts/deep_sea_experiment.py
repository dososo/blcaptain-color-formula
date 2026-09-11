"""深海航线：绑定Pexels 2785127、v4分层资产、55%与最终媒体门。"""
import hashlib
import json
import subprocess
from pathlib import Path

try:
    from . import korean_cool_grade as media
except ImportError:
    import korean_cool_grade as media


SOURCE_HASH = "9e3eeab619e6cccd74414f0e5478621c8f1428fd513eeb617e812162effb32c6"
FRAME_COUNT = 265


def _audio_hash(path):
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-c:a", "pcm_s16le", "-f", "hash", "-hash", "sha256", "-"],
        capture_output=True, text=True)
    if result.returncode:
        raise ValueError("深海航线音轨无法完整解码")
    return result.stdout.strip()


def full_audio_remux_command(layered, source, candidate):
    return [
        "ffmpeg", "-y", "-v", "error", "-i", str(layered), "-i", str(source),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
        "-movflags", "+faststart", str(candidate),
    ]


def render(source, candidate, workdir, assets):
    try:
        from . import deep_sea_final_check as final_check
        from . import deep_sea_grade as deep
    except ImportError:
        import deep_sea_final_check as final_check
        import deep_sea_grade as deep
    source, candidate, workdir = Path(source), Path(candidate), Path(workdir)
    if media._sha256(source) != SOURCE_HASH:
        raise ValueError("深海航线只接受绑定Pexels 2785127原片")
    resolved = {name: Path(item["path"]) for name, item in assets.items()}
    if any(not path.is_file() or media._sha256(path) != assets[name]["sha256"]
           for name, path in resolved.items()):
        raise ValueError("深海航线Foundation或保护代理缺失/变化")
    workdir.mkdir(parents=True)
    layer = workdir / "deep-layer-55.mp4"
    layer_report = deep.render_deep_layer(resolved["foundation"], layer, .55)
    layered = workdir / "deep-layered-short-audio.mp4"
    rendered = deep.render_layered_video(
        resolved["foundation"], layer, resolved["environment_mask"],
        resolved["anchor_mask"], resolved["memory_mask"], source, layered)
    remux = full_audio_remux_command(layered, source, candidate)
    result = subprocess.run(remux, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"深海航线完整音轨复用失败：{result.stderr.strip()}")
    measured = final_check.measure(
        resolved["foundation"], candidate, resolved["environment_mask"], resolved["anchor_mask"])
    gate = final_check.evaluate(measured)
    report = {"measurements": measured, "gate": gate}
    (workdir / "final-gates.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if gate["status"] != "passed":
        raise ValueError("深海航线最终分层或安全门未通过，不交付")
    source_audio, candidate_audio = _audio_hash(source), _audio_hash(candidate)
    if source_audio != candidate_audio:
        raise ValueError("深海航线AAC音频内容未保持")
    rendered.update(layer=layer_report, layered_intermediate=str(layered),
                    full_audio_remux_command=remux, final_gates=report,
                    audio_sha256=source_audio, probe=media._probe(candidate),
                    mask_boundary=measured["boundary"])
    return rendered
