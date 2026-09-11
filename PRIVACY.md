# 隐私说明 / Privacy

## 核心处理

BLCaptain 核心照片／视频链路在本机读取用户指定的媒体，并把新文件写入用户指定的输出目录。项目没有账号系统、自建上传服务器、广告或遥测；核心链不会主动上传用户媒体，也不会覆盖输入文件。

Core photo/video processing reads user-selected media locally and writes new files to a user-selected output directory. The project has no account system, project-operated upload server, ads, or telemetry. The core pipeline does not upload or overwrite user media.

## 本地数据

- 计划与回执会记录输入／输出的绝对路径、素材 SHA-256、执行参数和检查结果，用于本地复现；不要直接把原始 JSON 公开发布。
- 反馈账本默认位于 `~/.blcaptain/feedback-ledger.json`，只按素材内容哈希、配方、强度和用户给出的理由记录，可用 `clear-feedback` 清除。
- 卸载 Skill 不会自动删除用户输出或反馈账本；是否保留由用户决定。

Plans and receipts contain absolute paths, media hashes, parameters, and checks for local reproducibility. Do not publish raw JSON without redaction. The optional feedback ledger is local and can be cleared with `clear-feedback`. Uninstalling the Skill does not delete user outputs or the ledger.

## 可选能力与联网边界

核心 L0 全局调色只需要 Python 标准库与 FFmpeg。可选语义能力需要额外依赖和本地模型缓存；仅安装依赖不会下载模型。当前 ADE 后端所用第三方库在缓存缺失时可能尝试访问 Hugging Face，因此本版不承诺该可选路径完全离线。开发用素材检索脚本可能访问 Openverse；它不属于普通用户核心运行路径。

The core L0 global pipeline uses the Python standard library and FFmpeg. Optional semantic features require extra dependencies and a local model cache. Their third-party loader may contact Hugging Face when cache is missing, so this optional path is not claimed to be fully offline. Development-only asset research may contact Openverse and is not part of normal use.

## 用户责任

照片和视频可能含人脸、位置或家庭信息。用户应确认自己有权处理、保存和公开展示素材，并在分享前检查成片、对比图、文件名和元数据。

Media may contain faces, locations, or private family information. Users are responsible for processing and publication rights and should inspect outputs, comparisons, filenames, and metadata before sharing.
