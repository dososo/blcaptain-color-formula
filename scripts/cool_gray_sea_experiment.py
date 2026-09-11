"""冷灰海水：绑定3K1-SEA-02单源、55%结构链和最终媒体门。"""
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

try:
    from . import cool_gray_sea_structure_v2 as structure
    from . import cool_gray_sea_final_check_v2 as final_check
    from . import korean_cool_grade as media
except ImportError:
    import cool_gray_sea_structure_v2 as structure
    import cool_gray_sea_final_check_v2 as final_check
    import korean_cool_grade as media


SOURCE_HASH = "183b0a0d4cc87047d0d8b718dce29fcf0badf92d1a51244861f8392629b5036a"
FRAME_COUNT = 294
FPS = Fraction(24000, 1001)
WIDTH = 1280
HEIGHT = 720


def _read_frame(pipe):
    size = WIDTH * HEIGHT * 3
    data = pipe.read(size)
    if not data:
        return None
    if len(data) != size:
        raise ValueError("冷灰海水RGB帧不完整")
    return np.frombuffer(data, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3).astype(np.float32) / 255


def render(source, candidate, workdir):
    source, candidate, workdir = Path(source), Path(candidate), Path(workdir)
    if media._sha256(source) != SOURCE_HASH:
        raise ValueError("冷灰海水只接受绑定3K1-SEA-02原片")
    probe = media._probe(source)
    video = media._video_stream(probe)
    if media._frame_count(video) != FRAME_COUNT or Fraction(video["avg_frame_rate"]) != FPS:
        raise ValueError("冷灰海水源时序合同不符")
    if any(stream["codec_type"] == "audio" for stream in probe["streams"]):
        raise ValueError("冷灰海水绑定原片应无音轨")
    workdir.mkdir(parents=True)
    decode = subprocess.Popen([
        "ffmpeg", "-v", "error", "-i", str(source), "-vf",
        "format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], stdout=subprocess.PIPE)
    encode_command = [
        "ffmpeg", "-v", "error", "-n", "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS), "-i", "pipe:0",
        "-vf", "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p,"
               "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-threads", "2",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "tv", "-movflags", "+faststart", str(candidate),
    ]
    encode_log = (workdir / "encode.log").open("w")
    encode = subprocess.Popen(encode_command, stdin=subprocess.PIPE, stderr=encode_log)
    count = 0
    try:
        while True:
            rgb = _read_frame(decode.stdout)
            if rgb is None:
                break
            encode.stdin.write(np.uint8(np.rint(structure.grade(rgb, .55) * 255)).tobytes())
            count += 1
        decode.stdout.close()
        if decode.wait() != 0 or count != FRAME_COUNT:
            raise ValueError("冷灰海水原片解码失败或帧数不符")
        encode.stdin.close()
        if encode.wait() != 0:
            raise ValueError("冷灰海水成片编码失败")
    finally:
        if decode.poll() is None:
            decode.terminate()
            decode.wait()
        if encode.poll() is None:
            encode.terminate()
            encode.wait()
        encode_log.close()
    measured = final_check.measure(source, candidate)
    gate = final_check.evaluate(measured)
    report = {"measurements": measured, "gate": gate}
    (workdir / "final-gates.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if gate["status"] != "passed":
        raise ValueError("冷灰海水最终结构或端点门未通过，不交付")
    return {
        "command": encode_command,
        "frames": count,
        "final_gates": report,
        "probe": media._probe(candidate),
        "foundation": "恒等；直接从绑定显示参照原片执行一次Creative Look",
        "mask_boundary": "固定纵向海面带，不是海天岸语义分割，不适用于移动构图",
    }
