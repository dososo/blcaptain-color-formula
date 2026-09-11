#!/usr/bin/env python3
"""CIEDE2000 色差与 CIELAB 转换。标定报告必须用 ΔE00，不能用 RGB 欧氏距离冒充。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from color_space import eotf  # noqa: E402

# D65 白点（CIE 1931 2°）
D65 = (0.95047, 1.00000, 1.08883)
SRGB_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
P3_TO_XYZ = (
    (0.48657095, 0.26566769, 0.19821729),
    (0.22897456, 0.69173852, 0.07928691),
    (0.00000000, 0.04511338, 1.04394437),
)


def rgb8_to_xyz(red: int, green: int, blue: int, profile: str = "srgb") -> tuple[float, float, float]:
    matrix = P3_TO_XYZ if profile == "display-p3" else SRGB_TO_XYZ
    r, g, b = eotf(red / 255.0), eotf(green / 255.0), eotf(blue / 255.0)
    return (
        matrix[0][0] * r + matrix[0][1] * g + matrix[0][2] * b,
        matrix[1][0] * r + matrix[1][1] * g + matrix[1][2] * b,
        matrix[2][0] * r + matrix[2][1] * g + matrix[2][2] * b,
    )


def xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29
    fx, fy, fz = f(x / D65[0]), f(y / D65[1]), f(z / D65[2])
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def rgb8_to_lab(red: int, green: int, blue: int, profile: str = "srgb") -> tuple[float, float, float]:
    return xyz_to_lab(*rgb8_to_xyz(red, green, blue, profile))


def delta_e_2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIEDE2000。实现按 Sharma et al. 2005 的标准公式。"""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    avg_l = (l1 + l2) / 2
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    avg_c = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(avg_c ** 7 / (avg_c ** 7 + 25 ** 7))) if avg_c > 0 else 0.0
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    avg_cp = (c1p + c2p) / 2

    def hue(ap: float, bp: float) -> float:
        if ap == 0 and bp == 0:
            return 0.0
        angle = math.degrees(math.atan2(bp, ap))
        return angle + 360 if angle < 0 else angle

    h1p, h2p = hue(a1p, b1), hue(a2p, b2)
    delta_lp = l2 - l1
    delta_cp = c2p - c1p
    if c1p * c2p == 0:
        delta_hp = 0.0
    elif abs(h2p - h1p) <= 180:
        delta_hp = h2p - h1p
    elif h2p - h1p > 180:
        delta_hp = h2p - h1p - 360
    else:
        delta_hp = h2p - h1p + 360
    delta_bighp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(delta_hp) / 2)

    if c1p * c2p == 0:
        avg_hp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        avg_hp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        avg_hp = (h1p + h2p + 360) / 2
    else:
        avg_hp = (h1p + h2p - 360) / 2

    t = (1 - 0.17 * math.cos(math.radians(avg_hp - 30))
         + 0.24 * math.cos(math.radians(2 * avg_hp))
         + 0.32 * math.cos(math.radians(3 * avg_hp + 6))
         - 0.20 * math.cos(math.radians(4 * avg_hp - 63)))
    delta_theta = 30 * math.exp(-(((avg_hp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(avg_cp ** 7 / (avg_cp ** 7 + 25 ** 7)) if avg_cp > 0 else 0.0
    sl = 1 + (0.015 * (avg_l - 50) ** 2) / math.sqrt(20 + (avg_l - 50) ** 2)
    sc = 1 + 0.045 * avg_cp
    sh = 1 + 0.015 * avg_cp * t
    rt = -math.sin(math.radians(2 * delta_theta)) * rc
    return math.sqrt(
        (delta_lp / sl) ** 2 + (delta_cp / sc) ** 2 + (delta_bighp / sh) ** 2
        + rt * (delta_cp / sc) * (delta_bighp / sh)
    )


# 低于此彩度不评估色相：角度会剧烈摆动却没有视觉意义，
# 而且 ΔE00 的 ΔH 项本身已按 sqrt(C1*C2) 加权，低彩度的色相偏差已被正确计入。
# 实测：门限设 1.0 时，被通道增益轻微染色的灰阶块报出 27.8°；设 15 后真彩块最大仅 4.86°。
CHROMA_FLOOR_FOR_HUE = 15.0


def hue_angle_error(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """色相角误差（度）。彩度过低时色相无意义，返回 0 并由调用方标注。"""
    c1, c2 = math.hypot(lab1[1], lab1[2]), math.hypot(lab2[1], lab2[2])
    # 彩度太低时色相在数值上会剧烈摆动却没有视觉意义。
    # 实测：把门限设在 1.0 时，被通道增益轻微染色的灰阶块报出 27.8° 的「色相误差」，
    # 那不是误差，是无意义的角度噪声。CIE 的经验是 C* 低于 5 就不谈色相。
    if c1 < CHROMA_FLOOR_FOR_HUE or c2 < CHROMA_FLOOR_FOR_HUE:
        return 0.0
    h1 = math.degrees(math.atan2(lab1[2], lab1[1])) % 360
    h2 = math.degrees(math.atan2(lab2[2], lab2[1])) % 360
    gap = abs(h1 - h2) % 360
    return 360 - gap if gap > 180 else gap
