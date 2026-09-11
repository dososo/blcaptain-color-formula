#!/usr/bin/env python3
"""共享色彩空间原语：sRGB / Display P3 → 线性光 → XYZ → OKLab → OKLCH。

只用标准库。所有转换都显式携带来源色域，禁止静默按 sRGB 解释 P3 像素。
"""

from __future__ import annotations

import math

# 与 blcaptain_color.rgb_to_oklab 保持同一组矩阵，避免两套实现漂移。
_XYZ_FROM_LINEAR = {
    "srgb": (
        (0.4124564, 0.3575761, 0.1804375),
        (0.2126729, 0.7151522, 0.0721750),
        (0.0193339, 0.1191920, 0.9503041),
    ),
    "display-p3": (
        (0.48657095, 0.26566769, 0.19821729),
        (0.22897456, 0.69173852, 0.07928691),
        (0.00000000, 0.04511338, 1.04394437),
    ),
}
_LINEAR_FROM_XYZ = {
    "srgb": (
        (3.2404542, -1.5371385, -0.4985314),
        (-0.9692660, 1.8760108, 0.0415560),
        (0.0556434, -0.2040259, 1.0572252),
    ),
    "display-p3": (
        (2.49349691, -0.93138362, -0.40271078),
        (-0.82948897, 1.76266406, 0.02362469),
        (0.03584583, -0.07617239, 0.95688452),
    ),
}

_LMS_FROM_XYZ = (
    (0.8189330101, 0.3618667424, -0.1288597137),
    (0.0329845436, 0.9293118715, 0.0361456387),
    (0.0482003018, 0.2643662691, 0.6338517070),
)
_XYZ_FROM_LMS = (
    (1.2270138511, -0.5577999807, 0.2812561490),
    (-0.0405801784, 1.1122568696, -0.0716766787),
    (-0.0763812845, -0.4214819784, 1.5861632204),
)
_OKLAB_FROM_LMS = (
    (0.2104542553, 0.7936177850, -0.0040720468),
    (1.9779984951, -2.4285922050, 0.4505937099),
    (0.0259040371, 0.7827717662, -0.8086757660),
)
_LMS_FROM_OKLAB = (
    (1.0000000000, 0.3963377774, 0.2158037573),
    (1.0000000000, -0.1055613458, -0.0638541728),
    (1.0000000000, -0.0894841775, -1.2914855480),
)

SUPPORTED_PROFILES = tuple(_XYZ_FROM_LINEAR)


def _profile_key(profile: str) -> str:
    return "display-p3" if profile == "display-p3" else "srgb"


def eotf(value: float) -> float:
    """sRGB / Display P3 共用的 IEC 61966-2-1 传递函数（编码值 → 线性光）。"""
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def oetf(linear: float) -> float:
    linear = 0.0 if linear < 0.0 else (1.0 if linear > 1.0 else linear)
    return 12.92 * linear if linear <= 0.0031308 else 1.055 * (linear ** (1 / 2.4)) - 0.055


def _mul(matrix, a: float, b: float, c: float) -> tuple[float, float, float]:
    return (
        matrix[0][0] * a + matrix[0][1] * b + matrix[0][2] * c,
        matrix[1][0] * a + matrix[1][1] * b + matrix[1][2] * c,
        matrix[2][0] * a + matrix[2][1] * b + matrix[2][2] * c,
    )


def _cbrt(value: float) -> float:
    return math.copysign(abs(value) ** (1 / 3), value)


def rgb8_to_oklab(red: int, green: int, blue: int, profile: str = "srgb") -> tuple[float, float, float]:
    key = _profile_key(profile)
    r, g, b = eotf(red / 255.0), eotf(green / 255.0), eotf(blue / 255.0)
    x, y, z = _mul(_XYZ_FROM_LINEAR[key], r, g, b)
    l, m, s = _mul(_LMS_FROM_XYZ, x, y, z)
    return _mul(_OKLAB_FROM_LMS, _cbrt(l), _cbrt(m), _cbrt(s))


def oklab_to_rgb8(lightness: float, a: float, b: float, profile: str = "srgb") -> tuple[int, int, int]:
    """反变换。超出色域时做逐通道钳位并由调用方检查 out_of_gamut。"""
    key = _profile_key(profile)
    l_, m_, s_ = _mul(_LMS_FROM_OKLAB, lightness, a, b)
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    x, y, z = _mul(_XYZ_FROM_LMS, l, m, s)
    r, g, bl = _mul(_LINEAR_FROM_XYZ[key], x, y, z)
    return tuple(max(0, min(255, round(oetf(channel) * 255))) for channel in (r, g, bl))


def oklab_out_of_gamut(lightness: float, a: float, b: float, profile: str = "srgb") -> bool:
    key = _profile_key(profile)
    l_, m_, s_ = _mul(_LMS_FROM_OKLAB, lightness, a, b)
    x, y, z = _mul(_XYZ_FROM_LMS, l_ ** 3, m_ ** 3, s_ ** 3)
    channels = _mul(_LINEAR_FROM_XYZ[key], x, y, z)
    return any(channel < -0.001 or channel > 1.001 for channel in channels)


def oklab_to_oklch(lightness: float, a: float, b: float) -> tuple[float, float, float]:
    chroma = math.hypot(a, b)
    hue = math.degrees(math.atan2(b, a))
    if hue < 0:
        hue += 360.0
    return lightness, chroma, hue


def oklch_to_oklab(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    radians = math.radians(hue)
    return lightness, chroma * math.cos(radians), chroma * math.sin(radians)


def delta_e_ok(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def hue_distance(first: float, second: float) -> float:
    """两个色相角之间的最短夹角，0–180。"""
    gap = abs(first - second) % 360.0
    return 360.0 - gap if gap > 180.0 else gap


def hex_from_rgb8(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def format_oklch(lightness: float, chroma: float, hue: float) -> str:
    """CSS Color 4 语法。色相在无彩色时按 none 表达，避免伪造方向。"""
    if chroma < 0.002:
        return f"oklch({lightness * 100:.1f}% 0 none)"
    return f"oklch({lightness * 100:.1f}% {chroma:.4f} {hue:.1f})"
