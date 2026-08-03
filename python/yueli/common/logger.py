"""
structlog 日志封装。

所有模块通过 get_logger(__name__) 拿到绑定了模块名的 logger，
不直接调 print() / logging.warning()。
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def initialize_logging(level: str = "INFO") -> None:
    """
    应用启动时调用一次。

    开发模式输出彩色人类可读格式；生产/管道模式输出 JSON（方便日志收集）。
    判断依据：stdout 是否为 tty，**或** YUELI_FORCE_COLOR=1。

    ★ 后者是必须的：被 Electron 的 PythonSupervisor 拉起时，stdout 永远是
      管道（`sys.stdout.isatty()` 恒为 False），但这个管道最终确实会被
      逐行转发进一个真终端（`src/main/python/supervisor.ts` 的 `_onLine`）。
      不加这个环境变量信号，`npm run dev` 里永远只能看到未着色的 JSON。
    """
    is_tty = sys.stdout.isatty() or os.environ.get("YUELI_FORCE_COLOR") == "1"

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        # ⚠ 这里**不能**用 structlog.stdlib.add_logger_name。
        #   它读的是 logger.name，而下面用的 PrintLoggerFactory 造出的
        #   PrintLogger 没有 .name 属性 —— 配上去会在**第一次真正打日志时**
        #   抛 AttributeError，而不是在 configure() 时报错。
        #   模块名改由 get_logger() 用 .bind(logger=name) 显式绑定，
        #   与 logger_factory 的选择解耦。
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_tty:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        shared_processors.append(structlog.processors.dict_tracebacks)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    """
    返回绑定了模块名的 logger。

    模块名走 .bind(logger=...) 而不是 add_logger_name 处理器：
    后者依赖 stdlib logger 的 .name 属性，与 PrintLoggerFactory 不兼容。
    显式绑定则对任何 logger_factory 都成立。
    """
    return structlog.get_logger().bind(logger=name)
