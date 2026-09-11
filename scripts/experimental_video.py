"""显式实验视频入口：只开放已有证据覆盖的单源55%，不晋级目录。"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from . import film_soft_grade as film
except ImportError:
    import film_soft_grade as film
try:
    from . import teal_orange_grade as teal_grade
except ImportError:
    import teal_orange_grade as teal_grade
try:
    from . import blue_hour_experiment as blue_grade
except ImportError:
    import blue_hour_experiment as blue_grade
try:
    from . import rainy_blue_green_experiment as rainy_grade
except ImportError:
    import rainy_blue_green_experiment as rainy_grade

ROOT = Path(__file__).resolve().parents[1]
SOURCE_HASH = '9d0e8e28f2d881858561fcb27028611ff638bd118578fac7d9d023e29193b8ef'
APPROVED_FILM_HASH = '5d8408d5f7cdf9f90eb9e51c0ff5b5256b7d54020c0a39b3cda1c758fff27715'
WARM_SOURCE_HASH = 'f466bd053a9ca99f308e4795846f30555a3986186c52a24b5c687f38d2a0ded2'
APPROVED_WARM_HASH = '22c8c1aef1937116d5392b33635f9d0190aa87e3c081221db89a4bc4879b9a27'
FOOD_SOURCE_HASH = '8b49b3f776eeea9b5f94d8601a75e21807f8137f9cf53eb89349c048161ead32'
APPROVED_FOOD_HASH = 'eacdf955ac0b07c870d781783bf63173d896270bb076c4696ee14ebedee07bb0'
CREAM_SOURCE_HASH = '55f05d438b5b8312e7eb6da562296c0fd66b9015129811f1044fce30d60ae7af'
APPROVED_CREAM_HASH = 'e206c17b89e7158c2844d598ed5ac4dbb585a36f3e22308d7bcbe4b8db00d8ab'
SUNSET_SOURCE_HASH = 'e60ab9c5c70678c35261ef8efd540cc7b1e5d6bcae1389e4e5f339e775e1ab61'
APPROVED_SUNSET_HASH = '19aa01e62547af0472fdb88769e19835d3b5efc8e8766ad5aa1b587add3054c8'
LANDSCAPE_SOURCE_HASH = 'c22126b765973a1a9006c714999a6ff0ed73aa7d61d2a16e82c8020d5a688b9f'
APPROVED_LANDSCAPE_HASH = '8f0f2f102d1e525cfa0688251e45d165082dea6f63ffb7a19deb76b362be23e6'
KOREAN_SOURCE_HASH = '0b804333d2ad409a1ba2fc32a0e07dcccacd7cb12c7eb3e75d7d07985f7fd623'
KOREAN_MASK_HASH = 'c8d42fb83021547dc36dc478393c19ec238fda7870a71711f36b06972cb0c8f0'
KOREAN_MASK = ROOT.parents[1] / 'outputs/blcaptain-foundation-phase2d2/arri-rec709/memory-mask-v2.mkv'
APPROVED_KOREAN_HASH = '9a5bd75765f0ece16cdc35466bb4adaad35607822bb23a02acc3fc72ff7af746'
SEA_SOURCE_HASH = '183b0a0d4cc87047d0d8b718dce29fcf0badf92d1a51244861f8392629b5036a'
FOREST_SOURCE_HASH = 'ce86a71c6253ea43398377b790bf5b20bbf5444ddb74dc81925eb7219eaf74d0'
DEEP_SEA_SOURCE_HASH = '2f8adf9deeebbe776765314e78e141b6e1c5d0b189530bd18edcd397dac42fbb'
DEEP_SEA_ASSET_ROOT = ROOT.parents[1] / 'outputs/blcaptain-foundation-phase3i3'


def deep_sea_assets():
    paths = {
        'foundation': DEEP_SEA_ASSET_ROOT / 'source/mixkit-2401-720.mp4',
        'environment_mask': DEEP_SEA_ASSET_ROOT / 'neutral-night/intimate-v7-final/environment.mkv',
        'subject_mask': DEEP_SEA_ASSET_ROOT / 'neutral-night/intimate-v6/subject.mkv',
    }
    return {name: {'path': str(path), 'sha256': sha(path)} for name, path in paths.items()}


def sha(path):
    return film._sha256(Path(path))


def artifact_locations(stage, output):
    """保留原命令路径，另提供发布后工件的可达位置。"""
    return {str(path): str(output / path.relative_to(stage))
            for path in stage.rglob('*') if path.is_file()}


def comparison_command(source, candidate, output):
    return [shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-n',
            '-i', str(source), '-i', str(candidate), '-filter_complex',
            '[0:v]scale=960:-2,setsar=1[a];[1:v]scale=960:-2,setsar=1[b];'
            '[a][b]vstack,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709',
            '-t', '8', '-an', '-c:v', 'libx264', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
            str(output)]


def make_plan(source, style, strength, output):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().resolve()
    if style not in ('film-soft', 'warm-cozy', 'food-vivid', 'cream-soft', 'sunset-warm', 'landscape-crisp', 'korean-cool', 'teal-orange', 'cinematic-muted', 'blue-hour', 'rainy-blue-green', 'cool-gray-sea', 'forest-cyan', 'captain-deep-sea') or float(strength) != 55:
        raise ValueError('本批仅支持实验CLI列出的已绑定单源55%合同；能建计划不等于最终门已通过')
    warm = style == 'warm-cozy'
    food = style == 'food-vivid'
    cream = style == 'cream-soft'
    sunset = style == 'sunset-warm'
    landscape = style == 'landscape-crisp'
    korean = style == 'korean-cool'
    teal = style == 'teal-orange'
    muted = style == 'cinematic-muted'
    blue = style == 'blue-hour'
    rainy = style == 'rainy-blue-green'
    sea = style == 'cool-gray-sea'
    forest = style == 'forest-cyan'
    deep_sea = style == 'captain-deep-sea'
    source_hash = (DEEP_SEA_SOURCE_HASH if deep_sea else FOREST_SOURCE_HASH if forest else SEA_SOURCE_HASH if sea else rainy_grade.SOURCE_HASH if rainy else blue_grade.SOURCE_HASH if blue else teal_grade.SOURCE_HASH if teal else KOREAN_SOURCE_HASH if korean or muted else LANDSCAPE_SOURCE_HASH if landscape else SUNSET_SOURCE_HASH if sunset else CREAM_SOURCE_HASH if cream else FOOD_SOURCE_HASH if food else
                   WARM_SOURCE_HASH if warm else SOURCE_HASH)
    if not source.is_file() or sha(source) != source_hash:
        raise ValueError('输入不在该风格已验证素材范围；普通原片与Foundation不能互换')
    if output.exists() or output == source or output.is_relative_to(ROOT):
        raise ValueError('实验输出必须是仓库外的新目录，不覆盖已有结果或原片')
    if sha(ROOT / 'scripts/film_soft_grade.py') != APPROVED_FILM_HASH:
        raise ValueError('胶片实现已变化，必须重新验证后再开放实验')
    modules = ['scripts/experimental_video.py', 'scripts/film_soft_grade.py', 'scripts/blcaptain_color.py']
    if rainy:
        auxiliary=rainy_grade.bound_assets()
        modules += ['scripts/rainy_blue_green_experiment.py','scripts/rainy_surface_grade.py','scripts/rainy_final_check.py','scripts/video_memory_protection.py','scripts/korean_cool_grade.py','references/recipes.json']
    if sea:
        modules += ['scripts/cool_gray_sea_experiment.py', 'scripts/cool_gray_sea_grade.py',
                    'scripts/cool_gray_sea_structure_v2.py', 'scripts/cool_gray_sea_final_check_v2.py',
                    'scripts/korean_cool_grade.py']
    if forest:
        modules += ['scripts/forest_cyan_experiment.py', 'scripts/forest_cyan_grade_v3.py',
                    'scripts/forest_cyan_final_check.py', 'scripts/korean_cool_grade.py']
    if deep_sea:
        auxiliary = deep_sea_assets()
        modules += ['scripts/deep_sea_transfer_experiment.py',
                    'scripts/deep_sea_transfer_final_check.py', 'scripts/korean_cool_grade.py']
    if blue:
        auxiliary=blue_grade.bound_assets()
        modules += ['scripts/blue_hour_experiment.py','scripts/blue_hour_grade.py','scripts/blue_hour_final_check.py',
                    'scripts/video_memory_protection.py','scripts/korean_cool_grade.py']
    if teal:
        auxiliary = teal_grade.bound_assets()
        modules += ['scripts/teal_orange_grade.py', 'scripts/korean_cool_grade.py', 'references/teal-frozen-55.json']
    if muted:
        auxiliary = teal_grade.bound_assets()
        modules += ['scripts/cinematic_muted_grade.py','scripts/teal_orange_grade.py','scripts/korean_cool_grade.py']
    if korean:
        if sha(ROOT / 'scripts/korean_cool_grade.py') != APPROVED_KOREAN_HASH:
            raise ValueError('韩系实现变化，须重新验证')
        if not KOREAN_MASK.is_file() or sha(KOREAN_MASK) != KOREAN_MASK_HASH:
            raise ValueError('韩系固定保护代理缺失或变化')
        modules += ['scripts/korean_cool_grade.py']
    if warm:
        if sha(ROOT / 'scripts/warm_cozy_grade.py') != APPROVED_WARM_HASH:
            raise ValueError('暖调实现已变化，必须重新验证后再开放实验')
        modules += ['scripts/warm_cozy_grade.py', 'scripts/sunset_warm_grade.py',
                    'scripts/video_memory_protection.py', 'scripts/visual_density_gate.py']
    if food:
        if sha(ROOT / 'scripts/food_vivid_grade.py') != APPROVED_FOOD_HASH:
            raise ValueError('美食实现已变化，必须重新验证后再开放实验')
        modules += ['scripts/food_vivid_grade.py', 'scripts/video_memory_protection.py']
    if cream:
        if sha(ROOT / 'scripts/cream_soft_grade.py') != APPROVED_CREAM_HASH:
            raise ValueError('奶油实现已变化，必须重新验证后再开放实验')
        modules += ['scripts/cream_soft_grade.py', 'scripts/video_memory_protection.py',
                    'scripts/cream_shadow_check.py']
    if sunset:
        if sha(ROOT / 'scripts/sunset_warm_grade.py') != APPROVED_SUNSET_HASH:
            raise ValueError('日落实现已变化，必须重新验证后再开放实验')
        modules += ['scripts/sunset_warm_grade.py', 'scripts/video_memory_protection.py',
                    'scripts/visual_density_gate.py']
    if landscape:
        if sha(ROOT / 'scripts/landscape_crisp_grade.py') != APPROVED_LANDSCAPE_HASH:
            raise ValueError('风景实现已变化，必须重新验证后再开放实验')
        modules += ['scripts/landscape_crisp_grade.py', 'scripts/video_memory_protection.py']
    plan = {
        'schema': 'experimental-video-1', 'style': style, 'execution_tier': 'experimental',
        'auto_recommendable': False, 'production_status_changed': False,
        'source': str(source), 'source_sha256': source_hash, 'output_root': str(output),
        'strength_percent': 55, 'input_stage': 'source' if warm or sunset or landscape or sea or forest or deep_sea else 'foundation',
        'foundation_policy': ('显示参照原片作为零调整Foundation；Creative Look只按明暗结构进入环境层，人物与象牙暖锚回护原片' if deep_sea else
                              'v3实现内部只执行一次Foundation；随后仅在有色中低明度绿代理上施加Creative Look' if forest else
                              '绑定显示参照原片无需额外一级校正；Creative Look只执行一次' if sea else
                              '保留真实雾与黑位，Foundation为零调整；单独编码母版后执行五区代理分层' if landscape else
                              '保留日落剪影原有密度，Foundation为零调整，不按全画面中位数提亮' if sunset else
                              '原模块内部执行一次Foundation并重建暖／冷／保护三路代理蒙版' if warm else '保留已完成的Foundation，不重复执行'),
        'strength_policy': '直接传入0.55，不再感知映射或二次混合',
        'filter_complex': None if warm or food or cream or sunset or landscape or korean or sea else film.build_filter_complex(.55),
        'implementation': {name: sha(ROOT / name) for name in modules},
        'boundary': '仅单源55%实验；目标色卡、通用方向门和肤色语义门未运行；审美待人工',
    }
    if cream:
        plan['shadow_policy'] = '输入参考不足预筛预算8%；最终全帧参考必须充足，逐通道及深黑子集编码误差检查不可省略'
        plan['geometry'] = {'center': [0.40, 0.52], 'radius': [0.32, 0.46],
                            'tracking': False, 'scope': '仅历史已确认固定构图的静态椭圆'}
    if korean:
        plan.update(mask=str(KOREAN_MASK), mask_sha256=KOREAN_MASK_HASH,
                    required_gates=['korean-spatial-signature'],
                    audio_policy='MOV保留五声道PCM，不降混；同源代理不是通用语义跟踪')
    if teal:
        plan.update(input_stage='source', auxiliary=auxiliary, filter_complex=None,
                    foundation_policy='绑定历史基础母版；创意分支从原片独立重建且仅含一次Foundation',
                    strength_policy='冻结历史55%有效混合0.706985，不二次映射',
                    required_gates=['teal-frame-protection'],
                    audio_policy='MOV保留原五声道PCM；同源预计算保护代理，不是本轮语义识别')
    if muted:
        plan.update(auxiliary=auxiliary,filter_complex=None,
                    required_gates=['muted-final-signature'],
                    audio_policy='MOV保留原五声道PCM；绑定同源val*1.5保护代理，不作全局洗灰')
    if blue:
        plan.update(auxiliary=auxiliary,filter_complex=None,
                    required_gates=['blue-hour-final-gradient'],
                    audio_policy='该源没有音轨，不添加或伪造声音',
                    mask_boundary='同源预计算三段及暖灯代理，不是本轮语义识别；空人物mask不证明人物保护')
    if rainy:
        plan.update(auxiliary=auxiliary,filter_complex=None,prepared_filter=rainy_grade.prepared_filter(),required_gates=['rainy-final-material-concentration'],audio_policy='保留Foundation内原双声道AAC；不增加声音',mask_boundary='同源预计算湿路、暖灯和记忆色代理；不是干湿语义识别')
    if sea:
        plan.update(foundation_filter='null', required_gates=['cool-gray-sea-final-structure'],
                    audio_policy='绑定原片无音轨，不添加或伪造声音',
                    mask_boundary='固定纵向海面带，不是海天岸语义分割；仅固定机位单素材')
    if forest:
        plan.update(filter_complex=None, required_gates=['forest-cyan-final-separation'],
                    audio_policy='绑定原片无音轨，不添加或伪造声音',
                    mask_boundary='显示RGB色相、饱和度与亮度代理，不是树叶、林地或景深语义分割；仅单素材')
    if deep_sea:
        plan.update(filter_complex=None, auxiliary=auxiliary,
                    required_gates=['deep-sea-transfer-final'],
                    audio_policy='绑定原片无音轨，不添加或伪造声音',
                    mask_boundary='同源预计算明暗/运动代理，不识别人、肤色、建筑或真实灯光；仅单素材')
    if sunset:
        plan['foundation_filter'] = 'null'
        plan['density_first'] = True
    if landscape:
        plan['foundation_filter'] = 'null'
        plan['required_gates'] = ['region-density-v1', 'landscape-light-path-v1']
        plan['mask_boundary'] = '逐帧RGB互斥代理，不是天空、水面或树林语义识别；不裁切'
    plan['plan_id'] = hashlib.sha256(json.dumps(plan, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:16]
    return plan


def render_food(source, candidate, workdir):
    """复用历史Foundation之后的完整美食分层链，不重复一级校正。"""
    import food_vivid_grade as food
    workdir.mkdir(parents=True)
    masks = {name: workdir / (name + '.mp4') for name in ('food', 'environment', 'protect')}
    report = food.build_dynamic_food_masks(source, masks, workdir / 'analysis')
    if not food.capabilities_from_report(report):
        raise ValueError('美食颜色分区证据不足，不回退全局加彩')
    filters = food.selective_color_filters(.55)
    layers = {}
    for name in ('food', 'environment'):
        layers[name] = workdir / (name + '-layer.mp4')
        food.render_color_layer(source, layers[name], filters[name])
    rendered = food.render_layered_video(source, layers['food'], layers['environment'],
                                        masks, candidate, food.probe(source))
    rendered['mask_report'] = report
    rendered['probe'] = film._probe(candidate)
    return rendered


def render_cream(source, candidate, workdir):
    """保持v2d-r2层序：注意力塑光在前两暖层合成之后执行。"""
    import cream_soft_grade as cream
    workdir.mkdir(parents=True)
    masks = {name: workdir / (name + '.mkv') for name in
             ('shoulder', 'warm_midtone', 'neutral_midtone',
              'attention_midtone', 'attention_holdout', 'protect')}
    report = cream.build_dynamic_masks(source, masks, workdir / 'analysis')
    cream.save_report(workdir / 'mask-report.json', report)
    if not cream.capabilities_from_report(report):
        raise ValueError('奶油局部分区证据不足，不回退全局抬白')
    shoulder, midtone = workdir / 'shoulder-layer.mp4', workdir / 'midtone-layer.mp4'
    cream.render_warm_layer(source, shoulder, .55)
    cream.render_color_layer(source, midtone, cream.warm_midtone_filters(.55), '暖中间调')
    rendered = cream.render_layered_video(source, shoulder, midtone, masks, candidate,
                                         cream.probe(source), .55)
    rendered['mask_report'] = report
    rendered['probe'] = film._probe(candidate)
    return rendered


def render_sunset(source, candidate, workdir):
    """剪影原片保持密度，仅增强已有暖光带，再恢复冷锚与保护区。"""
    import sunset_warm_grade as sunset
    workdir.mkdir(parents=True)
    masks = {name: workdir / (name + '.mkv') for name in ('warm', 'cool', 'protect')}
    report = sunset.build_dynamic_sunset_masks(source, masks, workdir / 'analysis')
    sunset.save_report(workdir / 'mask-report.json', report)
    if not sunset.capabilities_from_report(report):
        raise ValueError('日落暖区／冷锚证据不足，不回退全局染黄')
    layer = workdir / 'warm-layer.mp4'
    sunset.render_warm_layer(source, layer, .55, density_first=True)
    rendered = sunset.render_layered_video(source, layer, masks, candidate, sunset.probe(source))
    original_frames = sorted((workdir / 'analysis' / 'frames').glob('*.png'))
    output_frames, _ = sunset.video_masks.extract_analysis_frames(candidate, workdir / 'output-analysis', 960)
    fixed_masks = [sorted((workdir / 'analysis' / 'masks' / name).glob('*.pgm'))
                   for name in ('warm', 'cool', 'protect')]
    baseline = sunset.masked_color_metrics(original_frames, *fixed_masks)
    measured = sunset.masked_color_metrics(output_frames, *fixed_masks)
    gate = sunset.evaluate_visual_density(baseline, measured, 55)
    density = {'baseline': baseline, 'candidate': measured, 'gate': gate}
    sunset.save_report(workdir / 'density-report.json', density)
    if gate['status'] != 'passed':
        raise ValueError('日落密度／光比方向门未通过，不发布成片')
    rendered.update(mask_report=report, density_report=density, probe=film._probe(candidate))
    return rendered


def render_landscape(source, candidate, workdir):
    """雾山湖单源：零调整母版、五区代理、最终同源密度与塑光双门。"""
    import landscape_crisp_grade as landscape
    workdir.mkdir(parents=True)
    base = workdir / '基础校正.mp4'
    foundation = landscape.render_foundation(source, base)
    print('风景清透：基础母版完成，正在重建全帧五区代理。', file=sys.stderr, flush=True)
    masks = {name: workdir / (name + '.mkv') for name in landscape.MASK_NAMES}
    report = landscape.build_dynamic_masks(base, masks, workdir / 'analysis')
    landscape.save_report(workdir / 'mask-report.json', report)
    if not landscape.capabilities_from_report(report):
        raise ValueError('风景五区代理证据不足，不回退全局加蓝加绿')
    layers = {}
    for name in ('detail', 'green', 'cool', 'earth'):
        layers[name] = workdir / (name + '-layer.mp4')
        landscape.render_layer(base, layers[name], getattr(landscape, name + '_filter')(.55))
    print('风景清透：四层完成，正在合成并检查最终媒体。', file=sys.stderr, flush=True)
    rendered = landscape.render_layered_video(base, layers, masks, candidate, landscape.probe(base))
    original_frames = sorted((workdir / 'analysis' / 'frames').glob('*.png'))
    output_frames, _ = landscape.video_masks.extract_analysis_frames(candidate, workdir / 'output-analysis', 960)
    fixed_masks = {name: sorted((workdir / 'analysis' / 'masks' / name).glob('*.pgm'))
                   for name in landscape.MASK_NAMES}
    baseline = landscape.masked_metrics(original_frames, fixed_masks)
    measured = landscape.masked_metrics(output_frames, fixed_masks, reference_frames=original_frames)
    density = landscape.evaluate_landscape_density(baseline, measured, 55)
    light = landscape.evaluate_landscape_light_path(landscape.light_path_from_metrics(baseline),
                                                  landscape.light_path_from_metrics(measured), 55)
    gates = {'baseline': baseline, 'candidate': measured, 'density': density, 'light_path': light}
    landscape.save_report(workdir / 'final-gates.json', gates)
    if density['status'] != 'passed' or light['status'] != 'passed':
        raise ValueError('风景最终密度／塑光双门未通过，不发布成片')
    rendered.update(foundation=foundation, mask_report=report, final_gates=gates,
                    probe=film._probe(candidate))
    return rendered


def render_korean(source, candidate, workdir, mask):
    try:
        from . import korean_cool_grade as korean
    except ImportError:
        import korean_cool_grade as korean
    workdir.mkdir(parents=True)
    rendered = korean.render(source, mask, candidate, .55)
    measured = korean.measure_signature(source, candidate, mask)
    measured['gate'] = korean.evaluate_signature(measured.get('baseline', {}), measured.get('candidate', {}), 55)
    (workdir / 'final-gates.json').write_text(json.dumps(measured, ensure_ascii=False, indent=2))
    if measured['gate']['status'] != 'passed':
        raise ValueError('韩系最终空间签名或保护门未通过')
    rendered.update(final_gates=measured, probe=korean._probe(candidate))
    return rendered


def render_muted(source, candidate, workdir):
    try:
        from . import cinematic_muted_grade as muted
    except ImportError:
        import cinematic_muted_grade as muted
    workdir.mkdir(parents=True)
    mask = teal_grade.ASSETS/'memory-mask-v2.mkv'
    rendered = muted.render(source, mask, candidate, .55)
    measurements = muted.measure_signature(source,candidate,mask)
    gates = muted.evaluate_signature(measurements['baseline'],measurements['candidate'],55)
    (workdir/'final-gates.json').write_text(json.dumps({'measurements':measurements,'gates':gates},ensure_ascii=False,indent=2))
    if gates['status'] != 'passed':
        raise ValueError('电影低饱和最终签名门未通过，不交付')
    rendered.update(gates=gates,measurements=measurements,probe=teal_grade.media._probe(candidate))
    return rendered


def render_sea(source, candidate, workdir):
    try:
        from . import cool_gray_sea_experiment as sea_grade
    except ImportError:
        import cool_gray_sea_experiment as sea_grade
    return sea_grade.render(source, candidate, workdir)


def render_forest(source, candidate, workdir):
    try:
        from . import forest_cyan_experiment as forest_grade
    except ImportError:
        import forest_cyan_experiment as forest_grade
    return forest_grade.render(source, candidate, workdir)


def render_deep_sea(source, candidate, workdir, assets):
    try:
        from . import deep_sea_transfer_experiment as deep_grade
    except ImportError:
        import deep_sea_transfer_experiment as deep_grade
    return deep_grade.render(source, candidate, workdir, assets)


def execute(plan, confirmation):
    if not confirmation or plan.get('plan_id') != confirmation:
        raise ValueError('实验方案未确认或编号不匹配')
    fresh = make_plan(plan['source'], plan['style'], plan['strength_percent'], plan['output_root'])
    if fresh != plan:
        raise ValueError('实验计划、素材或实现变化，必须重新确认')
    source, output = Path(plan['source']), Path(plan['output_root'])
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.film-experiment-', dir=output.parent))
    try:
        warm = plan['style'] == 'warm-cozy'
        food = plan['style'] == 'food-vivid'
        cream = plan['style'] == 'cream-soft'
        sunset = plan['style'] == 'sunset-warm'
        landscape = plan['style'] == 'landscape-crisp'
        korean = plan['style'] == 'korean-cool'
        teal = plan['style'] == 'teal-orange'
        muted = plan['style'] == 'cinematic-muted'
        blue = plan['style'] == 'blue-hour'
        rainy = plan['style'] == 'rainy-blue-green'
        sea = plan['style'] == 'cool-gray-sea'
        forest = plan['style'] == 'forest-cyan'
        deep_sea = plan['style'] == 'captain-deep-sea'
        title = '深海航线' if deep_sea else '森林青绿' if forest else '冷灰海水' if sea else '雨夜蓝绿' if rainy else '蓝调时刻' if blue else '电影低饱和' if muted else '青橙电影' if teal else '韩系清冷' if korean else '风景清透' if landscape else '日落暖金' if sunset else '奶油柔光' if cream else '美食鲜亮' if food else '暖调治愈' if warm else '柔和胶片'
        candidate = stage / (title + ('55.mov' if korean or teal or muted else '55.mp4'))
        comparison_name = ('对比_上原片_下深海航线.mp4' if deep_sea else '对比_上原片_下森林青绿.mp4' if forest else '对比_上原片_下冷灰海水.mp4' if sea else '对比_上基础校正_下雨夜蓝绿.mp4' if rainy else '对比_上基础校正_下蓝调.mp4' if blue else '对比_上基础校正_下电影低饱和.mp4' if muted else '对比_上原片_下青橙.mp4' if teal else '对比_上基础校正_下韩系.mp4' if korean else '对比_上原片_下风景.mp4' if landscape else '对比_上原片_下日落.mp4' if sunset else '对比_上基础校正_下奶油.mp4' if cream else
                           '对比_上基础校正_下美食.mp4' if food else
                           '对比_上原片_下暖调.mp4' if warm else '对比_上基础校正_下胶片.mp4')
        if deep_sea:
            rendered = render_deep_sea(source, candidate, stage / '诊断中间层', plan['auxiliary'])
        elif forest:
            rendered = render_forest(source, candidate, stage / '诊断中间层')
        elif sea:
            rendered = render_sea(source, candidate, stage / '诊断中间层')
        elif rainy:
            rendered=rainy_grade.render(source,candidate,stage/'诊断中间层')
        elif blue:
            rendered=blue_grade.render(source,candidate,stage/'诊断中间层')
        elif muted:
            rendered = render_muted(source,candidate,stage/'诊断中间层')
        elif teal:
            rendered = teal_grade.render(source, candidate, stage / '诊断中间层')
        elif korean:
            rendered = render_korean(source, candidate, stage / '诊断中间层', Path(plan['mask']))
        elif landscape:
            rendered = render_landscape(source, candidate, stage / '诊断中间层')
        elif sunset:
            rendered = render_sunset(source, candidate, stage / '诊断中间层')
        elif cream:
            rendered = render_cream(source, candidate, stage / '诊断中间层')
        elif food:
            rendered = render_food(source, candidate, stage / '诊断中间层')
        elif warm:
            import warm_cozy_grade
            rendered = warm_cozy_grade.render(source, candidate, stage / '诊断中间层', .55)
            rendered['probe'] = film._probe(candidate)
        else:
            rendered = film.render(source, candidate, .55)
        streams = rendered['probe']['streams']
        video = next(s for s in streams if s['codec_type'] == 'video')
        if (int(video.get('nb_frames', 0)) != (288 if deep_sea else 231 if forest else 294 if sea else 266 if rainy else 360 if blue else 248 if korean or teal or muted else 500 if landscape else 150 if sunset else 292 if cream else 662 if food else 245 if warm else 253)
                or any(s['codec_type'] == 'audio' for s in streams) != (not (deep_sea or forest or sea or warm or cream or sunset or blue))):
            raise ValueError('实验成片帧数或音轨不符合已验证素材合同')
        if any(video.get(key) != 'bt709' for key in ('color_space', 'color_transfer', 'color_primaries')):
            raise ValueError('实验成片色彩标签错误')
        if blue and blue_grade.bound_assets()!=plan['auxiliary']:
            raise ValueError('执行期间蓝调代理变化')
        if rainy:
            if rainy_grade.bound_assets()!=plan['auxiliary'] or rainy_grade.prepared_filter()!=plan['prepared_filter']:
                raise ValueError('执行期间雨夜代理或准备层变化')
            audio=next(s for s in streams if s['codec_type']=='audio')
            if audio.get('codec_name')!='aac' or audio.get('channels')!=2: raise ValueError('雨夜双声道AAC合同未保留')
        if korean or teal or muted:
            audio = next(s for s in streams if s['codec_type'] == 'audio')
            if audio.get('channels') != 5 or not audio.get('codec_name', '').startswith('pcm_'):
                raise ValueError('五声道PCM合同未保留')
            if korean and sha(Path(plan['mask'])) != plan['mask_sha256']:
                raise ValueError('执行期间韩系保护代理变化')
            if (teal or muted) and teal_grade.bound_assets() != plan['auxiliary']:
                raise ValueError('执行期间青橙辅助媒体变化')
        compare = comparison_command(source, candidate, stage / comparison_name)
        subprocess.run(compare, check=True, capture_output=True)
        if sha(source) != plan['source_sha256'] or any(sha(ROOT / n) != h
                for n, h in plan['implementation'].items()):
            raise ValueError('执行期间原片或实现发生变化')
        result = {'plan': plan, 'technical': {'status': 'passed_with_findings'},
            'aesthetic': {'status': 'pending'}, 'production_status_changed': False,
            'output': str(output / candidate.name), 'output_sha256': sha(candidate),
            'comparison': str(output / comparison_name),
            'comparison_labels': {'top': '原片' if deep_sea or forest or sea or warm or sunset or landscape or teal else '已完成基础校正，不是相机原片',
                                  'bottom': title + '55%'},
            'comparison_duration': '最多8秒，静音；输入和成片均无音频' if deep_sea or forest or sea or warm or cream or sunset or blue else '最多8秒，静音；成片完整且含音轨',
            'source_unchanged': True, 'render_details': rendered,
            'artifact_locations': artifact_locations(stage, output),
            'unchecked': ['人工完整回放与音画同步', '肤色语义保护', '目标色卡', '通用方向审计'],
            'render_log_path_note': 'render_details.command记录真实临时执行路径；查看成片使用顶层output'}
        (stage / '回执.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if output.exists():
            raise ValueError('执行期间目标目录出现，不覆盖')
        stage.rename(output)
        return result
    except Exception as error:
        raise ValueError(f'实验未交付，诊断暂存于{stage}；原因：{error}') from error
