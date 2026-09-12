"""普通韩系清冷的共享执行链；人物保留基础校正，冷感只进入环境。

不使用历史实验蒙版，不把逐帧分割称为身份跟踪。局部结果不能烘焙为全局 LUT。
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
import tempfile

import blcaptain_color as engine

STRATEGY = 'korean-cool-protection'


def implementation_hash() -> str:
    scripts = Path(__file__).resolve().parent
    names = ('korean_cool_execution.py', 'korean_cool_validation.py',
             'korean_cool_grade.py', 'blcaptain_color.py', 'suggest.py')
    return hashlib.sha256(json.dumps({name: engine.sha256(scripts / name) for name in names},
                                    sort_keys=True).encode()).hexdigest()


def check_options(plan: dict) -> None:
    if ((plan.get('composition') or {}).get('action', 'none') != 'none'
            or (plan.get('composition') or {}).get('rotate_deg', 0)
            or (plan.get('composition') or {}).get('crop')
            or (plan.get('attention_map') or {}).get('mode', 'none') != 'none'
            or plan.get('shot_grade') or plan.get('adjustments')):
        raise engine.SkillError(
            '韩系人物保护暂不与裁切、旋转、径向局部、逐镜头校正或额外参数调整组合；'
            '请保持原构图独立预演，不会回落全局调色。', 4)


def foundation_filter(plan: dict) -> str:
    return engine.build_filter(engine.scaled_parameters({}, 0), None, [],
                               plan.get('foundation_grade') or engine.foundation_grade(plan['source']),
                               render_mix=0.0)


def validate(plan: dict) -> None:
    import korean_cool_protection as protection
    if (plan.get('style') or {}).get('id') != 'korean-cool':
        raise engine.SkillError('韩系保护协议只能用于韩系清冷计划，请重新生成并确认。', 2)
    check_options(plan)
    evidence = plan.get('korean_cool_protection')
    if not evidence or plan.get('korean_execution_sha256') != implementation_hash():
        raise engine.SkillError('韩系保护执行协议已更新或缺失，请重新生成方案并确认。', 2)
    if (evidence.get('foundation_filter') != foundation_filter(plan)
            or evidence.get('foundation_filter') != foundation_filter({'source': plan['source']})):
        raise engine.SkillError('韩系保护证据与当前素材诊断或方案 Foundation 不一致，请重新生成并确认。', 2)
    if (evidence.get('timeline') or {}).get('source_start_pts', 0) != 0:
        raise engine.SkillError('韩系保护暂不处理非零首帧时间戳，以免重置视频时破坏音画相对偏移。', 4)
    try:
        protection.validate(evidence, plan['source'])
    except (protection.ProtectionError, OSError, KeyError, ValueError) as error:
        raise engine.SkillError(f'韩系保护证据不再有效：{error}；请重新生成方案。', 2) from error


def check_foundation(plan: dict, baseline: Path) -> dict:
    import diagnose
    foundation = plan.get('foundation_grade') or engine.foundation_grade(plan['source'])
    targets = foundation.get('readiness_targets') or {}
    if not targets.get('required'):
        return {'status': 'not-required', 'blocking_failures': []}
    profile = 'display-p3' if plan['source']['color']['profile'] == 'display-p3' else 'srgb'
    after = diagnose.diagnose(baseline, profile, use_semantic=False)
    report = engine.foundation_readiness(plan['source']['foundation_diagnosis'], after,
                                         active_axes=targets.get('active_axes') or [])
    if report['status'] == 'blocked':
        raise engine.SkillError('韩系基础校正未通过，不能进入创意预演：'
                                 + '、'.join(report['blocking_failures']), 6)
    return report


def confirm(plan: dict, chosen: str | None) -> None:
    if chosen != STRATEGY:
        raise engine.SkillError(
            '韩系清冷包含独立人物保护，请看过蒙版和预演后明确确认局部策略：'
            f'--confirm-local {STRATEGY}；不能只确认全局方案。', 2)
    validate(plan)
    if (plan.get('execution_preflight') or {}).get('status') != 'executable':
        raise engine.SkillError('韩系局部保护尚未通过完整三档预演，不生成正式成片。', 4)
    try:
        artifacts = plan['execution_preflight']['preview_artifacts']
        levels = artifacts['levels']
        required = {.3, .55, .8, float(plan['strength'])}
        if (artifacts['status'] != 'internal-preview-not-final'
                or artifacts['baseline']['strength'] != 0 or len(levels) != len(required)
                or {item['strength'] for item in levels} != required):
            raise ValueError('缺少 Foundation、三档或实际请求档')
        records = [artifacts['baseline'], *levels]
        directory = (Path(plan['korean_cool_protection']['mask']['path']).resolve()
                     .parents[2 if plan['source']['media_type'] == 'photo' else 1] / 'previews')
        paths = [Path(item['path']) for item in records]
        if len({path.resolve() for path in paths}) != len(paths):
            raise ValueError('不同档位不能复用同一预演文件')
        for path, item in zip(paths, records):
            if (path.is_symlink() or path.resolve().parent != directory or not path.is_file()
                    or path.stat().st_size == 0 or engine.sha256(path) != item['sha256']):
                raise ValueError('本次预演文件缺失、位置不符或字节已变化')
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise engine.SkillError(f'本次留存预演无法确认：{error}；请重新生成并查看方案。', 2) from error


def build_graph(plan: dict, baseline: str, strength: float | None = None) -> str:
    """16 位 RGB 内部工作；全范围灰蒙版，不沿用受限视频的电平重映射。"""
    source = plan['source']
    mix = float(plan['render_mix'] if strength is None else strength)
    if not 0 <= mix <= 1:
        raise engine.SkillError('韩系保护混合强度必须在 0 到 1。', 3)
    clock = 'setpts=PTS-STARTPTS'
    if source['media_type'] == 'video':
        fps = Fraction(plan['korean_cool_protection']['timeline']['fps_fraction'])
        clock = f'settb=AVTB,setpts=N*{fps.denominator}/({fps.numerator}*TB)'
    base = f'[0:v]{baseline},{clock},format=gbrp16le'
    tags = (engine.photo_output_filter(plan, '') if source['media_type'] == 'photo'
            else 'format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709')
    # 零档严格沿用同一基础校正和输出编码，不让额外RGB转换冒充创意变化。
    if mix == 0:
        return f'[0:v]{baseline},{clock},{tags}[out]'
    # 共同除以最大增益，避开整数矩阵的中间夹断；pc 再消去共同尺度。
    # pc=lum 保持的是 HSL 中点，不是物理亮度；最终仍须通过原空间安全门。
    # 不用 colorbalance:pl=1，其通道触边分支会把有彩像素突然归灰。
    target = ('curves=all=\'0/0 .2/.178 .5/.482 .8/.792 1/1\','
              'colorchannelmixer=rr=.702479338843:gg=.806611570248:bb=1:pc=lum:pa=1')
    # 一个感知映射、一次线性混合；保护区内恢复同一 Foundation。
    return (f'{base},split=3[keep][mixbase][env];'
            f'[env]{target}[target];'
            f'[mixbase][target]blend=all_expr=\'A*(1-{mix:.6f})+B*{mix:.6f}\'[world];'
            f'[1:v]{clock},scale={source["width"]}:{source["height"]}:flags=bilinear,'
            'format=gray16le,format=gbrp16le[protect];'
            f'[world][keep][protect]maskedmerge=planes=7,{tags}[out]')


def command(plan: dict, baseline: str, output: Path, strength: float | None = None,
            include_audio: bool = False) -> tuple[list[str], str]:
    validate(plan)
    graph = build_graph(plan, baseline, strength)
    args = [engine.require_tool('ffmpeg'), '-v', 'error', '-n', '-filter_complex_threads', '4',
            '-i', plan['source']['path'], '-i', plan['korean_cool_protection']['mask']['path'],
            '-filter_complex', graph, '-map', '[out]']
    if plan['source']['media_type'] == 'photo':
        args += ['-frames:v', '1', '-c:v', 'png', '-pix_fmt', plan['color_pipeline']['pixel_format']]
    else:
        args += (['-map', '0:a?', '-c:a', 'copy'] if include_audio else ['-an'])
        args += ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-pix_fmt', 'yuv420p',
                 '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
                 '-fps_mode', 'passthrough', '-movflags', '+faststart']
    return args + [str(output)], graph


def probe_colors(plan: dict, colors: list, with_foundation: bool = True) -> list:
    """真实双输入探针：无人物假设，近中性亮部使用同一连续权重；不是目标色卡。"""
    import korean_cool_protection as protection
    if not colors:
        return []
    width = len(colors)
    pixels = bytes(c for rgb in colors for c in rgb)
    baseline = foundation_filter(plan) if with_foundation else 'null'
    analysis = 'format=gbrp16le'
    if (plan['source'].get('color') or {}).get('profile') == 'display-p3':
        analysis += (',zscale=primariesin=smpte432:transferin=iec61966-2-1:matrixin=gbr:rangein=full:'
                     'primaries=709:transfer=13:matrix=gbr:range=full')
    analysis += ',format=rgb24'
    # 与保护证据相同：先运行真实 Foundation，再进入 sRGB 分析；两份白位取并集。
    analysis_graph = (f'[0:v]split[raw][foundation];[raw]{analysis}[original];'
                      f'[foundation]{baseline},{analysis}[corrected];'
                      '[original][corrected]hstack=inputs=2[out]')
    measured = subprocess.run([
        engine.require_tool('ffmpeg'), '-v', 'error', '-filter_complex_threads', '1',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x1', '-i', 'pipe:0',
        '-filter_complex', analysis_graph, '-map', '[out]', '-frames:v', '1',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], input=pixels, capture_output=True)
    if measured.returncode or len(measured.stdout) != width * 6:
        raise engine.SkillError('韩系 Foundation 白位探针失败：'
                                + measured.stderr.decode('utf-8', 'replace'), 6)
    original_weights = protection.neutral_white_weights(measured.stdout[:width * 3])
    foundation_weights = protection.neutral_white_weights(measured.stdout[width * 3:])
    weights = bytes(max(a, b) for a, b in zip(original_weights, foundation_weights))
    stub = {**plan, 'source': {**plan['source'], 'media_type': 'photo', 'width': width, 'height': 1}}
    # 探针保持输入编码数值，工作色域解释由调用方负责；不模拟人脸或空间组成。
    stub['color_pipeline'] = {'output_profile': 'sRGB', 'pixel_format': 'rgb24'}
    graph = build_graph(stub, baseline)
    with tempfile.TemporaryDirectory(prefix='korean-physics-probe-') as folder:
        mask = Path(folder) / 'weights.pgm'
        mask.write_bytes(f'P5\n{width} 1\n255\n'.encode() + weights)
        args = [engine.require_tool('ffmpeg'), '-v', 'error', '-filter_complex_threads', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x1', '-i', 'pipe:0',
                '-i', str(mask), '-filter_complex', graph, '-map', '[out]', '-frames:v', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']
        result = subprocess.run(args, input=pixels, capture_output=True)
    if result.returncode or len(result.stdout) != width * 3:
        raise engine.SkillError('韩系双输入物理探针失败：' + result.stderr.decode('utf-8', 'replace'), 6)
    return [tuple(result.stdout[i:i + 3]) for i in range(0, len(result.stdout), 3)]


def preview(recipe: dict, source: dict, strength: float, output: Path) -> tuple[str, dict]:
    import korean_cool_protection as protection
    evidence = recipe.get('_korean_protection')
    if evidence is None:
        with tempfile.TemporaryDirectory(prefix='blcaptain-korean-preview-') as folder:
            bound = copy.deepcopy(recipe)
            bound['_korean_protection'] = protection.prepare(
                source, Path(folder) / 'evidence', foundation_filter({'source': source}))
            return preview(bound, source, strength, output)
    effective, mix, _ = engine.strength_execution_for(recipe, strength)
    plan = {
        'source': source, 'style': engine.style_payload(recipe, source['media_type']),
        'strength': strength, 'effective_strength': effective, 'render_mix': mix,
        'parameters': engine.scaled_parameters(recipe['parameters'], 1),
        'tone_curve': engine.scaled_curve(recipe.get('tone_curve'), 1),
        'hsl_bands': engine.scaled_hsl(recipe.get('hsl_bands'), 1),
        'foundation_grade': engine.foundation_grade(source),
        'composition': {'action': 'none', 'crop': None, 'rotate_deg': 0.0,
                        'output_width': source['width'], 'output_height': source['height']},
        'attention_map': {'mode': 'none'}, 'visual_brief': {'semantic_analysis_claimed': False},
        'color_pipeline': engine.color_pipeline_for(source), 'output_path': str(output),
        'korean_cool_protection': evidence,
        'korean_execution_sha256': implementation_hash(),
    }
    args, graph = command(plan, foundation_filter(plan), output)
    engine.run(args)
    if source['media_type'] == 'photo':
        engine.validate_photo_color(output, plan)
    else:
        protection.validate_output_timeline(evidence, output)
    validate(plan)
    if strength == 0:
        plan['foundation_readiness'] = check_foundation(plan, output)
    return graph, plan
