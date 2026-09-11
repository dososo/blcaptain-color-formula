"""森林青绿单素材研究：按色相/亮度选色，不宣称深度或语义分割。"""
import cv2
import numpy as np

def ramp(x,a,b):
    t=np.clip((x-a)/(b-a),0,1)
    return t*t*(3-2*t)

def luminance(x):return .2126*x[...,0]+.7152*x[...,1]+.0722*x[...,2]

def foundation(rgb):
    x=np.asarray(rgb,dtype=float)
    if x.ndim!=3 or x.shape[-1]!=3 or not np.isfinite(x).all() or x.min()<0 or x.max()>1:
        raise ValueError('要求有限0至1高×宽×3 RGB')
    y=luminance(x)
    target=np.interp(y,[0,.12,.35,.65,.9,1],[0,.10,.36,.68,.91,1])
    # 压暗最多消耗最低通道25%的余量，避免浮点安全而8位量化触底。
    delta=np.clip(target-y,-.25*x.min(axis=2),.98*(1-x.max(axis=2)))
    return x+delta[...,None]

def grade(rgb,strength):
    if not np.isfinite(strength) or not 0<=strength<=1:raise ValueError('强度须在0至1')
    base=foundation(rgb)
    if strength==0:return base
    hsv=cv2.cvtColor(base.astype(np.float32),cv2.COLOR_RGB2HSV)
    h,s,v=np.moveaxis(hsv,-1,0);y=luminance(base)
    # 黄绿逐渐退出，绿色至青绿进入；亮叶保留原本暖光。
    weight=ramp(h,65,95)*(1-ramp(h,155,175))*ramp(s,.08,.25)
    weight*=ramp(y,.025,.12)*(1-ramp(y,.48,.78))
    changed=hsv.copy()
    changed[...,0]=(h+24*strength*weight)%360
    changed[...,1]=s*(1+.06*strength*weight*(1-s))
    candidate=cv2.cvtColor(changed,cv2.COLOR_HSV2RGB).astype(float)
    # 色相调整独立于明暗；保留同亮度通道差并压回可用色域。
    chroma=candidate-luminance(candidate)[...,None]
    target_y=y-.025*strength*weight
    limit=np.ones(y.shape)
    for c in range(3):
        cc=chroma[...,c]
        room=np.where(cc>0,(1-target_y)/np.maximum(cc,1e-12),target_y/np.maximum(-cc,1e-12))
        limit=np.minimum(limit,room*.999)
    result=target_y[...,None]+chroma*np.clip(limit,0,1)[...,None]
    return np.where((weight==0)[...,None],base,np.clip(result,0,1))
