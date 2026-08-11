"""定义管线阶段的稳定 ID、中文标签及查找表。

阶段定义由事件追踪和观察面板共同使用；业务层只传递 `Stage` 或稳定 ID，不在
多个调用点重复维护显示文本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class Stage:
    """表示一个不可变的管线阶段。

    :ivar id: 稳定的机器可读阶段 ID。
    :ivar label: 面向中文观察面板的显示标签。
    """

    id: str
    label: str


RECEIVED = Stage("received", "已收到")
GATED = Stage("gated", "静默接收")
CONTEXT = Stage("context", "组织上下文")
EXPRESSION = Stage("expression", "挑表达方式")
GENERATING = Stage("generating", "等待模型")
DISPATCHING = Stage("dispatching", "投递出站")
REPLIED = Stage("replied", "已回复")
FAILED = Stage("failed", "失败")

STAGES: Tuple[Stage, ...] = (
    RECEIVED,
    GATED,
    CONTEXT,
    EXPRESSION,
    GENERATING,
    DISPATCHING,
    REPLIED,
    FAILED,
)
STAGES_BY_ID: Dict[str, Stage] = {stage.id: stage for stage in STAGES}


def label_for(stage_id: str) -> str:
    """把阶段 ID 转换为中文标签。

    :param stage_id: 阶段 ID；空字符串表示未绑定阶段。
    :return: 已登记阶段的中文标签；未知 ID 原样返回，空值返回空字符串。
    :side_effects: 不修改阶段注册表。
    """
    if not stage_id:
        return ""
    stage = STAGES_BY_ID.get(stage_id)
    return stage.label if stage is not None else stage_id
