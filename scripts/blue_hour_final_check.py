"""蓝调逐帧空间工程代理门，不代表审美验收或语义识别。

RGB 为解码后的 8 位显示参照码值 / 255（非线性光）。冷化为 B-R
差值的变化，亮度为 .2126R+.7152G+.0722B 的变化；锚点为三通道
平均绝对差。成员与连续权重仅由基础蒙版确定，不看成片选区。
"""
import json
import math
import shutil
import subprocess
from fractions import Fraction


REGIONS = ('top', 'middle', 'horizon', 'anchor')


def _measure_frame(base, candidate, masks, index):
    size = len(base) // 3
    if not size or len(base) % 3 or len(candidate) != len(base):
        raise ValueError('RGB 帧长度不完整或不一致')
    row = {'frame': index}
    for name in REGIONS:
        mask = masks[name]
        if len(mask) != size:
            raise ValueError('蒙版与 RGB 帧长度不一致')
        count = 0
        total_weight = 0.0
        cold = luma = mae = 0.0
        for pixel, weight in enumerate(mask):
            if weight == 0:
                continue
            weight = weight / 255
            offset = pixel * 3
            dr, dg, db = ((candidate[offset + c] - base[offset + c]) / 255
                          for c in range(3))
            count += 1
            total_weight += weight
            cold += (db - dr) * weight
            luma += (.2126 * dr + .7152 * dg + .0722 * db) * weight
            mae += (abs(dr) + abs(dg) + abs(db)) / 3 * weight
        row[name] = {'count': count, 'weight_sum': total_weight,
                     'cool_delta': cold / total_weight if total_weight else None,
                     'luma_delta': luma / total_weight if total_weight else None,
                     'rgb_mae': mae / total_weight if total_weight else None}
    return row


def evaluate(rows):
    """每帧必须三带正冷化且顶部>中部>地平线；锚点与亮度有上限。"""
    failures = []
    if not rows:
        failures.append({'frame': None, 'reason': '没有完整帧'})
    for index, row in enumerate(rows):
        valid = True
        for name in REGIONS:
            region = row.get(name, {})
            keys = ('rgb_mae',) if name == 'anchor' else ('cool_delta', 'luma_delta')
            if (not isinstance(region.get('count'), int) or region['count'] <= 0
                    or any(not isinstance(region.get(k), (int, float))
                           or not math.isfinite(region[k]) for k in keys)):
                failures.append({'frame': index, 'reason': name + ' 区域缺失或数值无效'})
                valid = False
        if not valid:
            continue
        if not row['top']['cool_delta'] > row['middle']['cool_delta'] > row['horizon']['cool_delta'] > 0:
            failures.append({'frame': index, 'reason': '三带冷化方向未分离'})
        if not 0 <= row['anchor']['rgb_mae'] <= .015:
            failures.append({'frame': index, 'reason': '锚点 RGB 平均绝对差超过 .015'})
        for name in REGIONS[:3]:
            if abs(row[name]['luma_delta']) > .02:
                failures.append({'frame': index, 'reason': name + ' 亮度差超过 .02'})
    return {'status': 'blocked' if failures else 'passed', 'frame_count': len(rows),
            'failures': failures, 'aesthetic_status': 'pending',
            'units': '8 位显示参照 RGB 码值归一化至 0–1，非线性光；工程代理非审美'}


def _probe(path):
    result = subprocess.run([shutil.which('ffprobe') or 'ffprobe', '-v', 'error',
                             '-select_streams', 'v:0', '-show_entries',
                             'stream=width,height,avg_frame_rate:format=duration', '-of', 'json', str(path)],
                            capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    stream = report['streams'][0]
    return (stream['width'], stream['height'], Fraction(stream['avg_frame_rate']),
            float(report['format']['duration']))


def _validate_probes(info):
    width, height, rate, duration = info[0]
    if rate <= 0 or duration <= 0 or not math.isfinite(duration):
        raise ValueError('基础帧率或时长无效')
    for index, (w, h, fps, seconds) in enumerate(info[1:], 1):
        if (w * height != h * width or (index == 1 and (w, h) != (width, height))
                or abs(float(fps - rate)) > .001
                or not math.isfinite(seconds) or abs(seconds - duration) > float(1 / rate)):
            raise ValueError('多路纵横比、帧率或时长不一致')


def measure(base, candidate, masks):
    """全帧读取基础、成片及四个同源蒙版；长度或合同容差以外差异报错。

    返回逐帧 rows，交 evaluate(rows)。宽度固定缩至320，保留纵横比。
    按解码位置统一为源精确时钟，不插帧；同源与原始起点由入口额外验证。
    """
    if set(masks) != set(REGIONS):
        raise ValueError('必须提供 top、middle、horizon、anchor 四个蒙版')
    paths = [base, candidate] + [masks[name] for name in REGIONS]
    info = [_probe(path) for path in paths]
    _validate_probes(info)
    width, height, rate, duration = info[0]
    out_width, out_height = 320, max(1, round(height * 320 / width))
    processes = []
    rows = []
    try:
        for index, path in enumerate(paths):
            fmt = 'rgb24' if index < 2 else 'gray'
            command = [shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-xerror', '-threads', '1',
                       '-i', str(path), '-map', '0:v:0', '-an', '-vf',
                       'settb=AVTB,setpts=N*{}/({}*TB),scale={}:{}:flags=area'.format(
                           rate.denominator, rate.numerator, out_width, out_height),
                       '-vsync', '0', '-pix_fmt', fmt, '-f', 'rawvideo', 'pipe:1']
            processes.append(subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        sizes = [out_width * out_height * (3 if i < 2 else 1) for i in range(6)]
        while True:
            frames = [process.stdout.read(size) for process, size in zip(processes, sizes)]
            if not any(frames):
                break
            if any(len(frame) != size for frame, size in zip(frames, sizes)):
                raise ValueError('基础、成片或蒙版帧数不一致，或出现残帧')
            rows.append(_measure_frame(frames[0], frames[1], dict(zip(REGIONS, frames[2:])), len(rows)))
        for process in processes:
            error = process.stderr.read()
            if process.wait():
                raise ValueError('完整解码失败：' + error.decode('utf-8', errors='replace'))
        if not rows:
            raise ValueError('没有可测量的视频帧')
        return rows
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()
