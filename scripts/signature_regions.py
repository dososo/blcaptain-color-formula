"""六套签名的人工区域执行；不做材质识别，不宣称自动跟踪或人工验收。"""
from __future__ import annotations

import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import blcaptain_color as engine

STRATEGY = 'signature-regions'
_VERIFIED_MASK_CONTENT = set()
ROLES = {
    'plateau-sacred-light': ('ridge', 'ground', 'shadow'),
    'obsidian-gold-realm': ('specular', 'background'),
    'rain-ink-neon': ('source', 'reflection', 'background'),
    'desert-silent-rose': ('sand', 'distant'),
    'gilded-autumn-city': ('warm', 'air', 'shade'),
    'vermilion-snow-dream': ('red', 'background'),
}
# 每个动作只进入人工标注区域；未选区和保护区严格沿用 Foundation。
OPERATIONS = {
    'plateau-sacred-light': {
        'ridge': 'colorchannelmixer=rr=1:gg=.91:bb=.68',
        'ground': 'hue=s=.78,curves=all=\'0/0 .5/.38 1/.9\'',
        'shadow': 'colorchannelmixer=rr=.78:gg=.89:bb=1'},
    'obsidian-gold-realm': {
        'specular': 'colorchannelmixer=rr=1:gg=.9:bb=.60',
        'background': 'hue=s=.12,curves=all=\'0/0 .2/.15 .5/.39 1/1\''},
    'rain-ink-neon': {
        'source': 'hue=s=.03', 'reflection': 'hue=s=1.3',
        'background': 'hue=s=.30,curves=all=\'0/0 .5/.4 1/.95\''},
    'desert-silent-rose': {
        'sand': 'hue=h=-15:s=.78',
        'distant': 'hue=s=.6,colorchannelmixer=rr=.90:gg=.95:bb=1'},
    'gilded-autumn-city': {
        'warm': 'colorchannelmixer=rr=1:gg=.94:bb=.80',
        'air': 'hue=s=.65,colorchannelmixer=rr=.86:gg=.95:bb=1',
        'shade': 'hue=s=.75,curves=all=\'0/0 .5/.38 1/.95\''},
    'vermilion-snow-dream': {
        'red': 'hue=s=.03',
        'background': 'hue=s=.18,colorchannelmixer=rr=.94:gg=.96:bb=1'},
}


def implementation_hash() -> str:
    folder = Path(__file__).resolve().parent
    names = ('signature_regions.py', 'blcaptain_color.py', 'suggest.py')
    return hashlib.sha256(json.dumps({n: engine.sha256(folder / n) for n in names},
                                    sort_keys=True).encode()).hexdigest()


def foundation_filter(plan: dict) -> str:
    return engine.build_filter(engine.scaled_parameters({}, 0), None, [],
                               plan.get('foundation_grade') or engine.foundation_grade(plan['source']),
                               render_mix=0)


def check_options(plan: dict) -> None:
    if ((plan.get('composition') or {}).get('action', 'none') != 'none'
            or (plan.get('composition') or {}).get('rotate_deg', 0)
            or (plan.get('attention_map') or {}).get('mode', 'none') != 'none'
            or plan.get('shot_grade') or plan.get('adjustments') or plan.get('detect_local')):
        raise engine.SkillError('人工签名区域暂不组合裁切、旋转、注意力、自动局部、额外参数或逐镜头校正。', 4)


def _mask(polygons: list, width: int, height: int):
    import numpy as np
    from PIL import Image, ImageDraw
    image = Image.new('L', (width, height))
    draw = ImageDraw.Draw(image)
    if not isinstance(polygons, list):
        raise engine.SkillError('区域须为多边形列表；不存在的保护区明确用空列表。', 3)
    for polygon in polygons:
        if (len(polygon) < 3 or any(len(point) != 2 or any(type(v) is not int for v in point)
                                   or not 0 <= point[0] < width or not 0 <= point[1] < height
                                   for point in polygon)):
            raise engine.SkillError('多边形须至少三点，使用原生整数坐标且不能越界。', 3)
        draw.polygon([tuple(p) for p in polygon], fill=255)
    return np.asarray(image).copy()


