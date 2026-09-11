import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SkillContractTests(unittest.TestCase):
    def test_skill_entrypoint_contains_confirmation_and_non_overwrite_gates(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("用户明确确认", text)
        self.assertIn("不得覆盖原文件", text)
        self.assertIn("展示原文件与调色结果", text)
        self.assertIn("人工验收重点", text)

    def test_openai_metadata_is_usable_and_implicitly_discoverable(self):
        payload = yaml.safe_load((ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8"))
        interface = payload["interface"]
        self.assertGreaterEqual(len(interface["short_description"]), 25)
        self.assertLessEqual(len(interface["short_description"]), 64)
        self.assertIn("$blcaptain-color-formula", interface["default_prompt"])
        self.assertTrue(payload["policy"]["allow_implicit_invocation"])

    def test_open_source_package_has_license_and_install_instructions(self):
        self.assertTrue((ROOT / "LICENSE").exists())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("安装", readme)
        self.assertIn("ffmpeg", readme.lower())
        self.assertIn("数据与来源边界", readme)


if __name__ == "__main__":
    unittest.main()
