"""管线阶段词表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class Stage:
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
    if not stage_id:
        return ""
    return STAGES_BY_ID[stage_id].label
