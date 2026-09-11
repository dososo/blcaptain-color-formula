# BLCaptain 美学指南：不用术语也能说清“想要什么”

## 先说感受，再说颜色

“更蓝”“更饱和”只是手段。更有效的描述是：

- **清透**：灰雾减少，白色干净，暗部仍有层次；不是全图变亮。
- **治愈**：光比柔和、肤色可信、暖色有呼吸；不是整屏橙黄。
- **电影感**：主体与背景有层级，冷暖或明暗关系服务叙事；不是加黑边和降饱和。
- **清冷**：冷色是环境基调，肤色与记忆色仍需保护；不是把人也染青。
- **冲击力**：第一眼明确、主辅关系强、亮暗有方向；不是把对比和饱和拉满。

## 六个观察顺序

1. **主体**：第一眼是否落在该看的地方？
2. **明暗**：高光有没有纹理，暗部有没有堵死，画面是否发灰？
3. **冷暖**：环境色与主体色是顺势还是对立？
4. **色彩**：主色、辅色、强调色是否各司其职？
5. **质感**：皮肤、天空、金属、食物是否被同一种锐化伤害？
6. **余韵**：看完记住的是内容、光，还是一个过重滤镜？

## 一条完整的新用户路径

```bash
python3 scripts/blcaptain_color.py suggest --input /绝对路径/照片.jpg --mode smart --count 3 --strength 55 --display-only
python3 scripts/blcaptain_color.py plan --input /绝对路径/照片.jpg --style 配方ID --strength 55 --output-dir /绝对路径/输出 --plan-out /绝对路径/计划.json
python3 scripts/blcaptain_color.py render --plan /绝对路径/计划.json --confirm-plan 计划编号
```

不满意时不要直接叠第二层滤镜。从原图记录反馈：

```bash
python3 scripts/blcaptain_color.py record-feedback --input /绝对路径/照片.jpg --style 配方ID --strength 55 --verdict rejected --reason "色彩不够，氛围、情绪不够"
```

## 常见错误口诀的纠正

- “橙色就是肤色”不成立：橙色还可能是木头、灯光、墙面和秋叶；必须先确认人物或肤色蒙版。
- “S 曲线一定高级”不成立：原片已高反差时再拉 S，会先剪掉高光与暗部。
- “去雾越高越通透”不成立：去雾同时放大噪声、边缘与色偏。
- “电影感就是青橙”不成立：没有主体层级和光线叙事，青橙只是一层颜色。
- “参数可跨 App 等价”不成立：同名滑杆的算法、顺序和工作色域可能不同；未标定参数只能当起点。

## 自动结论的边界

`technical` 只说明本次实际跑过的技术门；`aesthetic` 默认永远是 `pending`。只有你明确接受或否决，系统才会记录 `human_accepted` 或 `human_rejected`。`impact_profile` 是情绪方向代理，不是审美评分。
