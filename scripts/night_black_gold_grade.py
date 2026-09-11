"""夜景黑金分层研究链：真实暖亮点＋蓝色环境代理，不是光源语义。"""
from __future__ import annotations
import math
import subprocess
from fractions import Fraction
from pathlib import Path
from PIL import Image, ImageChops, ImageFilter
import night_cool_neon_grade as common


def region_masks(rgb: Image.Image) -> dict[str, Image.Image]:
    rgb=rgb.convert('RGB'); y=rgb.convert('L')
    hue,sat,_=rgb.convert('HSV').split()
    # 邻域更亮的已有暖像素；均匀黄墙不得当灯。镜面/暖物体仍可能误选。
    local=ImageChops.subtract(y,y.filter(ImageFilter.GaussianBlur(max(2,rgb.width*.015))))
    gold=ImageChops.multiply(hue.point(common._band(14,22,40,48)),sat.point(common._ramp(24,75)))
    gold=ImageChops.multiply(gold,y.point(common._band(45,85,160,225)))
    gold=ImageChops.multiply(gold,local.point(common._ramp(4,24)))
    environment=ImageChops.multiply(hue.point(common._band(120,135,165,180)),sat.point(common._ramp(18,75)))
    environment=ImageChops.multiply(environment,y.point(common._band(6,24,125,200)))
    return {'gold':gold,'environment':environment}


def eligible(rows: list[dict]) -> bool:
    # 工程入口仅代表每帧有足够可测像素，必须先人工确认是城市夜景。
    return bool(rows) and all(.0001 <= r['gold'] <= .12 and r['environment'] >= .01 for r in rows)


def build_filter_complex(strength: float,width: int,height: int,fps: str,*,revision: int=1) -> str:
    if revision not in (1,2):
        raise ValueError('未知研究修订版')
    if not math.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('强度必须为0到1有限数')
    rate=Fraction(fps); clock=f'settb=1/{rate.numerator},setpts=N*{rate.denominator}'
    # 蓝环境降彩，不向暖物体加冷；保持暗端，不追求固定纯黑面积。
    density=f'0/0 .035/.035 .15/{.15-.025*strength:.6f} .4/{.4-.025*strength:.6f} .75/.75 1/1'
    light=f'0/0 .08/.08 .25/{.25+.07*strength:.6f} .5/{.5+.045*strength:.6f} .8/.8 1/1'
    reduction=.82
    if revision==2:
        # 保留蓝色空间，只增强既有暖中调；高亮灯芯不追加亮度。
        reduction=.35
        light=f'0/0 .08/.08 .25/{.25+.10*strength:.6f} .5/{.5+.03*strength:.6f} .8/.8 1/1'
    return (
        f'[0:v]format=gbrp,{clock},split=2[original][environmentinput];'
        f"[environmentinput]hue=s={1-reduction*strength:.6f},curves=all='{density}'[shaped];"
        f'[1:v]scale={width}:{height}:flags=bilinear,format=gbrp,{clock}[environment];'
        '[original][shaped][environment]maskedmerge=planes=7,split=2[base][lightinput];'
        f"[lightinput]hue=s={1+.4*strength:.6f},curves=all='{light}'[lit];"
        f'[2:v]scale={width}:{height}:flags=bilinear,format=gbrp,{clock}[gold];'
        '[base][lit][gold]maskedmerge=planes=7,format=yuv420p,'
        'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709[out]')


def strength_advice(strength_percent: int,passed: bool) -> str:
    return '交人工复看，尚未接受。' if passed else '回退至较低档或Foundation，重新检查区域；不继续加力。'


def render(foundation: Path,masks: dict[str,Path],output: Path,strength: float,
           *, scene_confirmed: bool, coverage: list[dict],revision: int=1) -> dict:
    if not scene_confirmed or not eligible(coverage):
        raise ValueError('素材未确认或逐帧灯光/环境代理不足，停止研究渲染')
    inputs=[foundation,masks['environment'],masks['gold']]
    if output.exists() or output.resolve() in [p.resolve() for p in inputs]:
        raise ValueError('不覆盖原片、蒙版或既有成片')
    hashes={str(p):common._sha256(p) for p in inputs}
    probe=common._probe(foundation); v=common._video_stream(probe)
    count=common._frame_count(v); fps=v['avg_frame_rate']
    if count<=0 or len(coverage)!=count:
        raise ValueError('逐帧证据与原片帧数不一致')
    if any(v.get(k)!='bt709' for k in ('color_space','color_transfer','color_primaries')):
        raise ValueError('只接受完整BT.709显示参照Foundation，不猜测Log')
    for p in inputs[1:]:
        mv=common._video_stream(common._probe(p))
        if common._frame_count(mv)!=count or Fraction(mv['avg_frame_rate'])!=Fraction(fps):
            raise ValueError('蒙版帧时钟不一致')
    graph=build_filter_complex(strength,int(v['width']),int(v['height']),fps,revision=revision)
    output.parent.mkdir(parents=True,exist_ok=True)
    command=['ffmpeg','-v','error','-n']
    for p in inputs: command+=['-i',str(p)]
    command+=['-filter_complex',graph,'-map','[out]','-map','0:a?','-c:a','copy',
              '-frames:v',str(count),'-c:v','libx264','-crf','18','-pix_fmt','yuv420p',
              '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',
              '-movflags','+faststart',str(output)]
    subprocess.run(command,check=True,capture_output=True)
    outprobe=common._probe(output); ov=common._video_stream(outprobe)
    if common._frame_count(ov)!=count or Fraction(ov['avg_frame_rate'])!=Fraction(fps):
        raise ValueError('输出帧时钟错误，保留失败文件不交付')
    if any(ov.get(k)!='bt709' for k in ('color_space','color_transfer','color_primaries')):
        raise ValueError('输出色彩标签缺失')
    if any(common._sha256(p)!=hashes[str(p)] for p in inputs):
        raise ValueError('输入哈希变化')
    return {'technical':'encoded_not_yet_evaluated','aesthetic':'pending','strength':strength,'revision':revision,
            'input_hashes':hashes,'output':str(output),'output_sha256':common._sha256(output),
            'command':command,'probe':outprobe,'boundary':'独立研究链，色相/亮度/邻域代理不是光源语义；不晋级。'}
