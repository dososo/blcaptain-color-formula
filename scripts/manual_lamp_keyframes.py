"""研究用人工逐帧窗口；不插值、不跟踪、不进入正式渲染。"""
from lamp_window import window

def selection_for(frame,annotations,shape):
    record=annotations['frames'].get(str(frame))
    if record is None:
        raise ValueError('本帧没有人工几何标注，禁止插值代替')
    if record['state']!='visible':
        raise ValueError('透射或模糊混合信号未解决，不生成可执行选区')
    return window(shape,record['polygon'],(0,0))
