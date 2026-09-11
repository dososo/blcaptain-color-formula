# BLCaptain 摄影视觉导演与调色公式 Skill v4.9.0

[中文](README.md) · [English](README.en.md) · [下载最新版](https://github.com/dososo/blcaptain-color-formula/releases/latest) · [报告问题](https://github.com/dososo/blcaptain-color-formula/issues)

![BLCaptain：同一张图，效果看得见](showcase/hero-1600x900.png)

把照片或普通 SDR 视频交给 Codex：它先看懂问题，最多给 3 个安全方向；你确认方案后，它才在本地生成成片和前后对比。**不覆盖原图。**

![原片与银盐晨雾 55% 的真实前后对比](showcase/before-after.png)

> v4.9.0 是公开测试版。当前为 3 个自动推荐方向、1 个需明确点名的 `manual-executable` 方向、28 个研究候选。目录存在不等于都能执行，技术通过也不等于审美通过。完整边界见 [公开测试范围](references/public-beta-scope.md)。

## 为什么做这个 Skill

普通滤镜通常先给效果，再让人猜它是否适合素材。BLCaptain 把专业调色中最重要的判断前置：先读画面、说明方向与风险，再由你确认唯一方案。它适合希望获得明确审美建议、保留原素材、并能复查每次结果的人；不适合追求一键套用大量预设，或要求本版直接完成 Log／RAW、HDR 与跨软件逐参数等效的人。

## 安装

已验证环境：macOS、Python **3.9** 或更高版本、FFmpeg／ffprobe。Windows 与 Linux 尚未完成冷安装验收。

### 方法一：下载 Release（推荐）

1. 从 [Latest Release](https://github.com/dososo/blcaptain-color-formula/releases/latest) 下载 `blcaptain-color-formula-4.9.0.zip` 和对应 `.sha256`。
2. 解压 ZIP，在解压后的目录运行：

```bash
python3 scripts/install_skill.py
```

3. 重新打开 Codex，发送：

```text
用 BLCaptain 调色公式，帮我实际调色这张照片。
```

默认安装到 `~/.codex/skills/blcaptain-color-formula`。如果同名目录已经存在，安装器会停止并且不会覆盖；请先自行改名保存旧版本，再重新安装。

### 方法二：Git 克隆

```bash
git clone https://github.com/dososo/blcaptain-color-formula.git ~/.codex/skills/blcaptain-color-formula
```

核心照片处理只用 Python 标准库与 FFmpeg。可选语义能力见 `requirements-optional.txt`；**仅安装依赖不会下载模型**，没有本地模型缓存时自动降级到全局链路。

## 第一张照片

最简单的方式不是记命令，而是把照片附给 Codex，然后说：

```text
用 BLCaptain 调色公式实际处理这张照片。先给我最多 3 个适合它的方向，等我确认后再生成；不要覆盖原图。
```

你会依次看到：

1. 一句话画面诊断；
2. 最多 3 个可选方向及各自风险；
3. 方案摘要和唯一 `plan_id`；
4. 你确认后生成的成片、前后对比、色卡和回执；
5. 不满意时从原图重新调整，不在旧结果上反复叠加。

## 新用户 Quick Start（照片）

需要终端复现时，先让 `suggest` 返回真实可执行的风格 ID，再把该 ID 传给 `plan`。下面的 `推荐返回的风格ID` 是占位符，不能原样照抄：

```bash
# 1. 最多返回 3 个安全候选；此步不生成成片
python3 scripts/blcaptain_color.py suggest --input /照片路径/photo.jpg --mode smart --count 3 --strength 55 --display-only

# 2. 把上一步 status=executable 的 recipe_id 填入这里
python3 scripts/blcaptain_color.py plan --input /照片路径/photo.jpg --style 推荐返回的风格ID --strength 55 --output-dir /输出目录 --plan-out /输出目录/plan.json

# 3. 只确认当前计划里的 plan_id
python3 scripts/blcaptain_color.py render --plan /输出目录/plan.json --confirm-plan 当前plan_id

# 4. 不满意就回到原图提出修改
python3 scripts/blcaptain_color.py refine --input /照片路径/photo.jpg --current-style 推荐返回的风格ID --feedback "肤色更自然，保留更多暖色" --strength 0.55
```

`--strength 55` 与 `--strength 0.55` 都表示 55%。`executable` 只说明当前素材通过同链路预演；它不是审美签字。任一生成步骤失败，结果组会回滚整组，原图保持不变。

## 新用户 Quick Start（视频）

视频先检查镜头边界，再做逐镜头基础校正与共享 Look。当前完整连续回放仍是人工门：短预览或单帧不能代替全片观看。

```bash
python3 scripts/blcaptain_color.py suggest --input /视频路径/video.mp4 --mode smart --count 3 --strength 55 --display-only
python3 scripts/blcaptain_color.py shots --input /视频路径/video.mp4
python3 scripts/blcaptain_color.py plan --input /视频路径/video.mp4 --style silver-morning-mist --strength 55 --shot-grade --confirm-shot-boundaries --output-dir /输出目录 --plan-out /输出目录/video-plan.json
python3 scripts/blcaptain_color.py render --plan /输出目录/video-plan.json --confirm-plan 当前plan_id
```

## 它和滤镜包有什么不同

- 先做 Foundation：检查曝光、白平衡、黑白位、综合色彩与质感，再考虑 Creative Look。
- 先确认后生成：构图、局部策略、风格与强度都进入当前方案；旧 `plan_id` 不能授权新方案。
- 结果可回退：从原素材重新生成，不覆盖、不层层烘焙。
- 结论分层：自动技术门、Codex 自审、用户人工接受分别记录，绝不用自动指标冒充审美。
- 名称不决定选材：演示原图必须有真实变化余量，不能拿已接近目标色的画面美化前后对比。

## 当前全景

| 层级 | 数量 | 普通用户能否执行 |
|---|---:|---|
| 自动推荐 `active` | 3 | 可以，仍按素材动态筛选 |
| 点名执行 `manual-executable` | 1 | 只有用户明确点名才进入计划 |
| 研究候选 `research` | 28 | 不生成正式计划 |

支持照片，以及声明允许视频的普通 SDR 素材。Log／RAW、HDR、跨软件精确等效、任意语义局部和完整视频审美验收不在本版保证范围。指导模式可给 iPhone 照片、醒图、Lightroom、剪映、Premiere Pro、DaVinci Resolve 的手工起点，但未做同图实机标定的参数只标为启发式建议。

需要对照片做语义局部处理时，先在计划阶段加 `--detect-local` 查看本张素材真实可用的候选；看过类别、覆盖率与边缘风险并确认后，渲染时再加 `--confirm-local <策略>`。没有明确确认就不会执行局部蒙版。

## 结果怎么看

- `technical`：编码、色彩标签、原图不变、变化门与安全门。
- `aesthetic`：只允许 `pending / self_reviewed / human_accepted / human_rejected`。
- `human_accepted`：只对被看到的素材、方案、强度和结果有效，不能外推到同名风格的任意素材。

## 隐私、安全与数据边界

核心调色在本机读取素材并写入你指定的输出目录，没有项目账号、遥测或自建上传服务器。原始计划和回执会记录本机路径与素材哈希，公开分享前应先脱敏。反馈账本默认保存在 `~/.blcaptain/feedback-ledger.json`，可运行 `clear-feedback` 清除。

用户必须拥有输入素材的处理和公开展示权。MIT License 只覆盖本项目代码与原创文档，不替代 FFmpeg、编解码器、模型、平台或用户媒体的许可。详见 [隐私说明](PRIVACY.md)、[安全策略](SECURITY.md) 与 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 数据与来源边界

研究目录只保存事实型来源元数据、链接和本项目的原创归纳，不打包第三方图片、视频、商业预设、LUT 或完整原帖。色彩空间与工具语义以官方文档为依据，但不据此宣称跨 App 滑杆等价。仅安装依赖不会下载模型；可选后端是否联网以 [隐私说明](PRIVACY.md) 的当前代码边界为准。

## 验证

Python 3.9+ 核心运行合同与 Python 3.10.18 完整开发测试合同分开。v4.9.0 Release 只以对应标签的 CI、候选包核验与冷安装记录为准。

```bash
python3 -m unittest tests.test_v490_public_release tests.test_skill_package tests.test_public_beta_docs tests.test_portable_startup
python3 scripts/build_skill_package.py audit --root . --public-assets
```

全量测试仅适用于完整开发仓库；Release Skill 包不包含 `tests/`、历史证据、用户媒体、开发工作区或 Git 元数据。

## 仓库结构

```text
SKILL.md                 Skill 入口与工作规则
scripts/                 安装器、命令行与打包核验
references/              公开方法、范围和来源边界
agents/                  Codex 展示元数据
README.md / README.en.md  中英文使用说明
```

公开仓库只保留运行、理解和验证本 Skill 所需的最终文件，不包含本地路径、用户素材、过程稿、缓存或内部验收记录。

## 常见问题

**会覆盖原图吗？** 不会。结果写入新的输出目录。

**为什么必须确认 `plan_id`？** 它把授权限定在你刚看过的素材、方向、强度和局部策略，避免旧确认误用于新方案。

**技术检查通过就代表好看吗？** 不代表。技术门、Codex 审查与用户审美接受始终分开。

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
