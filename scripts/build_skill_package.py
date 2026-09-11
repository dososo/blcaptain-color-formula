"""白名单候选包审计与完整性验证；不安装、不授权发布、不代替许可审核。"""
import argparse
import ast
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

try:
    from . import style_atlas_sources
except ImportError:
    import style_atlas_sources

ROOT_FILES = (
    'SKILL.md', 'README.md', 'README.en.md', 'ABOUT.md', 'PRIVACY.md',
    'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md', 'THIRD_PARTY_NOTICES.md',
    'LICENSE', 'VERSION', 'requirements.txt', 'requirements-optional.txt',
    '.blcaptain-interpreter',
)
TREES = {'agents', 'references', 'research', 'scripts', 'validators', 'evals'}
TEXT = {'.md', '.txt', '.json', '.yaml', '.yml', '.py', '.csv', '.toml', '.rst'}
PREFIX = 'blcaptain-color-formula/'
MANIFEST = PREFIX + 'PACKAGE_MANIFEST.json'
PRIVATE_PATH = re.compile(r'(?<![A-Za-z0-9/])(?:/(?:Users|home)/[^/\s"\']+/|/var/'
                          r'folders/|[A-Za-z]:[\\/]Users[\\/])')
TOKEN = re.compile(r'[?&](?:xsec_token|access_token|signature|sig)=', re.I)
INTERNAL = {'scripts/research_replay.py', 'scripts/film_soft_replay_driver.py'}
ASSETS = 'research/style_atlas_sources.json'
PUBLIC_EXCLUDED = {
    'references/archive_changes.json',
    'references/evidence-ledger-v3.md',
    'references/lessons-traceability.json',
    'scripts/archive_integrity.py',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def project_assets(data):
    """只生成公开候选副本；所有来源及权利字段原样保留，不赋予公开许可。"""
    payload = json.loads(data)
    if payload.get('distribution_mode') == 'public-export':
        errors = style_atlas_sources.validate_manifest(payload, mode='public-export')
        if errors:
            raise ValueError('公开素材账本合同失败：' + '；'.join(errors))
        return json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    errors = style_atlas_sources.validate_manifest(payload)
    if errors:
        raise ValueError('原素材账本不满足本地合同：' + '；'.join(errors))
    for item in payload['sources']:
        item['local_path'] = None
        item['asset_binding'] = item['id']
        item['availability'] = 'external-not-bundled'
        if item['landing_page'].startswith('user-provided-' + 'local-file:'):
            item['landing_page'] = 'private-source:' + item['id']
    payload['distribution_mode'] = 'public-export'
    return json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')


def audit(root, public_assets=False):
    root = Path(root).resolve()
    included, excluded, blockers, transformations = [], [], [], []
    sources = {}
    def block(path, reason):
        blockers.append({'path': path, 'reason': reason})
    paths = []
    for child in sorted(root.iterdir()):
        if child.name in ROOT_FILES or child.name in TREES:
            if child.is_symlink():
                block(child.name, '白名单入口是符号链接，不跟随外部文件')
            elif child.is_dir():
                paths.extend(sorted(child.rglob('*')))
            else:
                paths.append(child)
        else:
            excluded.append({'path': child.name, 'reason': '不在候选包白名单；本机配置和历史证据不随包分发'})
    for path in paths:
        name = path.relative_to(root).as_posix()
        if '__pycache__' in path.parts or path.name == '.DS_Store' or path.suffix == '.pyc':
            excluded.append({'path': name, 'reason': '本机缓存不纳入'})
            continue
        if path.is_symlink():
            block(name, '白名单内符号链接不可打包')
            continue
        if path.is_dir():
            continue
        if public_assets and name in PUBLIC_EXCLUDED:
            excluded.append({'path': name, 'reason': '内部过程账本不进入公开发行包'})
            continue
        if path.suffix not in TEXT and name not in ROOT_FILES:
            block(name, '白名单内非必要文本，可能为媒体、模型或二进制；需人工审查')
            continue
        data = path.read_bytes()
        if name == ASSETS and public_assets:
            original = data
            try:
                data = project_assets(data)
            except ValueError as error:
                block(name, str(error))
                continue
            transformations.append({'path': name, 'source_sha256': digest(original),
                                    'output_sha256': digest(data),
                                    'rules': ['保留全部来源和权利状态；local_path=null',
                                              'asset_binding=原id；availability=external-not-bundled',
                                              '私有来源地址替换为private-source:原id'],
                                    'boundary': '媒体不在包内；公开投影不改变公开许可或配方状态'})
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            block(name, '不是UTF-8文本')
            continue
        if name == '.blcaptain-interpreter' and text.strip() != 'current':
            block(name, '候选解释器配置只允许current，不分发本机解释器路径')
            continue
        if name in INTERNAL or (name.startswith('scripts/migrate_') and path.suffix == '.py'):
            excluded.append({'path': name, 'reason': '仅供内部历史重放或迁移，不属于可移植运行入口'})
            continue
        if PRIVATE_PATH.search(text):
            if name.startswith('scripts/'):
                excluded.append({'path': name, 'reason': '硬编码作者或临时私有路径；不纳入研究脚本，继续核查导入依赖'})
            else:
                block(name, '包含用户或本机私有路径；阻断而非静默脱敏')
            continue
        if TOKEN.search(text):
            block(name, '可能带访问令牌URL，需人工检查原文')
            continue
        if path.stem.lower().startswith('raw60') or '原始调色公式库' in name:
            block(name, '第三方完整配方原材料不允许再分发')
            continue
        included.append({'path': name, 'sha256': digest(data), 'size': len(data)})
        sources[name] = text
    names = {item['path'] for item in included}
    for name in (*ROOT_FILES, 'scripts/blcaptain_color.py'):
        if name not in names:
            block(name, '核心必需文件未进入白名单候选')
    # 保留所有安全运行模块；检查本地静态导入是否被排除，不按猜测缩小模块集。
    local = {p.stem: p.relative_to(root).as_posix()
             for folder in TREES for p in (root / folder).glob('*.py')}
    for name, text in sources.items():
        if not name.endswith('.py'):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            block(name, 'Python语法不可解析')
            continue
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split('.')[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append((node.module or '').split('.')[-1])
                if node.module in TREES:
                    imports.extend(alias.name for alias in node.names)
        for module in sorted(set(imports)):
            if module in local and local[module] not in names:
                block(name, '本地运行依赖被排除：' + local[module])
    return {'schema': 1, 'status': 'blocked' if blockers else 'candidate',
            'included': included, 'excluded': excluded, 'blockers': blockers,
            'transformations': transformations,
            'release_authorized': False, 'installation_verified': False,
            'boundary': '仅白名单、私有路径与静态导入审计；动态资源、第三方权利和真实安装须独立复核，候选不等于发布。'}


def build(root, output, public_assets=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('候选包已存在，不覆盖')
    if output.is_relative_to(root):
        raise ValueError('候选包必须写到仓库外')
    report = audit(root, public_assets)
    if report['blockers']:
        raise ValueError('候选包审计阻断；先运行audit查看清单，不生成ZIP')
    # 全部读取并核对后再创建包，避免失败中途留下貌似有效的候选。
    payload = {item['path']: (root / item['path']).read_bytes() for item in report['included']}
    for change in report['transformations']:
        if digest(payload[change['path']]) != change['source_sha256']:
            raise ValueError('投影源文件在审计后变化，停止构建')
        payload[change['path']] = project_assets(payload[change['path']])
    if any(digest(payload[item['path']]) != item['sha256'] for item in report['included']):
        raise ValueError('审计后文件变化，必须重审')
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            archive.writestr(PREFIX + name, data)
        archive.writestr(MANIFEST, json.dumps(report, ensure_ascii=False, indent=2))
    return verify(output)


def verify(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)) or any(
                not n.startswith(PREFIX) or '..' in PurePosixPath(n).parts or '\\' in n
                or n != str(PurePosixPath(n))
                for n in names) or any(stat.S_ISLNK(i.external_attr >> 16) for i in infos):
            raise ValueError('候选包存在越界、重复或符号链接成员')
        if MANIFEST not in names:
            raise ValueError('缺少候选清单')
        report = json.loads(archive.read(MANIFEST))
        if report.get('status') != 'candidate' or report.get('blockers'):
            raise ValueError('清单仍有阻断')
        expected = {PREFIX + item['path'] for item in report['included']}
        if set(names) != expected | {MANIFEST} or len(expected) != len(report['included']):
            raise ValueError('候选包成员与清单不一致')
        for item in report['included']:
            name = item['path']
            relative = PurePosixPath(name)
            if (name not in ROOT_FILES and
                    (len(relative.parts) < 2 or relative.parts[0] not in TREES or relative.suffix not in TEXT)):
                raise ValueError('候选包成员不在必要文本白名单')
            if name in INTERNAL or '__pycache__' in relative.parts or name.startswith('scripts/migrate_'):
                raise ValueError('候选包含内部迁移、缓存或历史重放脚本')
            data = archive.read(PREFIX + name)
            if digest(data) != item['sha256'] or len(data) != item['size']:
                raise ValueError('候选包哈希或大小不匹配')
            text = data.decode('utf-8')
            if name == '.blcaptain-interpreter' and text.strip() != 'current':
                raise ValueError('候选解释器配置只允许current')
            if PRIVATE_PATH.search(text) or TOKEN.search(text):
                raise ValueError('候选包内容仍含私有路径或访问令牌')
            if name == ASSETS:
                payload = json.loads(text)
                mode = 'public-export' if payload.get('distribution_mode') == 'public-export' else 'local'
                errors = style_atlas_sources.validate_manifest(payload, mode)
                if errors:
                    raise ValueError('包内素材账本合同失败：' + '；'.join(errors))
        if not {PREFIX + name for name in (*ROOT_FILES, 'scripts/blcaptain_color.py')} <= expected:
            raise ValueError('候选包缺少核心入口')
        if archive.testzip() is not None:
            raise ValueError('ZIP校验失败')
    return {'integrity_passed': True, 'files': len(expected),
            'installation_verified': False, 'release_authorized': False,
            'boundary': '完整性校验不是签名认证、许可结论或真实安装验证。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('audit', 'build', 'verify'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--public-assets', action='store_true', help='显式投影素材元数据副本，不改变媒体许可')
    args = parser.parse_args()
    if args.command in ('build', 'verify') and args.output is None:
        print(json.dumps({'status': 'blocked', 'reason': '构建或验证必须指定--output路径'}, ensure_ascii=False))
        return 2
    try:
        report = audit(args.root, args.public_assets) if args.command == 'audit' else (
            build(args.root, args.output, args.public_assets) if args.command == 'build' else verify(args.output))
        if args.manifest:
            with args.manifest.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error),
                          'release_authorized': False}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report.get('blockers') else 0


if __name__ == '__main__':
    raise SystemExit(main())
