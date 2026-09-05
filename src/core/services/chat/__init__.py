"""对话编排包。

``ChatService`` 是整套对话流程的编排者：持有按角色拆分的 LLM provider、
``MemoryStore``、``Persona`` 与 ``DayPlanService``，把入站消息推进为一次回合，
并通过 WebSocket 把事件推给 Electron 主进程。

包内分工：

- ``service``：``ChatService`` 本体与回合主循环。
- ``state``：回合与会话的内部数据结构（缓冲、门控结果、留痕、等待句柄等）。
- ``helpers``：不依赖 ``self`` 的模块级纯函数。

本模块只做再导出。原先 ``chat`` 是单文件模块，全项目都以
``from src.core.services.chat import ChatService`` 的形式引用；拆包后在此保持
同一入口，调用方无需改动。
"""

from .constants import (
    CHAT_POLL_INTERVAL_S,
    EMOJI_MAX_PER_REPLY_WINDOW,
    MAX_TURN_ROUNDS,
    PROACTIVE_TRIGGER_MESSAGE,
    SCENE_WINDOW_MESSAGES,
    SESSION_GAP_MS,
)
from .helpers import _facts_for_prompt
from .service import ChatService, InboundMessage
from .state import (
    _DirectFollowUpState,
    _InflightTurn,
    _RetrievalTrace,
    _WaitHold,
)

__all__ = [
    'CHAT_POLL_INTERVAL_S',
    'ChatService',
    'EMOJI_MAX_PER_REPLY_WINDOW',
    'InboundMessage',
    'MAX_TURN_ROUNDS',
    'PROACTIVE_TRIGGER_MESSAGE',
    'SCENE_WINDOW_MESSAGES',
    'SESSION_GAP_MS',
]
