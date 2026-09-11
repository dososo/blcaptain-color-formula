# 色彩管理与常见色域支持

本文件用于决定素材能否进入自动渲染。不要只凭扩展名猜测色域，也不要把“P3”一律当成 Display P3。

## 照片支持矩阵

| 输入 | 自动处理 | 输出 | 用户提示 |
|---|---|---|---|
| sRGB | 支持 | 带 sRGB 标签的 PNG | 正常确认风格与强度 |
| Display P3（P3-D65 + sRGB 传递函数） | 支持 | 带 Display P3 标签的 `rgb48be` PNG | “会保持 P3，不需要先转成 sRGB” |
| 无色彩标签 | 条件支持 | 带 sRGB 标签的 PNG | 确认单必须写“按 sRGB 解释” |
| Adobe RGB / JEDEC P22 | 不支持自动渲染 | 无 | 当前 FFmpeg 构建缺少可靠 ICC 变换；转 Lightroom、Photoshop 或 ColorSync |
| ProPhoto RGB | 不支持自动渲染 | 无 | 同上，不得当成 sRGB 猜测 |
| DCI-P3 | 不支持自动渲染 | 无 | DCI-P3 与 Display P3 的白点和传递特性不同 |
| BT.2020 SDR/HDR 照片 | 不支持自动渲染 | 无 | 尚未完成配方与输出链路标定 |
| RAW | 不支持自动渲染 | 无 | 先在 RAW 软件中完成解码、白平衡与工作色域选择 |

照片统一输出 PNG，是为了避免再次进行有损 JPEG 压缩并可靠写入色彩标签。Display P3 使用 16 位通道容器，目的是降低曲线与多步运算后的量化风险；它不会凭空增加原图已有的信息。

## 视频支持矩阵

| 输入 | 自动处理 | 说明 |
|---|---|---|
| Rec.709 SDR，标签完整 | 支持 | 输出 H.264/AAC 或原音频，完整写回 Rec.709 三项标签 |
| SDR 标签缺失 | 条件支持 | 用户明确确认后，使用 `--assume-sdr` 按 Rec.709 SDR 解释 |
| Rec.601 / BT.470 标清视频 | 不支持 | 当前流程未验证标清色度矩阵到 Rec.709 的转换 |
| Display P3 / DCI-P3 视频 | 不支持 | 尚未完成目标交付色域、色差和时序标定 |
| BT.2020 SDR | 不支持 | 不静默压到 Rec.709 |
| BT.2100 PQ / HLG / Dolby Vision | 不支持 | 需要受控色调映射与显示目标 |
| Log / RAW 视频 | 不支持 | 需要准确相机 Log/RAW 解码和输入变换 |

## 执行与验收

1. `inspect` 同时报告原始 primaries、transfer、matrix、range、ICC/cICP 名称、归一化 profile 与 support。
2. `plan` 把输入、工作与输出色域、位深、标签方式和限制写入 `color_pipeline`；它属于 `plan_id` 指纹的一部分。
3. `render` 先生成临时文件，再用 ffprobe 验收像素格式与四项色彩标签。验收失败时不发布结果、对比图和回执。
4. Display P3 输出必须满足：`rgb48be / gbr / iec61966-2-1 / smpte432 / pc`。
5. 不宣称同一配方在 sRGB 与 P3 中审美完全等价；当前保证的是正确解释、保持目标色域和不静默错标。
