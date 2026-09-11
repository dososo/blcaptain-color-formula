"""固定机位纪实研究：上部红色色带削峰，非语义识别。"""
import numpy as np
import cv2


def luminance(x):
    return x[...,0]*.2126+x[...,1]*.7152+x[...,2]*.0722


def grade(rgb,strength):
    x=np.asarray(rgb,dtype=float)
    if x.ndim!=3 or x.shape[-1]!=3 or not x.size or not np.isfinite(x).all() or x.min()<0 or x.max()>1:
        raise ValueError('要求有限0至1高×宽×3 RGB')
    if not np.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('强度须在0至1')
    if strength==0:
        return x.copy()
    hsv=cv2.cvtColor(x.astype(np.float32),cv2.COLOR_RGB2HSV)
    h,s,_=np.moveaxis(hsv,-1,0)
    def ramp(a,lo,hi):
        t=np.clip((a-lo)/(hi-lo),0,1)
        return t*t*(3-2*t)
    # 红色色相距离，橙黄不动；高彩渐入。几何范围需逐素材人工确认。
    distance=np.minimum(h,360-h)
    weight=(1-ramp(distance,8,22))*ramp(s,.35,.65)
    vertical=np.arange(x.shape[0])/max(x.shape[0]-1,1)
    weight*= (1-ramp(vertical,.24,.38))[:,None]
    y=luminance(x)
    # 向同编码亮度中性轴内插，无新增剪切，不添加颗粒/锐化/暗角。
    amount=.70*strength*weight
    return x+(y[...,None]-x)*amount[...,None]
