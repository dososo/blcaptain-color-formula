# BLCaptain 摄影视觉导演与调色公式 4.9.2

一张照片、一段视频 → 看懂画面，选对色彩，确认后生成新成片。

本版升级 GitHub 首页和首次使用体验：中英文首页、清晰的下载与安装入口、完整公式图谱、自然语言使用方式、环境排查和常见问题。

## 全新横竖发布视频

两版均为 48 秒、30fps，横版 1920×1080，竖版 1080×1920；分别编排，不是简单裁切。以真实照片的原片／成片揭示、同源三种风格、两组连续视频对比，串起“附上素材 → 选择方向 → 确认生成”的使用方式和完整公式图谱。

配乐采用 Michael Ramir C. 的《Gimme that Groove!》，另配置八个轻音效节点；音乐与音效已混入成片，不发布独立曲目。图片、视频、音乐与音效的作者和许可见[素材说明](https://github.com/dososo/blcaptain-color-formula/blob/main/showcase/VIDEO_CREDITS_4.9.2.md)。代码采用 MIT，第三方媒体各自遵守原许可。

- [横版视频](https://github.com/dososo/blcaptain-color-formula/releases/download/v4.9.2/blcaptain-color-formula-4.9.2-landscape.mp4)
- [竖版视频](https://github.com/dososo/blcaptain-color-formula/releases/download/v4.9.2/blcaptain-color-formula-4.9.2-portrait.mp4)

正式目录包含 32 个公式：31 个支持照片，31 个支持普通 SDR 视频。62 项同源前后对比中，13 项复用已获人工接受的结果，49 项展示公开素材上的公式方向；每份新素材仍会独立检查。

## 安装与升级

下载 `blcaptain-color-formula-4.9.2.zip` 和同名 `.sha256`，解压后运行：

```bash
python3 scripts/install_skill.py
```

已有安装时，安装器会停止且不覆盖。请先自行改名保留旧版本，再安装并重新打开 Codex。附上照片后说：“用 BLCaptain 调色公式处理这张照片，先给我最多 3 个方向，等我确认后再生成。”

已验证核心环境为 macOS、Python 3.9+、FFmpeg 与 ffprobe。Windows 与 Linux 冷安装尚未验证。支持照片和普通 SDR 视频；RAW、未识别 Log 与 HDR 不在本版保证范围。

## 验证范围

以本标签的自动检查、安装包完整性和冷安装记录为准。调色引擎未修改；本版主要核验公开文档、版本、安装与分发合同。技术通过不替代完整成片审看与用户接受。

作者：爆裂队长 NEXT（BLCaptain） · https://github.com/dososo · https://x.com/thinkszyg · blteam2026@outlook.com
