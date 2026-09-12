"""新源保护合同：替身不证明模型能力，真实 FFmpeg 只验证编码与时钟。"""
import hashlib
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import korean_cool_protection as protection


class PersonBackend:
    def analyze(self, frame, directory):
        w, h = protection.memory._dimensions(frame)
        path = directory / "person.pgm"
        protection.memory._write_pgm(path, w, h, bytes([0, 64, 128, 255] * (w * h // 4)))
        return {"classes": {"person": {"mask_path": str(path), "coverage": .5}},
                "backend": {"backend": "test-only"}}


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.path = self.root / "source.ppm"
        self.path.write_bytes(b"P6\n8 4\n255\n" + bytes([70, 90, 110] * 32))
        self.source = {"path": str(self.path), "sha256": protection._sha(self.path),
                       "media_type": "photo", "width": 8, "height": 4,
                       "color": {"profile": "srgb", "support": "direct"}}
        self.backend = patch.object(protection, "_backend", return_value=(PersonBackend(), {"backend": "test-only"}))
        self.backend.start()

    def tearDown(self):
        self.backend.stop()
        self.folder.cleanup()

    def prepare(self, foundation_filter="null"):
        return protection.prepare(self.source, self.root / "evidence", foundation_filter)

    def test_photo_evidence_preserves_soft_mask_weights_and_source(self):
        result = self.prepare()
        protection.validate(result, self.source)
        self.assertEqual(result["mask"]["encoding"], "full-range-gray8")
        self.assertEqual(result["mask"]["frame_count"], 1)
        raw = protection.memory._read_raw(Path(result["mask"]["path"]), "gray", 8, 4)
        self.assertEqual(sorted(set(raw)), [0, 64, 128, 255])
        self.assertEqual(protection._sha(self.path), self.source["sha256"])

    def test_missing_dependency_rejects_before_creating_workdir(self):
        with patch.object(protection, "_backend", side_effect=protection.ProtectionError("缺依赖")):
            with self.assertRaises(protection.ProtectionError):
                self.prepare()
        self.assertFalse((self.root / "evidence").exists())

    def test_absent_person_rejects(self):
        with patch.object(PersonBackend, "analyze", return_value={"classes": {}}):
            with self.assertRaisesRegex(protection.ProtectionError, "人物"):
                self.prepare()

    def test_changed_source_mask_module_and_backend_are_each_rejected(self):
        result = self.prepare()
        self.path.write_bytes(b"changed")
        with self.assertRaisesRegex(protection.ProtectionError, "原片"):
            protection.validate(result, self.source)
        self.path.write_bytes(b"P6\n8 4\n255\n" + bytes([70, 90, 110] * 32))
        with patch.object(protection, "_implementation", return_value={}):
            with self.assertRaisesRegex(protection.ProtectionError, "模块"):
                protection.validate(result, self.source)
        with patch.object(protection, "_backend", return_value=(PersonBackend(), {"backend": "changed"})):
            with self.assertRaisesRegex(protection.ProtectionError, "后端"):
                protection.validate(result, self.source)
        Path(result["mask"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(protection.ProtectionError, "蒙版"):
            protection.validate(result, self.source)

    def test_evidence_tamper_and_reused_directory_reject(self):
        result = self.prepare()
        result["person_coverage"] = [1]
        with self.assertRaisesRegex(protection.ProtectionError, "证据内容"):
            protection.validate(result, self.source)
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_p3_analysis_only_leaves_original_unchanged(self):
        self.source["color"]["profile"] = "display-p3"
        result = self.prepare()
        self.assertEqual(result["source_binding"]["color"]["profile"], "display-p3")
        self.assertEqual(protection._sha(self.path), self.source["sha256"])

    def test_full_relative_pts_reject_vfr_even_when_average_rate_matches(self):
        def payload(pts):
            return {"streams": [{"width": 8, "height": 4, "avg_frame_rate": "30/1", "time_base": "1/90000"}],
                    "frames": [{"best_effort_timestamp": n} for n in pts]}
        with patch.object(protection, "_probe", return_value=payload([900, 3900, 6900])):
            timeline = protection._timeline(self.path)
            self.assertEqual(timeline["relative_pts"], ["0", "1/30", "1/15"])
            self.assertEqual(timeline["source_start_pts"], 900)
        with patch.object(protection, "_probe", return_value=payload([900, 2400, 6900])):
            with self.assertRaisesRegex(protection.ProtectionError, "变帧率"):
                protection._timeline(self.path)

    def test_ffv1_mask_roundtrip_preserves_weights_and_all_timestamps(self):
        masks = self.root / "masks"
        masks.mkdir()
        data = bytes([0, 16, 64, 128, 200, 255, 32, 100] * 4)
        for n in range(3):
            protection.memory._write_pgm(masks / f"mask-{n:06d}.pgm", 8, 4, data)
        path = self.root / "mask.mkv"
        protection.memory.assemble_mask_video(masks, path, "60000/1001", 3)
        timeline = protection._timeline(path, "60000/1001")
        self.assertEqual(timeline["frame_count"], 3)
        self.assertEqual(timeline["fps_fraction"], "60000/1001")
        decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
                                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
        self.assertEqual(decoded, data * 3)

    def test_output_timeline_rejects_shortened_or_shifted_output(self):
        timeline = {"fps_fraction": "30/1", "time_base": "1/30", "frame_count": 3,
                    "source_start_pts": 7, "relative_pts": ["0", "1/30", "1/15"]}
        actual = {**timeline, "source_start_pts": 0}
        with patch.object(protection, "_timeline", return_value=actual):
            protection.validate_output_timeline({"timeline": timeline}, self.path)
        for wrong in ({**actual, "frame_count": 2}, {**actual, "source_start_pts": 1},
                      {**actual, "relative_pts": ["0", "1/30", "1/5"]}):
            with patch.object(protection, "_timeline", return_value=wrong):
                with self.assertRaises(protection.ProtectionError):
                    protection.validate_output_timeline({"timeline": timeline}, self.path)

    def test_neutral_white_ramp_has_no_hard_threshold_jump(self):
        ramp = bytes(channel for value in range(130, 191) for channel in (value, value, value))
        weights = protection.neutral_white_weights(ramp)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(weights, weights[1:])), 20,
                             "连续灰阶不能在白位门槛突然从0跳到255，形成冷暖斑块")
        self.assertGreater(sum(0 < value < 255 for value in weights), 15)
        self.assertEqual(weights[-1], 255)

    def test_soft_white_keeps_original_core_and_colored_highlights_unprotected(self):
        pixels = [(r, g, b) for r in range(0, 256, 17) for g in range(0, 256, 17)
                  for b in range(0, 256, 17)]
        weights = protection.neutral_white_weights(bytes(v for pixel in pixels for v in pixel))
        for (r, g, b), weight in zip(pixels, weights):
            luma = (.2126 * r + .7152 * g + .0722 * b) / 255
            chroma = (max(r, g, b) - min(r, g, b)) / 255
            if luma >= .68 and chroma <= .11:
                self.assertEqual(weight, 255)
            if luma <= .56 or chroma >= .17:
                self.assertEqual(weight, 0)

    def test_soft_transition_does_not_change_person_coverage_or_shrink_old_union(self):
        self.path.write_bytes(b"P6\n8 4\n255\n" + bytes([160, 160, 160] * 32))
        self.source["sha256"] = protection._sha(self.path)
        result = self.prepare()
        self.assertEqual(result["person_coverage"], [.5])
        raw = protection.memory._read_raw(Path(result["mask"]["path"]), "gray", 8, 4)
        old = bytes([0, 64, 128, 255] * 8)
        self.assertTrue(all(new >= previous for new, previous in zip(raw, old)))
        self.assertGreater(raw[0], 0)
        self.assertEqual(raw[3], 255)

    def test_foundation_is_required_and_bound_to_evidence(self):
        self.assertIn("foundation_filter", inspect.signature(protection.prepare).parameters)
        with self.assertRaises(TypeError):
            protection.prepare(self.source, self.root / "missing")
        result = self.prepare("null")
        self.assertEqual(result["foundation_filter"], "null")
        result["foundation_filter"] = "hflip"
        with self.assertRaisesRegex(protection.ProtectionError, "证据内容"):
            protection.validate(result, self.source)

    def test_foundation_runs_before_downsampling_real_pixels(self):
        self.assertIn("foundation_filter", inspect.signature(protection.prepare).parameters)
        self.path.write_bytes(b"P6\n1920 4\n255\n" + bytes([32, 32, 32, 224, 224, 224] * 3840))
        self.source.update(width=1920, height=4, sha256=protection._sha(self.path))
        baseline = "curves=all='0/0 .4/.8 1/1'"
        result = self.prepare(baseline)
        def analyze(chain):
            return subprocess.check_output(["ffmpeg", "-v", "error", "-filter_complex_threads", "1",
                "-i", str(self.path), "-filter_complex", f"[0:v]{chain}[out]", "-map", "[out]",
                "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        expected = analyze(baseline + ",format=gbrp16le,scale=960:2:flags=lanczos,format=rgb24")
        wrong_order = analyze("scale=960:2:flags=lanczos,format=rgb24," + baseline + ",format=gbrp16le,format=rgb24")
        self.assertNotEqual(expected, wrong_order)
        self.assertEqual(result["foundation_analysis_sha256"], [hashlib.sha256(expected).hexdigest()])
        self.assertEqual(result["analysis_dimensions"], [960, 2])

    def test_foundation_union_keeps_old_core_and_does_not_repeat_person_detection(self):
        self.assertIn("foundation_filter", inspect.signature(protection.prepare).parameters)
        class CountingBackend(PersonBackend):
            count = 0
            def analyze(inner, frame, directory):
                inner.count += 1
                return super().analyze(frame, directory)
        backend = CountingBackend()
        with patch.object(protection, "_backend", return_value=(backend, {"backend": "test-only"})):
            result = self.prepare("lutrgb=r=200:g=200:b=200")
        self.assertEqual(backend.count, 1)
        self.assertEqual(result["person_coverage"], [.5])
        self.assertEqual(protection.memory._read_raw(Path(result["mask"]["path"]), "gray", 8, 4), bytes([255] * 32))
        self.path.write_bytes(b"P6\n8 4\n255\n" + bytes([200, 200, 200] * 32))
        self.source["sha256"] = protection._sha(self.path)
        dimmed = protection.prepare(self.source, self.root / "dimmed", "colorchannelmixer=rr=.3:gg=.3:bb=.3")
        self.assertEqual(protection.memory._read_raw(Path(dimmed["mask"]["path"]), "gray", 8, 4), bytes([255] * 32))

    def test_foundation_stream_rejects_missing_extra_frames_and_ffmpeg_failure(self):
        self.assertIn("foundation_filter", inspect.signature(protection.prepare).parameters)
        video = self.root / "source.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=8x4:r=3:d=1",
                        "-vf", "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709",
                        "-c:v", "ffv1", "-color_primaries", "bt709", "-color_trc", "bt709",
                        "-colorspace", "bt709", "-color_range", "tv", str(video)], check=True)
        source = {**self.source, "path": str(video), "sha256": protection._sha(video), "media_type": "video",
                  "color": {"profile": "rec709-sdr", "support": "direct"}}
        for name, baseline in (("short", "select=lt(n\\,2)"), ("long", "tpad=stop_mode=clone:stop=1"),
                               ("bad", "no_such_foundation_filter")):
            with self.subTest(name=name):
                with self.assertRaisesRegex(protection.ProtectionError, "Foundation"):
                    protection.prepare(source, self.root / name, baseline)
                self.assertFalse((self.root / name / "evidence.json").exists())


if __name__ == "__main__":
    unittest.main()
