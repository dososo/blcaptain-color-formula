"""森林青绿：绑定3L1-FOREST-01单源、55%与最终全片门。"""
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

try:
    from . import forest_cyan_grade_v3 as forest
    from . import forest_cyan_final_check as final_check
    from . import korean_cool_grade as media
except ImportError:
    import forest_cyan_grade_v3 as forest
    import forest_cyan_final_check as final_check
    import korean_cool_grade as media


SOURCE_HASH = "ce86a71c6253ea43398377b790bf5b20bbf5444ddb74dc81925eb7219eaf74d0"
FRAME_COUNT = 231
FPS = Fraction(24000, 1001)
WIDTH, HEIGHT = 1280, 720


def _read_frame(pipe):
    size = WIDTH * HEIGHT * 3
    data = pipe.read(size)
    if not data:
        return None
    if len(data) != size:
        raise ValueError("森林青绿RGB帧不完整")
    return np.frombuffer(data, np.uint8).reshape(HEIGHT, WIDTH, 3).astype(np.float32) / 255


def render(source, candidate, workdir):
    source, candidate, workdir = Path(source), Path(candidate), Path(workdir)
    if media._sha256(source) != SOURCE_HASH:
        raise ValueError("森林青绿只接受绑定3L1-FOREST-01原片")
    probe = media._probe(source)
    video = media._video_stream(probe)
    if media._frame_count(video) != FRAME_COUNT or Fraction(video["avg_frame_rate"]) != FPS:
        raise ValueError("森林青绿源时序合同不符")
    if any(item["codec_type"] == "audio" for item in probe["streams"]):
        raise ValueError("森林青绿绑定原片应无音轨")
    workdir.mkdir(parents=True)
    decode = subprocess.Popen([
        "ffmpeg", "-v", "error", "-i", str(source), "-vf",
        "format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], stdout=subprocess.PIPE)
    command = [
        "ffmpeg", "-v", "error", "-n", "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS), "-i", "pipe:0",
        "-vf", "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p,"
               "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-threads", "2",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "tv", "-movflags", "+faststart", str(candidate)]
    log = (workdir / "encode.log").open("w")
    encode = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
    count = 0
    try:
        while True:
            rgb = _read_frame(decode.stdout)
            if rgb is None:
                break
            encode.stdin.write(np.uint8(np.rint(forest.grade(rgb, .55) * 255)).tobytes())
            count += 1
        decode.stdout.close()
        if decode.wait() or count != FRAME_COUNT:
            raise ValueError("森林青绿原片解码失败或帧数不符")
        encode.stdin.close()
        if encode.wait():
            raise ValueError("森林青绿成片编码失败")
    finally:
        if decode.poll() is None:
            decode.terminate(); decode.wait()
        if encode.poll() is None:
            encode.terminate(); encode.wait()
        log.close()
    measured = final_check.measure(source, candidate)
    gate = final_check.evaluate(measured)
    report = {"measurements": measured, "gate": gate}
    (workdir / "final-gates.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if gate["status"] != "passed":
        raise ValueError("森林青绿最终分离或安全门未通过，不交付")
    return {"command": command, "frames": count, "final_gates": report,
            "probe": media._probe(candidate),
            "foundation": "v3模块内部只执行一次Foundation，再执行冷青叶幕Look",
            "mask_boundary": measured["boundary"]}
