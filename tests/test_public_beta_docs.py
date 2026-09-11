"""公开测试入口不得把研究范围、模型缓存或开发测试说成即装即用。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicBetaDocsTests(unittest.TestCase):
    def test_optional_install_does_not_promise_automatic_weights(self):
        for name in ('README.md', 'requirements-optional.txt'):
            text = (ROOT / name).read_text(encoding='utf-8')
            self.assertNotIn('首次使用时自动下载', text)
            self.assertNotIn('首次使用时由 transformers 自动下载', text)
            self.assertIn('仅安装依赖不会下载模型', text)

    def test_release_scope_is_visible_before_quick_start(self):
        text = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertIn('references/public-beta-scope.md', text)
        self.assertLess(text.index('references/public-beta-scope.md'),
                        text.index('## 新用户 Quick Start'))

    def test_package_does_not_claim_development_tests_are_bundled(self):
        text = (ROOT / 'README.md').read_text(encoding='utf-8')
        section = text.split('## 验证', 1)[1]
        self.assertIn('全量测试仅适用于完整开发仓库', section)

    def test_release_docs_do_not_repeat_superseded_regression_status(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        scope = (ROOT / 'references' / 'public-beta-scope.md').read_text(encoding='utf-8')
        self.assertNotIn('874 项、3 条失败', readme)
        self.assertNotIn('v4.8.3', readme)
        self.assertNotIn('1040', readme)
        self.assertNotIn('修复前不作为首发旗舰展示', scope)
        self.assertIn('v4.9.2 Release', readme)
        self.assertIn('素材级', scope)

    def test_public_docs_present_full_executable_media_contract(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        scope = (ROOT / 'references' / 'public-beta-scope.md').read_text(encoding='utf-8')
        self.assertIn('照片 31 个、视频 31 个', readme)
        self.assertIn('声明支持的公式都可直接选择、计划和执行', readme)
        self.assertIn('所有声明支持的入口都可直接选择、建立计划并执行', scope)
        for stale in ('manual-executable', '研究候选', '点名执行'):
            self.assertNotIn(stale, readme)

    def test_runtime_and_development_interpreter_contracts_are_separate(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertIn('Python 3.9+ 核心运行合同', readme)
        self.assertIn('Python 3.10.18 完整开发测试合同', readme)


if __name__ == '__main__':
    unittest.main()
