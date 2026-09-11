"""雨夜蓝绿全帧工程代理门；不等同干湿语义或人工审美。"""
import json
import math
import shutil
import subprocess
from fractions import Fraction

NAMES=('wet','warm','memory')

def _probe(path):
    out=subprocess.check_output([shutil.which('ffprobe') or 'ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height,avg_frame_rate:format=duration','-of','json',str(path)])
    data=json.loads(out);v=data['streams'][0]
    return v['width'],v['height'],Fraction(v['avg_frame_rate']),float(data['format']['duration'])

def _validate(infos):
    w,h,fps,duration=infos[0]
    if fps<=0 or not math.isfinite(duration): raise ValueError('基础时序无效')
    for index,(x,y,rate,seconds) in enumerate(infos[1:],1):
        if x*h!=y*w or (index==1 and (x,y)!=(w,h)) or abs(float(rate-fps))>.001 or abs(seconds-duration)>float(1/fps):
            raise ValueError('多路纵横比、帧率或时长不一致')

def _frame(base,out,masks,index):
    pixels=len(base)//3
    if not pixels or len(base)!=len(out) or any(len(x)!=pixels for x in masks.values()): raise ValueError('帧长度不一致')
    totals={k:[0.0,0.0,0.0] for k in ('wet','outside','protect')}
    for i in range(pixels):
        wet=masks['wet'][i]/255;protect=max(masks['warm'][i],masks['memory'][i])/255
        groups={'wet':wet,'outside':max(0.0,1-max(wet,protect)),'protect':protect}
        o=i*3;d=[(out[o+c]-base[o+c])/255 for c in range(3)]
        for name,weight in groups.items():
            if not weight: continue
            totals[name][0]+=weight
            totals[name][1]+=sum(abs(x) for x in d)/3*weight
            totals[name][2]+=(d[2]-d[0])*weight
    row={'frame':index}
    for name,(weight,mae,cold) in totals.items():
        row[name]={'weight':weight,'mae':mae/weight if weight else None,'cool_delta':cold/weight if weight else None}
    return row

def evaluate(rows):
    failures=[]
    if not rows: failures.append('没有完整帧')
    missing=sum(1 for row in rows if any(not row[n]['weight'] for n in ('wet','outside','protect')))
    if rows and missing/len(rows)>.02: failures.append('超过2%帧缺少湿路、外部或保护区域')
    usable=[r for r in rows if all(r[n]['weight'] for n in ('wet','outside','protect'))]
    def mean(name,key): return sum(r[name][key] for r in usable)/len(usable) if usable else float('nan')
    metrics={'wet_mae':mean('wet','mae'),'outside_mae':mean('outside','mae'),'protect_mae':mean('protect','mae'),'wet_cool_delta':mean('wet','cool_delta')}
    if not all(math.isfinite(v) for v in metrics.values()): failures.append('汇总数值无效')
    elif not (metrics['wet_mae']>=.012 and metrics['wet_cool_delta']>.02 and metrics['wet_mae']>=3*metrics['outside_mae'] and metrics['wet_mae']>=3*metrics['protect_mae']):
        failures.append('湿路变化、冷化或区域集中度未达合同')
    return {'status':'blocked' if failures else 'passed','frame_count':len(rows),'usable_frames':len(usable),'metrics':metrics,'failures':failures,'aesthetic_status':'pending','boundary':'320宽显示参照RGB固定代理；不是干湿语义或审美'}

def measure(base,candidate,masks):
    if set(masks)!=set(NAMES): raise ValueError('必须提供wet、warm、memory')
    paths=[base,candidate]+[masks[n] for n in NAMES];infos=[_probe(p) for p in paths];_validate(infos)
    w,h,fps,_=infos[0];ow=320;oh=round(h*ow/w);processes=[];rows=[]
    try:
        for i,path in enumerate(paths):
            fmt='rgb24' if i<2 else 'gray'
            vf=f"settb=AVTB,setpts=N*{fps.denominator}/({fps.numerator}*TB),scale={ow}:{oh}:flags=area"
            processes.append(subprocess.Popen([shutil.which('ffmpeg') or 'ffmpeg','-v','error','-xerror','-threads','1','-i',str(path),'-map','0:v:0','-an','-vf',vf,'-vsync','0','-pix_fmt',fmt,'-f','rawvideo','pipe:1'],stdout=subprocess.PIPE,stderr=subprocess.PIPE))
        sizes=[ow*oh*(3 if i<2 else 1) for i in range(5)]
        while True:
            frames=[p.stdout.read(s) for p,s in zip(processes,sizes)]
            if not any(frames): break
            if any(len(f)!=s for f,s in zip(frames,sizes)): raise ValueError('多路残帧或帧数不一致')
            rows.append(_frame(frames[0],frames[1],dict(zip(NAMES,frames[2:])),len(rows)))
        for p in processes:
            error=p.stderr.read();code=p.wait()
            if code: raise ValueError('完整解码失败：'+error.decode(errors='replace'))
        return rows
    finally:
        for p in processes:
            if p.poll() is None:p.kill()
            p.wait();p.stdout.close();p.stderr.close()
