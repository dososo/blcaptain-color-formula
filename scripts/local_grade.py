#!/usr/bin/env python3
"""语义局部调色的真实渲染路径。

存在的理由：组合语法一旦向用户提供「人物保护／人物塑光」，就必须真的能执行。
只提供选项而没有渲染器，等于用文案冒充能力——这是本项目反复记录过的失败模式。

两种策略：
- person-protect：全局风格照常执行，但人物区的变化被限幅，避免肤色被风格带走。
- person-lift：只在人物区追加提亮与彩度恢复，环境保持全局风格不变。

蒙版来自已登记的语义后端，绝不用固定 HSL 宽色带冒充。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOCAL_GRADE_SCHEMA_VERSION = "4.0.0"

STRATEGIES = {
    "person-protect": {
        "name": "人物保护",
        "requires_class": "person",
        "description": "全局风格照常执行，人物区的变化被限幅",
        "default_strength": 0.45,
        "strength_meaning": "人物区保留多少比例的全局风格；0 表示人物完全不受风格影响",
    },
    "person-lift": {
        "name": "人物塑光",
        "requires_class": "person",
        "description": "只在人物区追加提亮与彩度恢复，环境不变",
        "default_strength": 0.5,
        "strength_meaning": "人物区追加提亮与彩度的幅度",
    },
    "sky-depth": {
        "name": "天空层次",
        "requires_class": "sky",
        "description": "只压天空亮部并恢复云层层次，地面不动",
        "default_strength": 0.45,
        "strength_meaning": "天空区压高光与增局部对比的幅度",
        "risk": "天空是大面积平滑渐变，压过头会出色带；蒙版边缘在树枝、电线、发丝处不可靠",
    },
    "sky-protect": {
        "name": "天空保护",
        "requires_class": "sky",
        "description": "全局风格照常执行，天空区的变化被限幅，避免蓝天被染色",
        "default_strength": 0.4,
        "strength_meaning": "天空区保留多少比例的全局风格",
    },
    "foliage-tune": {
        "name": "植被分色",
        "requires_class": "vegetation",
        "description": "只在植被区调整绿色的黄绿倾向与彩度，肤色与环境不受影响",
        "default_strength": 0.45,
        "strength_meaning": "植被区色相与彩度调整的幅度",
        "risk": "植被与暖色地面、黄墙的边界容易泄漏；秋叶会被误当植被",
    },
    "building-structure": {
        "name": "建筑结构",
        "requires_class": "building",
        "description": "只在建筑区增强中频局部对比，突出石材与结构，天空与人物不动",
        "default_strength": 0.4,
        "strength_meaning": "建筑区局部对比增强的幅度",
        "risk": "玻璃幕墙与反射面会被算作建筑；过度增强会放大压缩块",
    },
    "product-neutral": {
        "name": "商品中性化",
        "requires_class": "product",
        "description": "只在商品区做中性校正，让商品颜色可信，背景保持风格",
        "default_strength": 0.5,
        "strength_meaning": "商品区向中性回归的幅度",
        "risk": "透明与高反光商品的蒙版不可靠；品牌色必须人工复核",
    },
    # --- 肤色策略（v4.6.0 新增）---
    #
    # 此前策略表里 7 个策略的 requires_class 分别是 person×2 / sky×2 /
    # vegetation / building / product，**没有一个是 skin**。
    # 语义层其实正确派生出了肤色蒙版（人脸框 ∩ 人物蒙版 ∩ 受限肤色似然像素），
    # 却没有任何策略消费它——于是肤色蒙版永远不会被生成，
    # 渲染后的 skin_tolerance 也就永远 skipped。这是断链的确切位置。
    #
    # 肤色蒙版只来自真实几何与实例分割，绝不用橙色宽色带近似（D4）；
    # 任何肤色处理都必须由用户显式确认后才执行（D3）。
    "skin-protect": {
        "name": "肤色保护",
        "requires_class": "skin",
        "description": "全局风格照常执行，但肤色区域内部的明度排序、彩度与色相稳定性按原片保留",
        "default_strength": 0.6,
        "strength_meaning": "肤色区保留多少比例的原片肤色关系；1 表示肤色完全不受风格影响",
        "needs_confirmation": True,
        "per_instance": False,
        "basis": "人脸几何 ∩ 人物实例蒙版 ∩ 受限肤色似然像素；不做任何肤型或族裔分类",
    },
    "skin-protect-per-instance": {
        "name": "逐人肤色保护",
        "requires_class": "skin",
        "description": "画面中每个人各自保留自己的肤色关系，互不共用同一套偏移",
        "default_strength": 0.6,
        "strength_meaning": "每个人的肤色区各自保留多少比例的原片肤色关系",
        "needs_confirmation": True,
        "per_instance": True,
        "basis": "逐实例人物蒙版分别求交；每个人的参考值取自他自己，不取全体平均——"
                 "共用一套偏移会把不同肤色、不同受光的人一起拉向同一个中间值",
    },
}
# 局部追加的安全上限。超过它就不再是「塑光」而是重画，必须由用户单独确认。
MAX_LIFT_BRIGHTNESS = 0.18
MAX_LIFT_SATURATION = 0.35
MASK_FEATHER_PX = 3


class LocalGradeError(Exception):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise LocalGradeError("缺少 ffmpeg")
    return found


def build_local_filter_complex(
    strategy: str,
    global_chain: str,
    width: int,
    height: int,
    strength: float,
    output_tag_chain: str = "",
    feather: int = MASK_FEATHER_PX,
) -> str:
    """构造 filter_complex。输入 0 是原片，输入 1 是灰度蒙版。"""
    if strategy not in STRATEGIES:
        raise LocalGradeError(f"未知局部策略：{strategy}")
    chain = global_chain.strip().strip(",") or "null"
    # 软边：蒙版边缘必须羽化，硬边会在皮肤与背景交界处形成可见接缝。
    mask_chain = f"scale={width}:{height}:flags=bilinear,format=gray"
    if feather > 0:
        mask_chain += f",boxblur=luma_radius={feather}:luma_power=1"
    tail = f",{output_tag_chain}" if output_tag_chain else ""

    if strategy in ("person-protect", "skin-protect", "skin-protect-per-instance"):
        keep = max(0.0, min(1.0, strength))
        return (
            f"[0:v]format=rgb24,split=2[bllocalbase][bllocalstyle];"
            f"[bllocalstyle]{chain}[bllocalgraded];"
            f"[bllocalgraded]split=2[bllocalfull][bllocalmix];"
            f"[bllocalbase][bllocalmix]blend=all_expr='A*(1-{keep:.4f})+B*{keep:.4f}'[bllimited];"
            f"[1:v]{mask_chain}[blmask];"
            f"[bllocalfull][bllimited][blmask]maskedmerge{tail}"
        )

    if strategy in ("sky-protect",):
        keep = max(0.0, min(1.0, strength))
        return (
            f"[0:v]format=rgb24,split=2[bllocalbase][bllocalstyle];"
            f"[bllocalstyle]{chain}[bllocalgraded];"
            f"[bllocalgraded]split=2[bllocalfull][bllocalmix];"
            f"[bllocalbase][bllocalmix]blend=all_expr='A*(1-{keep:.4f})+B*{keep:.4f}'[bllimited];"
            f"[1:v]{mask_chain}[blmask];"
            f"[bllocalfull][bllimited][blmask]maskedmerge{tail}"
        )

    amount = max(0.0, min(1.0, strength))
    # 每类局部的「追加动作」不同，但都必须有上限，且都只作用在蒙版内。
    if strategy == "sky-depth":
        # 天空：压高光 + 轻微增中频对比。天空是平滑渐变，动作必须比其他类更克制。
        inner = (f"eq=brightness={-0.06 * amount:.6f}:contrast={1.0 + 0.14 * amount:.6f},"
                 f"unsharp=5:5:{0.35 * amount:.4f}:5:5:0")
    elif strategy == "foliage-tune":
        # 植被：只动绿与黄绿两条带的色相与彩度，不碰其他色相。
        inner = (f"huesaturation=colors=g:hue={-3.5 * amount:.4f}:saturation={0.10 * amount:.4f}:"
                 f"intensity=0:strength=1:lightness=1,"
                 f"huesaturation=colors=y:hue={-2.0 * amount:.4f}:saturation={0.05 * amount:.4f}:"
                 f"intensity=0:strength=1:lightness=1")
    elif strategy == "building-structure":
        inner = (f"unsharp=7:7:{0.55 * amount:.4f}:7:7:0,"
                 f"eq=contrast={1.0 + 0.10 * amount:.6f}")
    elif strategy == "product-neutral":
        # 商品：向中性回归 = 降低彩度偏移并轻微提亮，让颜色可信。
        inner = (f"eq=saturation={1.0 - 0.18 * amount:.6f}:brightness={0.04 * amount:.6f}:"
                 f"contrast={1.0 + 0.05 * amount:.6f}")
    else:  # person-lift
        inner = (f"eq=brightness={MAX_LIFT_BRIGHTNESS * amount:.6f}:"
                 f"saturation={1.0 + MAX_LIFT_SATURATION * amount:.6f}")
    return (
        f"[0:v]format=rgb24,{chain}[bllocalgraded];"
        f"[bllocalgraded]split=2[blenv][bltolift];"
        f"[bltolift]{inner}[bllifted];"
        f"[1:v]{mask_chain}[blmask];"
        f"[blenv][bllifted][blmask]maskedmerge{tail}"
    )


def union_masks(mask_paths: list[Path], output: Path) -> Path:
    """把已唯一归属的逐人肤色蒙版合成联合蒙版，不改变每个像素的参考值。"""
    paths = [Path(item) for item in mask_paths if Path(item).is_file()]
    if not paths:
        raise LocalGradeError("没有可合并的逐人肤色蒙版")
    if len(paths) == 1:
        return paths[0]
    inputs = []
    for path in paths:
        inputs.extend(["-i", str(path)])
    current = "[0:v]"
    parts = []
    for index in range(1, len(paths)):
        out = f"[blunion{index}]"
        parts.append(f"{current}[{index}:v]blend=all_mode=lighten{out}")
        current = out
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [_ffmpeg(), "-v", "error", "-y", *inputs, "-filter_complex", ";".join(parts),
         "-map", current, "-frames:v", "1", "-pix_fmt", "gray", str(output)],
        capture_output=True,
    )
    if result.returncode or not output.is_file():
        raise LocalGradeError("无法合并逐人肤色蒙版")
    return output


def render_local(source: Path, mask: Path, strategy: str, global_chain: str,
                 output: Path, width: int, height: int, strength: float | None = None,
                 output_tag_chain: str = "") -> dict:
    if output.exists():
        raise LocalGradeError(f"输出已存在，不会覆盖：{output}")
    if output.resolve() == source.resolve():
        raise LocalGradeError("输出路径不得覆盖原文件")
    if not mask.is_file():
        raise LocalGradeError(f"蒙版不存在：{mask}")
    value = STRATEGIES[strategy]["default_strength"] if strength is None else strength
    graph = build_local_filter_complex(strategy, global_chain, width, height, value, output_tag_chain)
    command = [
        _ffmpeg(), "-v", "error", "-n", "-i", str(source), "-i", str(mask),
        "-filter_complex", graph, "-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgb24", str(output),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise LocalGradeError(
            f"局部渲染失败：{result.stderr.decode('utf-8','replace').strip().splitlines()[-1:] or ['未知']}"
        )
    return {
        "schema_version": LOCAL_GRADE_SCHEMA_VERSION,
        "strategy": strategy,
        "strategy_name": STRATEGIES[strategy]["name"],
        "strength": value,
        "mask_path": str(mask),
        "feather_px": MASK_FEATHER_PX,
        "filter_complex": graph,
        "command": command,
        "boundary": (
            "蒙版来自已登记的语义后端；局部效果只在该类别真实存在时施加，"
            "不使用固定 HSL 宽色带冒充语义区域。"
        ),
    }


PROTECTION_STRATEGIES = {
    "person-protect", "sky-protect", "skin-protect", "skin-protect-per-instance",
}


def strategy_separation_ratio(inside_mean: float, outside_mean: float,
                              strategy: str | None = None) -> tuple[float, str]:
    """按策略意图返回区内外分离比；保护型策略应当让区内变化小于区外。"""
    if strategy is None:
        return inside_mean / max(0.5, outside_mean), "descriptive-only-no-strategy"
    if strategy in PROTECTION_STRATEGIES:
        return outside_mean / max(0.5, inside_mean), "inside-less-than-outside"
    return inside_mean / max(0.5, outside_mean), "inside-more-than-outside"


def measure_inside_outside(source: Path, output: Path, mask: Path,
                           grid_w: int = 64, grid_h: int = 36,
                           strategy: str | None = None) -> dict:
    """量测区内外变化幅度。局部策略必须在区内外产生可分辨的差异，否则不成立。"""
    def grid(path: Path, gray: bool = False) -> bytes:
        fmt = "gray" if gray else "rgb24"
        result = subprocess.run(
            [_ffmpeg(), "-v", "error", "-i", str(path),
             "-vf", f"scale={grid_w}:{grid_h},format={fmt}",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", fmt, "-"],
            capture_output=True)
        if result.returncode or not result.stdout:
            raise LocalGradeError(f"无法采样 {path.name}")
        return result.stdout

    before, after, mask_grid = grid(source), grid(output), grid(mask, gray=True)
    inside, outside = [], []
    for index in range(grid_w * grid_h):
        offset = index * 3
        if offset + 3 > min(len(before), len(after)):
            break
        delta = sum(abs(before[offset + c] - after[offset + c]) for c in range(3)) / 3
        (inside if index < len(mask_grid) and mask_grid[index] >= 128 else outside).append(delta)
    inside_mean = sum(inside) / len(inside) if inside else 0.0
    outside_mean = sum(outside) / len(outside) if outside else 0.0
    ratio, expected_direction = strategy_separation_ratio(
        inside_mean, outside_mean, strategy)
    return {
        "inside_pixels": len(inside), "outside_pixels": len(outside),
        "inside_mean_delta": round(inside_mean, 3),
        "outside_mean_delta": round(outside_mean, 3),
        "separation_ratio": round(ratio, 3),
        "expected_direction": expected_direction,
        "metric": "8 位灰阶平均绝对变化；只验证局部确实生效，不代表审美通过",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="语义局部调色渲染")
    parser.add_argument("--input", required=True)
    parser.add_argument("--mask", required=True)
    parser.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    parser.add_argument("--global-chain", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--strength", type=float)
    args = parser.parse_args()
    source = Path(args.input).expanduser().resolve()
    import blcaptain_color as engine
    info = engine.inspect_media(source)
    try:
        receipt = render_local(
            source, Path(args.mask).expanduser().resolve(), args.strategy,
            args.global_chain, Path(args.output).expanduser().resolve(),
            int(info["width"]), int(info["height"]), args.strength,
        )
        receipt["measurement"] = measure_inside_outside(
            source, Path(args.output).expanduser().resolve(), Path(args.mask).expanduser().resolve(),
            strategy=args.strategy,
        )
    except LocalGradeError as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 超过一个人就必须逐实例。共用一套肤色偏移会把不同肤色、不同受光的人
# 一起拉向同一个中间值——那正是多人肤色处理此前被整体停用的原因。
PER_INSTANCE_MIN_COUNT = 2


def per_instance_required(instance_count: int) -> bool:
    """多人是否必须走逐实例路径。"""
    return int(instance_count or 0) >= PER_INSTANCE_MIN_COUNT


def per_instance_references(samples: list) -> list:
    """每个实例的肤色参考值取自它自己。

    **不取全体平均**：把三个人的明度平均成一个值再按它校正每个人，
    等于把深肤色往亮里拉、把浅肤色往暗里压，两个人都不像自己。
    这里只保留区域内部的相对关系，不做任何肤型或族裔分类（D5）。
    """
    out = []
    for item in samples or []:
        out.append({
            "index": item.get("index"),
            "lightness": float(item.get("lightness") or 0.0),
            "chroma": float(item.get("chroma") or 0.0),
            "hue": float(item.get("hue") or 0.0),
            "basis": "该实例自身区域内的读数，未与其它实例合并",
        })
    return out
