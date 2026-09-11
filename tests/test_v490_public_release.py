"""公开发布合同：普通用户入口、双语门面与公开包必须一致。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import build_skill_package as package


ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseTests(unittest.TestCase):
    def test_version_and_bilingual_readmes_are_aligned(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "4.9.1")
        for name in ("README.md", "README.en.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("4.9.1", text, name)
            self.assertIn("https://github.com/dososo/blcaptain-color-formula", text, name)

    def test_public_root_documents_are_in_the_package_contract(self):
        required = {
            "README.md", "README.en.md", "ABOUT.md", "PRIVACY.md",
            "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md",
            "THIRD_PARTY_NOTICES.md",
        }
        self.assertTrue(required <= set(package.ROOT_FILES))
        for name in required:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_readme_leads_with_install_and_first_photo_not_internal_evidence(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        first_screen = readme[:5000]
        for phrase in ("安装", "第一张照片", "不覆盖原图", "最多 3 个"):
            self.assertIn(phrase, first_screen)
        self.assertNotIn("evidence/", first_screen)
        self.assertNotIn("尚未发布", first_screen)
        self.assertNotIn("--style natural-clean", readme)

    def test_installer_copies_a_clean_skill_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "blcaptain-color-formula"
            command = [
                sys.executable, str(ROOT / "scripts" / "install_skill.py"),
                "--source", str(ROOT), "--target", str(target),
            ]
            first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue((target / "README.en.md").is_file())
            self.assertFalse((target / "evidence").exists())
            second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("不会覆盖", second.stderr)

    def test_public_docs_do_not_contain_private_paths_or_draft_notice(self):
        for name in package.ROOT_FILES:
            path = ROOT / name
            if path.suffix not in {".md", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text, name)
            self.assertNotIn("/var/folders/", text, name)
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertNotIn("（草稿）", notices)


if __name__ == "__main__":
    unittest.main()
