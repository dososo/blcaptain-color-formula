"""逐帧按输入的保护阴影取样；编码容差是工程门，不是审美评分。"""
import shutil
import subprocess


def compare_frame(source, output):
    if len(source) != len(output) or not source or len(source) % 3:
        raise ValueError('阴影比较帧尺寸不一致或为空')
    count = severe = 0
    bias = [0, 0, 0]
    absolute = 0
    channel_absolute = [0, 0, 0]
    deep_source = bytearray()
    deep_output = bytearray()
    for offset in range(0, len(source), 3):
        red, green, blue = source[offset:offset + 3]
        if .2126 * red + .7152 * green + .0722 * blue >= .12 * 255:
            continue
        count += 1
        if .2126 * red + .7152 * green + .0722 * blue <= .055 * 255:
            deep_source.extend(source[offset:offset + 3])
            deep_output.extend(output[offset:offset + 3])
        peak = 0
        for channel in range(3):
            delta = output[offset + channel] - source[offset + channel]
            bias[channel] += delta
            absolute += abs(delta)
            channel_absolute[channel] += abs(delta)
            peak = max(peak, abs(delta))
        severe += peak > 8
    coverage = count / (len(source) // 3)
    means = [value / max(count, 1) for value in bias]
    mae = absolute / max(3 * count, 1)
    channel_mae = [value / max(count, 1) for value in channel_absolute]
    severe_fraction = severe / max(count, 1)
    deep = None
    if deep_source:
        deltas = [b - a for a, b in zip(deep_source, deep_output)]
        deep_count = len(deltas) // 3
        deep_bias = [sum(deltas[c::3]) / deep_count for c in range(3)]
        deep_mae = [sum(abs(v) for v in deltas[c::3]) / deep_count for c in range(3)]
        deep_severe = sum(max(map(abs, deltas[i:i + 3])) > 8
                          for i in range(0, len(deltas), 3)) / deep_count
        deep = {'pixels': deep_count, 'channel_bias_codes': deep_bias,
                'channel_mae_codes': deep_mae, 'over_8_codes_fraction': deep_severe,
                'passed': max(map(abs, deep_bias)) <= 1 and max(deep_mae) <= 2
                          and deep_severe <= .01}
    return {'reference_pixels': count, 'coverage': coverage,
            'deep_black': deep,
            'channel_bias_codes': means, 'mean_absolute_codes': mae,
            'channel_mae_codes': channel_mae,
            'over_8_codes_fraction': severe_fraction,
            'passed': (coverage >= .003 and max(map(abs, means)) <= 1
                       and max(channel_mae) <= 2 and severe_fraction <= .01
                       and (deep is None or deep['passed']))}


def verify_video(source, output, width, height, frame_count):
    size = int(width) * int(height) * 3
    processes = []
    reports = []
    try:
        for path in (source, output):
            processes.append(subprocess.Popen([
                shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-i', str(path),
                '-map', '0:v:0', '-vsync', '0', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
        for _ in range(int(frame_count)):
            frames = [p.stdout.read(size) for p in processes]
            if any(len(frame) != size for frame in frames):
                raise ValueError('阴影验证视频提前结束')
            reports.append(compare_frame(*frames))
        if any(p.stdout.read(1) for p in processes):
            raise ValueError('阴影验证出现额外视频帧')
        if any(p.wait() != 0 for p in processes):
            raise ValueError('阴影验证解码失败')
    finally:
        for process in processes:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()
    failures = [index for index, item in enumerate(reports) if not item['passed']]
    return {'status': 'passed' if not failures and reports else 'blocked',
            'frames': reports, 'failed_frames': failures, 'full_resolution': True,
            'reference': '同源输入RGB亮度小于0.12，不按输出重新选择阴影',
            'budget': '每帧：样本面积至少0.003；各通道平均偏移≤1码；平均绝对误差≤2码；超过8码的像素≤1%'}
