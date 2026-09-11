#!/usr/bin/env python3
"""把带 Adobe RGB / ProPhoto 等宽色域 ICC 的照片显式转换到 sRGB。

**这不是放松「不静默改变色域」这条规则，而是给它配一条出路。**

渲染链路一如既往地拒绝处理无法确认的色域——那条硬停没有动。
变的只有一点：以前用户撞墙后只得到一句「请转专业色彩管理流程」，
现在他能拿到一条自己能跑的命令，转换是他主动发起的，损失也当面写清楚。

转换本身是有损的：Adobe RGB 覆盖的饱和青绿与橙红超出 sRGB 的部分，
落到 sRGB 里必然被压回边界。这个损失不可逆，所以：

  1. 永不覆盖原图，只写新文件；
  2. 默认相对比色 + 黑点补偿——这是把「照片看起来一样」放在第一位的选择，
     不是把「数值一样」放在第一位；
  3. 转换前后各测一次超界像素比例，如实写进结果里，不让用户以为无损。

依赖 PIL 的 ImageCms（littleCMS）。它是可选依赖，没装就如实报能力不足，
不用别的手段凑合——凑合出来的颜色比停下来更糟。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

CONVERTIBLE = {
    "adobe-rgb": "Adobe RGB (1998)",
    "prophoto-rgb": "ProPhoto RGB",
    "dci-p3-photo": "DCI-P3",
    "unsupported-photo": "未识别的宽色域",
}


def probe() -> dict:
    """如实回答：这台机器现在能不能做可靠的 ICC 变换。"""
    try:
        from PIL import ImageCms, features
    except Exception as exc:
        return {"available": False, "engine": None,
                "reason": f"PIL/ImageCms 不可用：{exc}",
                "how_to_enable": "pip install Pillow（见 requirements-optional.txt）"}
    try:
        version = features.version("littlecms2")
    except Exception:
        version = None
    if not version:
        return {"available": False, "engine": "PIL",
                "reason": "PIL 在，但没有编译进 littleCMS",
                "how_to_enable": "换一个带 littlecms2 的 Pillow 轮子"}
    return {"available": True, "engine": "PIL/ImageCms", "littlecms": version,
            "intent": "relative-colorimetric + 黑点补偿"}


def embedded_profile_name(path: Path) -> str | None:
    """读内嵌 ICC 的描述串。ffprobe 读不到这个，但色域判断全靠它。"""
    try:
        from PIL import Image, ImageCms
    except Exception:
        return None
    with Image.open(path) as im:
        raw = im.info.get("icc_profile")
    if not raw:
        return None
    try:
        return ImageCms.getProfileDescription(
            ImageCms.ImageCmsProfile(io.BytesIO(raw))).strip()
    except Exception:
        return None


def _out_of_gamut_ratio(image) -> float:
    """落在 0 或 255 上的像素比例。转换把超界颜色压到边界，这个数会涨。"""
    hist = image.histogram()
    total = image.width * image.height
    if not total:
        return 0.0
    edge = 0
    for ch in range(3):
        band = hist[ch * 256:(ch + 1) * 256]
        edge += band[0] + band[255]
    return round(edge / (total * 3), 6)


def convert_to_srgb(src: Path, dst: Path) -> dict:
    cap = probe()
    if not cap["available"]:
        raise RuntimeError(f"{cap['reason']}。{cap.get('how_to_enable', '')}")
    from PIL import Image, ImageCms

    if dst.resolve() == src.resolve():
        raise RuntimeError("拒绝原地覆盖：转换有损且不可逆，必须写到新文件。")

    with Image.open(src) as im:
        raw = im.info.get("icc_profile")
        if not raw:
            raise RuntimeError("这张图没有内嵌 ICC，不需要也无法做 ICC 转换。")
        source_name = embedded_profile_name(src) or "（ICC 无描述串）"
        rgb = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
        before = _out_of_gamut_ratio(rgb.convert("RGB"))
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(raw))
        dst_profile = ImageCms.createProfile("sRGB")
        out = ImageCms.profileToProfile(
            rgb, src_profile, dst_profile, outputMode="RGB",
            renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
            flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
        after = _out_of_gamut_ratio(out)
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst, format="PNG", icc_profile=ImageCms.ImageCmsProfile(dst_profile).tobytes())

    return {
        "schema_version": "4.4.0",
        "source": str(src), "output": str(dst),
        "source_profile": source_name, "target_profile": "sRGB IEC61966-2.1",
        "engine": cap["engine"], "littlecms": cap.get("littlecms"),
        "intent": "relative-colorimetric", "black_point_compensation": True,
        "clipped_pixel_ratio_before": before,
        "clipped_pixel_ratio_after": after,
        "clipped_pixel_ratio_delta": round(after - before, 6),
        "lossy": True,
        "boundary": "转换有损且不可逆：源色域超出 sRGB 的饱和色被压回边界，"
                    "上面的 clipped 比例变化就是它的量化下限（只统计落到 0/255 的像素，"
                    "边界内的压缩量它测不到）。原图未被改动。",
        "next": "拿输出文件重新跑 plan：它现在是带 sRGB 标签的 PNG，渲染链路可以直接处理。",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="把宽色域照片显式转换到 sRGB（不覆盖原图）")
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--probe-only", action="store_true", help="只报告本机的 ICC 变换能力")
    args = ap.parse_args()
    if args.probe_only:
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
        return 0
    if not args.input or not args.output:
        ap.error("除 --probe-only 外，--input 与 --output 都是必填")
    try:
        result = convert_to_srgb(Path(args.input), Path(args.output))
    except Exception as exc:
        print(f"转换失败：{exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