def _spec_masks(spec: dict, source: dict, count: int) -> list:
    import numpy as np
    style = spec['style_id']
    roles = ROLES[style]
    previous = -1
    prepared = []
    for segment in spec.get('segments', []):
        start, end = segment.get('start_frame'), segment.get('end_frame')
        if type(start) is not int or type(end) is not int or start != previous + 1 or end < start or end >= count:
            raise engine.SkillError('人工区间须按连续闭区间覆盖全部真实帧；不得缺帧或重叠。', 3)
        regions = segment.get('regions') or {}
        if set(regions) != set(roles) | {'protect'}:
            raise engine.SkillError(f'当前签名要求区域 {roles} 和 protect，不接受缺项或未知项。', 3)
        masks = {role: _mask(polys, source['width'], source['height']) for role, polys in regions.items()}
        selected = np.zeros((source['height'], source['width']), dtype=bool)
        for role in roles:
            active = masks[role] > 0
            if not active.any() or (selected & active).any():
                raise engine.SkillError('每个目标角色须非空且互不重叠；保护区域可以覆盖目标。', 3)
            selected |= active
            masks[role] = np.where(masks['protect'] > 0, 0, masks[role]).astype('uint8')
            if not masks[role].any():
                raise engine.SkillError('目标被保护区完全覆盖，没有可执行区域。', 3)
        if style == 'rain-ink-neon' and np.mean(masks['reflection'] > 0) < .12:
            raise engine.SkillError('雨墨霓虹真实湿反射区域不足 12%，签名条件不成立。', 4)
        if style in ('plateau-sacred-light', 'obsidian-gold-realm'):
            accent = 'ridge' if style == 'plateau-sacred-light' else 'specular'
            if np.mean(masks[accent] > 0) > .08:
                raise engine.SkillError('金色目标超过画面 8%，窄边签名条件不成立。', 4)
        prepared.append((start, end, masks))
        previous = end
    if previous != count - 1:
        raise engine.SkillError('区域未完整覆盖本次素材全部帧。', 3)
    return prepared


def _release_weights(spec: dict, timeline: dict | None) -> list:
    if not timeline:
        from PIL import Image
        sequence = spec.get('photo_sequence') or {}
        items = sequence.get('items') or []
        if (len(items) != 2 or [item.get('index') for item in items] != [0, 1]
                or [item.get('phase') for item in items] != ['suppress', 'release']
                or len({item.get('sha256') for item in items}) != 2
                or sequence.get('current_index') not in (0, 1)):
            raise engine.SkillError('绛雪照片须提供两张独立真实照片的压制/释放序列合同及当前索引。', 4)
        for item in items:
            path = Path(item['path'])
            if not path.is_file() or engine.sha256(path) != item['sha256']:
                raise engine.SkillError('序列照片原件缺失或哈希不符。', 4)
            with Image.open(path) as image: image.verify()
        current = items[sequence['current_index']]
        if current['sha256'] != spec['source_sha256']:
            raise engine.SkillError('当前照片与序列索引不一致。', 3)
        return [float(current['phase'] == 'release')]
    fps = float(Fraction(timeline['fps_fraction']))
    count = timeline['frame_count']
    weights = [0.] * count
    previous = -1
    for item in spec.get('release_segments', []):
        start, end, transition = (item.get(k) for k in ('start_frame', 'end_frame', 'transition_frames'))
        if (any(type(v) is not int for v in (start, end, transition)) or start <= previous
                or start < round(fps) or end >= count or end - start + 1 < round(fps)
                or transition < round(fps * .5) or transition * 2 > end - start + 1):
            raise engine.SkillError('释放须独立段落：前置压制至少 1 秒、释放至少 1 秒、过渡至少 0.5 秒且不重叠。', 3)
        for index in range(start, end + 1):
            x = min(1., (index - start + 1) / transition, (end - index + 1) / transition)
            weights[index] = x * x * (3 - 2 * x)
        previous = end
    if not any(weights):
        raise engine.SkillError('缺少明确压制与释放段落。', 3)
    return weights


