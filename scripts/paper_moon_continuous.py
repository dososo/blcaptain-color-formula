"""纸月连续影调独立研究：无面积配额、无语义局部。"""
import numpy as np
from scripts.paper_moon_grade import lightness,from_lightness

def tone(l,strength):
    if not np.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('强度须在0至1')
    x=np.asarray(l,dtype=float)
    if not np.isfinite(x).all() or np.any(x<0) or np.any(x>100.000001):
        raise ValueError('L*须在0至100')
    t=np.clip((x-65)/35,0,1)
    shoulder=t*t*(3-2*t)
    return x-strength*(4*np.sin(np.pi*x/100)**2+5*shoulder)

def grade(rgb,strength):
    return from_lightness(tone(lightness(rgb),strength))
