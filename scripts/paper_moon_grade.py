"""纸月独立全局影调研究；不接正式渲染，不识别主体或背景。"""
import numpy as np
from scripts.documentary_bw_grade import checked

def linear(v):
    # 连续连接的709近似，避免舍入常数在接点产生灰阶逆序。
    return np.where(v<4.5*.018053968510807,v/4.5,((v+.09929682680944)/1.09929682680944)**(1/.45))

def encode(v):
    return np.where(v<.018053968510807,4.5*v,1.09929682680944*v**.45-.09929682680944)

def lightness(rgb):
    x=linear(checked(rgb))
    y=x[...,0]*.2126+x[...,1]*.7152+x[...,2]*.0722
    return 116*np.where(y>(6/29)**3,np.cbrt(y),y/(3*(6/29)**2)+4/29)-16

def from_lightness(l):
    f=(l+16)/116
    y=np.where(l>8,f**3,l/903.2962962962963)
    return np.repeat(encode(np.maximum(y,0))[...,None],3,axis=2)

def neutral(rgb):
    return from_lightness(lightness(rgb))

def fit_curve(samples):
    l=np.asarray(samples,dtype=float).ravel()
    if not l.size or not np.isfinite(l).all() or l.min()<0 or l.max()>100.000001:
        raise ValueError('拟合需要0至100有限L*样本')
    xp=np.r_[0,np.quantile(l,[.05,.075,.875,.90]),100]
    if np.min(np.diff(xp))<.1:
        raise ValueError('源影调不足以拟合配额，不强造层次')
    yp=np.array([0.,12.,35.,70.,85.,98.])
    # 研究级上限8，尚未经留出集校准，不是审美门或行业标准。
    # 拒绝强凑配额，不静默钳制曲线后谎称仍满足原配额。
    if np.max(np.diff(yp)/np.diff(xp))>8:
        raise ValueError('拟合斜率过大：配额与源影调冲突，停止强制拉伸')
    return xp,yp

def grade(rgb,strength,curve):
    if not np.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('强度须在0至1')
    xp,yp=(np.asarray(v,dtype=float) for v in curve)
    if (xp.ndim!=1 or yp.shape!=xp.shape or len(xp)<2 or
        not np.isfinite(xp).all() or not np.isfinite(yp).all() or
        xp[0]!=0 or xp[-1]!=100 or np.any(np.diff(xp)<=0) or
        np.any(np.diff(yp)<0) or yp.min()<0 or yp.max()>100):
        raise ValueError('曲线须覆盖0至100，有限且单调')
    l=lightness(rgb)
    mapped=np.interp(l,xp,yp)
    return from_lightness(l+strength*(mapped-l))
