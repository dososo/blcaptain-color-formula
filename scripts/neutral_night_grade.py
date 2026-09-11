"""中性夜景研究分支：人工指定保护区，背景中间调塑形；不接正式目录。"""
from __future__ import annotations

import math
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import night_cool_neon_grade as base


def environment_mask(rgb: Image.Image, protection: Image.Image) -> Image.Image:
    """只选择未保护的中间调；不要求原片已有彩色灯光。"""
    if rgb.size != protection.size:
        raise ValueError('保护区与原帧尺寸不一致')
    tone = rgb.convert('L').point(base._band(16, 48, 106, 170))
    red, _, blue = rgb.convert('RGB').split()
    warm = ImageChops.subtract(red, blue).point(
        lambda x: round(255 * max(0., min(1., (x - 18) / 36))))
    available = ImageChops.invert(ImageChops.lighter(protection.convert('L'), warm))
    return ImageChops.multiply(tone, available)


def intimate_masks(rgb: Image.Image, foreground: Image.Image) -> tuple[Image.Image, Image.Image]:
    """运动代理与人工纵向范围交集，不声称肤色或人物语义。"""
    environment = environment_mask(rgb, foreground)
    luma = rgb.convert('L')
    light_protection = luma.point(lambda x: round(255 * max(0., min(1., (145-x)/100))))
    environment = ImageChops.multiply(environment, light_protection)
    subject = ImageChops.multiply(foreground.convert('L'), luma.point(base._band(20,50,150,200)))
    zone = Image.new('L', rgb.size)
    for y in range(rgb.height):
        height = y / max(1, rgb.height-1)
        amount = max(0.,min(1.,(height-.32)/.06,(.75-height)/.1))
        zone.paste(round(255*amount),(0,y,rgb.width,y+1))
    return environment, ImageChops.multiply(subject,zone)


def final_masks(rgb: Image.Image, foreground: Image.Image) -> tuple[Image.Image, Image.Image]:
    """本素材最后收口：下部冷色减弱、背景中高调小幅退后；非语义。"""
    environment, _ = intimate_masks(rgb, foreground)
    zone = Image.new('L', rgb.size)
    for y in range(rgb.height):
        weight = max(0., min(1., (y/max(1,rgb.height-1)-.52)/.20))
        zone.paste(round(255*(1-.7*weight)), (0,y,rgb.width,y+1))
    background = ImageChops.multiply(
        ImageChops.invert(foreground.convert('L')),
        rgb.convert('L').point(base._band(35,85,175,230)))
    return ImageChops.multiply(environment,zone), background


def activity_protection(size: tuple[int, int], seconds: float,
                        keys: list[tuple[float, float, float]]) -> Image.Image:
    """人工活动框线性插值，框内全保护、框外羽化；不是语义跟踪。"""
    if len(keys) < 2 or any(b[0] <= a[0] for a, b in zip(keys, keys[1:])):
        raise ValueError('至少两个严格递增的人工关键帧')
    t = max(keys[0][0], min(keys[-1][0], seconds))
    for a, b in zip(keys, keys[1:]):
        if a[0] <= t <= b[0]:
            weight = (t - a[0]) / (b[0] - a[0])
            left = a[1] + weight * (b[1] - a[1])
            right = a[2] + weight * (b[2] - a[2])
            break
    width, height = size
    core = Image.new('L', size)
    if right > 0 and left < 1:
        ImageDraw.Draw(core).rectangle((max(0, int(left*width)), int(.36*height),
                                       min(width-1, int(right*width)), height-1), fill=255)
    feather = core.filter(ImageFilter.GaussianBlur(max(1, width*.035)))
    return ImageChops.lighter(core, feather)


def lower_region_protection(size: tuple[int, int]) -> Image.Image:
    """仅用于已审视的固定机位：25% 开始羽化，35% 以下完全保留。"""
    width, height = size
    result = Image.new('L', size)
    for y in range(height):
        amount = max(0., min(1., (y / max(1, height - 1) - .25) / .10))
        result.paste(round(255 * amount), (0, y, width, y + 1))
    return result


def build_filter_complex(strength: float, width: int, height: int,
                         frame_rate: str = '24000/1001', subject_lift: bool = False,
                         background_shape: bool = False) -> str:
    if background_shape and not subject_lift:
        raise ValueError('收口实验必须保留原主体塑光链')
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('研究强度必须是 0–1 有限数')
    rate = Fraction(frame_rate)
    clock = f'settb=1/{rate.numerator},setpts=N*{rate.denominator}'
    graph = (
        f'[0:v]format=gbrp,{clock},split=2[original][world];'
        f'[world]colorbalance=rm={-.14*strength:.6f}:'
        f'gm={.025*strength:.6f}:bm={.20*strength:.6f}:pl=1[cool];'
        f'[1:v]scale={width}:{height}:flags=bilinear,format=gbrp,'
        f'{clock}[region];'
        '[original][cool][region]maskedmerge=planes=7'
    )
    if not subject_lift:
        return graph + ',format=yuv420p[out]'
    curve = f'0/0 .06/.06 .18/{.18+.07*strength:.6f} .4/{.4+.06*strength:.6f} .7/.7 1/1'
    graph = (graph + ',split=2[base][liftinput];'
            f"[liftinput]curves=all='{curve}'[lifted];"
            f'[2:v]scale={width}:{height}:flags=bilinear,format=gbrp,{clock}[subject];'
            '[base][lifted][subject]maskedmerge=planes=7')
    if background_shape:
        curve = f'0/0 .1/.1 .35/{.35-.055*strength:.6f} .65/{.65-.06*strength:.6f} .9/.9 1/1'
        graph += (',split=2[composed][diminput];'
                  f"[diminput]curves=all='{curve}'[dimmed];"
                  f'[3:v]scale={width}:{height}:flags=bilinear,format=gbrp,{clock}[background];'
                  '[composed][dimmed][background]maskedmerge=planes=7')
    return graph + ',format=yuv420p[out]'


