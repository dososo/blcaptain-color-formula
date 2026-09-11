"""冷灰海水研究候选：显示参照RGB宽色带，不是海天岸语义分割。

仅用于已检查的无人海岸研究；未接正式plan/render，不能处理Log。
海水密度与岩岸降彩分开，保留中性白浪，不全局降饱和或染蓝。
"""
import numpy as np

def _ramp(x,low,high):
    t=np.clip((x-low)/(high-low),0,1)
    return t*t*(3-2*t)

def grade(rgb,strength):
    if not np.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('研究强度必须在0至1之间')
    rgb=np.asarray(rgb,dtype=float)
    if rgb.shape[-1]!=3 or not np.isfinite(rgb).all() or np.any((rgb<0)|(rgb>1)):
        raise ValueError('输入必须为有限的0至1 RGB')
    if strength==0:return rgb.copy()
    r,g,b=np.moveaxis(rgb,-1,0)
    y=.2126*r+.7152*g+.0722*b
    mid=_ramp(y,.08,.25)*(1-_ramp(y,.70,.92))
    cool=_ramp(b-r,.015,.09)*_ramp(g-r,.005,.06)*mid
    warm=_ramp(r-b,.03,.16)*_ramp(g-b,.01,.07)*mid
    # 蓝青中低调小幅压密；近中性白浪不受影响。不是全局压曝光。
    target_y=y-.065*strength*cool*4*y*(1-y)
    chroma=rgb-y[...,None]
    # 岸边橙黄稍收敛；蓝青保留原色相和彩度，不把冷灰做成失色。
    chroma_scale=1-.22*strength*warm
    return np.clip(target_y[...,None]+chroma*chroma_scale[...,None],0,1)
