# BLCaptain Color Formula v4.9.0

首个公开测试版：把“先看懂画面、再确认方案、最后本地生成”做成普通用户可以安装使用的照片／视频调色 Skill。

## 你能做什么

- 把照片或普通 SDR 视频交给 Codex，先得到真实像素诊断与最多 3 个安全方向。
- 查看风格、强度、风险和输出位置；只有确认当前 `plan_id` 后才生成。
- 同时得到成片、前后对比和回执；原文件不覆盖，修改从原片重新开始。
- 在指导模式中获得 iPhone 照片、醒图、Lightroom、剪映、Premiere Pro、DaVinci Resolve 的手工调色起点。

## 当前公开范围

- 3 个 `active` 自动推荐方向。
- 1 个 `manual-executable` 点名执行方向。
- 28 个 `research` 研究候选，不向普通用户生成正式计划。

目录存在、自动检查通过和单素材人工接受均不等于全部审美完成。视频输出仍需完整连续回放。

## 安装

已验证：macOS、Python 3.9+、FFmpeg／ffprobe。Windows 与 Linux 尚未完成冷安装验收。

1. 下载 `blcaptain-color-formula-4.9.0.zip` 与 `.sha256`。
2. 校验文件后解压。
3. 在解压目录运行 `python3 scripts/install_skill.py`。
4. 重启 Codex，发送：“用 BLCaptain 调色公式，帮我实际调色这张照片。”

## 完整性

- Release ZIP：`blcaptain-color-formula-4.9.0.zip`
- SHA-256：`0e76171f4f869ba726833aa019c4c5cdc14503960b436a048023d7de34956ae4`
- 白名单内容文件：154
- 随包用户媒体：0
- Git 元数据：0
- 历史证据、过程账本与开发工作区：0

候选包已从全新临时目录解压并安装，随后对真实 PNG 完成 `suggest → plan → render`；原图哈希不变，成片、对比图和回执齐全。此结果证明首次链路可运行，不代替队长对公开演示成片的审美确认。

## 隐私与许可

核心链本地处理，没有项目账号、遥测或自建上传服务器。原始计划与回执含本机路径和素材哈希，公开分享前需脱敏。MIT 只覆盖项目代码与原创文档，不替代 FFmpeg、编解码器、模型、平台或用户媒体许可。

- [README](https://github.com/dososo/blcaptain-color-formula#readme)
- [English README](https://github.com/dososo/blcaptain-color-formula/blob/main/README.en.md)
- [Privacy](https://github.com/dososo/blcaptain-color-formula/blob/main/PRIVACY.md)
- [Third-party notices](https://github.com/dososo/blcaptain-color-formula/blob/main/THIRD_PARTY_NOTICES.md)
- [Issues](https://github.com/dososo/blcaptain-color-formula/issues)

发布视频中的音乐与许可信息见仓库 `showcase/VIDEO_CREDITS.md`。
