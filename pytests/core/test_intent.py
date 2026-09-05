"""小本本意图队列的纯函数验收。"""

from __future__ import annotations

from src.core.awareness.intent import IntentType, PendingIntent, eligible_intents, stash


T0 = 1_760_000_000_000


def _intent(intent_type: IntentType, earliest_at: int = T0, expires_at: int = T0 + 60_000) -> PendingIntent:
    return PendingIntent(intent_type=intent_type, earliest_at=earliest_at, expires_at=expires_at,
                         activity='coding', wants_vision=False)


def test_future_intent_is_not_eligible_before_its_appointment() -> None:
    pending = stash([], _intent(IntentType.Promise, earliest_at=T0 + 3_600_000))
    remaining, candidates, expired = eligible_intents(pending, T0)
    assert remaining == pending
    assert candidates == []
    assert expired == []


def test_expired_intent_is_removed_and_priority_descends() -> None:
    expired = _intent(IntentType.Scene, expires_at=T0 - 1)
    idle = _intent(IntentType.Idle)
    promise = _intent(IntentType.Promise)
    remaining, candidates, removed = eligible_intents([idle, expired, promise], T0)
    assert expired not in remaining
    assert removed == [expired]
    assert candidates == [promise, idle]


def test_same_type_deduplicates_to_the_earliest_intent() -> None:
    first = _intent(IntentType.Scene, earliest_at=T0)
    later = _intent(IntentType.Scene, earliest_at=T0 + 1_000)
    assert stash(stash([], first), later) == [first]
