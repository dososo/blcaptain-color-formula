"""人工初始化的双窗口研究；不做语义识别，不从失配帧更新模板。"""
import numpy as np
from scripts.lamp_window import locate


def track_pair(reference,current,boxes):
    if set(boxes)!={'source','reflection'}:
        raise ValueError('必须同时提供光源和对应倒影的人工首帧窗口')
    result={}
    for role,box in boxes.items():
        try:
            result[role]=locate(reference,current,box)
        except ValueError as exc:
            raise ValueError(f'{role}定位失败，整对不可执行：{exc}') from exc
    return result


def compose(base,look,target,protect,strength):
    """对已存在的结果叠加一次局部差值；保护优先，绝不重新从原片起算。"""
    arrays=[np.asarray(v,dtype=float) for v in (base,look,target,protect)]
    base,look,target,protect=arrays
    if (base.ndim!=3 or base.shape[-1]!=3 or look.shape!=base.shape or
        target.shape!=base.shape[:2] or protect.shape!=target.shape):
        raise ValueError('图像或选区尺寸不一致')
    if not np.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('强度须为0至1')
    if any(not np.isfinite(v).all() or np.any(v<0) or np.any(v>1) for v in arrays):
        raise ValueError('图像与蒙版须为0至1有限数值')
    weight=(target*(1-protect)*strength)[...,None]
    return base+(look-base)*weight
