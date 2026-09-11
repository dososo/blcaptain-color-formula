"""蓝调时刻单源55%适配；复用绑定的预计算代理，不宣称本轮重新语义识别。"""
import json
from pathlib import Path
from fractions import Fraction
try:
    from . import blue_hour_grade as blue
    from . import korean_cool_grade as media
except ImportError:
    import blue_hour_grade as blue
    import korean_cool_grade as media

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT.parents[1]/'outputs/blcaptain-foundation-phase2g/run-20260901'
SOURCE_HASH='ed967628b476b4b87dbd7294a469031a98d78ffacbfffcbd5e77af3ea05483e0'
HASHES={
    'top-mask.mkv':'71d403baba2914f389bb9377909010f1a3dde9b684d7f048b1855154fabe2368',
    'middle-mask.mkv':'6762a0342365549f85dd244eaddc353ea27f8246586b0ca3eb288ac11f86ee1c',
    'horizon-mask.mkv':'216d23efffb6ca8f70dccc9cd3345f213b9551407cb1283ad869adb89b6cae4c',
    'anchor-mask.mkv':'3a53899494d1d7057c779764a4772b5ac629573ded97d4eb6252b17a0711382b',
    'memory-mask-empty.mkv':'60fcd767d3d669dae00a7fd93e210afe04f9a5ad9ee87cfe29e190a0e09fdde3',
}


def bound_assets():
    result={}
    for name,h in HASHES.items():
        path=ASSETS/name
        if not path.is_file() or media._sha256(path)!=h:
            raise ValueError('蓝调同源代理缺失或变化：'+name)
        result[str(path)]=h
    return result


def render(source,candidate,workdir):
    try:
        from . import blue_hour_final_check as check
    except ImportError:
        import blue_hour_final_check as check
    if media._sha256(source)!=SOURCE_HASH:
        raise ValueError('蓝调只接受绑定Foundation')
    bound_assets()
    masks={n:ASSETS/(n+'-mask.mkv') for n in ('top','middle','horizon','anchor')}
    memory=ASSETS/'memory-mask-empty.mkv'
    for p in [source,*masks.values(),memory]:
        video=media._video_stream(media._probe(p))
        if media._frame_count(video)!=360 or abs(float(Fraction(video['avg_frame_rate']))-30000/1001)>.001:
            raise ValueError('蓝调九路输入时序合同不符')
    workdir.mkdir(parents=True)
    layers={}
    for name,chain in blue.vertical_gradient_filters(.55).items():
        layers[name]=workdir/(name+'-layer.mp4')
        blue.render_gradient_layer(source,layers[name],chain)
    result=blue.render_layered_video(source,layers,masks,memory,source,candidate)
    rows=check.measure(source,candidate,masks)
    gates=check.evaluate(rows)
    (workdir/'final-gates.json').write_text(json.dumps({'rows':rows,'gates':gates},ensure_ascii=False,indent=2))
    if gates['status']!='passed':
        raise ValueError('蓝调最终梯度/暖灯保护门未通过，不交付')
    result.update(gates=gates,probe=media._probe(candidate),
        mask_boundary='绑定同源预计算三段及暖灯代理；空人物mask不是人物保护证明')
    return result
