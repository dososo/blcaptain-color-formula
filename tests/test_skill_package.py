"""候选包工程合同；临时夹具不是实际Skill安装验收。"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_skill_package as package
from scripts import style_atlas_sources as atlas


class SkillPackageTests(unittest.TestCase):
    def fixture(self, root):
        root.mkdir()
        for name in package.ROOT_FILES:
            (root / name).write_text('current\n' if name == '.blcaptain-interpreter' else '工程夹具\n', encoding='utf-8')
        (root / 'scripts').mkdir()
        (root / 'scripts/blcaptain_color.py').write_text('print("工程夹具")\n')
        return root

    def test_allowed_text_is_hashed_and_forbidden_trees_are_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            for name in ('evidence', 'tasks', 'tests', '.git'):
                (root / name).mkdir()
                (root / name / 'private.txt').write_text('不能进入包')
            (root / '.DS_Store').write_bytes(b'private')
            report = package.audit(root)
            self.assertEqual(report['blockers'], [])
            names = {x['path'] for x in report['included']}
            self.assertIn('scripts/blcaptain_color.py', names)
            self.assertFalse(any(n.startswith(('tasks/', 'evidence/', 'tests/', '.git/')) for n in names))

    def test_research_user_path_blocks_build_without_sanitizing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / 'research').mkdir()
            source = root / 'research/source.json'
            original = '{"local_path":"/Users/example/Downloads/photo.jpg"}'
            source.write_text(original)
            destination = Path(temp) / 'candidate.zip'
            with self.assertRaisesRegex(ValueError, '阻断'):
                package.build(root, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(source.read_text(), original)

    def test_excluded_author_script_dependency_blocks_not_silent_omission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / 'scripts/private_helper.py').write_text('SOURCE="/Users/example/media.mp4"')
            (root / 'scripts/blcaptain_color.py').write_text('import private_helper\n')
            report = package.audit(root)
            self.assertTrue(any('依赖' in x['reason'] for x in report['blockers']))
            self.assertIn('scripts/private_helper.py', [x['path'] for x in report['excluded']])

    def test_media_models_symlinks_and_tokens_block_inside_allowed_tree(self):
        for kind in ('photo.jpg', 'weights.safetensors', 'link.json', 'token.json'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = self.fixture(Path(temp) / 'source')
                (root / 'references').mkdir()
                path = root / 'references' / kind
                if kind == 'link.json':
                    path.symlink_to(root / 'README.md')
                elif kind == 'token.json':
                    path.write_text('{"url":"https://example.test/?access_token=secret"}')
                else:
                    path.write_bytes(b'not-public')
                self.assertTrue(package.audit(root)['blockers'])

    def test_valid_candidate_roundtrip_and_existing_zip_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            destination = Path(temp) / 'candidate.zip'
            package.build(root, destination)
            result = package.verify(destination)
            self.assertTrue(result['integrity_passed'])
            self.assertFalse(result['release_authorized'])
            before = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                package.build(root, destination)
            self.assertEqual(destination.read_bytes(), before)

    def test_tampered_payload_fails_manifest_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            original, changed = Path(temp) / 'a.zip', Path(temp) / 'b.zip'
            package.build(root, original)
            with zipfile.ZipFile(original) as src, zipfile.ZipFile(changed, 'x') as dst:
                for name in src.namelist():
                    dst.writestr(name, b'changed' if name.endswith('/README.md') else src.read(name))
            with self.assertRaisesRegex(ValueError, '哈希'):
                package.verify(changed)

    def test_unsafe_or_duplicate_zip_member_rejected(self):
        for name in ('../escape', '/absolute', 'blcaptain-color-formula/../escape'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'bad.zip'
                with zipfile.ZipFile(path, 'x') as archive:
                    archive.writestr(name, 'unsafe')
                with self.assertRaises(ValueError):
                    package.verify(path)

    def test_missing_required_entry_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / 'scripts/blcaptain_color.py').rename(root / 'scripts/other.py')
            self.assertTrue(package.audit(root)['blockers'])

    def test_public_home_url_is_not_a_private_filesystem_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / 'research').mkdir()
            (root / 'research/source.json').write_text('{"url":"https://example.org/home/kor/info"}')
            self.assertEqual(package.audit(root)['blockers'], [])

    def test_self_consistent_manifest_cannot_authorize_media_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            original, changed = Path(temp) / 'a.zip', Path(temp) / 'b.zip'
            package.build(root, original)
            with zipfile.ZipFile(original) as src, zipfile.ZipFile(changed, 'x') as dst:
                report = json.loads(src.read(package.MANIFEST))
                value = b'not-authorized-media'
                report['included'].append({'path': 'references/photo.jpg', 'sha256': package.digest(value), 'size': len(value)})
                for name in src.namelist():
                    if name != package.MANIFEST:
                        dst.writestr(name, src.read(name))
                dst.writestr(package.PREFIX + 'references/photo.jpg', value)
                dst.writestr(package.MANIFEST, json.dumps(report))
            with self.assertRaises(ValueError):
                package.verify(changed)

    def test_actual_bootstrap_starts_from_clean_extracted_candidate(self):
        """使用仓库真实bootstrap，只验启动闭包，不冒充完整Skill或调色。"""
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / '.blcaptain-interpreter').write_text('current\n')
            shutil.copy2(Path(package.__file__).with_name('bootstrap.py'), root / 'scripts/bootstrap.py')
            (root / 'scripts/blcaptain_color.py').write_text(
                'from pathlib import Path\nfrom bootstrap import bootstrap_or_exit\n'
                'bootstrap_or_exit(Path(__file__).resolve().parents[1])\nprint("真实启动器成功")\n')
            destination = Path(temp) / 'candidate.zip'
            package.build(root, destination)
            target = Path(temp) / 'clean'
            with zipfile.ZipFile(destination) as archive:
                archive.extractall(target)
            installed = target / 'blcaptain-color-formula'
            result = subprocess.run([sys.executable, '-B', str(installed / 'scripts/blcaptain_color.py')],
                                    cwd=installed, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('真实启动器成功', result.stdout)

    def test_interpreter_config_must_be_portable_current(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / '.blcaptain-interpreter').write_text('${HOME}/.pyenv/versions/3.10.18/bin/python3')
            self.assertTrue(package.audit(root)['blockers'])

    def test_cli_missing_output_or_blocked_build_does_not_show_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / 'research').mkdir()
            (root / 'research/private.json').write_text('{"path":"/Users/example/source.jpg"}')
            for extra in ([], ['--output', str(Path(temp) / 'blocked.zip')]):
                result = subprocess.run([sys.executable, '-B', str(Path(package.__file__)),
                                         'build', '--root', str(root), *extra], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('Traceback', result.stdout + result.stderr)

    def test_explicit_public_projection_preserves_all_nine_source_rights(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / 'research').mkdir()
            data = Path(atlas.DEFAULT_MANIFEST).read_bytes()
            original = json.loads(data)
            resource = root / 'research/style_atlas_sources.json'
            resource.write_bytes(data)
            report = package.audit(root, public_assets=True)
            self.assertEqual(report['blockers'], [])
            archive_path = Path(temp) / 'candidate.zip'
            package.build(root, archive_path, public_assets=True)
            with zipfile.ZipFile(archive_path) as archive:
                projected = json.loads(archive.read(package.PREFIX + 'research/style_atlas_sources.json'))
            self.assertEqual(len(projected['sources']), 9)
            self.assertEqual(atlas.validate_manifest(projected, mode='public-export'), [])
            self.assertTrue(atlas.validate_manifest(projected))
            for before, after in zip(original['sources'], projected['sources']):
                self.assertEqual(after['asset_binding'], before['id'])
                self.assertEqual(after['availability'], 'external-not-bundled')
                self.assertIsNone(after['local_path'])
                for key, value in before.items():
                    if key not in ('local_path', 'landing_page'):
                        self.assertEqual(after[key], value, key)
                expected = 'private-source:' + before['id'] if before['landing_page'].startswith('user-provided-local-file:') else before['landing_page']
                self.assertEqual(after['landing_page'], expected)
            self.assertEqual(resource.read_bytes(), data)
            self.assertEqual(report['transformations'][0]['source_sha256'], package.digest(data))

    def test_public_projection_does_not_hide_private_strings_in_other_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp) / 'source')
            (root / 'research').mkdir()
            payload = json.loads(Path(atlas.DEFAULT_MANIFEST).read_text())
            payload['sources'][0]['rights_risks'].append('/Users/example/private/info')
            (root / 'research/style_atlas_sources.json').write_text(json.dumps(payload))
            self.assertTrue(package.audit(root, public_assets=True)['blockers'])

    def test_public_projection_is_idempotent_after_release_extraction(self):
        payload = json.loads(Path(atlas.DEFAULT_MANIFEST).read_text())
        projected = package.project_assets(json.dumps(payload).encode())
        self.assertEqual(package.project_assets(projected), projected)
        self.assertEqual(
            atlas.validate_manifest(json.loads(projected), mode='public-export'),
            [],
        )

    def test_public_package_excludes_internal_process_ledgers(self):
        root = Path(package.__file__).resolve().parents[1]
        report = package.audit(root, public_assets=True)
        included = {item['path'] for item in report['included']}
        self.assertTrue(package.PUBLIC_EXCLUDED.isdisjoint(included))
        excluded = {item['path'] for item in report['excluded']}
        for name in package.PUBLIC_EXCLUDED:
            self.assertTrue(name in excluded or not (root / name).exists(), name)

    def test_public_mode_requires_external_asset_binding_and_keeps_local_contract(self):
        payload = json.loads(Path(atlas.DEFAULT_MANIFEST).read_text())
        if payload.get('distribution_mode') == 'public-export':
            self.assertEqual(atlas.validate_manifest(payload, mode='public-export'), [])
            self.assertTrue(atlas.validate_manifest(payload))
        else:
            self.assertEqual(atlas.validate_manifest(payload), [])
            self.assertTrue(atlas.validate_manifest(payload, mode='public-export'))
        projected = json.loads(package.project_assets(json.dumps(payload).encode()))
        projected['sources'][0]['asset_binding'] = 'wrong'
        self.assertTrue(atlas.validate_manifest(projected, mode='public-export'))
        projected['sources'][0]['asset_binding'] = projected['sources'][0]['id']
        projected['sources'][0]['availability'] = 'local'
        self.assertTrue(atlas.validate_manifest(projected, mode='public-export'))


if __name__ == '__main__':
    unittest.main()
