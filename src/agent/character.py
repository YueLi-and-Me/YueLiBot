"""一次性语调的抽取逻辑；具体人设与语调内容只来自配置。"""

from __future__ import annotations

from typing import List, Optional
import random


def pick_tone(
    probability: float,
    variants: List[str],
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """按概率抽一个只影响这一轮的语调。没抽中返回 None。"""

    if probability <= 0 or not variants:
        return None
    picker = rng or random
    if picker.random() > probability:
        return None
    return picker.choice(variants)
