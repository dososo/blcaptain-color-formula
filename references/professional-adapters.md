# 专业软件适配边界

## 本地直接渲染

- 照片：sRGB 与 Display P3 的 JPEG、PNG 均可直接渲染；P3 保持 P3 并输出带标签的 16 位 PNG，不需要先转 sRGB。
- 视频：确认是 Rec.709 SDR 的 MP4、MOV。
- 普通视频标签缺失时，只有用户明确确认“按 Rec.709 SDR 解释”后，计划才可使用 `--assume-sdr`。

## 必须转专业流程

- RAW：先在 Lightroom、Camera Raw 或其他 RAW 显影器完成相机配置文件、白平衡与基础曝光。
- Apple Log、S-Log3、C-Log、D-Log、V-Log：在 Premiere、DaVinci 或剪映专业工作流中先做与相机/Gamut 匹配的技术转换，再施加创意 Look。
- PQ、HLG、Dolby Vision：首版不做自动 SDR/HDR 转换；确认目标显示标准后再进入受控色彩管理。
- Adobe RGB、ProPhoto RGB、DCI-P3、BT.2020 与未知 ICC 图片：当前 FFmpeg 环境无法保证 ICC 往返，先在支持色彩管理的软件中转为受控 sRGB 或 Display P3 副本。（Display P3 本身已在 v1.5 起直接支持，不在此列。）

## 参数交付方式

给 Lightroom、剪映、Premiere 或 DaVinci 的数值只能标为“起始建议”，并说明下列顺序：

1. 输入技术变换/色彩管理。
2. 基础曝光与白平衡。
3. 影调与对比曲线。
4. 全局饱和、HSL/色轮。
5. 肤色或天空等局部保护。
6. 锐化、降噪、颗粒和输出。

不得把不同软件的 `+20` 横向视为相同效果；同一素材应以示波器、矢量示波器和视觉对比校准。

## 视频指导适配器

### 剪映

- 用基础调节完成曝光、对比、高光、阴影、色温和饱和度，再按需进入 HSL、曲线与色轮。
- 多镜头先逐段校正，再复制风格方向；禁止让自动调节在逐帧间漂移。
- 本 Skill 的数值是标准化方向，不保证等于某个剪映版本的原生滑杆位置。
- 能力依据：[CapCut官方调色说明](https://www.capcut.com/resource/capcut-color-grading)。CapCut与剪映的版本和地区功能可能不同，因此只借用工具语义，不推断产品完全一致。

### Premiere

- 先设置序列和素材色彩管理，再使用 Basic Correction、RGB／Hue曲线和三路色轮。
- Lumetri与Premiere新版Color模式的处理范围和色彩管理不同；同一项目不要无说明地混用。
- 能力依据：[Adobe基础校正](https://helpx.adobe.com/mena_en/premiere/desktop/correct-color/color-correction-fundamentals/basic-color-correction-options.html)、[Adobe色轮](https://helpx.adobe.com/premiere/desktop/correct-color/add-color-effects/correct-color-using-color-wheel.html)。

### DaVinci Resolve

- 先用Offset或Primary Wheels统一镜头，再用曲线、Qualifier和Power Window进行风格和局部处理。
- Lift／Gamma／Gain是重叠影调范围，不是照片阴影／亮度／高光滑杆的等价物。
- 局部窗口必须跟踪；交付前用示波器、矢量示波器和完整回放检查。
- 能力依据：[Blackmagic DaVinci Color](https://www.blackmagicdesign.com/ca/products/davinciresolve/color)。

## 视频时序顺序

1. 输入色彩空间与技术转换。
2. 逐镜头曝光、白平衡和镜头匹配。
3. 全局创意风格、曲线、HSL或色轮。
4. 人物、天空、商品等局部蒙版与跟踪。
5. 时序降噪、锐化、颗粒；降噪必须先于锐化，颗粒最后添加。
6. 完整回放检查闪烁、色带、跳色、压缩噪点和蒙版漂移。
7. 核对帧率、时长、音频、码率、色彩标签和目标平台。
