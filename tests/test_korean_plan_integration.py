"""普通韩系完整控制流合同；替代昂贵分析，不冒充分割模型或审美验收。"""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import blcaptain_color as engine  # noqa: E402
import korean_cool_execution as korean  # noqa: E402
import korean_cool_protection as protection  # noqa: E402
import semantic_backend  # noqa: E402
import suggest  # noqa: E402


def legacy_fingerprint(plan):
    """修改前 HEAD 的字段与默认值；测试不依赖 git 或动态读取待测函数。"""
    required = (
        "source", "style", "strength", "effective_strength", "render_mix",
        "visual_brief", "composition", "attention_map", "primary_grade",
        "review_focus", "parameters", "input_assumption", "color_pipeline",
        "capability_profile", "output_path", "comparison_path", "receipt_path",
    )
    defaults = {
        "input_truth": None, "foundation_grade": None, "foundation_hash": None,
        "monotonicity": None, "tone_curve": None, "hsl_bands": [],
        "highlight_protection": None, "adjustments": {}, "shot_grade": None,
        "local_grade": None, "skin_strength_decision": None, "assume_sdr": False,
    }
    payload = {key: plan[key] for key in required}
    payload.update({key: plan.get(key, value) for key, value in defaults.items()})
    payload["renderer"] = "ffmpeg-v3.0"
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


class KoreanPlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="korean-plan-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_path = self.root / "source.png"
        self.source_path.write_bytes(b"synthetic-source-for-control-flow")
        self.source = {
            "path": str(self.source_path), "sha256": engine.sha256(self.source_path),
            "media_type": "photo", "width": 64, "height": 48,
            "duration": 0.0, "has_audio": False,
            "color": {"profile": "srgb", "label": "sRGB", "support": "direct",
                      "primaries": "bt709", "transfer": "iec61966-2-1", "matrix": "gbr"},
            "luminance_diagnosis": {"p1": .03, "p50": .28, "p99": .8},
        }
        self.backend = {"id": "fixture-only", "version": "1"}
        self.events = []
        self.sequence = 0

    def args(self, extra=(), style="korean-cool"):
        self.sequence += 1
        output = self.root / f"output-{self.sequence}"
        self.last_args = engine.parser().parse_args([
            "plan", "--input", str(self.source_path), "--style", style,
            "--strength", "55", "--output-dir", str(output),
            "--plan-out", str(output / "plan.json"), *extra,
        ])
        return self.last_args

    def prepare_fixture(self, source, directory, foundation_filter=None):
        self.events.append("prepare")
        directory.mkdir(parents=True, exist_ok=False)
        self.evidence_dir = directory
        (directory / 'masks').mkdir()
        mask = directory / "masks" / "mask.pgm"
        mask.write_bytes(b"P5\n64 48\n255\n" + bytes([0, 128, 255]) * 1024)
        evidence = {
            "schema_version": protection.SCHEMA, "status": "ready",
            "source_binding": protection._source_binding(source),
            "implementation": protection._implementation(), "backend": self.backend,
            "analysis_dimensions": [64, 48],
            "mask": {"path": str(mask), "sha256": engine.sha256(mask),
                     "encoding": protection.ENCODING, "dimensions": [64, 48],
                     "frame_count": 1, "timeline": None},
            "timeline": None, "person_coverage": [.5], "neutral_white_coverage": [.1],
            "neutral_white_mean_weight": [.1], "foundation_white_mean_weight": [.1],
            "foundation_analysis_sha256": [hashlib.sha256(b'fixture-foundation-analysis').hexdigest()],
            "neutral_white_rule": {"method": "smoothstep-product", "luma_transition": [.56, .68],
                                   "chroma_transition": [.11, .17], "original_core_preserved": True,
                                   "domains": ["source", "foundation"]},
            "person_mask_sources": ["fixture-only"],
            "method": "independent-per-frame-segmentation", "human_review": "pending",
            "boundary": "工程夹具，不证明人物后端能力",
        }
        if foundation_filter is not None:
            evidence['foundation_filter'] = foundation_filter
        evidence["fingerprint"] = protection._fingerprint(evidence)
        self.prepared = evidence
        return evidence

    def make(self, status="executable", extra=(), style="korean-cool"):
        def preflight(recipe, source, strength):
            self.events.append("preflight")
            self.assertEqual(source, self.source)
            self.assertEqual(strength, engine.normalize_strength(self.last_args.strength)['normalized'])
            if style == "korean-cool":
                self.assertIs(recipe["_korean_protection"], self.prepared)
            result = {"status": status, "reason": "测试预演结果",
                      "monotonicity": {"status": "passed", "strengths": [.3, .55, .8]}}
            if style == "korean-cool" and recipe.get('_korean_preview_dir'):
                directory = Path(recipe['_korean_preview_dir'])
                self.assertEqual(directory, self.evidence_dir.parent / 'previews')
                directory.mkdir()
                records = []
                for level in [0.0, *sorted({.3, .55, .8, strength})]:
                    path = directory / f'fixture-{level}.png'
                    path.write_text(f'本次预演夹具 {level}')
                    records.append({'path': str(path), 'sha256': engine.sha256(path), 'strength': level})
                result['preview_artifacts'] = {'status': 'internal-preview-not-final',
                                               'baseline': records[0], 'levels': records[1:]}
            return result
        with patch.object(engine, "inspect_media", return_value=copy.deepcopy(self.source)), \
                patch.object(protection, "prepare", side_effect=self.prepare_fixture), \
                patch.object(suggest, "run_execution_preflight", side_effect=preflight), \
                patch.object(semantic_backend, "capability_report", return_value={
                    "active_backend": {"available": False, "reason": "测试未启用"}}):
            return engine.make_plan(self.args(extra, style))

    @contextmanager
    def protection_dependencies(self):
        # 保留真实 evidence/source/mask/implementation 指纹检查，只隔离系统后端与媒体探针。
        with patch.object(protection, "_backend", return_value=(None, self.backend)), \
                patch.object(protection, "_probe", return_value={"streams": [
                    {"pix_fmt": "gray", "width": 64, "height": 48}]}):
            yield

    def render_args(self, plan, local=korean.STRATEGY):
        plan["plan_id"] = engine.plan_fingerprint(plan)
        path = self.root / "render-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        flags = ["render", "--plan", str(path), "--confirm-plan", plan["plan_id"], "--no-palette"]
        if local is not None:
            flags += ["--confirm-local", local]
        return engine.parser().parse_args(flags)

    def assert_rejected_before_writes(self, plan, reason, local=korean.STRATEGY):
        args = self.render_args(plan, local)
        with self.protection_dependencies(), \
                patch.object(engine, "ensure_new", side_effect=AssertionError("必须在正式输出准备前拒绝")), \
                patch.object(engine, "render_photo", side_effect=AssertionError("不应进入照片渲染")), \
                patch.object(engine, "render_video", side_effect=AssertionError("不应进入视频渲染")):
            with self.assertRaisesRegex(engine.SkillError, reason):
                engine.render_plan(args)
        for key in ("output_path", "comparison_path", "receipt_path"):
            self.assertFalse(Path(plan[key]).exists())

    def test_make_plan_prepares_current_source_before_same_evidence_preflight(self):
        first = self.make()
        self.assertEqual(self.events, ["prepare", "preflight"])
        self.assertEqual(first["korean_cool_protection"], self.prepared)
        self.assertEqual(first["korean_execution_sha256"], korean.implementation_hash())
        self.assertEqual(first["plan_id"], engine.plan_fingerprint(first))
        saved = json.loads(Path(self.last_args.plan_out).read_text())
        self.assertEqual(saved, first)
        self.source_path.write_bytes(b"different-source-for-next-plan")
        self.source["sha256"] = engine.sha256(self.source_path)
        second = self.make()
        self.assertNotEqual(first["korean_cool_protection"]["source_binding"]["sha256"],
                            second["korean_cool_protection"]["source_binding"]["sha256"])
        self.assertNotEqual(first["korean_cool_protection"]["mask"]["path"],
                            second["korean_cool_protection"]["mask"]["path"])
        self.assertFalse(second["confirmed"])

    def test_make_plan_passes_real_foundation_to_protection(self):
        plan = self.make()
        self.assertIn('foundation_filter', plan['korean_cool_protection'])
        self.assertEqual(plan['korean_cool_protection']['foundation_filter'], korean.foundation_filter(plan))

    def test_missing_or_changed_foundation_binding_rejects_recomputed_plan(self):
        for damage in ('missing', 'changed'):
            with self.subTest(damage=damage):
                plan = self.make()
                evidence = plan['korean_cool_protection']
                if damage == 'missing':
                    evidence.pop('foundation_filter', None)
                else:
                    evidence['foundation_filter'] = 'null'
                evidence['fingerprint'] = protection._fingerprint({
                    key: value for key, value in evidence.items() if key != 'fingerprint'})
                self.assert_rejected_before_writes(plan, 'Foundation|基础校正')

    def test_source_analysis_and_explicit_foundation_cannot_diverge_from_evidence(self):
        for damage in ('source-analysis', 'plan-foundation'):
            with self.subTest(damage=damage):
                plan = self.make()
                evidence = plan['korean_cool_protection']
                evidence['foundation_filter'] = korean.foundation_filter(plan)
                evidence['fingerprint'] = protection._fingerprint({
                    key: value for key, value in evidence.items() if key != 'fingerprint'})
                if damage == 'source-analysis':
                    plan['source']['luminance_diagnosis']['p50'] = .03
                else:
                    plan['foundation_grade']['gamma'] = 1.2
                with self.protection_dependencies(), self.assertRaisesRegex(
                        engine.SkillError, 'Foundation|基础校正'):
                    korean.validate(plan)

    def test_risky_or_blocked_preflight_never_writes_confirmable_plan(self):
        for status in ("risky", "blocked"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(engine.SkillError, "预演|资格"):
                    self.make(status=status)
                self.assertFalse(Path(self.last_args.plan_out).exists())

    def test_unsupported_options_reject_before_prepare_or_preflight(self):
        crop = {"action": "crop", "crop": {"x": 0, "y": 0, "width": .8, "height": .8},
                "rotate_deg": 0, "rationale": "测试裁切"}
        rotation = {**crop, "action": "crop_rotate", "rotate_deg": 1}
        attention = {"mode": "radial", "center_x": .5, "center_y": .5, "radius": .7,
                     "strength": .1, "rationale": "测试注意力"}
        cases = [("--shot-grade",)]
        for index, (flag, payload) in enumerate((
                ("--composition-file", crop), ("--composition-file", rotation),
                ("--attention-file", attention), ("--adjustments-file", {"shadow_lift": .02}))):
            path = self.root / f"option-{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            cases.append((flag, str(path)))
        for flags in cases:
            with self.subTest(flags=flags), \
                    patch.object(engine, "inspect_media", return_value=self.source), \
                    patch.object(protection, "prepare", side_effect=AssertionError("不应准备保护")), \
                    patch.object(suggest, "run_execution_preflight", side_effect=AssertionError("不应预演")):
                args = self.args(flags)
                with self.assertRaisesRegex(engine.SkillError, "暂不.*组合"):
                    engine.make_plan(args)
                self.assertFalse(Path(args.output_dir).exists())

    def test_old_korean_plan_without_new_protocol_is_rejected(self):
        plan = self.make()
        for key in ("korean_cool_protection", "korean_execution_sha256", "execution_preflight"):
            plan.pop(key)
        self.assert_rejected_before_writes(plan, "协议.*缺失|协议.*更新")

    def test_missing_or_wrong_local_confirmation_is_rejected(self):
        plan = self.make()
        for local in (None, "skin-protect", "korean-cool"):
            with self.subTest(local=local):
                self.assert_rejected_before_writes(copy.deepcopy(plan), "独立人物保护|独立.*确认", local)

    def test_non_korean_plan_cannot_smuggle_protection_without_local_confirmation(self):
        protected = self.make()
        other = self.make(style="cream-soft")
        # 使用当前能力报告，避免因测试期间的后端替身消失而提前误挡此反例。
        other["capability_profile"] = engine.capability_profile_for(
            "photo", False, other.get("local_grade"))
        full = {key: protected[key] for key in (
            "korean_cool_protection", "korean_execution_sha256", "execution_preflight")}
        for extra in (full, {"korean_cool_protection": None}, {"korean_cool_protection": {}},
                      {"korean_execution_sha256": protected["korean_execution_sha256"]},
                      {"korean_execution_sha256": None}):
            with self.subTest(fields=list(extra), empty=not any(extra.values())):
                self.assert_rejected_before_writes(
                    {**copy.deepcopy(other), **copy.deepcopy(extra)}, "韩系|协议", local=None)

    def test_tampered_evidence_payload_rejects_even_after_recomputing_plan_id(self):
        plan = self.make()
        plan["korean_cool_protection"]["person_coverage"] = [1.0]
        self.assert_rejected_before_writes(plan, "证据内容发生变化")

    def test_changed_mask_rejects_even_after_recomputing_plan_id(self):
        plan = self.make()
        Path(plan["korean_cool_protection"]["mask"]["path"]).write_bytes(b"changed-mask")
        self.assert_rejected_before_writes(plan, "蒙版文件.*改变")

    def test_each_execution_module_change_rejects_recomputed_plan_id(self):
        plan = self.make()
        original_sha = engine.sha256
        for name in ("korean_cool_execution.py", "korean_cool_validation.py", "blcaptain_color.py", "suggest.py"):
            with self.subTest(module=name):
                def changed_sha(path):
                    return "0" * 64 if Path(path).name == name else original_sha(path)
                with patch.object(engine, "sha256", side_effect=changed_sha):
                    self.assertNotEqual(korean.implementation_hash(), plan["korean_execution_sha256"])
                    self.assert_rejected_before_writes(copy.deepcopy(plan), "协议.*更新")

    def test_failed_second_evidence_check_cleans_staged_group(self):
        plan = self.make()
        args = self.render_args(plan)
        source_before = self.source_path.read_bytes()
        staged = [engine.partial_path(Path(plan[key])) for key in ("output_path", "comparison_path")]

        def rendering(current, vf, baseline):
            for path in staged:
                path.write_bytes(b"staged-control-flow-result")
            Path(current["korean_cool_protection"]["mask"]["path"]).write_bytes(b"changed-during-render")
            return [], [], {"status": "passed"}, {"status": "passed"}, *staged

        with self.protection_dependencies(), \
                patch.object(engine, "render_photo", side_effect=rendering), \
                patch.object(korean, "validate", wraps=korean.validate) as validation:
            with self.assertRaisesRegex(engine.SkillError, "蒙版文件.*改变"):
                engine.render_plan(args)
            self.assertEqual(validation.call_count, 2)
        self.assertFalse(any(path.exists() for path in staged))
        for key in ("output_path", "comparison_path", "receipt_path"):
            self.assertFalse(Path(plan[key]).exists())
        self.assertEqual(self.source_path.read_bytes(), source_before)

    def test_non_korean_fingerprint_matches_legacy_field_contract(self):
        plan = self.make(style="natural-clean")
        self.assertNotIn("korean_cool_protection", plan)
        self.assertEqual(self.events, ["preflight"])
        self.assertEqual(engine.plan_fingerprint(plan), legacy_fingerprint(plan))
        minimal = copy.deepcopy(plan)
        for key in ("input_truth", "foundation_grade", "foundation_hash", "monotonicity", "local_grade"):
            minimal.pop(key, None)
        self.assertEqual(engine.plan_fingerprint(minimal), legacy_fingerprint(minimal))

    def test_plan_capability_is_detected_l2_not_executed_or_l3_tracking(self):
        plan = self.make()
        for media in ("photo", "video"):
            with self.subTest(media=media):
                capability = engine.capability_profile_for(media, False, plan["local_grade"])
                self.assertEqual(capability["semantic_local"]["level"], "L2")
                self.assertEqual(capability["semantic_local"]["status"], "detected")
                self.assertEqual(capability["temporal_semantic"]["status"], "unavailable")
        self.assertTrue(plan["local_grade"]["confirmation_required"])
        self.assertNotIn("confirmed", plan["local_grade"])

    def test_plan_binds_current_preview_paths_and_hashes(self):
        plan = self.make(extra=('--strength', '55.4'))
        self.assertIn('preview_artifacts', plan['execution_preflight'])
        artifacts = plan['execution_preflight']['preview_artifacts']
        self.assertEqual({item['strength'] for item in artifacts['levels']}, {.3, .55, .8, .554})
        with self.protection_dependencies():
            korean.confirm(plan, korean.STRATEGY)
        altered = copy.deepcopy(plan)
        altered['execution_preflight']['preview_artifacts']['baseline']['sha256'] = '0' * 64
        self.assertNotEqual(engine.plan_fingerprint(altered), plan['plan_id'])

    def test_confirm_rejects_missing_or_changed_retained_preview_before_writes(self):
        for damage in ('absent', 'missing-file', 'changed-bytes', 'missing-level', 'missing-requested'):
            with self.subTest(damage=damage):
                plan = self.make(extra=('--strength', '55.4'))
                artifacts = plan['execution_preflight']['preview_artifacts']
                if damage == 'absent':
                    plan['execution_preflight'].pop('preview_artifacts')
                elif damage == 'missing-file':
                    Path(artifacts['baseline']['path']).unlink()
                elif damage == 'changed-bytes':
                    Path(artifacts['levels'][0]['path']).write_bytes(b'changed-preview')
                elif damage == 'missing-level':
                    artifacts['levels'] = [item for item in artifacts['levels'] if item['strength'] != .3]
                else:
                    artifacts['levels'] = [item for item in artifacts['levels'] if item['strength'] != .554]
                self.assert_rejected_before_writes(plan, '预演')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要真实媒体工具')
    def test_real_prepare_layout_confirms_photo_and_video_previews(self):
        # 只替代人物模型；prepare、目录布局、蒙版编码、源及预演哈希检查均真实。
        class PersonBackend:
            def analyze(self, frame, directory):
                width, height = protection.memory._dimensions(frame)
                path = directory / 'person.pgm'
                protection.memory._write_pgm(path, width, height,
                                             bytes([0, 64, 128, 255]) * (width * height // 4))
                return {'classes': {'person': {'mask_path': str(path), 'coverage': .5}},
                        'backend': {'backend': 'test-only'}}

        image = self.root / 'real-layout.ppm'
        image.write_bytes(b'P6\n32 16\n255\n' + bytes([70, 90, 110]) * 32 * 16)
        video = self.root / 'real-layout.mp4'
        subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-n', '-loop', '1',
                        '-i', str(image), '-r', '2', '-frames:v', '2', '-c:v', 'libx264',
                        '-vf', 'format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709',
                        '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                        '-colorspace', 'bt709', '-color_range', 'tv', str(video)],
                       check=True, capture_output=True)
        for media, path in (('photo', image), ('video', video)):
            with self.subTest(media=media), patch.object(protection, '_backend', return_value=(
                    PersonBackend(), {'backend': 'test-only'})):
                source = {'path': str(path), 'sha256': engine.sha256(path), 'media_type': media,
                          'width': 32, 'height': 16,
                          'luminance_diagnosis': {'p1': .01, 'p50': .28, 'p99': .85},
                          'color': {'profile': 'srgb' if media == 'photo' else 'rec709-sdr',
                                    'support': 'direct'}}
                evidence_dir = (self.root / media / 'evidence').resolve()
                evidence = protection.prepare(source, evidence_dir, korean.foundation_filter({'source': source}))
                mask = Path(evidence['mask']['path'])
                self.assertEqual(mask.parent, evidence_dir / 'masks' if media == 'photo' else evidence_dir)
                previews = evidence_dir.parent / 'previews'
                previews.mkdir()
                records = []
                for level in (0, .3, .55, .8):
                    preview = previews / f'fixture-{level}.{media}'
                    preview.write_text(f'内部预演路径测试 {level}')
                    records.append({'path': str(preview), 'sha256': engine.sha256(preview), 'strength': level})
                plan = {'source': source, 'style': {'id': 'korean-cool'}, 'strength': .55,
                        'korean_cool_protection': evidence,
                        'korean_execution_sha256': korean.implementation_hash(),
                        'execution_preflight': {'status': 'executable', 'preview_artifacts': {
                            'status': 'internal-preview-not-final', 'baseline': records[0], 'levels': records[1:]}}}
                try:
                    korean.confirm(plan, korean.STRATEGY)
                except engine.SkillError as error:
                    self.fail(f'真实 prepare 的 {media} 目录结构被错误拒绝：{error}')


if __name__ == "__main__":
    unittest.main()
