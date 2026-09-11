"""沿用启动 Python，或按用户的显式声明切换；只使用标准库。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


CONFIG_NAME = ".blcaptain-interpreter"


class BootstrapError(RuntimeError):
    """解释器声明不可用。"""


def configured_interpreter(root: Path) -> Path:
    config = root / CONFIG_NAME
    if not config.is_file():
        raise BootstrapError(f"缺少解释器声明：{config}")
    value = config.read_text(encoding="utf-8").strip()
    if not value:
        raise BootstrapError(f"解释器声明为空：{config}")
    if value == "current":
        return Path(sys.executable).resolve()
    interpreter = Path(os.path.expandvars(value)).expanduser().resolve()
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise BootstrapError(f"声明的 Python 不可执行：{interpreter}")
    return interpreter


def ensure_configured_interpreter(root: Path) -> None:
    """需要时原位重启；成功返回表示当前已经是声明解释器。"""
    interpreter = configured_interpreter(root)
    current = Path(sys.executable).resolve()
    if current == interpreter:
        return
    os.execv(str(interpreter), [str(interpreter), *sys.argv])


def bootstrap_or_exit(root: Path) -> None:
    try:
        ensure_configured_interpreter(root)
    except BootstrapError as error:
        print(f"BLCaptain 无法启动：{error}", file=sys.stderr)
        raise SystemExit(5) from None
