"""研究用人工凸多边形关键帧；插值候选不是跟踪或人工通过。"""
import cv2
import numpy as np
import hashlib
import json


def candidate_mask(track,frame,shape,feather):
    if not isinstance(frame,int) or frame<0:
        raise ValueError('帧号须为非负整数')
    if not np.isfinite(feather) or feather<=0:
        raise ValueError('内羽化须为有限正数')
    h,w=shape
    keys=sorted((int(k),np.asarray(p,dtype=float)) for k,p in track['keyframes'].items())
    if not keys or frame<keys[0][0] or frame>keys[-1][0]:
        raise ValueError('禁止关键帧范围外推')
    for start,end in track['blocked_intervals']:
        if start>end:raise ValueError('遮挡区间顺序错误')
        if start<=frame<=end:raise ValueError('本帧处于遮挡或不确定区间')
    left=max((v for v in keys if v[0]<=frame),key=lambda v:v[0])
    right=min((v for v in keys if v[0]>=frame),key=lambda v:v[0])
    for start,end in track['blocked_intervals']:
        if left[0]<=end and right[0]>=start:
            raise ValueError('禁止跨遮挡区间插值，需独立重建关键帧')
    if left[1].shape!=right[1].shape:raise ValueError('顶点拓扑不一致')
    def validate(p):
        if p.ndim!=2 or p.shape[1]!=2 or len(p)<3 or not np.isfinite(p).all():
            raise ValueError('多边形坐标非法')
        if np.any(p<0) or np.any(p[:,0]>=w) or np.any(p[:,1]>=h):
            raise ValueError('多边形越界')
        contour=np.rint(p).astype(np.int32)
        if not cv2.isContourConvex(contour) or abs(cv2.contourArea(contour))<1:
            raise ValueError('仅支持不退化凸多边形，不接受自交')
        return contour
    validate(left[1]);validate(right[1])
    t=0 if left[0]==right[0] else (frame-left[0])/(right[0]-left[0])
    polygon=validate(left[1]+t*(right[1]-left[1]))
    raster=np.zeros((h,w),np.uint8)
    cv2.fillPoly(raster,[polygon],1)
    return np.minimum(cv2.distanceTransform(raster,cv2.DIST_L2,3)/feather,1)


def selection_digest(track,shape,feather):
    """指纹只绑定选区条件，不代表已经完成审核。"""
    payload={'keyframes':track['keyframes'],'blocked_intervals':track['blocked_intervals'],
             'shape':list(shape),'feather':feather}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),
                                    allow_nan=False).encode()).hexdigest()


def execution_mask(track,frame,shape,feather,*,source_sha256=None):
    evidence=track.get('review_evidence',{})
    if (not isinstance(source_sha256,str) or len(source_sha256)!=64 or
        any(c not in '0123456789abcdef' for c in source_sha256)):
        raise ValueError('执行必须提供本次实际原片SHA-256')
    if (evidence.get('source_sha256')!=source_sha256 or
        evidence.get('selection_sha256')!=selection_digest(track,shape,feather)):
        raise ValueError('审核未绑定本原片与当前选区，或选区已变化')
    if (not evidence.get('reviewer') or not evidence.get('evidence_path') or
        frame not in evidence.get('reviewed_frames',[])):
        raise ValueError('本帧缺少具名逐帧复核证据，不能执行调色')
    return candidate_mask(track,frame,shape,feather)