def prepare(source: dict, specification: dict | str, workdir: Path, baseline: str) -> dict:
    import korean_cool_protection as clock
    from PIL import Image
    try:
        spec = (json.loads(Path(specification).read_text(encoding='utf-8'))
                if isinstance(specification, (str, Path)) else json.loads(json.dumps(specification)))
        binding = clock._source_binding(source)
        review = spec.get('review') or {}
        if (spec.get('schema_version') != 1 or spec.get('style_id') not in ROLES
                or spec.get('source_sha256') != source['sha256']
                or review.get('basis') != 'original-full-resolution'
                or not review.get('reviewer') or not review.get('notes')
                or review.get('full_timeline_reviewed') is not True):
            raise engine.SkillError('缺少本次源哈希与实际全分辨率/全时轴区域审查记录。', 3)
        timeline = clock._timeline(Path(source['path'])) if source['media_type'] == 'video' else None
        if timeline and (timeline['source_start_pts'] != 0 or timeline['dimensions'] != binding['dimensions']):
            raise engine.SkillError('人工签名仅接受零起点、完整 CFR 和原尺寸的视频。', 4)
        count = timeline['frame_count'] if timeline else 1
        weights = (_release_weights(spec, timeline) if spec['style_id'] == 'vermilion-snow-dream' else None)
        parts = _spec_masks(spec, source, count)
        workdir = Path(workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=False)
        masks = {}
        for role in (*ROLES[spec['style_id']], 'protect'):
            target = workdir / f'{role}{".mkv" if timeline else ".png"}'
            if not timeline:
                data = parts[0][2][role]
                if weights is not None and role == 'red':
                    data = (data.astype(float) * (1 - weights[0])).round().astype('uint8')
                Image.fromarray(data).save(target)
            else:
                command = [engine.require_tool('ffmpeg'), '-v', 'error', '-n', '-f', 'rawvideo',
                           '-pix_fmt', 'gray', '-s', f'{source["width"]}x{source["height"]}',
                           '-r', timeline['fps_fraction'], '-i', '-', '-an', '-c:v', 'ffv1',
                           '-pix_fmt', 'gray', str(target)]
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for start, end, regions in parts:
                        for frame in range(start, end + 1):
                            data = regions[role]
                            if weights is not None and role == 'red':
                                data = (data.astype(float) * (1 - weights[frame])).round().astype('uint8')
                            process.stdin.write(data.tobytes())
                    process.stdin.close()
                    errors = process.stderr.read()
                    if process.wait(): raise engine.SkillError('区域视频生成失败：' + errors.decode(), 6)
                finally:
                    if process.poll() is None: process.kill(); process.wait()
                    if not process.stdin.closed: process.stdin.close()
                    process.stderr.close()
                measured = clock._timeline(target, timeline['fps_fraction'])
                if measured['frame_count'] != count:
                    raise engine.SkillError('区域视频漏帧。', 6)
            masks[role] = {'path': str(target), 'sha256': engine.sha256(target)}
        return {'schema_version': 1, 'source': binding, 'specification': spec,
                'timeline': timeline, 'foundation_filter': baseline, 'masks': masks,
                'capability': 'manual-regions-explicit-frame-ranges-not-tracking'}
    except engine.SkillError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise engine.SkillError(f'人工区域证据无法建立：{error}；不会回落全局调色。', 4) from error


