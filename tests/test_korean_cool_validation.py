"""内核统计合同；合成像素只验证测量，不证明人物分割或审美能力。"""
import builtins
import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import korean_cool_grade as grade
from scripts import korean_cool_protection as protection


class MaskBackend:
    weights = [255, 243, 242, 0]

    def analyze(self, frame, directory):
        w, h = protection.memory._dimensions(frame)
        path = directory / "person.pgm"
        protection.memory._write_pgm(path, w, h, bytes(self.weights * (w * h // 4)))
        return {"classes": {"person": {"mask_path": str(path), "coverage": .5}},
                "backend": {"backend": "test-only"}}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("scripts.korean_cool_validation")
        except ModuleNotFoundError:
            self.fail("缺少韩系逐帧保护内核验收模块")
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.backend = MaskBackend()
        backend_patch = patch.object(protection, "_backend", return_value=(self.backend, {"backend": "test-only"}))
        backend_patch.start()
        self.addCleanup(backend_patch.stop)

    def photo(self, name, pixels, dimensions=(8, 4)):
        path = self.root / name
        width, height = dimensions
        path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + bytes(value for pixel in pixels for value in pixel))
        return path

    def video(self, name, frames):
        path = self.root / name
        subprocess.run(["ffmpeg", "-v", "error", "-n", "-f", "rawvideo", "-pix_fmt", "rgb24",
                        "-s", "8x4", "-r", "30", "-i", "-", "-vf",
                        "scale=out_color_matrix=bt709,format=yuv444p,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
                        "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p",
                        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", str(path)],
                       input=bytes(sum((sum(frame, []) for frame in frames), [])), check=True, capture_output=True)
        return path

    def plan(self, source, media="photo", dimensions=(8, 4)):
        source = {"path": str(source), "sha256": protection._sha(source), "media_type": media,
                  "width": dimensions[0], "height": dimensions[1],
                  "color": {"profile": "srgb" if media == "photo" else "rec709-sdr", "support": "direct"}}
        # 本组统计夹具以原片作恒等 Foundation，不引入新的调色或阈值。
        evidence = protection.prepare(source, self.root / "evidence", "null")
        return {"source": source, "korean_cool_protection": evidence, "strength": .55}

    def test_photo_identity_and_exact_core_threshold(self):
        source = self.photo("base.ppm", [[150, 90, 100]] * 32)
        report = self.module.measure(self.plan(source), source, source)
        self.assertTrue(report["protection_passed"])
        self.assertEqual(report["frame_count"], 1)
        self.assertEqual(report["frames"][0]["protected_pixels"], 16)
        self.assertEqual(report["frames"][0]["rgb_abs_mean"], 0)
        self.assertFalse(report["human_accepted"])
        self.assertIn("近中性亮部", report["boundary"])
        self.assertIn("边缘", report["boundary"])

    def test_outside_core_is_not_claimed_as_protected(self):
        pixels = [[150, 90, 100]] * 32
        source = self.photo("base.ppm", pixels)
        changed = [value if n % 4 < 2 else [80, 120, 180] for n, value in enumerate(pixels)]
        output = self.photo("out.ppm", changed)
        report = self.module.measure(self.plan(source), source, output)
        self.assertTrue(report["protection_passed"])
        self.assertEqual(report["frames"][0]["rgb_abs_max"], 0)

    def test_existing_cool_luma_and_chroma_limits_are_not_relaxed(self):
        cases = [([150, 90, 100], [150, 90, 103], None),
                 ([150, 90, 100], [150, 90, 104], "protected_region_cooled"),
                 ([150, 90, 100], [154, 94, 104], None),
                 ([150, 90, 100], [155, 95, 105], "protected_luma_drifted"),
                 ([180, 80, 100], [177, 80, 97], None),
                 ([180, 80, 100], [175, 80, 95], "protected_memory_color_regressed")]
        source = self.photo("base.ppm", [[150, 90, 100]] * 32)
        plan = self.plan(source)
        for n, (base, changed, failure) in enumerate(cases):
            with self.subTest(failure=failure, changed=changed):
                baseline = self.photo(f"baseline-{n}.ppm", [base] * 32)
                output = self.photo(f"out-{n}.ppm", [changed] * 32)
                report = self.module.measure(plan, baseline, output)
                self.assertEqual(report["protection_passed"], failure is None)
                if failure:
                    self.assertIn(failure, report["frames"][0]["failures"])
        signature = dict(world_cool_bias=0, spatial_cool_partition=0, world_luma=.5,
                         protected_cool_bias=0, protected_chroma=.4, protected_luma=.5,
                         neutral_white_chroma=0, black_luma=.1, highlight_clip_ratio=0)
        changed = {**signature, "protected_cool_bias": .0121, "protected_luma": .5181,
                   "protected_chroma": .3839}
        expected = grade.evaluate_signature(signature, changed, 55)["safety_failures"]
        self.assertEqual(set(expected), {"protected_region_cooled", "protected_luma_drifted",
                                         "protected_memory_color_regressed"})

    def test_opposite_local_changes_remain_visible_in_absolute_metrics(self):
        self.backend.weights = [255] * 4
        source = self.photo("base.ppm", [[150, 90, 100]] * 32)
        output = self.photo("out.ppm", [[150, 100 if n % 2 else 80, 100] for n in range(32)])
        row = self.module.measure(self.plan(source), source, output)["frames"][0]
        self.assertAlmostEqual(row["luma_delta"], 0, places=6)
        self.assertGreater(row["luma_abs_mean"], .018)
        self.assertGreater(row["rgb_abs_mean"], 0)

    def test_empty_core_is_blocked_not_zero_error_pass(self):
        self.backend.weights = [242] * 4
        source = self.photo("base.ppm", [[150, 90, 100]] * 32)
        with self.assertRaisesRegex(ValueError, "内核"):
            self.module.measure(self.plan(source), source, source)

    def test_all_video_frames_are_measured_and_middle_failure_blocks(self):
        frames = [[[150, 90, 100]] * 32 for _ in range(3)]
        source = self.video("base.mkv", frames)
        output = self.video("out.mkv", [frames[0], [[158, 98, 108]] * 32, frames[2]])
        report = self.module.measure(self.plan(source, "video"), source, output)
        self.assertFalse(report["passed"])
        self.assertEqual(report["frame_count"], 3)
        self.assertEqual([r["frame_index"] for r in report["frames"]], [0, 1, 2])
        self.assertEqual(report["failed_frame_indices"], [1])
        self.assertEqual(report["worst_frame_index"], 1)

    def test_truncated_video_and_tampered_mask_are_rejected(self):
        frames = [[[150, 90, 100]] * 32 for _ in range(3)]
        source = self.video("base.mkv", frames)
        short = self.video("short.mkv", frames[:2])
        plan = self.plan(source, "video")
        with self.assertRaisesRegex(ValueError, "帧数"):
            self.module.measure(plan, source, short)
        mask = Path(plan["korean_cool_protection"]["mask"]["path"])
        mask.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "蒙版"):
            self.module.measure(plan, source, source)

    def test_photo_rejects_multiple_output_frames(self):
        source = self.photo("base.ppm", [[150, 90, 100]] * 32)
        output = self.video("out.mkv", [[[150, 90, 100]] * 32 for _ in range(2)])
        with self.assertRaisesRegex(ValueError, "帧数"):
            self.module.measure(self.plan(source), source, output)

    def test_missing_binding_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "保护"):
            self.module.measure({}, self.root / "missing", self.root / "missing")

    def test_signature_requires_two_axes_with_fixed_foundation_regions(self):
        base = [[150, 90, 100], [220, 220, 220], [80, 90, 100], [128, 128, 128]] * 8
        base[4] = [20, 20, 20]
        source = self.photo("base.ppm", base)
        plan = self.plan(source)
        same = self.module.measure(plan, source, source)
        self.assertTrue(same["protection_passed"])
        self.assertFalse(same["passed"])
        self.assertIn("fewer_than_two_signature_axes", same["signature_report"]["blocking_failures"])
        changed = [[121, 126, 136] if n % 4 == 3 else value for n, value in enumerate(base)]
        output = self.photo("out.ppm", changed)
        result = self.module.measure(plan, source, output)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(len(result["signature_report"]["passed_signature_axes"]), 2)
        self.assertEqual(result["signature"]["baseline"]["sample_counts"]["world"], 8)
        expected = grade.evaluate_signature(result["signature"]["baseline"],
                                            result["signature"]["candidate"], 55)
        self.assertEqual(result["signature_report"], expected)
        self.assertEqual(result.get("baseline_metrics"), result["signature"]["baseline"])
        self.assertEqual(result.get("candidate_metrics"), result["signature"]["candidate"])
        for mix, percentage in ((.3, 30), (.55, 55), (.8, 80)):
            with self.subTest(mix=mix):
                measured = self.module.measure({**plan, "strength": mix}, source, output)
                self.assertEqual(measured["signature_report"]["strength_percent"], percentage)

    def test_signature_missing_regions_never_become_zero_statistics(self):
        source = self.photo("base.ppm", [[150, 90, 100]] * 32)
        result = self.module.measure(self.plan(source), source, source)
        self.assertTrue(result["protection_passed"])
        self.assertFalse(result["passed"])
        self.assertIn("missing_signature_regions", result["signature_report"]["blocking_failures"])

    def test_numpy_is_lazy_and_missing_dependency_is_explicit(self):
        result = subprocess.run([sys.executable, "-c", "import sys; from scripts import korean_cool_validation; "
                                 "assert 'numpy' not in sys.modules"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual_import = builtins.__import__
        def unavailable(name, *args, **kwargs):
            if name == "numpy":
                raise ImportError("测试缺依赖")
            return actual_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=unavailable):
            with self.assertRaisesRegex(ValueError, "NumPy"):
                self.module.measure({}, self.root / "missing", self.root / "missing")

    def test_large_valid_rgb_has_no_floating_point_warning(self):
        import numpy as np
        self.backend.weights = [255] * 4
        pixels = np.random.default_rng(20260912).integers(0, 256, size=(320 * 180, 3)).tolist()
        source = self.photo("large.ppm", pixels, (320, 180))
        plan = self.plan(source, dimensions=(320, 180))
        try:
            with np.errstate(all="raise"):
                report = self.module.measure(plan, source, source)
        except FloatingPointError as error:
            self.fail(f"合法0到1大阵列不应触发浮点警告：{error}")
        self.assertTrue(report["protection_passed"])
        self.assertEqual(report["frames"][0]["rgb_abs_max"], 0)
        self.assertEqual(report["analysis_dimensions"], [320, 180])


if __name__ == "__main__":
    unittest.main()
