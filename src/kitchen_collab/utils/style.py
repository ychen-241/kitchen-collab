"""终端美化：零依赖 ANSI 颜色、符号与 CJK 感知对齐。

兼容性策略：
- Windows 经典 cmd：通过 os.system("") 触发 VT 序列支持（Python 3.6+ 有效）；
- 输出被重定向（管道/文件）或设置 NO_COLOR 时自动退化为纯文本；
- 东亚宽字符按 2 列宽度计算，保证表格对齐。
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# ---------------------------------------------------------------------------
# 颜色支持探测
# ---------------------------------------------------------------------------

def _enable_windows_vt() -> bool:
    """经典 cmd 需要手动开启 VT 序列；os.system("") 是最小侵入的开关。"""
    if os.name == "nt":
        try:
            os.system("")
        except Exception:
            return False
    return True


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


_COLOR = supports_color() and _enable_windows_vt()

# ANSI 转义码
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
FG_RED = "\033[31m"
FG_GREEN = "\033[32m"
FG_YELLOW = "\033[33m"
FG_BLUE = "\033[34m"
FG_MAGENTA = "\033[35m"
FG_CYAN = "\033[36m"
FG_WHITE = "\033[97m"
BG_BLUE = "\033[44m"


def paint(text: str, *codes: str) -> str:
    """按需着色；不支持颜色时原样返回。"""
    if not _COLOR or not codes:
        return text
    return "".join(codes) + text + RESET


def bold(t: str) -> str:
    return paint(t, BOLD)


def dim(t: str) -> str:
    return paint(t, DIM)


def red(t: str) -> str:
    return paint(t, FG_RED, BOLD)


def green(t: str) -> str:
    return paint(t, FG_GREEN)


def yellow(t: str) -> str:
    return paint(t, FG_YELLOW)


def blue(t: str) -> str:
    return paint(t, FG_BLUE)


def magenta(t: str) -> str:
    return paint(t, FG_MAGENTA)


def cyan(t: str) -> str:
    return paint(t, FG_CYAN)


# ---------------------------------------------------------------------------
# 符号（cmd 安全：优先用 BMP 符号，避免生僻 emoji 乱码）
# ---------------------------------------------------------------------------
OK = "✔"
NO = "✘"
WARN = "▲"
GEAR = "▶"
SIGN = "✎"
DOT = "·"
LINE = "─"


# ---------------------------------------------------------------------------
# CJK 感知对齐
# ---------------------------------------------------------------------------

def display_width(text: str) -> int:
    """字符串的终端列宽：忽略 ANSI 转义序列；东亚宽字符计 2，其余计 1。"""
    text = _ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
               for ch in text)


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格（str.ljust 对含中文的文本会错位）。"""
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def hr(width: int = 56, char: str = LINE) -> str:
    return dim(char * width)