def validate(plan: dict) -> None:
    import korean_cool_protection as clock
    check_options(plan)
    try:
        evidence = plan['signature_regions']
        style = plan['style']['id']
        if (style not in ROLES or evidence['specification']['style_id'] != style
                or plan.get('signature_execution_sha256') != implementation_hash()
                or evidence['foundation_filter'] != foundation_filter(plan)
                or evidence['foundation_filter'] != foundation_filter({'source': plan['source']})
                or evidence['source'] != clock._source_binding(plan['source'])):
            raise ValueError('源、执行器、风格或 Foundation 已漂移')
        timeline = evidence['timeline']
        if timeline and timeline != clock._timeline(Path(plan['source']['path'])):
            raise ValueError('完整源时间轴已变化')
        spec = evidence['specification']
        review = spec.get('review') or {}
        if (spec.get('source_sha256') != plan['source']['sha256']
                or review.get('basis') != 'original-full-resolution'
                or not review.get('reviewer') or not review.get('notes')
                or review.get('full_timeline_reviewed') is not True):
            raise ValueError('本次源或实际区域审查记录被改变')
        parts = _spec_masks(spec, plan['source'], timeline['frame_count'] if timeline else 1)
        releases = _release_weights(spec, timeline) if style == 'vermilion-snow-dream' else None
        if set(evidence['masks']) != set(ROLES[style]) | {'protect'}:
            raise ValueError('蒙版缺角色')
        for role, mask in evidence['masks'].items():
            path = Path(mask['path'])
            if path.is_symlink() or not path.is_file() or engine.sha256(path) != mask['sha256']:
                raise ValueError('蒙版缺失或字节变化')
            expected = hashlib.sha256()
            for start, end, regions in parts:
                for frame in range(start, end + 1):
                    pixels = regions[role]
                    if releases is not None and role == 'red':
                        pixels = (pixels.astype(float) * (1 - releases[frame])).round().astype('uint8')
                    expected.update(pixels.tobytes())
            key = (mask['sha256'], expected.hexdigest())
            if key not in _VERIFIED_MASK_CONTENT:
                measured = hashlib.sha256()
                with subprocess.Popen([engine.require_tool('ffmpeg'), '-v', 'error', '-i', str(path),
                                       '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    while True:
                        chunk = process.stdout.read(1048576)
                        if not chunk: break
                        measured.update(chunk)
                    if process.wait() or measured.hexdigest() != expected.hexdigest():
                        raise ValueError('实际蒙版像素不对应人工多边形/保护/释放帧，不能只重写哈希')
                _VERIFIED_MASK_CONTENT.add(key)
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise engine.SkillError(f'人工签名证据无效：{error}；请重新 plan。', 2) from error


def confirm(plan: dict, chosen: str | None) -> None:
    if chosen != STRATEGY:
        raise engine.SkillError('人工签名必须明确确认 --confirm-local signature-regions。', 2)
    validate(plan)
    preflight = plan.get('execution_preflight') or {}
    if preflight.get('status') != 'executable':
        raise engine.SkillError('人工区域尚未通过同链三档预演。', 4)
    artifacts = preflight.get('preview_artifacts') or {}
    records = [artifacts.get('baseline', {}), *artifacts.get('levels', [])]
    if {item.get('strength') for item in records} != {0., .3, .55, .8, float(plan['strength'])}:
        raise engine.SkillError('缺少本次 Foundation、三档或精确请求档预演。', 2)
    for item in records:
        if not Path(item['path']).is_file() or engine.sha256(Path(item['path'])) != item['sha256']:
            raise engine.SkillError('本次内部预演缺失或字节已改变。', 2)


def build_graph(plan: dict, baseline: str, strength: float | None = None) -> str:
    mix = float(plan['render_mix'] if strength is None else strength)
    if not 0 <= mix <= 1: raise engine.SkillError('区域混合强度须在 0 至 1。', 3)
    tags = (engine.photo_output_filter(plan, '') if plan['source']['media_type'] == 'photo'
            else 'format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709')
    if plan['source']['media_type'] == 'video':
        tags = ('zscale=matrixin=gbr:matrix=709:rangein=full:range=limited,' + tags)
    if mix == 0: return f'[0:v]{baseline},format=gbrp16le,{tags}[out]'
    roles = ROLES[plan['style']['id']]
    clock = 'setpts=PTS-STARTPTS'
    if plan['source']['media_type'] == 'video':
        fps = Fraction(plan['signature_regions']['timeline']['fps_fraction'])
        clock = f'settb=AVTB,setpts=N*{fps.denominator}/({fps.numerator}*TB)'
    graph = [f'[0:v]{baseline},{clock},format=gbrp16le,split={len(roles)+1}[base]' + ''.join(f'[r{i}]' for i in range(len(roles)))]
    previous = 'base'
    for index, role in enumerate(roles):
        effect = OPERATIONS[plan['style']['id']][role]
        graph.append(f'[r{index}]{effect},format=gbrp16le[target{index}]')
        weight = min(1., mix / .3) if plan['style']['id'] == 'vermilion-snow-dream' and role == 'red' else mix
        if plan['style']['id'] == 'rain-ink-neon' and role == 'source':
            weight = min(1., mix / .55)
        graph.append(f'[{index+1}:v]{clock},format=gray16le,lut=y=val*{weight:.9f},format=gbrp16le[mask{index}]')
        graph.append(f'[{previous}][target{index}][mask{index}]maskedmerge=planes=7[merged{index}]')
        previous = f'merged{index}'
    graph.append(f'[{previous}]{tags}[out]')
    return ';'.join(graph)


def command(plan: dict, baseline: str, output: Path, strength: float | None = None,
            include_audio: bool = False) -> tuple[list, str]:
    validate(plan)
    graph = build_graph(plan, baseline, strength)
    args = [engine.require_tool('ffmpeg'), '-v', 'error', '-n', '-filter_complex_threads', '4',
            '-i', plan['source']['path']]
    for role in ROLES[plan['style']['id']]: args += ['-i', plan['signature_regions']['masks'][role]['path']]
    args += ['-filter_complex', graph, '-map', '[out]']
    if plan['source']['media_type'] == 'photo':
        args += ['-frames:v', '1', '-c:v', 'png', '-pix_fmt', plan['color_pipeline']['pixel_format']]
    else:
        args += ['-map', '0:a?', '-c:a', 'copy'] if include_audio else ['-an']
        args += ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-pix_fmt', 'yuv420p',
                 '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
                 '-fps_mode', 'passthrough', '-movflags', '+faststart']
    return args + [str(output)], graph


def preview(recipe: dict, source: dict, strength: float, output: Path) -> tuple[str, dict]:
    evidence = recipe.get('_signature_regions')
    if not evidence: raise engine.SkillError('当前签名缺少人工区域/时序证据，不能预演全局替代版。', 4)
    effective, mix, _ = engine.strength_execution_for(recipe, strength)
    plan = {'source': source, 'style': engine.style_payload(recipe, source['media_type']),
            'strength': strength, 'effective_strength': effective, 'render_mix': mix,
            'parameters': engine.scaled_parameters(recipe['parameters'], 1),
            'tone_curve': engine.scaled_curve(recipe.get('tone_curve'), 1),
            'hsl_bands': engine.scaled_hsl(recipe.get('hsl_bands'), 1),
            'foundation_grade': engine.foundation_grade(source), 'composition': engine.composition_for(source, None),
            'attention_map': {'mode': 'none'}, 'visual_brief': {'semantic_analysis_claimed': False},
            'color_pipeline': engine.color_pipeline_for(source), 'signature_regions': evidence,
            'signature_execution_sha256': implementation_hash()}
    args, graph = command(plan, foundation_filter(plan), output)
    engine.run(args)
    if source['media_type'] == 'photo': engine.validate_photo_color(output, plan)
    validate(plan)
    return graph, plan


def measure(plan: dict, foundation: Path, rendered: Path) -> dict:
    """逐帧逐像素校验保护/未选区和目标变化；材质判断仍属于实际区域审查。"""
    import numpy as np
    import korean_cool_protection as clock
    evidence = plan['signature_regions']
    source = plan['source']
    timeline = evidence['timeline']
    count = timeline['frame_count'] if timeline else 1
    if timeline:
        for path in (foundation, rendered):
            measured = clock._timeline(path, timeline['fps_fraction'])
            if (measured['frame_count'] != count or measured['relative_pts'] != timeline['relative_pts']
                    or measured['source_start_pts'] != 0 or measured['dimensions'] != timeline['dimensions']):
                raise engine.SkillError('人工签名输出帧数、完整 PTS 或尺寸不匹配。', 6)
    parts = _spec_masks(evidence['specification'], source, count)
    processes = [subprocess.Popen([engine.require_tool('ffmpeg'), '-v', 'error', '-i', str(path),
                                  '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for path in (foundation, rendered)]
    width, height = source['width'], source['height']
    size = width * height * 3
    rows = []
    releases = (_release_weights(evidence['specification'], timeline)
                if plan['style']['id'] == 'vermilion-snow-dream' else None)
    try:
        segment = 0
        for frame in range(count):
            arrays = []
            for process in processes:
                data = process.stdout.read(size)
                if len(data) != size: raise engine.SkillError('全像素验收缺帧，不能仅按抽样通过。', 6)
                arrays.append(np.frombuffer(data, np.uint8).reshape(height, width, 3).astype(float))
            while frame > parts[segment][1]: segment += 1
            masks = parts[segment][2]
            selected = np.logical_or.reduce([masks[role] > 0 for role in ROLES[plan['style']['id']]])
            delta = np.max(np.abs(arrays[1] - arrays[0]), axis=2)
            # 编码边缘 2px 不参与硬保真，但仍被记录；照片/视频均不能声称无损。
            from PIL import Image, ImageFilter
            protected = np.asarray(Image.fromarray((~selected).astype('uint8') * 255).filter(ImageFilter.MinFilter(5))) > 0
            protected_p99 = float(np.percentile(delta[protected], 99)) if protected.any() else 0.
            limit = 2 if source['media_type'] == 'photo' else 8
            if protected_p99 > limit:
                raise engine.SkillError(f'保护/未选区偏离 Foundation：帧 {frame}，P99={protected_p99:.2f} > {limit}。', 6)
            changes = {role: float(np.mean(delta[masks[role] > 0])) for role in ROLES[plan['style']['id']]}
            stats = [{role: _region_stats(array, masks[role]) for role in ROLES[plan['style']['id']]} for array in arrays]
            if float(plan['strength']) > 0:
                check_signature(plan['style']['id'], stats[0], stats[1], plan['strength'],
                                releases[frame] if releases else None)
            rows.append({'frame': frame, 'protected_p99_rgb8': protected_p99,
                         'region_mean_change_rgb8': changes, 'foundation': stats[0], 'candidate': stats[1]})
        for process in processes:
            if process.stdout.read(1) or process.wait(): raise engine.SkillError('全像素验收发现额外帧或解码错误。', 6)
    finally:
        for process in processes:
            if process.poll() is None: process.kill(); process.wait()
            process.stdout.close()
            process.stderr.close()
    means = {role: sum(row['region_mean_change_rgb8'][role] for row in rows) / count for role in ROLES[plan['style']['id']]}
    required_roles = set(means) - ({'red'} if releases is not None else set())
    if float(plan['strength']) > 0 and any(means[role] < .25 for role in required_roles):
        raise engine.SkillError('某个人工目标区域没有可测的真实变化；不能宣称完成签名。', 6)
    return {'passed': True, 'signature_checks_passed': True, 'frame_count': count, 'frames': rows, 'mean_region_change_rgb8': means,
            'boundary': '逐帧像素/保护/时间轴技术验证；不是通用材质识别、跟踪或视觉接受。'}


def _region_stats(rgb, mask):
    import colorsys
    import numpy as np
    pixels = rgb[mask > 0] / 255
    mean = pixels.mean(axis=0)
    chroma = pixels.max(axis=1) - pixels.min(axis=1)
    return {'luma': float((pixels * [.2126, .7152, .0722]).sum(axis=1).mean()),
            'chroma': float(chroma.mean()), 'chroma_p95': float(np.percentile(chroma, 95)),
            'warm': float(mean[0] - mean[2]), 'hue': colorsys.rgb_to_hsv(*mean)[0] * 360}


def check_signature(style: str, prior: dict, current: dict, strength: float, release: float | None = None) -> None:
    """只量测人工已定位角色；不把色相阈值当材质定位器。"""
    floor = .004 * min(1., strength / .55)
    def diff(role, key): return current[role][key] - prior[role][key]
    checks = {}
    if style == 'plateau-sacred-light':
        checks = {'金线须变暖': diff('ridge', 'warm') >= floor,
                  '阴影须转冷蓝': diff('shadow', 'warm') <= -floor,
                  '地表须退暗': diff('ground', 'luma') < -floor}
    elif style == 'obsidian-gold-realm':
        checks = {'镜面边缘须独立释放金': diff('specular', 'warm') >= floor,
                  '黑曜背景须减彩': diff('background', 'chroma') <= -floor}
    elif style == 'rain-ink-neon':
        checks = {'倒影峰值彩度须至少为配对直接光源的 1.6 倍':
                  current['reflection']['chroma_p95'] >= max(.015, current['source']['chroma_p95'] * 1.6),
                  '直接光源须减彩': diff('source', 'chroma') <= -floor}
    elif style == 'desert-silent-rose':
        shift = (diff('sand', 'hue') + 180) % 360 - 180
        checks = {'沙面须有可测去橙玫瑰位移': -13 <= shift <= -6 * min(1., strength / .55),
                  '沙面彩度不能增加': diff('sand', 'chroma') <= .004,
                  '远沙丘须退彩': diff('distant', 'chroma') <= -floor}
    elif style == 'gilded-autumn-city':
        checks = {'受光面须变暖': diff('warm', 'warm') >= floor,
                  '远景空气须转冷': diff('air', 'warm') <= -floor,
                  '非受光侧须退暗': diff('shade', 'luma') <= -floor}
    elif style == 'vermilion-snow-dream':
        if release == 0:
            checks['压制段红彩度须低于原有十分之一'] = current['red']['chroma'] <= prior['red']['chroma'] * .1 + .005
        elif release == 1:
            checks['释放红不得追加提亮或漂色'] = (abs(diff('red', 'luma')) <= .015
                                                and abs(diff('red', 'chroma')) <= .02)
        checks['背景须减彩'] = diff('background', 'chroma') <= -floor
    failed = [name for name, passed in checks.items() if not passed]
    if failed: raise engine.SkillError('人工区域签名未达成：' + '；'.join(failed), 6)


def validate_photo_sequence_receipts(paths: list) -> dict:
    """两份正式成员回执和原件/成片均存在，才证明序列完整；不替代视觉审查。"""
    if len(paths) != 2: raise engine.SkillError('绛雪完整序列需要两份正式回执。', 6)
    rows = [json.loads(Path(path).read_text(encoding='utf-8')) for path in paths]
    shared = None
    indices = set()
    for row in rows:
        evidence = row.get('signature_regions') or {}
        spec = evidence.get('specification') or {}
        sequence = spec.get('photo_sequence') or {}
        _release_weights(spec, None)
        if (row.get('style', {}).get('id') != 'vermilion-snow-dream'
                or row.get('local_confirmation') != STRATEGY
                or row.get('visual_validation', {}).get('signature_regions', {}).get('passed') is not True
                or row.get('original_sha256') != spec['source_sha256']
                or not Path(row['output_path']).is_file()
                or engine.sha256(Path(row['output_path'])) != row['output_sha256']):
            raise engine.SkillError('序列成员不是可验证正式区域成片。', 6)
        if shared is None: shared = sequence['items']
        if sequence['items'] != shared: raise engine.SkillError('两份回执不属于同一源序列。', 6)
        indices.add(sequence['current_index'])
    if indices != {0, 1}: raise engine.SkillError('序列缺少压制或释放成员。', 6)
    return {'status': 'complete-sequence-technical-evidence', 'members': [row['output_path'] for row in rows],
            'boundary': '两张序列真实执行证据完整；不等于视觉或人工接受。'}
