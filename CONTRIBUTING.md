# 贡献指南 / Contributing

欢迎提交可复现的错误、文档修正、平台适配证据和有明确来源边界的调色研究。

## 原则

- 不把目录存在、测试通过或单素材结果写成审美普遍成立。
- 不提交用户照片、视频、访问令牌、本机绝对路径或未获再分发许可的第三方内容。
- 新风格先写适用／避用条件和失败反例，再进入实现。
- 任何渲染改动必须保持原文件不变，并继续使用 `plan_id` 确认门。

## 最小验证

```bash
python3 -m unittest tests.test_v490_public_release tests.test_skill_package tests.test_public_beta_docs tests.test_portable_startup
python3 scripts/build_skill_package.py audit --root . --public-assets
```

完整开发回归需要 Python 3.10.18 和额外依赖；请在 Pull Request 中分别报告通过、失败、跳过和未覆盖项。

Contributions must preserve source media, confirmation gates, privacy boundaries, and the separation between technical checks and human aesthetic acceptance.
