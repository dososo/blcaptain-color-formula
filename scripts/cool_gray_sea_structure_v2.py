"""冷灰海水v2：固定机位海岸的空间冷暖与浪花光带研究。

这是显示参照RGB中的人工几何近似，不是海水、浪花或礁岸语义分割。
"""
import numpy as np

try:
    from .cool_gray_sea_grade import _ramp
except ImportError:
    from cool_gray_sea_grade import _ramp


def _validate(rgb, strength):
    if not np.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("研究强度必须在0至1之间")
    value = np.asarray(rgb, dtype=float)
    if value.ndim != 3 or value.shape[-1] != 3:
        raise ValueError("固定海岸结构要求高×宽×3图像")
    if not np.isfinite(value).all() or np.any((value < 0) | (value > 1)):
        raise ValueError("输入必须为有限的0至1 RGB")
    return value


def grade(rgb, strength):
    source = _validate(rgb, strength)
    if strength == 0:
        return source.copy()
    height = source.shape[0]
    position = np.linspace(0, 1, height)[:, None]
    y = .2126 * source[..., 0] + .7152 * source[..., 1] + .0722 * source[..., 2]
    chroma = source.max(axis=-1) - source.min(axis=-1)

    # 只在本素材海平面以下建立海体，浪花与亮中性像素退出。
    sea_band = _ramp(position, .39, .47) * (1 - _ramp(position, .67, .74))
    foam = _ramp(y, .55, .70) * (1 - _ramp(chroma, .08, .18))
    midtone = _ramp(y, .10, .24) * (1 - _ramp(y, .73, .88))
    sea_weight = sea_band * (1 - foam) * midtone

    # 深冷海体：中间调略压密，蓝青分离增长；不是整幅白平衡偏蓝。
    # 该绑定原片是高键海景，海体主要落在0.60至0.82；普通居中S曲线会反而
    # 抬起这一段并压平浪线。这里让海面中间调向下收束，亮浪由foam权重退出，
    # 从而用“深海托亮浪”建立层次，而不是全局增对比。
    target_y = np.interp(y, [0, .12, .30, .45, .58, .72, .85, 1],
                           [0, .07, .18, .31, .43, .59, .80, 1])
    water_target = source + (target_y - y)[..., None]
    # 色相位移只由海体权重混合一次。上一版在目标内部和最终混合各乘一次
    # sea_weight，真实浪花密集素材上的有效冷侧手势被平方稀释，造成“技术有
    # 数值、观看无变化”。
    water_target += np.array([-.090, .004, .078])
    water_target = np.clip(water_target, .002, .998)
    result = source * (1 - strength * sea_weight[..., None]) + water_target * strength * sea_weight[..., None]

    # 暖礁岸是小面积反色锚：压住橙黄噪声但不抹成灰，也不向蓝侧迁移。
    shore_band = _ramp(position, .66, .75)
    warm = _ramp(source[..., 0] - source[..., 2], .035, .18) * _ramp(chroma, .08, .30)
    shore_weight = shore_band * warm
    neutral = y[..., None]
    shore_target = neutral + (source - neutral) * .70 - .010
    shore_target = np.clip(shore_target, .002, .998)
    result = result * (1 - strength * shore_weight[..., None]) + shore_target * strength * shore_weight[..., None]
    return np.clip(result, 0, 1)
