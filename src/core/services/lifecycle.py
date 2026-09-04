"""集中管理进程内服务的启动与关闭顺序。

调用方为每个服务注册异步 ``startup`` 和 ``shutdown`` 回调；管理器按注册顺序
启动，遇到启动异常立即停止后续启动并向上抛出，关闭时按逆序执行并记录单个
服务的关闭错误。模块级 ``lifecycle`` 提供进程范围的默认管理器。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from src.core.common.logger import get_logger

logger = get_logger(__name__)

StartupFn = Callable[[], Awaitable[None]]
ShutdownFn = Callable[[], Awaitable[None]]


@dataclass
class _Service:
    """一项服务的名称和生命周期回调。"""

    name: str
    startup: StartupFn
    shutdown: ShutdownFn


class LifecycleManager:
    """维护服务注册表并执行有序生命周期操作。"""

    def __init__(self) -> None:
        """创建空的服务注册表。

        副作用：
            初始化进程内服务列表；不执行启动或关闭回调。
        """

        self._services: list[_Service] = []

    def register(
        self,
        name: str,
        startup: StartupFn,
        shutdown: ShutdownFn,
    ) -> None:
        """注册一项服务的启动和关闭回调。

        :param name: 用于日志和故障定位的服务名称。
        :param startup: 无参数异步启动回调。
        :param shutdown: 无参数异步关闭回调。

        副作用：
            将服务追加到启动顺序列表；不会立即执行任一回调。
        """

        self._services.append(_Service(name=name, startup=startup, shutdown=shutdown))

    async def start_all(self) -> None:
        """按注册顺序启动全部服务。

        :return: ``None``。

        :raises Exception: 任一启动回调失败时记录错误并立即向调用方传播，后续服务
                不再启动。
        """

        # 日志形态：开头一次性给出启动顺序，之后每个服务只在完成时打一行并带耗时。
        #
        # - 现象：此前每个服务输出「正在启动」「已启动」两行，多数服务为毫秒级，
        #   启动日志被冗余输出占据。
        # - 原因：「正在启动」一行仅用于定位启动卡死的位置。
        # - 后果：直接删除会导致卡死无法定位。改为开头一次性输出启动顺序：
        #   最后一条「已启动」的下一个服务即卡住位置，且不增加日志行数。
        if not self._services:
            return
        logger.info(
            "service_plan",
            count=len(self._services),
            order=" -> ".join(svc.name for svc in self._services),
        )
        for svc in self._services:
            started_at = time.perf_counter()
            try:
                await svc.startup()
            except Exception as exc:
                logger.error("service_start_failed", name=svc.name, error=str(exc))
                raise
            logger.info(
                "service_started",
                name=svc.name,
                elapsedMs=round((time.perf_counter() - started_at) * 1000),
            )

    async def stop_all(self) -> None:
        """按注册逆序关闭全部服务。

        :return: ``None``。

        副作用：
            执行所有关闭回调；单个关闭异常只记录日志，继续处理其余服务。
        """

        for svc in reversed(self._services):
            logger.info("service_stopping", name=svc.name)
            try:
                await svc.shutdown()
                logger.info("service_stopped", name=svc.name)
            except Exception as exc:
                logger.warning("service_stop_error", name=svc.name, error=str(exc))


# 进程级单例
lifecycle = LifecycleManager()
