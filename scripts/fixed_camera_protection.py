"""固定机位研究工具：背景差分保护运动区域，不是人物分割或跟踪。"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageChops, ImageFilter


def register_background(frame: Image.Image, reference: Image.Image) -> tuple[Image.Image, dict]:
    """只对分析背景配准；上方35%须人工确认是静态建筑且无人物。"""
    import cv2
    if frame.size != reference.size:
        raise ValueError('配准图尺寸不一致')
    current, source = np.asarray(frame.convert('RGB')), np.asarray(reference.convert('RGB'))
    region = np.zeros(current.shape[:2], np.uint8)
    region[:int(frame.height*.35)] = 255
    detector = cv2.ORB_create(nfeatures=2500, edgeThreshold=12)
    kp1, des1 = detector.detectAndCompute(cv2.cvtColor(source, cv2.COLOR_RGB2GRAY), region)
    kp2, des2 = detector.detectAndCompute(cv2.cvtColor(current, cv2.COLOR_RGB2GRAY), region)
    if des1 is None or des2 is None:
        raise ValueError('静态区域无足够特征，不继续差分')
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(des1, des2, k=2)
    matches = [p[0] for p in pairs if len(p)==2 and p[0].distance < .7*p[1].distance]
    if len(matches) < 20:
        raise ValueError('静态区域可靠匹配不足20个')
    a = np.float32([kp1[m.queryIdx].pt for m in matches])
    b = np.float32([kp2[m.trainIdx].pt for m in matches])
    transform, inliers = cv2.estimateAffinePartial2D(a,b,method=cv2.RANSAC,
                                                   ransacReprojThreshold=2,maxIters=3000)
    if transform is None or int(inliers.sum()) < 20:
        raise ValueError('相机运动估计失败')
    selected = inliers.ravel().astype(bool)
    projected = cv2.transform(a[:,None,:],transform)[:,0,:]
    residual = float(np.median(np.linalg.norm(projected[selected]-b[selected],axis=1)))
    if residual > 1.5 or float(selected.mean()) < .6:
        raise ValueError('配准残差或内点比例不满足本轮研究门')
    aligned = cv2.warpAffine(source,transform,frame.size,flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(aligned), {'matrix':transform.tolist(),
            'inliers':int(inliers.sum()), 'median_residual_pixels':residual,
            'boundary':'仅静态建筑估计的小幅相似变换，非相机标定，前景视频不变形。'}


def background_plate(frames: np.ndarray, *, fixed_camera_confirmed: bool) -> Image.Image:
    """调用方必须检查固定机位；背景只作分析，不替换视频内容。"""
    if not fixed_camera_confirmed:
        raise ValueError('未确认固定机位，不建立背景参照')
    if frames.ndim != 4 or frames.shape[-1] != 3 or len(frames) < 3:
        raise ValueError('至少三帧同尺寸 RGB 原帧')
    if frames.dtype != np.uint8:
        raise ValueError('本研究工具只接收八位 RGB 分析帧')
    return Image.fromarray(np.median(frames, axis=0).astype(np.uint8))


def foreground_mask(frame: Image.Image, background: Image.Image) -> Image.Image:
    """编码值差 4→12 羽化；扩大两像素并保留实心区，阴影也可能受保护。"""
    if frame.size != background.size:
        raise ValueError('背景与当前帧尺寸不一致')
    # 只平滑分析支路，避免亚像素配准残差把建筑纹理当成前景。
    current = np.asarray(frame.convert('RGB').filter(ImageFilter.GaussianBlur(1.5)), dtype=np.int16)
    reference = np.asarray(background.convert('RGB').filter(ImageFilter.GaussianBlur(1.5)), dtype=np.int16)
    difference = np.max(np.abs(current-reference), axis=2)
    weight = np.clip((difference.astype(np.float32)-4)/8, 0, 1)
    core = Image.fromarray(np.rint(weight*255).astype(np.uint8))
    expanded = core.filter(ImageFilter.MaxFilter(5))
    return ImageChops.lighter(expanded, expanded.filter(ImageFilter.GaussianBlur(1.5)))
