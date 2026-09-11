# 摄影视觉导演与作者风格方法

## 核心判断

调色不是把颜色变得更漂亮，而是控制观众先看什么、如何停留、最后记住什么。BLCaptain 的工作顺序固定为：

`故事命题 → 观看路径 → 光影结构 → 色彩关系 → 材质与余韵 → 技术保护`

这与专业流程中“先做一级校正，再做二级和创意调整”的分层一致。Adobe 把校正流程拆为输入、基础校正、创意调整与输出；Blackmagic 的官方训练也把色彩管理、一级校正、二级校正和外观建立分开。ACES 的角色是让不同设备与制作阶段在明确的色彩管理框架中交换，而不是提供一种万能风格。

- [Adobe：颜色校正工作流](https://helpx.adobe.com/premiere/desktop/correct-color/color-correction-fundamentals/color-correction-workflow.html)
- [Blackmagic Design：DaVinci Resolve 官方培训](https://www.blackmagicdesign.com/au/products/davinciresolve/training)
- [ACES：系统概览](https://docs.acescentral.com/background/overview/)

## 六个视觉问题

1. **主体**：真正要表达的是人、空间、事件，还是某种关系？
2. **观看路径**：第一眼、第二眼和最后停留分别在哪里？
3. **空间**：前中后景、负空间、遮挡、尺度和地平线怎样形成呼吸或压迫？
4. **图形**：线条、形状、重复、消失点和轮廓是否共同指向主题？
5. **光线**：明暗面积、方向、软硬、轮廓与反射在表达亲密、庄严、危险还是疏离？
6. **余韵**：离开画面后，希望观众记住一种颜色、一束光、一种材料，还是一种未说完的情绪？

Nikon 对构图三角的说明强调结构、平衡、节奏与视觉流动；它是观察框架，不是必须把主体塞进几何模板的规则。

- [Nikon：The Composition Triangle](https://www.nikonusa.com/learn-and-explore/c/ideas-and-inspiration/the-composition-triangle)

## 大师方法的原创转译

吸收案例时只学习决策方法，不复制电影 LUT、摄影师成片或品牌胶片：

- **故事先于效果**：John Wick 的案例体现摄影、美术、服装、场景与颜色共同建立叙事世界；不能期待后期单独替代前期设计。
- **限制形成作者性**：少数主色、克制辅色和有意义的强调色，比所有颜色同时抢眼更有力量。
- **光线建立节奏**：Robert Richardson 对强光与曝光的运用说明，高光不是单纯“不过曝”，也可以成为叙事符号；但显示参照素材里已经剪切的信息不能被后期凭空恢复。
- **色彩世界可以分区但要同语法**：Darius Khondji 的访谈体现不同场景可以拥有不同视觉节奏，同时仍服务整部作品的情绪结构。
- **统一从现场延续到完成**：FilmLight 的案例强调从 dailies 到最终调色的连续性；视频不能每帧自适应漂移，镜头间匹配应先于共享风格。

- [ASC：John Wick Chapter 3 的摄影与颜色世界](https://theasc.com/article/john-wick-chapter-3-slayin-in-the-rain/)
- [ASC：Darius Khondji 的电影节奏](https://theasc.com/articles/darius-khondji-cinematic-rhythms)
- [ASC：Robert Richardson 谈颜色](https://theasc.com/articles/robert-richardson-asc-on-color)
- [FilmLight：Disclaimer 的双世界统一](https://www.filmlightcolourawards.com/colour-awards-2025/winners-series-disclaimer-peter-doyle-two-worlds-one-grade/)
- [FilmLight：The Brutalist 从现场到完成](https://www.filmlightcolourawards.com/colour-awards-2025/winners-series-the-brutalist-mate-ternyik-from-dailies-to-finishing/)

## BLCaptain 作者语法

每套 Signature 必须完整回答：

`世界观命题 + 情绪弧线 + 影调手势 + 主辅强调色 + 光线哲学 + 构图倾向 + 材质指纹 + 禁止项`

执行时遵守四条约束：

- 每套风格只保留一个主手势；其他调整都为它服务。
- 风格应增强原素材潜力，不遮盖内容，也不把所有素材变成同一种颜色。
- 明显夸张必须有叙事理由，并有高光、暗部、综合色彩、肤色和噪点回退边界。
- 自动指标只检验变化、单调性与技术安全；“是否动人、独特、值得记住”必须通过真实素材 A/B 与人工盲测。

## 可执行边界

当前引擎能执行全局色彩管理、适应性一级校正、单调影调曲线、宽色带 HSL、综合色彩、颗粒、锐化、暗角和固定构图。它不能自动识别人脸、天空或商品做语义蒙版，不能制造不存在的布光、景深和细节，也不处理 RAW／Log／HDR 的完整场景参照流程。FFmpeg 滤镜能力仅用于实现和核查这些明确原语，不代表视觉判断本身。

- [FFmpeg：滤镜官方文档](https://ffmpeg.org/ffmpeg-filters.html)
