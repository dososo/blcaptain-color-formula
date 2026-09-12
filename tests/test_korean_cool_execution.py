"""韩系普通入口合同：先保护人物，再组织环境；不能回落旧全局链。"""
import importlib
from contextlib import ExitStack
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import blcaptain_color as engine
import suggest


class KoreanExecutionContract(unittest.TestCase):
    def setUp(self):
        self.recipe = engine.recipe_for_media(engine.find_recipe(
            engine.load_catalog(), 'korean-cool', 'photo'), 'photo')

    def test_normal_preview_uses_protected_renderer(self):
        module = importlib.import_module('korean_cool_execution')
        for media, entry in [('photo', suggest._render_photo_preflight_file),
                             ('video', suggest._render_video_preflight_file)]:
            with self.subTest(media=media), patch.object(module, 'preview',
                    return_value=('新保护链', {})) as render:
                self.assertEqual(entry(self.recipe, {'media_type': media}, .55,
                                       Path('unused.png')), ('新保护链', {}))
                render.assert_called_once()

    def test_global_filter_cannot_impersonate_protection(self):
        with patch.object(engine, 'adaptive_primary_grade', return_value={}), \
                patch.object(engine, 'highlight_protection_for', return_value={}), \
                patch.object(engine, 'build_filter', return_value='旧全局'), \
                self.assertRaisesRegex(engine.SkillError, '保护'):
            suggest._filtergraph_for(self.recipe, {}, .55)

    def test_old_global_wash_is_not_active_recipe(self):
        self.assertEqual(self.recipe.get('execution_role'), 'person-protected-environment')
        self.assertEqual(self.recipe['parameters']['gamma'], 1.0)
        self.assertEqual(self.recipe['parameters']['saturation'], 1.0)

    def test_protected_formal_requires_separate_confirmation(self):
        module = importlib.import_module('korean_cool_execution')
        with self.assertRaisesRegex(engine.SkillError, 'confirm-local'):
            module.confirm({'style': {'id': 'korean-cool'}}, None)

    def test_unsupported_composition_is_explicit(self):
        module = importlib.import_module('korean_cool_execution')
        plan = {'composition': {'action': 'crop'}, 'source': {'media_type': 'photo'}}
        with self.assertRaisesRegex(engine.SkillError, '裁切'):
            module.check_options(plan)

    def test_unrelated_fingerprint_does_not_gain_empty_field(self):
        # 条件式字段不改变其他风格已有方案的指纹。
        source = Path(engine.__file__).read_text()
        self.assertTrue('if "korean_cool_protection" in plan:' in source)

    def test_physics_probe_uses_same_white_protection_and_keeps_black(self):
        module = importlib.import_module('korean_cool_execution')
        plan = {'source': {'media_type': 'photo', 'width': 4, 'height': 1}, 'render_mix': .65}
        colors = module.probe_colors(plan, [(0, 0, 0), (128, 128, 128),
                                           (220, 220, 220), (255, 255, 255)], False)
        self.assertEqual(colors[0], (0, 0, 0))
        self.assertGreater(colors[1][2], colors[1][0])
        self.assertEqual(colors[2:], [(220, 220, 220), (255, 255, 255)])

    def test_region_contract_is_declared_without_global_wash_targets(self):
        self.assertEqual(self.recipe['visual_targets']['colorfulness'], 'preserve')
        self.assertEqual(self.recipe['visual_targets']['subject_separation'], 'increase')
        self.assertIn('direction_scope', engine.style_payload(self.recipe, 'photo'))

    def test_physics_probe_uses_shared_neutral_white_weights(self):
        module = importlib.import_module('korean_cool_execution')
        protection = importlib.import_module('korean_cool_protection')
        plan = {'source': {'media_type': 'photo', 'width': 1, 'height': 1}, 'render_mix': .65}
        with patch.object(protection, 'neutral_white_weights', return_value=bytes([255])) as weights:
            self.assertEqual(module.probe_colors(plan, [(128, 128, 128)], False), [(128, 128, 128)])
            self.assertEqual(weights.call_count, 2)
            self.assertTrue(all(call.args == (bytes([128, 128, 128]),) for call in weights.call_args_list))

    def test_preview_and_suggest_pass_foundation_to_prepare(self):
        module = importlib.import_module('korean_cool_execution')
        protection = importlib.import_module('korean_cool_protection')
        source = {'media_type': 'photo', 'luminance_diagnosis': {'p1': .01, 'p50': .03, 'p99': .85}}
        expected = module.foundation_filter({'source': source})
        for owner, name, args in (
                (module, 'preview', (self.recipe, source, .55, Path('unused.png'))),
                (suggest, 'run_execution_preflight', (self.recipe, source, .55))):
            with self.subTest(entry=name):
                original = getattr(owner, name)
                with patch.object(protection, 'prepare', return_value={'fixture_only': True}) as prepare, \
                        patch.object(owner, name, return_value={}):
                    original(*args)
                self.assertEqual(len(prepare.call_args.args), 3)
                self.assertEqual(prepare.call_args.args[2], expected)

    def test_probe_preserves_whites_from_source_or_real_foundation(self):
        module = importlib.import_module('korean_cool_execution')
        for profile in ('srgb', 'display-p3'):
            for median, value in ((.03, 150), (.9, 180)):
                with self.subTest(profile=profile, median=median):
                    plan = {'source': {'media_type': 'photo', 'width': 1, 'height': 1,
                                      'color': {'profile': profile},
                                      'luminance_diagnosis': {'p1': .01, 'p50': median, 'p99': .85}},
                            'render_mix': .55}
                    raw = bytes([value] * 3)
                    baseline = module.foundation_filter(plan)
                    reference = subprocess.run([
                        engine.require_tool('ffmpeg'), '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                        '-s', '1x1', '-i', 'pipe:0', '-filter_complex',
                        f'[0:v]{baseline},format=gbrp16le,format=rgb24[out]', '-map', '[out]',
                        '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                        input=raw, capture_output=True, check=True).stdout
                    self.assertEqual(len(reference), 3)
                    actual = module.probe_colors(plan, [(value, value, value)])[0]
                    self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, reference)), 1)

    def test_p3_probe_white_inputs_match_foundation_then_srgb_analysis(self):
        module = importlib.import_module('korean_cool_execution')
        protection = importlib.import_module('korean_cool_protection')
        colors = [(175, 160, 140), (128, 170, 195), (200, 185, 170)]
        pixels = bytes(channel for color in colors for channel in color)
        plan = {'source': {'media_type': 'photo', 'width': 3, 'height': 1,
                          'color': {'profile': 'display-p3'},
                          'luminance_diagnosis': {'p1': .01, 'p50': .03, 'p99': .85}},
                'render_mix': .55}
        analysis = ('format=gbrp16le,zscale=primariesin=smpte432:transferin=iec61966-2-1:'
                    'matrixin=gbr:rangein=full:primaries=709:transfer=13:matrix=gbr:range=full,format=rgb24')
        expected = []
        for baseline in ('null', module.foundation_filter(plan)):
            expected.append(subprocess.run([
                engine.require_tool('ffmpeg'), '-v', 'error', '-filter_complex_threads', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '3x1', '-i', 'pipe:0',
                '-filter_complex', f'[0:v]{baseline},{analysis}[out]', '-map', '[out]',
                '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                input=pixels, capture_output=True, check=True).stdout)
        self.assertNotEqual(expected[0], pixels)
        self.assertNotEqual(expected[0], expected[1])
        with patch.object(protection, 'neutral_white_weights', wraps=protection.neutral_white_weights) as weights:
            module.probe_colors(plan, colors)
        self.assertEqual([call.args[0] for call in weights.call_args_list], expected)

    def test_fractional_requested_strength_is_previewed_with_live_baseline(self):
        # 隔离昂贵渲染，仅检查真实调度与临时基线生命周期，不代表媒体通过。
        recipe = {**self.recipe, '_korean_protection': {'fixture_only': True}}
        strength = engine.normalize_strength('55.4')['normalized']
        for media in ('photo', 'video'):
            with self.subTest(media=media):
                calls, baselines = [], []

                def render_baseline(recipe, source, level, output):
                    self.assertEqual(level, 0.0)
                    output.write_bytes(b'fixture-foundation')
                    baselines.append(output)

                def preview(recipe, source, level, baseline_path=None):
                    self.assertIsNotNone(baseline_path)
                    self.assertTrue(baseline_path.is_file(), '请求档预演必须仍能读取同一基线')
                    self.assertEqual(baseline_path, baselines[0])
                    calls.append(level)
                    return {'predicted_delta': level * .1, 'creative_delta_e_ok': level * .1,
                            'direction_failures': [], 'preview_size': '工程夹具',
                            'working_profile': '工程夹具'}

                with patch.object(suggest, '_render_' + media + '_preflight_file',
                                  side_effect=render_baseline), \
                        patch.object(suggest, '_run_' + media + '_preflight_once',
                                     side_effect=preview):
                    result = suggest.run_execution_preflight(recipe, {'media_type': media}, strength)
                self.assertEqual(calls, [.3, .55, .8, .554])
                self.assertEqual(result['status'], 'executable')
                self.assertAlmostEqual(result['predicted_delta'], .0554)
                self.assertFalse(baselines[0].exists())

    def test_receipt_keeps_perceptual_minimum_separate_from_spatial_threshold(self):
        # 真正运行门限与回执组装；像素/空间测量为控制流夹具，不是验收证据。
        validation = importlib.import_module('korean_cool_validation')
        diagnose = importlib.import_module('diagnose')
        plan = {
            'source': {'media_type': 'photo', 'duration': 0, 'color': {'profile': 'srgb'}},
            'style': engine.style_payload(self.recipe, 'photo'),
            'strength': .55, 'effective_strength': .55,
            'composition': {'action': 'none', 'rotate_deg': 0, 'crop': None},
            'korean_cool_protection': {'fixture_only': True},
        }
        protected = {'passed': True, 'signature': {
            'baseline': {'protected_chroma': .1, 'spatial_cool_partition': 0},
            'candidate': {'protected_chroma': .1, 'spatial_cool_partition': .1}}}

        def samples(path, *args):
            return bytes([136 if path.name == 'output.png' else 128] * 3) * 4

        for minimum in (0.0, .004):
            with self.subTest(minimum=minimum), ExitStack() as stack:
                stack.enter_context(patch.object(validation, 'measure', return_value=protected))
                stack.enter_context(patch.object(engine, 'sample_rgb', side_effect=samples))
                stack.enter_context(patch.object(engine, 'measure', return_value={}))
                stack.enter_context(patch.object(engine, 'evaluate_direction', return_value={
                    'checks': {}, 'failed_dimensions': [], 'status': 'passed'}))
                stack.enter_context(patch.object(engine, 'impact_profile', return_value={}))
                stack.enter_context(patch.object(engine, 'visual_grammar_gates', return_value={
                    'blocking_failures': [], 'gates': {}}))
                stack.enter_context(patch.object(diagnose, 'diagnose', side_effect=RuntimeError('工程夹具')))
                result = engine.validate_visual_impact(
                    Path('source.png'), Path('output.png'), plan, minimum_override=minimum,
                    creative_baseline=Path('foundation.png'))
                self.assertEqual(result['required_min_delta_e'], minimum)
                self.assertEqual(result['directional_audit']['checks']['subject_separation']
                                 ['required_delta'], .018)

    def retained_preflight(self, folder, media='photo', style='korean-cool'):
        recipe = engine.recipe_for_media(engine.find_recipe(
            engine.load_catalog(), style, media), media)
        recipe.update(_korean_protection={'fixture_only': True}, _korean_preview_dir=str(folder))
        source = {'path': 'fixture-source', 'media_type': media, 'duration': 1,
                  'width': 64, 'height': 48, 'color': {'profile': 'srgb'}}
        calls, temporary = [], []

        def render(recipe, source, strength, output):
            calls.append(strength)
            temporary.append(output)
            output.write_text(str(strength))
            return '工程夹具', {}

        def measure(source, output, *args, **kwargs):
            return {'mean_delta_e_ok': float(output.read_text()) * .1,
                    'directional_audit': {'failed_dimensions': []}}

        with patch.object(suggest, '_render_' + media + '_preflight_file', side_effect=render), \
                patch.object(engine, 'validate_visual_impact', side_effect=measure), \
                patch.object(suggest, '_mean_delta_between', side_effect=lambda a, b, s:
                             float(b.read_text()) * .1):
            report = suggest.run_execution_preflight(recipe, source, .554)
        return report, calls, temporary

    def test_plan_previews_persist_exact_bytes_without_extra_render(self):
        for media, suffix in (('photo', '.png'), ('video', '.mp4')):
            with self.subTest(media=media), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder) / 'previews'
                report, calls, temporary = self.retained_preflight(directory, media)
                self.assertIn('preview_artifacts', report)
                artifacts = report['preview_artifacts']
                self.assertEqual(artifacts['status'], 'internal-preview-not-final')
                records = [artifacts['baseline'], *artifacts['levels']]
                self.assertEqual([item['strength'] for item in records], [0, .3, .55, .8, .554])
                self.assertEqual(calls, [0, .3, .55, .8, .554])
                self.assertFalse(any(path.exists() for path in temporary))
                for item in records:
                    path = Path(item['path'])
                    self.assertEqual(path.parent, directory.resolve())
                    self.assertEqual(path.suffix, suffix)
                    self.assertEqual(path.read_text(), str(float(item['strength'])))
                    self.assertEqual(item['sha256'], engine.sha256(path))

    def test_retained_previews_never_overwrite_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'previews'
            report, _, _ = self.retained_preflight(directory)
            self.assertIn('preview_artifacts', report)
            originals = {path: path.read_bytes() for path in directory.iterdir()}
            repeated, _, _ = self.retained_preflight(directory)
            self.assertEqual(repeated['status'], 'blocked')
            self.assertEqual({path: path.read_bytes() for path in directory.iterdir()}, originals)

    def test_other_styles_keep_temporary_previews(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'previews'
            report, _, temporary = self.retained_preflight(directory, style='cream-soft')
            self.assertNotIn('preview_artifacts', report)
            self.assertFalse(directory.exists())
            self.assertFalse(any(path.exists() for path in temporary))


if __name__ == '__main__':
    unittest.main()
