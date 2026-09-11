"""青橙55%单源实验：冻结历史链，逐帧颜色代理门，不声称语义跟踪。"""
import json
import subprocess
from fractions import Fraction
from pathlib import Path

try:
    from . import korean_cool_grade as media
except ImportError:
    import korean_cool_grade as media

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / 'references/teal-frozen-55.json'
ASSETS = ROOT.parents[1] / 'outputs/blcaptain-foundation-phase2d2/arri-rec709'
SOURCE_HASH = 'b7532898e677fd389d51ab72b59fa56c3baa1264637abe38f4c5676643d3e2fb'
AUXILIARY = {
    'foundation-only.mov': '0b804333d2ad409a1ba2fc32a0e07dcccacd7cb12c7eb3e75d7d07985f7fd623',
    'memory-mask-v2.mkv': 'c8d42fb83021547dc36dc478393c19ec238fda7870a71711f36b06972cb0c8f0',
}
BLACK_LOWER_LIMIT = -.045
BLACK_MEASUREMENT_TOLERANCE = .05 / 255
BLACK_NOMINAL_EXCEED_FRACTION = .01


def bound_assets():
    result = {}
    for name, expected in AUXILIARY.items():
        path = ASSETS / name
        if not path.is_file() or media._sha256(path) != expected:
            raise ValueError('青橙基础母版或同源保护代理缺失/变化：' + name)
        result[str(path)] = expected
    return result


def protection_graph(width, height, fps):
    rate = Fraction(fps)
    if rate <= 0:
        raise ValueError('帧率必须为正')
    clock = f'settb=AVTB,setpts=N*{rate.denominator}/({rate.numerator}*TB)'
    return (f'[0:v]format=gbrp,{clock}[base];'
            f'[1:v]format=gbrp,{clock},split=2[full][mix];'
            "[base][mix]blend=all_expr='A*0.75+B*0.25'[limited];"
            f'[2:v]scale={width}:{height}:flags=bilinear,format=gbrp,{clock}[mask];'
            '[full][limited][mask]maskedmerge=planes=7,format=yuv420p[out]')


def pixels(path, gray=False):
    return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
        '-vf', 'scale=160:90', '-pix_fmt', 'gray' if gray else 'rgb24', '-f', 'rawvideo', '-'])


def measure_frames(base, candidate, mask):
    before, after, weights = pixels(base), pixels(candidate), pixels(mask, True)
    size = 160 * 90
    if not weights or len(weights) % size or len(before) != len(weights)*3 or len(after) != len(before):
        raise ValueError('三路分析帧不完整或不同步')
    rows = []
    for start in range(0, len(weights), size):
        groups = {name: [] for name in ('world_change', 'protect_change', 'world_cool_delta', 'white_change', 'black_lift')}
        for i in range(start, start + size):
            a = before[i*3:i*3+3]; b = after[i*3:i*3+3]
            change = sum(abs(x-y) for x,y in zip(a,b)) / 765
            luma = (.2126*a[0] + .7152*a[1] + .0722*a[2]) / 255
            if weights[i] >= 230:
                groups['protect_change'].append(change)
            if weights[i] <= 25:
                groups['world_change'].append(change)
                groups['world_cool_delta'].append(((b[2]-b[0])-(a[2]-a[0]))/255)
            if luma >= .75 and max(a)-min(a) <= 25:
                groups['white_change'].append(change)
            if luma <= .15:
                groups['black_lift'].append((.2126*b[0]+.7152*b[1]+.0722*b[2])/255-luma)
        row = {name: sum(values)/len(values) if values else None for name,values in groups.items()}
        row.update(protected_count=len(groups['protect_change']), world_count=len(groups['world_change']),
                   white_count=len(groups['white_change']), black_count=len(groups['black_lift']))
        rows.append(row)
    return rows


def evaluate_frames(rows):
    if not rows:
        raise ValueError('缺少逐帧测量')
    failures = []
    for i,r in enumerate(rows):
        reasons = []
        if min(r[k] for k in ('protected_count','white_count','black_count','world_count')) < 16:
            reasons.append('参考区域不足')
        else:
            if r['world_change'] < .004 or r['world_cool_delta'] < .002:
                reasons.append('环境创意方向未发生')
            if r['protect_change'] > .03 or r['protect_change'] > r['world_change']*.7:
                reasons.append('保护区变化过大')
            if r['white_change'] > .025:
                reasons.append('白位偏移过大')
            if (r['black_lift'] > .01 or
                    r['black_lift'] < BLACK_LOWER_LIMIT - BLACK_MEASUREMENT_TOLERANCE):
                reasons.append('黑位变化越界')
        if reasons:
            failures.append({'frame': i, 'reasons': reasons})
    black_values = [r['black_lift'] for r in rows if r.get('black_lift') is not None]
    below_nominal = sum(value < BLACK_LOWER_LIMIT for value in black_values)
    allowed_below_nominal = max(1, int(len(rows) * BLACK_NOMINAL_EXCEED_FRACTION))
    if below_nominal > allowed_below_nominal:
        failures.append({'frame': 'sequence', 'reasons': ['序列黑位持续越界']})
    return {'status': 'blocked' if failures else 'passed', 'frames': len(rows),
            'failures': failures,
            'black_gate': {
                'nominal_lower_limit': BLACK_LOWER_LIMIT,
                'measurement_tolerance': BLACK_MEASUREMENT_TOLERANCE,
                'below_nominal_frames': below_nominal,
                'allowed_below_nominal_frames': allowed_below_nominal,
            },
            '边界': '160×90全时间轴颜色/亮度代理；不是肤色语义或审美分数'}


def render(source, output, workdir):
    if media._sha256(source) != SOURCE_HASH:
        raise ValueError('青橙只接受绑定Rec.709原片，不接受Foundation替代原片')
    bound_assets()
    workdir.mkdir(parents=True)
    creative = workdir / '独立创意层.mov'
    graph = json.loads(GRAPH.read_text())['graph']
    codec = ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-pix_fmt', 'yuv420p',
             '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    first = ['ffmpeg','-v','error','-n','-i',str(source),'-filter_complex',graph,
             '-map','[blgraded]','-an'] + codec + [str(creative)]
    subprocess.run(first, check=True, capture_output=True)
    base, mask = ASSETS/'foundation-only.mov', ASSETS/'memory-mask-v2.mkv'
    probes = [media._video_stream(media._probe(p)) for p in (base, creative, mask)]
    if any(media._frame_count(p) != 248 or Fraction(p['avg_frame_rate']) != 24 for p in probes):
        raise ValueError('三路时序合同不一致')
    graph2 = protection_graph(2048, 1152, '24')
    second = ['ffmpeg','-v','error','-n','-i',str(base),'-i',str(creative),'-i',str(mask),
              '-i',str(source),'-filter_complex',graph2,'-map','[out]','-map','3:a:0'] + codec + ['-c:a','copy',str(output)]
    subprocess.run(second, check=True, capture_output=True)
    rows = measure_frames(base, output, mask)
    gates = evaluate_frames(rows)
    (workdir/'逐帧门.json').write_text(json.dumps({'gates':gates,'frames':rows},ensure_ascii=False,indent=2))
    if gates['status'] != 'passed':
        raise ValueError('青橙最终保护/方向门未通过；保留诊断，不交付')
    return {'commands':[first,second], 'probe':media._probe(output), 'gates':gates,
            'foundation':str(base), 'mask':str(mask), 'audio_policy':'复制原五声道PCM'}
