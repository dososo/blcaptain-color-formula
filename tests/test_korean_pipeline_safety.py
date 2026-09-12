"""真实 FFmpeg 像素与时间轴合同；隔离证据校验，不代表分割后端或审美验收。"""
import array
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import blcaptain_color as engine  # noqa: E402
import korean_cool_execution as korean  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
WIDTH, HEIGHT = 96, 32
BASELINE = "curves=all='0/0 .5/.65 1/1'"


@unittest.skipUnless(FFMPEG and FFPROBE, "需要真实 ffmpeg 与 ffprobe")
class KoreanPipelineSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="korean-pixel-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_ffmpeg(self, arguments, data=None):
        result = subprocess.run(
            [FFMPEG, "-v", "error", "-n", *arguments], input=data,
            capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout

    def probe(self, path, frames=False):
        args = [FFPROBE, "-v", "error", "-show_streams"]
        if frames:
            args += ["-select_streams", "v:0", "-show_frames"]
        result = subprocess.run(
            [*args, "-of", "json", str(path)], capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return json.loads(result.stdout)

    def photo_plan(self):
        source, mask = self.root / "source.png", self.root / "mask.pgm"
        # 同一中性原片，三条蒙版带分别为不保护、128/255、完整保护。
        pixels = struct.pack(">HHH", 22001, 22003, 22007) * WIDTH * HEIGHT
        ppm = f"P6\n{WIDTH} {HEIGHT}\n65535\n".encode() + pixels
        plan = {
            "source": {"path": str(source), "media_type": "photo",
                       "width": WIDTH, "height": HEIGHT},
            "render_mix": 1.0,
            "color_pipeline": {"output_profile": "Display P3", "pixel_format": "rgb48be"},
            "korean_cool_protection": {"mask": {"path": str(mask)}},
        }
        self.run_ffmpeg([
            "-f", "image2pipe", "-c:v", "ppm", "-i", "pipe:0",
            "-vf", engine.photo_output_filter(plan, ""),
            "-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgb48be", str(source),
        ], ppm)
        row = bytes([0]) * 32 + bytes([128]) * 32 + bytes([255]) * 32
        mask.write_bytes(f"P5\n{WIDTH} {HEIGHT}\n255\n".encode() + row * HEIGHT)
        return plan

    def render(self, plan, name, strength=None, include_audio=False, filter_threads=None):
        output = self.root / name
        # 仅隔离另一个模块负责的身份/来源证据；构图、滤镜、编码及文件均真实。
        with patch.object(korean, "validate"):
            args, graph = korean.command(
                plan, BASELINE, output, strength, include_audio=include_audio)
        if filter_threads is not None:
            args[args.index("-filter_complex_threads") + 1] = str(filter_threads)
        result = subprocess.run(args, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(output.is_file())
        return output, graph

    def rgb16(self, path):
        raw = self.run_ffmpeg([
            "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
            "-pix_fmt", "rgb48le", "pipe:1",
        ])
        values = array.array("H")
        values.frombytes(raw)
        if sys.byteorder != "little":
            values.byteswap()
        self.assertEqual(len(values), WIDTH * HEIGHT * 3)
        return values

    def pixel(self, values, x):
        offset = ((HEIGHT // 2) * WIDTH + x) * 3
        return list(values[offset:offset + 3])

    def foundation_photo(self, plan):
        output = self.root / "foundation-reference.png"
        self.run_ffmpeg([
            "-i", plan["source"]["path"], "-frames:v", "1", "-vf",
            engine.photo_output_filter(plan, BASELINE), "-c:v", "png",
            "-pix_fmt", "rgb48be", str(output),
        ])
        return self.rgb16(output)

    def test_zero_mix_matches_independent_foundation_pixels(self):
        plan = self.photo_plan()
        output, _ = self.render(plan, "zero.png", strength=0.0)
        self.assertEqual(self.rgb16(output), self.foundation_photo(plan))

    def test_adjacent_blue_pixels_do_not_collapse_to_gray_at_channel_boundary(self):
        # 从真实失焦蓝色渐变提取的相邻16位像素；不依赖原照片或人物后端。
        colors = [(3330, 18363, 32656), (4220, 19138, 32388)]
        mask = self.root / 'blue-boundary.pgm'
        mask.write_bytes(b'P5\n2 1\n255\n' + bytes([0, 0]))
        plan = {'source': {'media_type': 'photo', 'width': 2, 'height': 1},
                'render_mix': 1.0,
                'color_pipeline': {'output_profile': 'sRGB', 'pixel_format': 'rgb48be'}}
        graph = korean.build_graph(plan, 'null')
        raw = self.run_ffmpeg([
            '-filter_complex_threads', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb48le',
            '-s', '2x1', '-i', 'pipe:0', '-i', str(mask), '-filter_complex', graph,
            '-map', '[out]', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb48le', 'pipe:1',
        ], b''.join(struct.pack('<HHH', *rgb) for rgb in colors))
        result = [struct.unpack_from('<HHH', raw, offset) for offset in (0, 6)]
        for rgb in result:
            self.assertGreater(rgb[2] - rgb[0], 10000, '连续蓝色不应因通道触边突然变灰')
        self.assertLess(max(abs(a - b) for a, b in zip(*result)), 4096,
                        '相邻小差异不能被保亮算子变成大幅色彩跳变')

    def test_full_mask_restores_same_position_foundation_not_source(self):
        plan = self.photo_plan()
        output, _ = self.render(plan, "protected.png")
        actual = self.pixel(self.rgb16(output), 80)
        foundation = self.pixel(self.foundation_photo(plan), 80)
        original = self.pixel(self.rgb16(Path(plan["source"]["path"])), 80)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, foundation)), 2)
        self.assertGreater(min(abs(a - b) for a, b in zip(actual, original)), 1000)

    def test_zero_mask_cools_environment(self):
        plan = self.photo_plan()
        output, _ = self.render(plan, "environment.png")
        red, _, blue = self.pixel(self.rgb16(output), 16)
        base_red, _, base_blue = self.pixel(self.foundation_photo(plan), 16)
        self.assertGreater((blue - red) - (base_blue - base_red), 1000)

    def test_half_mask_uses_full_range_linear_weight(self):
        plan = self.photo_plan()
        output, graph = self.render(plan, "half.png")
        values = self.rgb16(output)
        environment, middle, protected = [self.pixel(values, x) for x in (16, 48, 80)]
        expected = [a * (127 / 255) + b * (128 / 255)
                    for a, b in zip(environment, protected)]
        self.assertLessEqual(max(abs(a - b) for a, b in zip(middle, expected)), 4)
        self.assertNotIn("val-16", graph.replace(" ", ""))

    def test_photo_keeps_real_16_bit_display_p3_labels(self):
        plan = self.photo_plan()
        output, _ = self.render(plan, "p3.png", strength=0.55)
        stream = self.probe(output)["streams"][0]
        self.assertEqual(
            tuple(stream.get(key) for key in (
                "pix_fmt", "color_space", "color_transfer", "color_primaries", "color_range")),
            ("rgb48be", "gbr", "iec61966-2-1", "smpte432", "pc"),
        )
        self.assertEqual(output.read_bytes()[24], 16)  # PNG IHDR 位深，不只检查文件后缀。
        self.assertEqual(engine.validate_photo_color(output, plan)["status"], "passed")

    def test_cfr_video_matches_every_mask_frame_and_preserves_longer_audio(self):
        source, mask = self.root / "source.mp4", self.root / "mask.mkv"
        frames = b"".join(bytes([60 + i * 10]) * WIDTH * HEIGHT * 3 for i in range(12))
        self.run_ffmpeg([
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
            "-r", "12", "-i", "pipe:0", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.5",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-crf", "0",
            "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", "-c:a", "aac", str(source),
        ], frames)
        masks = b"".join(bytes([255 if i % 2 else 0]) * WIDTH * HEIGHT for i in range(12))
        self.run_ffmpeg([
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{WIDTH}x{HEIGHT}",
            "-r", "12", "-i", "pipe:0", "-c:v", "ffv1", "-pix_fmt", "gray",
            "-color_range", "pc", str(mask),
        ], masks)
        plan = {
            "source": {"path": str(source), "media_type": "video",
                       "width": WIDTH, "height": HEIGHT},
            "render_mix": 1.0,
            "korean_cool_protection": {
                "mask": {"path": str(mask)}, "timeline": {"fps_fraction": "12/1"},
            },
        }
        output, _ = self.render(plan, "graded.mp4", include_audio=True)
        baseline, _ = self.render(plan, "foundation.mp4", strength=0.0)
        info = self.probe(output, frames=True)
        times = [float(frame["best_effort_timestamp_time"]) for frame in info["frames"]]
        self.assertEqual(len(times), 12)
        for index, timestamp in enumerate(times):
            self.assertAlmostEqual(timestamp, index / 12, places=5)
        stream = info["streams"][0]
        self.assertEqual((stream["r_frame_rate"], stream["color_primaries"],
                          stream["color_transfer"], stream["color_space"]),
                         ("12/1", "bt709", "bt709", "bt709"))
        decoded = [self.run_ffmpeg([
            "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ]) for path in (output, baseline)]
        frame_size = WIDTH * HEIGHT * 3
        self.assertEqual([len(data) for data in decoded], [12 * frame_size] * 2)
        for index in range(12):
            offset = index * frame_size + ((HEIGHT // 2) * WIDTH + WIDTH // 2) * 3
            actual, base = [list(data[offset:offset + 3]) for data in decoded]
            if index % 2:
                self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, base)), 3,
                                     f"第 {index} 帧应恢复本帧 Foundation")
            else:
                self.assertGreater((actual[2] - actual[0]) - (base[2] - base[0]), 10,
                                   f"第 {index} 帧应保留环境冷化")
        audio_payloads = [self.run_ffmpeg([
            "-i", str(path), "-map", "0:a:0", "-c:a", "copy", "-f", "data", "pipe:1",
        ]) for path in (source, output)]
        self.assertTrue(audio_payloads[0])
        self.assertEqual(*audio_payloads)
        source_audio, output_audio = [next(
            item for item in self.probe(path)["streams"] if item["codec_type"] == "audio"
        ) for path in (source, output)]
        self.assertGreater(float(output_audio["duration"]), 1.45)
        self.assertAlmostEqual(float(source_audio["duration"]),
                               float(output_audio["duration"]), places=5)

    def test_one_and_four_filter_threads_have_identical_full_video_pixels(self):
        width, height, count = 1280, 720, 8
        source, mask = self.root / "thread-source.mp4", self.root / "thread-mask.mkv"
        colors = [(245, 245, 245), (170, 120, 90), (35, 110, 160), (60, 140, 70),
                  (180, 50, 70), (180, 140, 35), (18, 18, 20), (128, 128, 128)]
        weights = [0, 16, 64, 128, 200, 255, 96, 224]
        # 每帧颜色和保护权重均移动，覆盖高光、暗部、综合色彩与软蒙版。
        frames = b"".join(b"".join(bytes(colors[(column + frame) % 8]) * (width // 8)
                                   for column in range(8)) * height for frame in range(count))
        masks = b"".join(b"".join(bytes([weights[(column + 3 * frame) % 8]]) * (width // 8)
                                  for column in range(8)) * height for frame in range(count))
        self.run_ffmpeg([
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
            "-r", "8", "-i", "pipe:0", "-c:v", "libx264", "-crf", "0",
            "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", str(source),
        ], frames)
        self.run_ffmpeg([
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}",
            "-r", "8", "-i", "pipe:0", "-c:v", "ffv1", "-pix_fmt", "gray",
            "-color_range", "pc", str(mask),
        ], masks)
        plan = {
            "source": {"path": str(source), "media_type": "video",
                       "width": width, "height": height},
            "render_mix": .55,
            "korean_cool_protection": {
                "mask": {"path": str(mask)}, "timeline": {"fps_fraction": "8/1"},
            },
        }
        for strength in (0.0, .55):
            with self.subTest(strength=strength):
                decoded = []
                for threads in (1, 4):
                    output, _ = self.render(
                        plan, f"threads-{threads}-strength-{strength}.mp4",
                        strength=strength, filter_threads=threads)
                    info = self.probe(output, frames=True)
                    self.assertEqual((info["streams"][0]["width"], info["streams"][0]["height"]),
                                     (width, height))
                    self.assertEqual(len(info["frames"]), count)
                    for index, frame in enumerate(info["frames"]):
                        self.assertAlmostEqual(float(frame["best_effort_timestamp_time"]), index / 8, places=5)
                    raw = self.run_ffmpeg([
                        "-i", str(output), "-map", "0:v:0", "-fps_mode", "passthrough",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                    ])
                    self.assertEqual(len(raw), width * height * 3 * count)
                    decoded.append(raw)
                self.assertTrue(decoded[0] == decoded[1], "1 与 4 滤镜线程的全帧逐像素输出不一致")


if __name__ == "__main__":
    unittest.main()
