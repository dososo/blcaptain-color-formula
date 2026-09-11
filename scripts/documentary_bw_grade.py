"""黑白纪实独立研究：不改变正式配方。"""
import numpy as np

def checked(rgb):
    x=np.asarray(rgb,dtype=float)
    if x.ndim!=3 or x.shape[-1]!=3 or not x.size or not np.isfinite(x).all() or x.min()<0 or x.max()>1:
        raise ValueError('要求有限0至1高×宽×3 RGB')
    return x

def neutral(rgb):
    x=checked(rgb)
    y=x[...,0]*.2126+x[...,1]*.7152+x[...,2]*.0722
    return np.repeat(y[...,None],3,axis=2)

def channel_gray(rgb):
    x=checked(rgb);y=neutral(x)[...,0]
    # 连续红蓝差而非语义色带；中性与红蓝等量的绿色不变。
    return y+.18*(x[...,0]-x[...,2])*4*y*(1-y)

def grade(rgb,strength):
    if not np.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('强度须在0至1')
    x=checked(rgb);base=neutral(x)[...,0];mixed=channel_gray(x)
    # 单层端点锁定S形，最大档定义后只混合一次，避免力度相互抵消。
    shaped=mixed+.25*mixed*(1-mixed)*(2*mixed-1)
    out=base+strength*(shaped-base)
    return np.repeat(out[...,None],3,axis=2)
