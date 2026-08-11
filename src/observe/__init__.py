"""观察事件、处理状态、事件账本与实时广播。

本包记录对话和后台服务的可追踪事件，提供统一来源绑定和状态转换接口；事件账本
由调用方配置数据库后启用。
"""

from .events import bind_origin, emit, enter_stage

__all__ = ["bind_origin", "emit", "enter_stage"]
