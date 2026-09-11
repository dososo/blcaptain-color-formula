"""森林青绿v3：冷青叶幕与暖棕林地双锚，单素材研究实现。"""
import cv2
import numpy as np


def _ramp(value, start, end):
    t = np.clip((value - start) / (end - start), 0, 1)
    return t * t * (3 - 2 * t)


def luminance(rgb):
    return .2126 * rgb[..., 0] + .7152 * rgb[..., 1] + .0722 * rgb[..., 2]


def _validate(rgb):
    value = np.asarray(rgb, dtype=float)
    if (value.ndim != 3 or value.shape[-1] != 3 or not np.isfinite(value).all()
            or value.min() < 0 or value.max() > 1):
        raise ValueError("要求有限0至1高×宽×3 RGB")
    return value


def foundation(rgb):
    """沿用已修复的显示参照曲线；只执行一次。"""
    value = _validate(rgb)
    y = luminance(value)
    target = np.interp(y, [0, .12, .35, .65, .9, 1], [0, .10, .36, .68, .91, 1])
    delta = np.clip(target - y, -.25 * value.min(axis=2), .98 * (1 - value.max(axis=2)))
    return value + delta[..., None]


def grade(rgb, strength):
    """只塑造有色中低明度绿，不把暖棕、近中性与高光整体染青。"""
    if not np.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("强度须在0至1")
    base = foundation(rgb)
    if strength == 0:
        return base
    hsv = cv2.cvtColor(base.astype(np.float32), cv2.COLOR_RGB2HSV)
    hue, saturation, value = np.moveaxis(hsv, -1, 0)
    y = luminance(base)
    # 黄绿缓入、蓝绿缓出；压住高光叶缘，避免荧光青和整片染色。
    weight = _ramp(hue, 55, 92) * (1 - _ramp(hue, 168, 190))
    weight *= _ramp(saturation, .075, .24)
    weight *= _ramp(y, .025, .13) * (1 - _ramp(y, .60, .84))
    # 朝青绿色目标靠近，而不是对所有像素统一旋转色相。
    target_hue = np.maximum(hue, 158.0)
    changed = hsv.copy()
    changed[..., 0] = hue + (target_hue - hue) * (.74 * strength * weight)
    changed[..., 1] = np.clip(saturation * (1 + .24 * strength * weight), 0, 1)
    candidate = cv2.cvtColor(changed, cv2.COLOR_HSV2RGB).astype(float)
    chroma = candidate - luminance(candidate)[..., None]
    target_y = np.clip(y - .038 * strength * weight, 0, 1)
    limit = np.ones(y.shape)
    for channel in range(3):
        component = chroma[..., channel]
        room = np.where(component > 0, (1 - target_y) / np.maximum(component, 1e-12),
                        target_y / np.maximum(-component, 1e-12))
        limit = np.minimum(limit, room * .999)
    result = target_y[..., None] + chroma * np.clip(limit, 0, 1)[..., None]
    return np.where((weight <= 1e-9)[..., None], base, np.clip(result, 0, 1))
