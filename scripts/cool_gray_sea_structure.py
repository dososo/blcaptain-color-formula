"""固定机位海岸结构研究；人工几何海面带，不是语义蒙版。

仅针对mixkit-51474。沿用第一版冷暖关系，在显示编码RGB中分离
海面中间调与亮部；不能解释为线性曝光或推广到移动镜头。
"""
import numpy as np
try:
    from .cool_gray_sea_grade import grade as previous_grade, _ramp
except ImportError:
    from cool_gray_sea_grade import grade as previous_grade, _ramp

def grade(rgb,strength):
    out=previous_grade(rgb,strength)
    if out.ndim!=3:
        raise ValueError('固定海面区域要求高×宽×3图像')
    if strength==0:return out
    position=np.linspace(0,1,out.shape[0])[:,None]
    band=_ramp(position,.43,.47)*(1-_ramp(position,.65,.74))
    y=.2126*out[:,:,0]+.7152*out[:,:,1]+.0722*out[:,:,2]
    target=np.interp(y,[0,.18,.42,.60,.78,.92,1],
                       [0,.13,.32,.61,.83,.94,1])
    delta=(target-y)*strength*band
    # 等量移动RGB保留通道差；限制位移以免新增端点剪切。
    delta=np.clip(delta,-.98*out.min(axis=2),.98*(1-out.max(axis=2)))
    return out+delta[:,:,None]
