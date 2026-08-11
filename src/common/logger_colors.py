"""
控制台日志的模块色表、中文别名与 ANSI 转换。

每个模块一种固定颜色 + 一个中文别名，这样一屏日志滚过去，凭颜色就能分出哪几行是
感知、哪几行是模型路由，不用逐行读模块名。

两处刻意从简：
  · 色表只登记本仓库真实存在的 logger，不为将来预留条目。
    漏登记不靠运行时兜底发现，由 pytests/test_logger.py 扫 src/ 断言覆盖率。
  · 色值元组是二元（前景、粗体）。没有任何模块需要背景色，就不为它留一位。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import itertools
import os
import sys

RESET_COLOR = "\033[0m"

# 模块名（已去掉 src. 前缀）→ (十六进制前景色, 是否粗体)。
# 分组着色：同一子系统的模块用相近色相，主程序亮白加粗，追踪层用深灰不抢戏。
MODULE_COLORS: Dict[str, Tuple[str, bool]] = {
    # 核心
    "main": ("#ffffff", True),
    "selftest": ("#ffff00", False),
    # 接口层
    "api.http": ("#5f87ff", False),
    "api.ws": ("#00d7ff", False),
    # QQ 适配器
    "adapters.napcat.backend": ("#d787ff", False),
    "adapters.napcat.runner": ("#ff5f87", False),
    "adapters.napcat.transport": ("#afafff", False),
    # 归属解析
    "platform_io.registry": ("#5fd7af", False),
    # 基础设施
    "common.logger": ("#808080", False),
    "config.loader": ("#5f5faf", False),
    "prompts.registry": ("#875fff", False),
    "common.db.migrations.bootstrap": ("#875f00", False),
    "common.db.migrations.manager": ("#d78700", False),
    "common.db.migrations.v3_to_v4": ("#af8700", False),
    "common.db.migrations.v4_to_v5": ("#af8700", False),
    "common.db.migrations.v5_to_v6": ("#af8700", False),
    "common.db.migrations.v6_to_v7": ("#af8700", False),
    "common.db.migrations.v7_to_v8": ("#af8700", False),
    # 模型
    "llm_models.router": ("#008080", False),
    "llm_models.openai": ("#00afaf", False),
    "memory.embed": ("#5f87d7", False),
    "services.vector": ("#af87ff", False),
    # 业务
    "services.chat": ("#5fff00", False),
    "schedule.plan": ("#87d7af", False),
    "services.proactive": ("#ff8700", False),
    "services.vision": ("#5fafff", False),
    "services.tts": ("#ffaf00", False),
    "services.lifecycle": ("#af00ff", False),
    "observe.events": ("#6c6c6c", False),
    "services.trace_console": ("#6c6c6c", False),
}

# 模块名 → 控制台上显示的中文别名。照 CLAUDE.md 的语言规范，控制台首选简体中文。
MODULE_ALIASES: Dict[str, str] = {
    "main": "主程序",
    "selftest": "自检",
    "api.http": "HTTP接口",
    "api.ws": "事件推流",
    "adapters.napcat.backend": "QQ主体",
    "adapters.napcat.runner": "QQ运行器",
    "adapters.napcat.transport": "QQ传输",
    "platform_io.registry": "归属登记",
    "common.logger": "日志",
    "config.loader": "配置加载",
    "prompts.registry": "提示词",
    "common.db.migrations.bootstrap": "建库",
    "common.db.migrations.manager": "数据库迁移",
    "common.db.migrations.v3_to_v4": "迁移v3→v4",
    "common.db.migrations.v4_to_v5": "迁移v4→v5",
    "common.db.migrations.v5_to_v6": "迁移v5→v6",
    "common.db.migrations.v6_to_v7": "迁移v6→v7",
    "common.db.migrations.v7_to_v8": "迁移v7→v8",
    "llm_models.router": "模型路由",
    "llm_models.openai": "模型连接",
    "memory.embed": "记忆嵌入",
    "services.vector": "向量召回",
    "services.chat": "对话",
    "schedule.plan": "日程",
    "services.proactive": "感知",
    "services.vision": "视觉",
    "services.tts": "语音",
    "services.lifecycle": "生命周期",
    "observe.events": "追踪",
    "services.trace_console": "追踪面板",
}


def is_color_enabled() -> bool:
    """
    这一路输出该不该上色。

    ★ 光看 sys.stdout.isatty() 不够：被 Electron 的 PythonSupervisor 拉起时
      stdout 永远是管道（isatty() 恒为 False），但这个管道最终确实会被逐行转发
      进一个真终端（`electron/main/python/supervisor.ts` 的 `_onLine`）。
      所以补一个显式信号 YUELI_FORCE_COLOR=1，由 supervisor 在 spawn 时设好。

    logger.py 的渲染器选择、trace_console.py 的 rich Console 构造、下面的
    supports_truecolor() 三处都用这一个判据，不再各写各的。
    """
    return sys.stdout.isatty() or os.environ.get("YUELI_FORCE_COLOR") == "1"


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """`#5f87ff` / `#58f` → (95, 135, 255)。"""
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    return int(value[:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def supports_truecolor() -> bool:
    """终端是否吃 24 位真彩色转义序列；否则退到 256 色调色板。"""
    colorterm = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return True
    # Windows Terminal 不设 COLORTERM，但它是真彩色的
    if "WT_SESSION" in os.environ:
        return True
    return is_color_enabled()


def rgb_to_ansi_truecolor(rgb: Tuple[int, int, int], bold: bool = False) -> str:
    """24 位真彩色前景转义序列。"""
    prefix = "1;" if bold else ""
    red, green, blue = rgb
    return f"\033[{prefix}38;2;{red};{green};{blue}m"


def rgb_to_256_index(red: int, green: int, blue: int) -> int:
    """在 xterm-256 调色板里找欧氏距离最近的一格。"""
    # 前 16 格是系统色，接着 6×6×6 的色立方，最后 24 级灰阶
    palette: List[Tuple[int, int, int]] = [
        (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
        (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
        (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    levels = [0, 95, 135, 175, 215, 255]
    for r_index, g_index, b_index in itertools.product(range(6), range(6), range(6)):
        palette.append((levels[r_index], levels[g_index], levels[b_index]))
    for step in range(24):
        gray = 8 + step * 10
        palette.append((gray, gray, gray))

    best_index = 0
    best_distance = float("inf")
    for index, (p_red, p_green, p_blue) in enumerate(palette):
        distance = (p_red - red) ** 2 + (p_green - green) ** 2 + (p_blue - blue) ** 2
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def index_to_ansi_256(index: int, bold: bool = False) -> str:
    """256 色前景转义序列。"""
    prefix = "1;" if bold else ""
    return f"\033[{prefix}38;5;{index}m"


def hex_to_ansi(hex_color: str, bold: bool = False) -> str:
    """按当前终端能力，把十六进制色转成真彩色或 256 色的前景转义序列。"""
    rgb = hex_to_rgb(hex_color)
    if supports_truecolor():
        return rgb_to_ansi_truecolor(rgb, bold)
    return index_to_ansi_256(rgb_to_256_index(*rgb), bold)


# 导入时一次性算好，渲染每行日志时只做一次字典查找。
CONVERTED_MODULE_COLORS: Dict[str, str] = {
    name: hex_to_ansi(hex_color, bold) for name, (hex_color, bold) in MODULE_COLORS.items()
}


def module_color(logger_name: str) -> str:
    """取模块的 ANSI 前景色；未登记的模块返回空串（不上色，但照常打印）。"""
    return CONVERTED_MODULE_COLORS.get(logger_name, "")


def module_alias(logger_name: str) -> str:
    """取模块的中文别名；未登记的模块原样返回点分模块名。"""
    return MODULE_ALIASES.get(logger_name, logger_name)


def normalize_logger_name(logger_name: str) -> str:
    """
    `src.services.proactive` → `services.proactive`。

    调用方基本都是 get_logger(__name__)，拿到的是带 src. 前缀的点分路径；
    色表键统一不带这个前缀，省得每条都重复一遍包名。
    """
    if logger_name.startswith("src."):
        return logger_name[len("src."):]
    return logger_name


def level_color(level: str) -> str:
    """日志级别对应的 ANSI 色。lite 排版下级别不占位置，只体现在时间戳颜色上。"""
    return _LEVEL_COLORS.get(level.lower(), "")


# 级别色：debug 橙、info 天蓝、warning 黄、error 红、critical 紫。
_LEVEL_COLORS: Dict[str, str] = {
    "debug": "\033[38;5;208m",
    "info": "\033[38;5;117m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "critical": "\033[35m",
}


def enable_windows_ansi() -> None:
    """
    让 Windows 控制台认 ANSI 转义序列。

    ★ 以前这件事是 structlog 的 ConsoleRenderer 在背后替我们做的（它内部会 init
      colorama）。换成自己写的渲染器之后没人做了，conhost 下会把转义序列原样打成
      乱码。just_fix_windows_console() 只开 VT 处理、不包装流，stdout 是管道时
      是空操作——不会影响被 supervisor 转发的那条路径。
    """
    if sys.platform != "win32":
        return
    import colorama
    colorama.just_fix_windows_console()


__all__ = [
    "MODULE_ALIASES",
    "MODULE_COLORS",
    "RESET_COLOR",
    "enable_windows_ansi",
    "is_color_enabled",
    "level_color",
    "module_alias",
    "module_color",
    "normalize_logger_name",
]
