"""把公开包安装到 Codex Skill 目录；只复制公开白名单，绝不覆盖已有目录。"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

try:
    from .build_skill_package import ROOT_FILES, TREES
except ImportError:
    from build_skill_package import ROOT_FILES, TREES


def install(source: Path, target: Path) -> None:
    source = source.resolve()
    target = target.expanduser().resolve()
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"下载目录不完整：没有找到 SKILL.md（{source}）")
    if target.exists():
        raise FileExistsError(f"目标已存在，安装器不会覆盖：{target}")
    for name in ROOT_FILES:
        if not (source / name).is_file():
            raise ValueError(f"下载目录不完整：缺少 {name}")
    target.mkdir(parents=True)
    for name in ROOT_FILES:
        shutil.copy2(source / name, target / name)
    for name in sorted(TREES):
        path = source / name
        if path.is_dir():
            shutil.copytree(path, target / name)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="安装 BLCaptain 调色公式 Skill")
    result.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1],
                        help="解压后的 Skill 目录；默认使用当前目录")
    result.add_argument("--target", type=Path,
                        default=Path.home() / ".codex" / "skills" / "blcaptain-color-formula",
                        help="安装位置；默认 ~/.codex/skills/blcaptain-color-formula")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        install(args.source, args.target)
    except (OSError, ValueError) as error:
        print(f"安装未完成：{error}", file=sys.stderr)
        return 2
    target = args.target.expanduser().resolve()
    print("安装完成。")
    print(f"位置：{target}")
    print("下一步：重新打开 Codex，然后发送“用 BLCaptain 调色公式，帮我实际调色这张照片”。")
    print("原图不会被覆盖；生成前会先给方案，等你确认后才执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
