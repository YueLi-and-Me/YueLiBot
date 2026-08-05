"""
服务生命周期编排。

借鉴 MaiBot src/common/runtime_loop 的思路，但粒度适配这个项目体量：
4 个 service 而不是 26 个。各 service 注册 startup/shutdown 回调，
manager 按注册顺序启动、逆序关停。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from yueli.common.logger import get_logger

logger = get_logger(__name__)

StartupFn = Callable[[], Awaitable[None]]
ShutdownFn = Callable[[], Awaitable[None]]


@dataclass
class _Service:
    name: str
    startup: StartupFn
    shutdown: ShutdownFn


class LifecycleManager:
    def __init__(self) -> None:
        self._services: list[_Service] = []

    def register(
        self,
        name: str,
        startup: StartupFn,
        shutdown: ShutdownFn,
    ) -> None:
        self._services.append(_Service(name=name, startup=startup, shutdown=shutdown))

    async def start_all(self) -> None:
        for svc in self._services:
            logger.info("service_starting", name=svc.name)
            try:
                await svc.startup()
                logger.info("service_started", name=svc.name)
            except Exception as exc:
                logger.error("service_start_failed", name=svc.name, error=str(exc))
                raise

    async def stop_all(self) -> None:
        for svc in reversed(self._services):
            logger.info("service_stopping", name=svc.name)
            try:
                await svc.shutdown()
                logger.info("service_stopped", name=svc.name)
            except Exception as exc:
                logger.warning("service_stop_error", name=svc.name, error=str(exc))


# 进程级单例
lifecycle = LifecycleManager()