def strength_advice(strength_percent: int, passed: bool) -> str:
    return ('保持并交人工观看。' if passed else
            '回退至较低档或原片；先检查区域和冷暖关系，不继续加力。')


def render(foundation: Path, mask: Path, output: Path, strength: float,
           subject_mask: Path | None = None, background_mask: Path | None = None) -> dict:
    """仅研究预演；调用方负责确认几何区域与提供同帧率蒙版。"""
    inputs = [foundation,mask] + ([subject_mask] if subject_mask else []) + ([background_mask] if background_mask else [])
    extra_hash = base._sha256(background_mask) if background_mask else None
    if background_mask and not subject_mask:
        raise ValueError('收口实验必须提供主体蒙版')
    if output.exists() or output.resolve() in [p.resolve() for p in inputs]:
        raise ValueError('不覆盖既有输出、原片或蒙版')
    source_hash, mask_hash = base._sha256(foundation), base._sha256(mask)
    probe, mask_probe = base._probe(foundation), base._probe(mask)
    video, mv = base._video_stream(probe), base._video_stream(mask_probe)
    count = base._frame_count(video)
    if count <= 0 or count != base._frame_count(mv):
        raise ValueError('原片与蒙版帧数不一致')
    if Fraction(video['avg_frame_rate']) != Fraction(mv['avg_frame_rate']):
        raise ValueError('原片与蒙版帧率不一致')
    subject_hash = None
    if background_mask:
        bv = base._video_stream(base._probe(background_mask))
        if base._frame_count(bv) != count or Fraction(bv['avg_frame_rate']) != Fraction(video['avg_frame_rate']):
            raise ValueError('背景蒙版帧时钟不一致')
    if subject_mask:
        sv = base._video_stream(base._probe(subject_mask))
        if base._frame_count(sv) != count or Fraction(sv['avg_frame_rate']) != Fraction(video['avg_frame_rate']):
            raise ValueError('塑光蒙版帧时钟不一致')
        subject_hash = base._sha256(subject_mask)
    if any(video.get(k) != 'bt709' for k in
           ('color_space', 'color_transfer', 'color_primaries')):
        raise ValueError('仅接受完整 BT.709 显示参照片')
    graph = build_filter_complex(strength, int(video['width']), int(video['height']),
                                 video['avg_frame_rate'], subject_lift=subject_mask is not None,
                                 background_shape=background_mask is not None)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-n',
               '-i', str(foundation), '-i', str(mask)]
    if subject_mask:
        command += ['-i',str(subject_mask)]
    if background_mask:
        command += ['-i',str(background_mask)]
    command += ['-filter_complex', graph,
               '-map', '[out]', '-map', '0:a?', '-c:a', 'copy',
               '-frames:v', str(count), '-c:v', 'libx264', '-crf', '18',
               '-preset', 'medium', '-pix_fmt', 'yuv420p',
               '-color_primaries', 'bt709', '-color_trc', 'bt709',
               '-colorspace', 'bt709', '-movflags', '+faststart', str(output)]
    subprocess.run(command, check=True, capture_output=True)
    result_probe = base._probe(output)
    if base._frame_count(base._video_stream(result_probe)) != count:
        raise ValueError('输出帧数错误；保留失败文件供检查，不交付')
    if base._sha256(foundation) != source_hash or base._sha256(mask) != mask_hash:
        raise ValueError('输入哈希发生变化')
    if subject_mask and base._sha256(subject_mask) != subject_hash:
        raise ValueError('塑光蒙版哈希发生变化')
    if background_mask and base._sha256(background_mask) != extra_hash:
        raise ValueError('背景蒙版哈希发生变化')
    return {'status': 'research_preview', 'aesthetic': 'pending',
            'foundation': str(foundation), 'foundation_sha256': source_hash,
            'mask': str(mask), 'mask_sha256': mask_hash,
            'output': str(output), 'output_sha256': base._sha256(output),
            'strength': strength, 'command': command, 'probe': result_probe,
            'subject_mask': str(subject_mask) if subject_mask else None,
            'subject_mask_sha256': subject_hash,
            'background_mask': str(background_mask) if background_mask else None,
            'background_mask_sha256': extra_hash,
            'boundary': '人工指定几何保护，不是人物识别或跟踪；尚未接入正式入口。'}
