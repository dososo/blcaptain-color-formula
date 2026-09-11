"""琥珀余烬一次因果修正：限制明亮暖表面的新增染色。"""
import numpy as np
from scripts.amber_residual_grade import grade as base_grade,luminance,smooth

def grade(rgb,strength):
    # 复用基线的输入、强度和色域验证，不改变冷区动作。
    endpoint=base_grade(rgb,strength)
    x=np.asarray(rgb,dtype=np.float64)
    weight=np.where(x[...,0]>x[...,2],1-smooth(.26,.44,luminance(x)),1.)
    return x+(endpoint-x)*weight[...,None]
