"""韩系空间保护不能被静默裁成普通 3D LUT；不证明实际媒体验收。"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import blcaptain_color as engine  # noqa: E402
import lut_export  # noqa: E402


class KoreanLutBoundaryTests(unittest.TestCase):
    def plan(self, media="photo"):
        return {
            "style": {"id": "korean-cool"},
            "source": {"media_type": media},
            "parameters": {
                "brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                "gamma": 1.0, "temperature": 0.0, "tint": 0.0,
                "sharpness": 0.0, "vignette": 0.0, "grain": 0.0,
            },
            "tone_curve": None, "hsl_bands": [], "primary_grade": None,
        }

    def test_legacy_korean_plan_cannot_export_without_protection_evidence(self):
        with self.assertRaisesRegex(ValueError, "韩系清冷.*3D LUT"):
            lut_export.color_only_filter(engine, self.plan())

    def test_confirmed_korean_protection_is_not_dropped_for_either_media(self):
        for media in ("photo", "video"):
            with self.subTest(media=media):
                plan = self.plan(media)
                plan["local_grade"] = {
                    "confirmed": {"strategy": "korean-cool-protection"},
                }
                original = copy.deepcopy(plan)
                with self.assertRaisesRegex(ValueError, "韩系清冷.*3D LUT"):
                    lut_export.color_only_filter(engine, plan)
                self.assertEqual(plan, original)

    def test_export_rejects_before_processing_or_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "new" / "korean.cube"
            with patch.object(lut_export.subprocess, "run", side_effect=AssertionError(
                    "韩系拒绝必须早于启动网格处理")):
                with self.assertRaisesRegex(ValueError, "韩系清冷.*3D LUT"):
                    lut_export.export(engine, self.plan(), output, 17)
            self.assertFalse(output.parent.exists())

    def test_rejection_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "existing.cube"
            output.write_bytes(b"existing-user-lut")
            with patch.object(lut_export.subprocess, "run", side_effect=AssertionError(
                    "韩系拒绝必须早于启动网格处理")):
                with self.assertRaisesRegex(ValueError, "韩系清冷.*3D LUT"):
                    lut_export.export(engine, self.plan(), output, 17)
            self.assertEqual(output.read_bytes(), b"existing-user-lut")

    def test_other_styles_keep_existing_color_only_filter(self):
        plan = self.plan()
        plan["style"]["id"] = "natural-clean"
        original = copy.deepcopy(plan)
        expected = lut_export.color_only_filter(engine, {
            key: value for key, value in plan.items() if key != "style"
        })
        self.assertEqual(lut_export.color_only_filter(engine, plan), expected)
        self.assertEqual(plan, original)

    def test_mismatched_style_cannot_silently_drop_protection_protocol(self):
        for key in ('korean_cool_protection', 'korean_execution_sha256'):
            with self.subTest(key=key):
                plan = self.plan()
                plan['style']['id'] = 'natural-clean'
                plan[key] = None
                with self.assertRaisesRegex(ValueError, '韩系清冷.*3D LUT'):
                    lut_export.color_only_filter(engine, plan)


if __name__ == "__main__":
    unittest.main()
