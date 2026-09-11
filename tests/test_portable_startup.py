"""新安装不依赖作者的主目录，同时保留显式固定解释器的失败门。"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.bootstrap import BootstrapError, configured_interpreter


ROOT = Path(__file__).resolve().parents[1]


class PortableStartupTests(unittest.TestCase):
    def test_current_keeps_the_invoking_interpreter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".blcaptain-interpreter").write_text("current\n", encoding="utf-8")
            self.assertEqual(configured_interpreter(root), Path(sys.executable).resolve())

    def test_explicit_missing_interpreter_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".blcaptain-interpreter").write_text(
                str(root / "missing-python"), encoding="utf-8")
            with self.assertRaises(BootstrapError):
                configured_interpreter(root)

    def test_public_entry_starts_with_a_new_home(self):
        with tempfile.TemporaryDirectory(prefix="blcaptain-new-user-") as folder:
            env = dict(os.environ, HOME=folder, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/blcaptain_color.py"), "--help"],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_quick_start_uses_only_active_examples(self):
        import json

        catalog = json.loads((ROOT / "references/recipes.json").read_text(encoding="utf-8"))
        recipes = catalog["recipes"]
        by_id = {recipe["id"]: recipe for recipe in recipes}
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## 新用户 Quick Start（视频）", 1)[1].split("### 明确记录", 1)[0]
        styles = re.findall(r"--style ([a-z0-9-]+)", section)
        self.assertTrue(styles)
        for style in styles:
            self.assertEqual(by_id[style]["status"], "active", style)

    def test_video_quick_start_shows_boundaries_before_confirmation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## 新用户 Quick Start（视频）", 1)[1].split("### 明确记录", 1)[0]
        self.assertIn("blcaptain_color.py shots --input", section)
        self.assertLess(section.index("blcaptain_color.py shots --input"),
                        section.index("--confirm-shot-boundaries"))


if __name__ == "__main__":
    unittest.main()
