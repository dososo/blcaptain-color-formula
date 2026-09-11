"""琥珀余烬独立研究：显示参照RGB，非语义、非正式目录实现。"""
import numpy as np

LUMA = np.array([.2126, .7152, .0722])

def luminance(x):
    # 显式三通道和避开当前环境大图matmul的数值告警。
    return x[...,0]*LUMA[0]+x[...,1]*LUMA[1]+x[...,2]*LUMA[2]

def smooth(a, b, x):
    t = np.clip((x-a)/(b-a), 0, 1)
    return t*t*(3-2*t)

def grade(rgb, strength):
    x = np.asarray(rgb, dtype=np.float64)
    if x.ndim != 3 or x.shape[-1] != 3 or not x.size or not np.isfinite(x).all() or x.min() < 0 or x.max() > 1:
        raise ValueError('要求有限0至1高×宽×3 RGB')
    if not np.isscalar(strength) or not np.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('强度须在0至1')
    y = luminance(x)
    # 黑端与亮部不新增染色；权重来自原信号，不逐帧估计曝光。
    envelope = smooth(.06,.2,y) * (1-smooth(.48,.82,y))
    warm = smooth(.025,.18,x[...,0]-x[...,2])
    cool = smooth(.025,.18,x[...,2]-x[...,0])
    # 显示编码亮度守恒的两个向量，不是物理光能或语义选区。
    amber = np.array([1.,-.15,0.])
    amber[2] = -(amber[0]*LUMA[0]+amber[1]*LUMA[1])/LUMA[2]
    blue = np.array([-.6,.1,0.])
    blue[2] = -(blue[0]*LUMA[0]+blue[1]*LUMA[1])/LUMA[2]
    delta = envelope[...,None]*(.055*warm[...,None]*amber + .025*cool[...,None]*blue)
    # 三通道共用边界缩放，避免逐通道截断改变明暗与方向。
    room = np.where(delta>0,1-x,x)
    limit = np.full_like(delta,np.inf)
    np.divide(room,np.abs(delta),out=limit,where=np.abs(delta)>1e-15)
    factor = np.minimum(1,limit.min(axis=-1))
    # 仅收回浮点舍入产生的约1e-17越界；主色域限制在上面的共用缩放。
    return np.clip(x + strength*factor[...,None]*delta,0,1)
