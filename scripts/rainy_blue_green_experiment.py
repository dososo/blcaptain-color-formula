"""雨夜蓝绿：绑定单源、固定代理和55% v7重放。"""
import json
import subprocess
from fractions import Fraction
from pathlib import Path
try:
    from . import rainy_surface_grade as rainy
    from . import rainy_final_check as check
    from . import korean_cool_grade as media
except ImportError:
    import rainy_surface_grade as rainy
    import rainy_final_check as check
    import korean_cool_grade as media

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT.parents[1]/'outputs/blcaptain-foundation-phase2e/run-20260831'
SOURCE_HASH='48d9c2459c527ccc28136eeb1e8c6f76543f8bc513c03c571c87932611abdfd3'
HASHES={'wet-mask-v3.mkv':'808a27b6f1b158e15152b9349a7d54c86f2e6db85e83d6199a8b95384683069b','warm-mask-v3.mkv':'8d442d883011a82a78782f7fe4c179d6a57e5a3dfa6516fcbb426661137aebef','memory-mask.mkv':'22da8aa29f6b236d11299aff34bb065036b424a0b9faebdf4f13d8ec6b9365b9'}

def bound_assets():
    result={}
    for name,digest in HASHES.items():
        path=ASSETS/name
        if not path.is_file() or media._sha256(path)!=digest: raise ValueError('雨夜代理缺失或变化：'+name)
        result[str(path)]=digest
    return result

def prepared_filter():
    try:
        from . import blcaptain_color as engine
    except ImportError:
        import blcaptain_color as engine
    catalog=json.loads((ROOT/'references/recipes.json').read_text())
    recipe=next(x for x in catalog['recipes'] if x['id']=='rainy-blue-green')
    return engine.build_filter(recipe['parameters'],recipe['tone_curve'],recipe['hsl_bands'],render_mix=.55)

def render(source,candidate,workdir):
    if media._sha256(source)!=SOURCE_HASH: raise ValueError('雨夜只接受绑定Foundation v2')
    bound_assets();probe=media._probe(source);video=media._video_stream(probe)
    if media._frame_count(video)!=266 or abs(float(Fraction(video['avg_frame_rate']))-25)>.001: raise ValueError('雨夜源时序合同不符')
    workdir.mkdir(parents=True)
    base=workdir/'准备层55.mp4';wet=workdir/'湿路层55.mp4'
    command=['ffmpeg','-v','error','-n','-i',str(source),'-vf',prepared_filter(),'-map','0:v:0','-an','-c:v','libx264','-crf','18','-preset','medium','-pix_fmt','yuv420p','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',str(base)]
    subprocess.run(command,check=True)
    rainy.render_wet_layer(base,wet,.55)
    masks={'wet':ASSETS/'wet-mask-v3.mkv','warm':ASSETS/'warm-mask-v3.mkv','memory':ASSETS/'memory-mask.mkv'}
    result=rainy.render_layered_video(base,wet,masks['wet'],masks['warm'],masks['memory'],source,candidate)
    rows=check.measure(base,candidate,masks);gates=check.evaluate(rows)
    (workdir/'final-gates.json').write_text(json.dumps({'rows':rows,'gates':gates},ensure_ascii=False,indent=2))
    if gates['status']!='passed': raise ValueError('雨夜最终湿路集中度或保护门未通过，不交付')
    result.update(gates=gates,prepared_filter=prepared_filter(),probe=media._probe(candidate),mask_boundary='绑定同源湿路反光、暖灯和记忆色代理；不是干湿语义分割')
    return result
