# BLCaptain 摄影视觉导演与调色公式 Skill v4.9.1

[中文](README.md) · [English](README.en.md) · [62 个公式-媒体入口与前后对比](FORMULAS.md) · [下载最新版](https://github.com/dososo/blcaptain-color-formula/releases/latest) · [报告问题](https://github.com/dososo/blcaptain-color-formula/issues)

把照片或普通 SDR 视频交给 Codex：它先看懂画面，最多推荐 3 个合适方向；你也可以从完整目录直接选择。确认当前 `plan_id` 后，它才在本地生成成片和前后对比。**不覆盖原图。**

> v4.9.1 公开测试版包含 32 个正式调色公式：照片 31 个、视频 31 个，声明支持的公式都可直接选择、计划和执行。每份素材仍须独立通过安全预演；“能执行”不等于“任意原图都适配”，技术通过也不等于审美通过。完整边界见 [公开测试范围](references/public-beta-scope.md)。

## 安装

已验证环境：macOS、Python **3.9** 或更高版本、FFmpeg／ffprobe。Windows 与 Linux 尚未完成冷安装验收。

### 方法一：下载 Release（推荐）

1. 从 [Latest Release](https://github.com/dososo/blcaptain-color-formula/releases/latest) 下载 `blcaptain-color-formula-4.9.1.zip` 和对应 `.sha256`。
2. 解压 ZIP，在解压后的目录运行：

```bash
python3 scripts/install_skill.py
```

3. 重新打开 Codex，附上照片并发送：

```text
用 BLCaptain 调色公式实际处理这张照片。先给我最多 3 个适合它的方向，等我确认后再生成；不要覆盖原图。
```

默认安装到 `~/.codex/skills/blcaptain-color-formula`。若同名目录已存在，安装器会停止且不会覆盖；请先自行改名保留旧版本。

### 方法二：Git 克隆

```bash
git clone https://github.com/dososo/blcaptain-color-formula.git ~/.codex/skills/blcaptain-color-formula
```

核心照片链路只需 Python 标准库与 FFmpeg。可选语义能力见 `requirements-optional.txt`；**仅安装依赖不会下载模型**，没有本地模型缓存时自动降级到全局链路。

## 第一张照片

你会依次看到：画面诊断 → 最多 3 个推荐方向 → 当前方案和唯一 `plan_id` → 确认后生成成片、前后对比、色卡与回执。想指定风格时可直接说中文名或使用目录 ID；不满意时从原图重新调整，不在旧结果上反复叠加。

推荐项的 `executable` 只表示本次素材通过了同链路预演，不是审美签字。需要修改时运行 `refine`，它只生成新方向和新确认单，不覆盖原图；成片、对比、色卡与回执任一失败都会回滚整组。

## 新用户 Quick Start（照片）

```bash
# 1. 诊断并返回最多 3 个适合当前素材的方向，不生成成片
python3 scripts/blcaptain_color.py suggest --input /照片路径/photo.jpg --mode smart --count 3 --strength 55 --display-only

# 2. 从 suggest 或完整目录中选择真实风格 ID
python3 scripts/blcaptain_color.py plan --input /照片路径/photo.jpg --style 选择的风格ID --strength 55 --output-dir /输出目录 --plan-out /输出目录/plan.json

# 3. 只确认当前计划里的 plan_id
python3 scripts/blcaptain_color.py render --plan /输出目录/plan.json --confirm-plan 当前plan_id

# 4. 不满意时从原图建立新方向，不在旧结果上叠加
python3 scripts/blcaptain_color.py refine --input /照片路径/photo.jpg --current-style 选择的风格ID --feedback "肤色回退" --strength 0.55
```

`--strength 55` 与 `--strength 0.55` 都表示 55%。计划被素材安全门拒绝时，应换方向、降低强度或换更适合的原图，不能绕过保护门。

## 新用户 Quick Start（视频）

```bash
python3 scripts/blcaptain_color.py suggest --input /视频路径/video.mp4 --mode smart --count 3 --strength 55 --display-only
python3 scripts/blcaptain_color.py shots --input /视频路径/video.mp4
python3 scripts/blcaptain_color.py plan --input /视频路径/video.mp4 --style 选择的风格ID --strength 55 --shot-grade --confirm-shot-boundaries --output-dir /输出目录 --plan-out /输出目录/video-plan.json
python3 scripts/blcaptain_color.py render --plan /输出目录/video-plan.json --confirm-plan 当前plan_id
```

视频必须检查完整成片的连续画面与声音；短预览和静帧不能代替全片观看。

需要语义局部处理时，先用 `plan --detect-local` 只查看本素材的真实候选；确认类别、覆盖率和边缘风险后，渲染时再提供 `render --confirm-local <策略>`。没有这道确认，不执行局部蒙版。

## 完整全景：照片 31 个，视频 31 个

正式目录共 32 个公式。`french-warm` 法式暖调仅照片，`night-black-gold` 夜景黑金仅视频，其余 30 个同时支持两种媒体。

### 照片公式（31）

自然通透 `natural-clean` · 奶油柔光 `cream-soft` · 韩系清冷 `korean-cool` · 花信晴蓝 `japanese-airy` · 法式暖调 `french-warm` · 柔和胶片 `film-soft` · 森林青绿 `forest-cyan` · 日落暖金 `sunset-warm` · 电影低饱和 `cinematic-muted` · 青橙电影 `teal-orange` · 美食鲜亮 `food-vivid` · 风景清透 `landscape-crisp` · 冷调霓虹夜景 `night-cool-neon` · 暖调治愈 `warm-cozy` · 克制低彩纪实 `documentary-low-color` · 闪光CCD `flash-ccd` · 雨夜蓝绿 `rainy-blue-green` · 冷灰海水 `cool-gray-sea` · 蓝调时刻 `blue-hour` · 黑白纪实 `bw-documentary` · 深海航线 `captain-deep-sea` · 青瓷森语 `celadon-forest` · 银盐晨雾 `silver-morning-mist` · 纸月黑白 `paper-moon-bw` · 高原寂光 `plateau-sacred-light` · 琥珀余烬 `amber-afterglow` · 绛雪梦境 `vermilion-snow-dream` · 黑曜金界 `obsidian-gold-realm` · 雨墨霓虹 `rain-ink-neon` · 沙海静玫 `desert-silent-rose` · 鎏金城纪 `gilded-autumn-city`。

![31 个照片公式前后对比](https://raw.githubusercontent.com/dososo/blcaptain-color-formula/main/showcase/formula-atlas/photo-contact-sheet.jpg)

### 视频公式（31）

自然通透 `natural-clean` · 奶油柔光 `cream-soft` · 韩系清冷 `korean-cool` · 花信晴蓝 `japanese-airy` · 柔和胶片 `film-soft` · 森林青绿 `forest-cyan` · 日落暖金 `sunset-warm` · 电影低饱和 `cinematic-muted` · 青橙电影 `teal-orange` · 夜景黑金 `night-black-gold` · 美食鲜亮 `food-vivid` · 风景清透 `landscape-crisp` · 冷调霓虹夜景 `night-cool-neon` · 暖调治愈 `warm-cozy` · 克制低彩纪实 `documentary-low-color` · 闪光CCD `flash-ccd` · 雨夜蓝绿 `rainy-blue-green` · 冷灰海水 `cool-gray-sea` · 蓝调时刻 `blue-hour` · 黑白纪实 `bw-documentary` · 深海航线 `captain-deep-sea` · 青瓷森语 `celadon-forest` · 银盐晨雾 `silver-morning-mist` · 纸月黑白 `paper-moon-bw` · 高原寂光 `plateau-sacred-light` · 琥珀余烬 `amber-afterglow` · 绛雪梦境 `vermilion-snow-dream` · 黑曜金界 `obsidian-gold-realm` · 雨墨霓虹 `rain-ink-neon` · 沙海静玫 `desert-silent-rose` · 鎏金城纪 `gilded-autumn-city`。

![31 个视频公式前后对比](https://raw.githubusercontent.com/dososo/blcaptain-color-formula/main/showcase/formula-atlas/video-contact-sheet.jpg)

[查看 62 个入口逐项大图、核心视觉作用、证据等级和素材许可](FORMULAS.md)。绿色“人工接受样例”严格绑定图中那份素材、强度和结果；“公开公式对比”只展示方向，不表示人工审美接受。原图按可塑空间和调整余量盲选，不按公式名称挑选已经自带目标色的素材。

## 它和滤镜包有什么不同

- Foundation 在前：先检查曝光、白平衡、黑白位、综合色彩与质感，再进入 Creative Look。
- 确认后生成：旧 `plan_id` 不能授权新素材、新强度或新局部策略。
- 逐素材安全：完整目录都可选，但黑位、白位、肤色、记忆色、剪切与变化量不合格时会拒绝当前计划。
- 结果可回退：不覆盖原文件，修改永远从原素材重做。
- 三道结论分开：自动技术门、Codex 顶级调色审查、用户人工接受不互相冒充。
- 不靠名称选样：展示原图必须有真实改善空间，前后变化在缩略图中也应可辨。

## 媒体、隐私与权利边界

支持 sRGB／Display P3 照片与普通 SDR 视频。RAW、未识别 Log、HDR／PQ／HLG／Dolby Vision、跨软件逐参数等效和任意视频语义跟踪不在本版保证范围。

核心调色在本机读取素材并写入你指定的输出目录，没有项目账号、遥测或自建上传服务器。原始计划和回执包含本机路径与素材哈希，公开分享前应脱敏。反馈账本默认位于 `~/.blcaptain/feedback-ledger.json`，可用 `clear-feedback` 清除。

用户必须拥有输入素材的处理与公开展示权。MIT License 只覆盖本项目代码与原创文档，不替代 FFmpeg、编解码器、模型、平台或用户媒体许可。详见 [隐私说明](PRIVACY.md)、[安全策略](SECURITY.md)、[数据与来源边界](references/source-and-data.md) 与 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 验证

Python 3.9+ 核心运行合同与 Python 3.10.18 完整开发测试合同分开。v4.9.1 Release 只以对应标签的 CI、候选包核验与冷安装记录为准。

```bash
python3 -m unittest tests.test_v491_all_formulas_executable tests.test_public_formula_atlas tests.test_skill_package tests.test_portable_startup
python3 scripts/build_skill_package.py audit --root . --public-assets
```

全量测试仅适用于完整开发仓库；Release Skill 包不包含 `tests/`、历史证据、用户媒体、开发工作区、图谱生成脚本或 Git 元数据。

## 常见问题

**会覆盖原图吗？** 不会，结果写入新目录。

**为什么全部可执行还会拒绝某张素材？** 公式可执行说明入口真实存在；拒绝只说明当前原图在该方向或强度下会伤害黑白位、肤色、记忆色或变化边界。

**技术通过就代表好看吗？** 不代表。技术门、Codex 审片和用户接受始终分开。

**能离线使用吗？** 核心照片链路可在本地运行；可选语义后端是否联网取决于你自行启用的实现与模型。

## 贡献与联系

- 作者：爆裂队长 NEXT（BLCaptain）
- GitHub：[@dososo](https://github.com/dososo)
- X：[@thinkszyg](https://x.com/thinkszyg)
- 邮箱：[blteam2026@outlook.com](mailto:blteam2026@outlook.com)
- 问题与建议：[GitHub Issues](https://github.com/dososo/blcaptain-color-formula/issues)

提交改动前请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## License

[MIT](LICENSE) © BLCaptain。第三方名称只用于事实性识别，不表示合作、授权或背书。
