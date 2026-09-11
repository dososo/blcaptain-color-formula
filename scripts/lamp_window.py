"""人工初始化纸灯的平移窗口实验，非语义或通用跟踪器。"""
import cv2
import numpy as np

def locate(reference,current,box):
    if reference.shape!=current.shape:
        raise ValueError('尺寸变化')
    x0,y0,x1,y1=box; h,w=reference.shape[:2]
    if not 0<=x0<x1<=w or not 0<=y0<y1<=h:
        raise ValueError('模板越界')
    gray=cv2.cvtColor(reference,cv2.COLOR_RGB2GRAY)
    template=gray[y0:y1,x0:x1]
    if float(template.std())<8:
        raise ValueError('模板纹理不足')
    x,y=max(0,x0-35),max(0,y0-35)
    scene=cv2.cvtColor(current,cv2.COLOR_RGB2GRAY)
    search=scene[y:min(h,y1+35),x:min(w,x1+35)]
    scores=cv2.matchTemplate(search,template,cv2.TM_CCOEFF_NORMED)
    _,score,_,pos=cv2.minMaxLoc(scores)
    xx,yy=x+pos[0],y+pos[1]
    patch=scene[yy:yy+y1-y0,xx:xx+x1-x0]
    error=float(np.abs(patch.astype(float)-template).mean())
    if score<.90 or error>18:
        raise ValueError(f'模板失配：相关{score:.4f}，灰度误差{error:.3f}')
    return {'offset':[xx-x0,yy-y0],'score':score,'mae':error}

def locate_visible(reference,current,box):
    # 首帧固定参照；九块中至少三块平移共识，不使用失配帧更新模板。
    x0,y0,x1,y1=box
    xs=np.linspace(x0,x1,4,dtype=int);ys=np.linspace(y0,y1,4,dtype=int)
    matches=[]
    for j in range(3):
        for i in range(3):
            try:
                matches.append(locate(reference,current,(xs[i],ys[j],xs[i+1],ys[j+1])))
            except ValueError:
                continue
    if len(matches)<3:raise ValueError('可见纹理不足三块')
    offsets=np.array([r['offset'] for r in matches])
    groups=[np.max(abs(offsets-p),axis=1)<=1 for p in offsets]
    best=max(groups,key=lambda g:int(g.sum()))
    if int(best.sum())<3:raise ValueError('可见纹理没有三块平移共识')
    offset=np.rint(np.median(offsets[best],axis=0)).astype(int).tolist()
    return {'offset':offset,'visible_blocks':len(matches),'consensus_blocks':int(best.sum())}

def unchanged_mask(reference,current,offset):
    # 像素变化保护，不是语义遮挡识别；同色遮挡仍可能漏检。
    h,w=current.shape[:2]
    aligned=cv2.warpAffine(reference,np.float32([[1,0,offset[0]],[0,1,offset[1]]]),(w,h))
    error=np.max(abs(aligned.astype(float)-current.astype(float)),axis=2)
    changed=(error>18).astype(np.uint8)
    changed=cv2.dilate(changed,np.ones((5,5),np.uint8))
    return 1-changed

def window(shape,polygon,offset):
    h,w=shape; points=np.asarray(polygon,dtype=np.int32)+np.asarray(offset,dtype=np.int32)
    if np.any(points<0) or np.any(points[:,0]>=w) or np.any(points[:,1]>=h):
        raise ValueError('窗口离画')
    mask=np.zeros((h,w),np.uint8);cv2.fillPoly(mask,[points],1)
    return np.minimum(cv2.distanceTransform(mask,cv2.DIST_L2,3)/3,1)

def paper_mask(rgb,selection):
    # 黑字、红字及高亮退出；颜色阈值不是文字识别。
    r,g,b=np.moveaxis(rgb.astype(float)/255,-1,0)
    y=.2126*r+.7152*g+.0722*b
    weight=np.clip((y-.12)/.12,0,1)*np.clip((.88-y)/.12,0,1)
    red=(r>g*1.7)&(r>b*1.7)
    return selection*weight*(~red)
