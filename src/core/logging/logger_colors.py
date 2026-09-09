"""维护控制台日志的模块颜色、中文别名和 ANSI 转义序列。

颜色表登记当前代码实际使用的 logger，别名用于控制台和 WebUI 的简体中文展示；
日志渲染器将模块名映射为高对比颜色与别名，并把事件、字段名和字段值分层着色。
颜色值使用 ``(前景色, 是否加粗)`` 二元组，不保留未使用的背景色字段。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import itertools
import os
import sys

RESET_COLOR = "\033[0m"

# 模块名（已去掉 src. 前缀）→ (十六进制前景色, 是否粗体)。
# 分组着色：同一子系统使用相近色相，但相邻高频模块保持足够的明度或色相差。
# 所有颜色都按深色终端选取，避免低亮灰色正文在 PowerShell 和 WebUI 中糊成一片。
MODULE_COLORS: Dict[str, Tuple[str, bool]] = {
    # 核心
    "main": ("#ffffff", True),
    "selftest": ("#ffff00", False),
    # 接口层
    "api.http": ("#5fafff", False),
    "api.model_config": ("#87afff", False),
    "api.ws": ("#00d7ff", True),
    # QQ 适配器
    "platforms.onebot11.host": ("#ffafd7", True),
    "platforms.onebot11.backend": ("#d787ff", True),
    "platforms.onebot11.cards": ("#ff87d7", False),
    "platforms.onebot11.forward": ("#ff87af", False),
    "platforms.onebot11.runner": ("#ff5f87", True),
    "platforms.onebot11.transport": ("#afafff", True),
    # 插件契约
    "plugin_system.adapter": ("#87d7af", True),
    "plugin_system.context": ("#87d7af", True),
    # 插件宿主
    "plugin_system.registry": ("#87d787", False),
    "plugin_system.config": ("#afd7af", False),
    "plugin_system.components": ("#d7d787", False),
    # 适配器插件
    "adapters.yueli_napcat_adapter.plugin": ("#ffd7af", False),
    # 归属解析
    "platform_io.registry": ("#5fffd7", False),
    "commands.registry": ("#ffaf5f", True),
    # 基础设施
    "logging.logger": ("#ffd75f", True),
    "logging.logger_colors": ("#ffaf5f", False),
    "runtime.self_check": ("#5fffd7", True),
    "runtime.telemetry": ("#87afd7", False),
    "config.loader": ("#8787ff", False),
    "prompts.registry": ("#af87ff", True),
    "config.model_webui": ("#87afff", False),
    "config.settings_webui": ("#af87ff", False),
    "db.migrations.bootstrap": ("#d7af5f", False),
    "db.schema_report": ("#ffaf00", True),
    "config.upgrade": ("#ffaf00", True),
    "config.bootstrap": ("#ffd787", True),
    "db.migrations.manager": ("#ffaf00", True),
    "db.migrations.v3_to_v4": ("#ffd75f", False),
    "db.migrations.v4_to_v5": ("#ffd75f", False),
    "db.migrations.v5_to_v6": ("#ffd75f", False),
    "db.migrations.v6_to_v7": ("#ffd75f", False),
    "db.migrations.v7_to_v8": ("#ffd75f", False),
    "db.migrations.v8_to_v9": ("#ffd75f", False),
    "db.migrations.v9_to_v10": ("#ffd75f", False),
    "db.migrations.v10_to_v11": ("#ffd75f", False),
    "db.migrations.v26_to_v27": ("#ffd75f", False),
    "db.migrations.v28_to_v29": ("#ffd75f", False),
    "db.migrations.v31_to_v32": ("#ffd75f", False),
    # 模型
    "llm_models.router": ("#00ffff", True),
    "llm_models.openai": ("#00d7d7", False),
    "memory.embed": ("#5fafff", False),
    "memory.pagerank": ("#5f87ff", True),
    "memory.quantize": ("#8787ff", False),
    "memory.vector_health": ("#5fafd7", False),
    "memory.tuning": ("#87afd7", True),
    "memory.import_center": ("#d7af87", True),
    "services.maintenance.vector": ("#af87ff", True),
    # 业务
    "services.chat.service": ("#5fff5f", True),
    "services.chat.background": ("#5fd75f", False),
    "services.chat.context_build": ("#87ff87", False),
    "services.chat.group_observe": ("#5fd7af", False),
    "services.chat.outbound": ("#afff87", False),
    "services.chat.scene": ("#87d75f", False),
    "services.media.chat_image": ("#5fafff", True),
    "services.media.emoji": ("#ffd75f", True),
    "services.maintenance.jargon_stats": ("#afd75f", False),
    "services.maintenance.edge_decay": ("#87d7af", False),
    "agent.jargon_mine": ("#5fd787", False),
    "services.maintenance.jargon_learn": ("#d7ff5f", True),
    "services.maintenance.memory_feedback": ("#afd7ff", False),
    "schedule.plan": ("#87ffaf", False),
    "schedule.timeline": ("#5fffd7", True),
    "services.proactive": ("#ff8700", True),
    "desktop.vision": ("#5fd7ff", True),
    "desktop.sensor": ("#5fffd7", False),
    "services.media.tts": ("#ffaf00", True),
    "services.host.lifecycle": ("#d75fff", True),
    "observe.events": ("#ff5fd7", True),
    "observe.source": ("#ff5faf", False),
    "services.console.trace_console": ("#ff87d7", True),
    "services.dev.dev_commands": ("#d7ffd7", True),
    "services.dev.install_stats": ("#afffaf", True),
    "agent.expression_learn": ("#ffaf87", False),
    "agent.impression": ("#d7af87", False),
    # 运行画像
    # 进程监护：入口拉起的 QQ 适配器与桌面外壳
    "runtime.child_process": ("#87d7d7", True),
    "services.host.adapter_host": ("#ffafd7", False),
    "services.host.desktop_shell": ("#afd7ff", True),
}

# 模块名 → 控制台上显示的中文别名；控制台输出统一使用简体中文。
MODULE_ALIASES: Dict[str, str] = {
    "main": "主程序",
    "selftest": "自检",
    "api.http": "HTTP接口",
    "api.model_config": "模型配置",
    "api.ws": "事件推流",
    "platforms.onebot11.host": "QQ适配宿主",
    "platforms.onebot11.backend": "QQ主体",
    "platforms.onebot11.cards": "QQ卡片解析",
    "platforms.onebot11.forward": "QQ转发解析",
    "platforms.onebot11.runner": "QQ运行器",
    "platforms.onebot11.transport": "QQ传输",
    "plugin_system.adapter": "插件契约",
    "plugin_system.context": "插件",
    "plugin_system.registry": "插件注册表",
    "plugin_system.config": "插件配置",
    "plugin_system.components": "插件组件",
    "adapters.yueli_napcat_adapter.plugin": "NapCat适配",
    "platform_io.registry": "归属登记",
    "commands.registry": "开发者命令",
    "logging.logger": "日志",
    "logging.logger_colors": "日志配色",
    "runtime.self_check": "运行自检",
    "runtime.telemetry": "匿名统计",
    "config.loader": "配置加载",
    "prompts.registry": "提示词",
    "config.model_webui": "模型配置读写",
    "config.settings_webui": "设置页配置",
    "db.migrations.bootstrap": "建库",
    "db.migrations.manager": "数据库迁移",
    "db.schema_report": "数据库结构",
    "config.upgrade": "配置升级",
    "config.bootstrap": "配置初始化",
    "db.migrations.v3_to_v4": "迁移v3→v4",
    "db.migrations.v4_to_v5": "迁移v4→v5",
    "db.migrations.v5_to_v6": "迁移v5→v6",
    "db.migrations.v6_to_v7": "迁移v6→v7",
    "db.migrations.v7_to_v8": "迁移v7→v8",
    "db.migrations.v8_to_v9": "迁移v8→v9",
    "db.migrations.v9_to_v10": "迁移v9→v10",
    "db.migrations.v10_to_v11": "迁移v10→v11",
    "db.migrations.v26_to_v27": "迁移v26→v27",
    "db.migrations.v28_to_v29": "迁移v28→v29",
    "db.migrations.v31_to_v32": "迁移v31→v32",
    "llm_models.router": "模型路由",
    "llm_models.openai": "模型连接",
    "memory.embed": "记忆嵌入",
    "memory.pagerank": "图谱重排",
    "memory.quantize": "向量量化",
    "memory.vector_health": "向量健康",
    "memory.tuning": "检索调优",
    "memory.import_center": "导入中心",
    "services.maintenance.vector": "向量召回",
    "services.chat.service": "对话",
    "services.chat.background": "对话后台队列",
    "services.chat.context_build": "上下文组装",
    "services.chat.group_observe": "群聊观察",
    "services.chat.outbound": "出站投递",
    "services.chat.scene": "场景观察",
    "services.media.chat_image": "聊天图片",
    "services.media.emoji": "表情包",
    "services.maintenance.jargon_stats": "高频词统计",
    "services.maintenance.edge_decay": "联想边衰减",
    "agent.jargon_mine": "黑话挖掘",
    "services.maintenance.jargon_learn": "黑话学习",
    "services.maintenance.memory_feedback": "记忆反馈",
    "schedule.plan": "生活方向",
    "schedule.timeline": "活动时间线",
    "services.proactive": "感知",
    "desktop.vision": "视觉",
    "desktop.sensor": "桌面感知",
    "services.media.tts": "语音",
    "services.host.lifecycle": "生命周期",
    "observe.events": "追踪",
    "observe.source": "消息来源",
    "services.console.trace_console": "追踪面板",
    "services.dev.dev_commands": "开发者命令",
    "services.dev.install_stats": "安装统计",
    "agent.expression_learn": "表达学习",
    "agent.impression": "会话印象",
    "runtime.child_process": "子进程",
    "services.host.adapter_host": "适配器进程",
    "services.host.desktop_shell": "桌面外壳",
}


# 全进程着色开关的显式裁定值。None 表示尚未裁定，此时按终端能力自行判断。
# 存在的理由：着色此前由三处各自决定（structlog 渲染器读 log.color_scope、管线追踪
# 出口与信息框各自调 is_color_enabled()），结果是同一屏里普通日志有色而信息框全灰。
# 收敛到这一个开关之后，`log.color_scope = 'none'` 能真正关掉全部着色。
_color_override: bool | None = None


def set_color_enabled(enabled: bool) -> None:
    """由日志初始化统一裁定全进程是否着色。

    :param enabled: 是否允许输出 ANSI 颜色，通常为终端能力与 ``log.color_scope`` 的合取。
    :return: 无返回值。
    副作用：改写模块级裁定值，此后 :func:`is_color_enabled` 一律返回该值。
    """
    global _color_override
    _color_override = enabled


def detect_color_support() -> bool:
    """只探测终端能力，不受裁定值影响。

    与 :func:`is_color_enabled` 分开的原因：裁定值是模块级全局状态，
    而做裁定的 ``initialize_logging`` 本身必须从环境重新推导。

    - 现象：两者合一时，先跑过一次无色初始化的进程里，后续
      ``initialize_logging`` 会读到上一次留下的裁定值，新设的 ``YUELI_FORCE_COLOR``
      与 ``log.color_scope`` 全部失效。
    - 原因：``colored = is_color_enabled() and ...`` 把上一次的结论当成了这一次的
      输入，裁定自我引用并固定不变。
    - 后果：全量测试里表现为顺序依赖——单跑通过、连跑失败，且失败的是断言日志
      渲染文本的用例（真机上则是重新初始化日志后颜色再也回不来）。

    :return: 标准输出是 TTY，或环境变量 ``YUELI_FORCE_COLOR`` 等于 ``1``。
    副作用：只读取标准输出状态和进程环境变量。
    """
    return sys.stdout.isatty() or os.environ.get("YUELI_FORCE_COLOR") == "1"


def is_color_enabled() -> bool:
    """供消费方判断是否着色：管线追踪出口与信息框都读这一个开关。

    :return: :func:`set_color_enabled` 已裁定时返回该裁定值；否则回落到
        :func:`detect_color_support`。做裁定的一方不要调用本函数，用探测函数。
    副作用：只读取模块级裁定值、标准输出状态和进程环境变量。
    """
    if _color_override is not None:
        return _color_override
    return detect_color_support()


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """将三位或六位十六进制颜色转换为 RGB 整数元组。

    :param hex_color: 带或不带 ``#`` 的三位或六位十六进制颜色文本。

    :return: ``(red, green, blue)`` 元组，每个分量范围为 ``0`` 到 ``255``。

    :raises ValueError: 颜色文本包含非十六进制字符或长度无法解析。
    """
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    return int(value[:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def supports_truecolor() -> bool:
    """判断当前终端是否支持 24 位 ANSI 真彩色。

    :return: 检测到 ``COLORTERM`` 为 truecolor/24bit、运行于 Windows Terminal，或
        标准输出已启用颜色时返回 ``True``；否则返回 ``False``。

    副作用：
        读取进程环境变量和标准输出状态，不修改终端配置。
    """
    colorterm = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return True
    # Windows Terminal 不设 COLORTERM，但它是真彩色的
    if "WT_SESSION" in os.environ:
        return True
    return is_color_enabled()


def rgb_to_ansi_truecolor(rgb: Tuple[int, int, int], bold: bool = False) -> str:
    """将 RGB 前景色编码为 ANSI 24 位真彩色转义序列。

    :param rgb: ``(red, green, blue)`` 分量元组；分量应在 ``0`` 到 ``255`` 范围内。
    :param bold: 是否追加粗体控制码，默认 ``False``。

    :return: 可直接写入终端的 ANSI 转义序列。

    :raises ValueError: 分量无法格式化为合法整数时抛出。
    """
    prefix = "1;" if bold else ""
    red, green, blue = rgb
    return f"\033[{prefix}38;2;{red};{green};{blue}m"


def rgb_to_256_index(red: int, green: int, blue: int) -> int:
    """在 xterm 256 色调色板中查找与 RGB 欧氏距离最近的颜色索引。

    :param red: 红色分量。
    :param green: 绿色分量。
    :param blue: 蓝色分量。

    :return: ``0`` 到 ``255`` 范围内的最接近调色板索引。

    :raises TypeError: 任一分量不支持数值减法或平方运算时抛出。

    性能：
        每次调用遍历完整的 256 色调色板，时间复杂度为常数，但不应在单条日志中重复计算。
    """
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
    """将 xterm 256 色索引编码为 ANSI 前景转义序列。

    :param index: 调色板索引，建议范围为 ``0`` 到 ``255``。
    :param bold: 是否追加粗体控制码，默认 ``False``。

    :return: 可直接写入终端的 ANSI 转义序列。
    """
    prefix = "1;" if bold else ""
    return f"\033[{prefix}38;5;{index}m"


def hex_to_ansi(hex_color: str, bold: bool = False) -> str:
    """按当前终端能力将十六进制颜色转换为 ANSI 前景转义序列。

    :param hex_color: 三位或六位十六进制颜色文本。
    :param bold: 是否追加粗体控制码，默认 ``False``。

    :return: 当前终端支持真彩色时返回 24 位序列，否则返回最接近的 256 色序列。

    :raises ValueError: 颜色文本无法解析为 RGB 值。
    """
    rgb = hex_to_rgb(hex_color)
    if supports_truecolor():
        return rgb_to_ansi_truecolor(rgb, bold)
    return index_to_ansi_256(rgb_to_256_index(*rgb), bold)


# 导入时一次性算好，渲染每行日志时只做一次字典查找。
CONVERTED_MODULE_COLORS: Dict[str, str] = {
    name: hex_to_ansi(hex_color, bold) for name, (hex_color, bold) in MODULE_COLORS.items()
}

# 正文不再跟随模块色整段染色。固定的高对比层级色让事件、字段名、字段值和分隔符
# 在高频日志中保持稳定位置感，模块色只负责标识来源。
EVENT_COLOR = hex_to_ansi("#ffffff", True)
FIELD_LABEL_COLOR = hex_to_ansi("#5fd7ff", True)
FIELD_VALUE_COLOR = hex_to_ansi("#d7e4f5")
SEPARATOR_COLOR = hex_to_ansi("#5f87af")


def module_color(logger_name: str) -> str:
    """返回 logger 模块对应的 ANSI 前景色。

    :param logger_name: 不带 ``src.`` 前缀的点分模块名。

    :return: 已登记模块的 ANSI 颜色序列；未登记模块返回空字符串。
    """
    # 插件标识由清单决定，无法逐个登记；同一契约命名空间使用统一颜色。
    if logger_name.startswith('plugin_system.context.'):
        return CONVERTED_MODULE_COLORS['plugin_system.context']
    return CONVERTED_MODULE_COLORS.get(logger_name, "")


def module_alias(logger_name: str) -> str:
    """返回 logger 模块的中文显示别名。

    :param logger_name: 不带 ``src.`` 前缀的点分模块名。

    :return: 已登记模块的中文别名；未登记模块返回原始模块名。
    """
    if logger_name.startswith('plugin_system.context.'):
        plugin_id = logger_name[len('plugin_system.context.'):]
        return f'{MODULE_ALIASES["plugin_system.context"]}·{plugin_id}'
    return MODULE_ALIASES.get(logger_name, logger_name)


def normalize_logger_name(logger_name: str) -> str:
    """移除 logger 名称中的包路径前缀。

    ``src.core.`` 一并剥掉：内核是绝大多数模块的归属，写进日志只会挤占行宽而
    不提供区分度。``desktop.`` 与 ``platforms.`` 保留，它们标明来源是桌面外壳
    还是某个协议适配器，排查时有用。

    :param logger_name: ``get_logger(__name__)`` 产生的点分模块路径。

    :return: 剥掉 ``src.core.`` 或 ``src.`` 前缀后的模块名，两者都不匹配时返回原字符串。

    :raises AttributeError: 参数不是字符串时由 ``startswith`` 操作触发。
    """
    for prefix in ("src.core.", "src."):
        if logger_name.startswith(prefix):
            return logger_name[len(prefix):]
    return logger_name


def level_color(level: str) -> str:
    """返回日志级别对应的 ANSI 颜色序列。

    :param level: 日志级别名称，不区分大小写。

    :return: 已知级别的 ANSI 颜色序列；未知级别返回空字符串。
    """
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
    """在 Windows 上启用控制台的 ANSI/VT 处理。

    :return: 无返回值；非 Windows 平台直接返回。
    副作用：Windows 平台调用 colorama 的控制台初始化函数。
    """
    if sys.platform != "win32":
        return
    import colorama
    colorama.just_fix_windows_console()


__all__ = [
    "EVENT_COLOR",
    "FIELD_LABEL_COLOR",
    "FIELD_VALUE_COLOR",
    "MODULE_ALIASES",
    "MODULE_COLORS",
    "RESET_COLOR",
    "SEPARATOR_COLOR",
    "detect_color_support",
    "enable_windows_ansi",
    "is_color_enabled",
    "level_color",
    "module_alias",
    "module_color",
    "normalize_logger_name",
    "set_color_enabled",
]
