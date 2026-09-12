"""人工区域执行合同：合成像素仅证明执行行为，不代替真实材质审查。"""
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import blcaptain_color as engine


class SignatureRegionsTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('signature_regions')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.png'
        engine.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=0xb99476:s=64x32', '-frames:v', '1', str(self.source)])
        self.info = {'path': str(self.source), 'sha256': engine.sha256(self.source),
                     'media_type': 'photo', 'width': 64, 'height': 32, 'duration': 0,
                     'color': {'profile': 'srgb', 'support': 'direct'}}
        self.spec = {'schema_version': 1, 'style_id': 'desert-silent-rose',
                     'source_sha256': self.info['sha256'],
                     'review': {'reviewer': '合成夹具测试', 'basis': 'original-full-resolution',
                                'notes': '合成夹具，非真实沙面验收', 'full_timeline_reviewed': True},
                     'segments': [{'start_frame': 0, 'end_frame': 0, 'regions': {
                         'sand': [[[0, 0], [30, 0], [30, 31], [0, 31]]],
                         'distant': [[[33, 0], [63, 0], [63, 31], [33, 31]]],
                         'protect': [[[0, 0], [12, 0], [12, 31], [0, 31]]]}}]}

    def prepare(self):
        return self.module.prepare(self.info, self.spec, self.root / 'evidence', 'null')

    def plan(self, evidence):
        return {'source': self.info, 'style': {'id': self.spec['style_id']},
                'render_mix': .55, 'strength': .55,
                'color_pipeline': {'output_profile': 'sRGB', 'pixel_format': 'rgb48be'},
                'signature_regions': evidence, 'signature_execution_sha256': self.module.implementation_hash()}

    def pixels(self, path):
        data = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                                       '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        return [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]

    def test_cli_preflight_has_complete_composition_contract(self):
        from PIL import Image, PngImagePlugin
        image = Image.open(self.source)
        tags = PngImagePlugin.PngInfo()
        tags.add(b'sRGB', bytes([0]))
        tags.add(b'cICP', bytes([1, 13, 0, 1]))
        image.save(self.source, pnginfo=tags)
        stream = next(item for item in engine.probe(self.source)['streams'] if item['codec_type'] == 'video')
        self.assertEqual(stream.get('color_primaries'), 'bt709')
        self.assertEqual(stream.get('color_transfer'), 'iec61966-2-1')
        self.spec['source_sha256'] = engine.sha256(self.source)
        spec = self.root / 'regions.json'
        spec.write_text(json.dumps(self.spec), encoding='utf-8')
        process = subprocess.run([
            sys.executable, str(Path(engine.__file__)), 'plan',
            '--input', str(self.source), '--style', self.spec['style_id'], '--strength', '55',
            '--signature-regions', str(spec), '--output-dir', str(self.root / 'cli'),
            '--plan-out', str(self.root / 'plan.json')], capture_output=True, text=True)
        # 此夹具仍可被材质或 Foundation 门拒绝，但绝不能因预演结构缺字段失败。
        messages = process.stdout + process.stderr
        self.assertNotIn('KeyError:', messages)
        if process.returncode:
            self.assertIn('同执行链预演可运行', messages)
        previews = list((self.root / 'cli').glob('masks/signature-*/previews/*internal-preview.png'))
        self.assertEqual(len(previews), 4, messages)

    def test_real_masks_protect_foundation_and_change_only_regions(self):
        evidence = self.prepare()
        plan = self.plan(evidence)
        outputs = []
        with patch.object(self.module, 'foundation_filter', return_value='null'):
            for strength in (0, .3, .55, .8):
                output = self.root / f'{strength}.png'
                command, _ = self.module.command(plan, 'null', output, strength)
                engine.run(command)
                outputs.append(self.pixels(output))
        self.assertEqual(outputs[0][5], outputs[3][5])
        self.assertNotEqual(outputs[0][22], outputs[3][22])
        distances = [sum(abs(a-b) for a,b in zip(out[22], outputs[0][22])) for out in outputs[1:]]
        self.assertEqual(distances, sorted(set(distances)))

    def test_missing_role_or_review_rejected(self):
        for change in ('role', 'review', 'source'):
            with self.subTest(change=change):
                spec = copy.deepcopy(self.spec)
                if change == 'role': del spec['segments'][0]['regions']['sand']
                if change == 'review': spec['review']['full_timeline_reviewed'] = False
                if change == 'source': spec['source_sha256'] = '0' * 64
                with self.assertRaises(engine.SkillError):
                    self.module.prepare(self.info, spec, self.root / change, 'null')

    def test_overlap_and_out_of_bounds_rejected(self):
        for invalid in ([[[0, 0], [40, 0], [40, 31], [0, 31]]], [[[64, 0], [70, 0], [70, 31]]]):
            self.spec['segments'][0]['regions']['distant'] = invalid
            with self.assertRaises(engine.SkillError): self.prepare()

    def test_mask_tamper_and_old_protocol_rejected(self):
        evidence = self.prepare()
        plan = self.plan(evidence)
        with patch.object(self.module, 'foundation_filter', return_value='null'):
            self.module.validate(plan)
            Path(evidence['masks']['sand']['path']).write_bytes(b'changed')
            with self.assertRaises(engine.SkillError): self.module.validate(plan)

    def test_rehashed_mask_still_must_match_manual_polygons(self):
        from PIL import Image
        evidence = self.prepare()
        plan = self.plan(evidence)
        path = Path(evidence['masks']['sand']['path'])
        Image.new('L', (64, 32), 255).save(path)
        evidence['masks']['sand']['sha256'] = engine.sha256(path)
        with patch.object(self.module, 'foundation_filter', return_value='null'), \
                self.assertRaisesRegex(engine.SkillError, '多边形'):
            self.module.validate(plan)
            plan['signature_execution_sha256'] = '0' * 64
            with self.assertRaises(engine.SkillError): self.module.validate(plan)

    def test_cross_style_and_missing_confirm_rejected(self):
        plan = self.plan(self.prepare())
        with self.assertRaises(engine.SkillError): self.module.confirm(plan, None)
        plan['style']['id'] = 'natural-clean'
        with self.assertRaises(engine.SkillError): self.module.validate(plan)

    def test_rain_reflection_area_not_optional(self):
        self.spec['style_id'] = 'rain-ink-neon'
        self.spec['segments'][0]['regions'] = {
            'source': [[[0, 0], [4, 0], [4, 4], [0, 4]]],
            'reflection': [[[10, 10], [13, 10], [13, 13], [10, 13]]],
            'background': [[[32, 0], [63, 0], [63, 31], [32, 31]]], 'protect': []}
        with self.assertRaises(engine.SkillError): self.prepare()

    def test_vermilion_single_photo_not_sequence(self):
        self.spec['style_id'] = 'vermilion-snow-dream'
        with self.assertRaisesRegex(engine.SkillError, '序列'): self.prepare()

    def test_lut_cannot_drop_manual_regions(self):
        import lut_export
        for style in self.module.ROLES:
            with self.subTest(style=style), self.assertRaisesRegex(ValueError, '区域|时序'):
                lut_export.color_only_filter(engine, {'style': {'id': style}})

    def test_missing_evidence_suggest_refuses_global_fallback(self):
        import suggest
        recipe = engine.find_recipe(engine.load_catalog(), 'desert-silent-rose', 'photo', allow_manual=True)
        result = suggest.run_execution_preflight(recipe, self.info, .55)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['code'], 'signature-regions-required')

    def test_cli_accepts_explicit_regions(self):
        parser = engine.parser()
        args = parser.parse_args(['plan', '--input', 'x.png', '--style', 'desert-silent-rose',
                                  '--output-dir', 'out', '--signature-regions', 'regions.json'])
        self.assertEqual(args.signature_regions, 'regions.json')

    def test_make_plan_prepares_before_three_levels_and_binds_protocol(self):
        import suggest
        self.info.update({'has_audio': False, 'luminance_diagnosis': {'p1': .03, 'p50': .4, 'p99': .8}})
        self.info['color']['label'] = 'sRGB'
        spec_path = self.root / 'regions.json'
        spec_path.write_text(json.dumps(self.spec))
        args = engine.parser().parse_args(['plan', '--input', str(self.source), '--style', self.spec['style_id'],
                                          '--output-dir', str(self.root / 'out'), '--signature-regions', str(spec_path)])
        def preflight(recipe, source, strength):
            self.assertTrue(Path(recipe['_signature_regions']['masks']['sand']['path']).is_file())
            self.assertEqual(recipe['_signature_regions']['source']['sha256'], source['sha256'])
            return {'status': 'executable', 'monotonicity': {'strictly_increasing': True}}
        with patch.object(engine, 'inspect_media', return_value=self.info), \
                patch.object(suggest, 'run_execution_preflight', side_effect=preflight):
            plan = engine.make_plan(args)
        old = plan['plan_id']
        self.assertIn('signature_regions', plan)
        plan['signature_regions']['specification']['review']['notes'] += '改变'
        self.assertNotEqual(old, engine.plan_fingerprint(plan))

    def test_real_video_all_frames_and_audio_not_truncated(self):
        video = self.root / 'source.mp4'
        engine.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=0xb99476:size=64x32:rate=6:duration=3,noise=alls=2:allf=t:all_seed=23', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000:duration=3', '-vf',
                    'setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709',
                    '-c:v', 'libx264', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(video)])
        self.info.update({'path': str(video), 'sha256': engine.sha256(video),
                          'media_type': 'video', 'duration': 3, 'has_audio': True,
                          'color': {'profile': 'rec709-sdr', 'support': 'direct'}})
        self.spec['source_sha256'] = self.info['sha256']
        self.spec['segments'][0]['end_frame'] = 17
        evidence = self.prepare()
        plan = self.plan(evidence)
        output, base = self.root / 'graded.mp4', self.root / 'base.mp4'
        with patch.object(self.module, 'foundation_filter', return_value='null'):
            for path, strength in ((base, 0), (output, .55)):
                args, _ = self.module.command(plan, 'null', path, strength, include_audio=True)
                engine.run(args)
            # 动态噪声夹具不是沙面，只隔离题材签名门；真实全帧/保护/音轨门仍执行。
            with patch.object(self.module, 'check_signature'):
                report = self.module.measure(plan, base, output)
        self.assertEqual(report['frame_count'], 18)
        original_audio = next(s for s in engine.probe(video)['streams'] if s['codec_type'] == 'audio')
        output_audio = next(s for s in engine.probe(output)['streams'] if s['codec_type'] == 'audio')
        self.assertEqual(original_audio['duration_ts'], output_audio['duration_ts'])
        self.assertEqual(original_audio['nb_frames'], output_audio['nb_frames'])

    def test_all_six_graphs_have_real_region_operations(self):
        for style, roles in self.module.ROLES.items():
            with self.subTest(style=style):
                evidence = self.prepare() if not (self.root / 'evidence').exists() else None
                plan = self.plan(evidence or {})
                plan['style']['id'] = style
                graph = self.module.build_graph(plan, 'null')
                self.assertEqual(graph.count('maskedmerge'), len(roles))
                for operation in self.module.OPERATIONS[style].values(): self.assertIn(operation, graph)

    def test_rain_gate_measures_paired_chroma_not_only_any_change(self):
        prior = {'source': {'chroma': .3, 'chroma_p95': .4},
                 'reflection': {'chroma': .2, 'chroma_p95': .3},
                 'background': {'chroma': .1, 'luma': .3}}
        wrong = copy.deepcopy(prior)
        wrong['source']['chroma'] = .2
        wrong['background']['chroma'] = .05
        with self.assertRaisesRegex(engine.SkillError, '1.6'):
            self.module.check_signature('rain-ink-neon', prior, wrong, .55)

    def test_real_desert_hue_moves_sand_not_protect(self):
        evidence = self.prepare()
        plan = self.plan(evidence)
        with patch.object(self.module, 'foundation_filter', return_value='null'):
            for name, strength in (('base.png', 0), ('result.png', .55)):
                mix = strength ** .52 if strength else 0
                command, _ = self.module.command(plan, 'null', self.root / name, mix)
                engine.run(command)
            report = self.module.measure(plan, self.root / 'base.png', self.root / 'result.png')
        self.assertTrue(report['signature_checks_passed'])

    def test_each_of_five_spatial_operators_renders_real_pixels(self):
        def box(x0, y0, x1, y1): return [[[x0,y0],[x1,y0],[x1,y1],[x0,y1]]]
        cases = {
            'plateau-sacred-light': {'ridge': box(0,0,63,1), 'ground': box(0,4,30,31), 'shadow': box(33,4,63,31)},
            'obsidian-gold-realm': {'specular': box(0,0,7,7), 'background': box(10,0,63,31)},
            'rain-ink-neon': {'source': box(0,0,15,15), 'reflection': box(0,18,63,31), 'background': box(20,0,63,15)},
            'desert-silent-rose': {'sand': box(0,0,30,31), 'distant': box(33,0,63,31)},
            'gilded-autumn-city': {'warm': box(0,0,19,31), 'air': box(22,0,40,31), 'shade': box(43,0,63,31)},
        }
        for style, regions in cases.items():
            with self.subTest(style=style):
                spec = copy.deepcopy(self.spec)
                spec['style_id'] = style
                spec['segments'][0]['regions'] = {**regions, 'protect': []}
                evidence = self.module.prepare(self.info, spec, self.root / style, 'null')
                plan = self.plan(evidence)
                plan['style']['id'] = style
                with patch.object(self.module, 'foundation_filter', return_value='null'):
                    for name, strength in (('base', 0), ('result', .55)):
                        recipe = engine.find_recipe(engine.load_catalog(), style, 'photo', allow_manual=True)
                        mix = engine.strength_execution_for(recipe, strength)[1]
                        command, _ = self.module.command(plan, 'null', self.root / f'{style}-{name}.png', mix)
                        engine.run(command)
                    report = self.module.measure(plan, self.root / f'{style}-base.png', self.root / f'{style}-result.png')
                self.assertTrue(report['signature_checks_passed'])

    def test_vermilion_two_photo_sequence_suppression_and_release(self):
        peer = self.root / 'peer.png'
        engine.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=0xc92a40:s=64x32', '-frames:v', '1', str(peer)])
        items = [{'index': 0, 'path': str(self.source), 'sha256': engine.sha256(self.source), 'phase': 'suppress'},
                 {'index': 1, 'path': str(peer), 'sha256': engine.sha256(peer), 'phase': 'release'}]
        for index, item in enumerate(items):
            with self.subTest(index=index):
                source = {**self.info, 'path': item['path'], 'sha256': item['sha256']}
                spec = copy.deepcopy(self.spec)
                spec.update({'style_id': 'vermilion-snow-dream', 'source_sha256': item['sha256'],
                             'photo_sequence': {'items': items, 'current_index': index}})
                spec['segments'][0]['regions'] = {'red': [[[0,0],[5,0],[5,7],[0,7]]],
                                                  'background': [[[10,0],[63,0],[63,31],[10,31]]], 'protect': []}
                evidence = self.module.prepare(source, spec, self.root / f'sequence-{index}', 'null')
                plan = self.plan(evidence)
                plan['source'], plan['style']['id'] = source, 'vermilion-snow-dream'
                with patch.object(self.module, 'foundation_filter', return_value='null'):
                    for name, strength in (('base', 0), ('result', .55)):
                        command, _ = self.module.command(plan, 'null', self.root / f'{index}-{name}.png', strength)
                        engine.run(command)
                    report = self.module.measure(plan, self.root / f'{index}-base.png', self.root / f'{index}-result.png')
                self.assertTrue(report['signature_checks_passed'])


if __name__ == '__main__':
    unittest.main()
