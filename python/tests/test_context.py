"""上下文连续性回归。

覆盖三类曾经把历史悄悄改坏的路径：中断、报错，以及回灌历史里的副作用标签。
另外钉住组装出的 messages 数组形状——此前只有「非空」断言，一个只发最新
用户消息的回归可以通过全部测试。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import pytest

from yueli.agent.history import (
    close_dangling_say, fit_char_budget, normalize_history, strip_side_effect_tags,
)
from yueli.services.chat import ChatService


class _ScriptedProvider:
    """按脚本吐 chunk；可选在第 n 个 chunk 之后抛错。"""

    def __init__(self, chunks: List[str], raise_at: int | None = None,
                 error: Exception | None = None) -> None:
        self._chunks = chunks
        self._raise_at = raise_at
        self._error = error or RuntimeError('boom')
        self.seen: List[Dict[str, Any]] = []

    async def stream(self, messages: List[Dict[str, Any]], **_kw: Any) -> AsyncIterator[Dict[str, str]]:
        self.seen = messages
        for index, chunk in enumerate(self._chunks):
            if self._raise_at is not None and index == self._raise_at:
                raise self._error
            yield {'text': chunk}


def _chat(db, provider) -> ChatService:
    async def _noop(_channel: str, _payload: Any) -> None:
        return None
    return ChatService(db, provider, _noop)


def _roles(store) -> List[str]:
    return [m.role for m in store.working_memory(40)]


# ── 读时修复 ────────────────────────────────────────────────────────────

def test_side_effect_tags_stripped_but_say_kept() -> None:
    raw = '<say emotion="smile">好啊</say><memory type="偏好">他喜欢咖啡</memory><mood favor="+1"/>'
    cleaned = strip_side_effect_tags(raw)

    assert '<say emotion="smile">好啊</say>' in cleaned
    assert 'memory' not in cleaned
    assert 'mood' not in cleaned
    # <say> 是输出协议的 few-shot，剥掉会让模型以为纯文本也算合法输出。
    assert '好啊' in cleaned


def test_dangling_say_from_interrupted_stream_is_closed() -> None:
    assert close_dangling_say('<say emotion="sad">说到一半就被打') == \
        '<say emotion="sad">说到一半就被打</say>'
    assert close_dangling_say('<say>完整</say>') == '<say>完整</say>'
    assert close_dangling_say('   ') == ''


def test_consecutive_same_role_messages_are_merged() -> None:
    # 这正是历史被打断损坏后的形态：回复丢了，剩下连着两条 user。
    history = normalize_history([
        {'role': 'user', 'content': '第一句'},
        {'role': 'user', 'content': '第二句'},
        {'role': 'assistant', 'content': '<say>接住了</say>'},
    ])

    assert [m['role'] for m in history] == ['user', 'assistant']
    assert history[0]['content'] == '第一句\n第二句'


def test_history_never_starts_with_assistant() -> None:
    history = normalize_history([
        {'role': 'assistant', 'content': '<say>我先说的</say>'},
        {'role': 'user', 'content': '嗯'},
    ])

    assert [m['role'] for m in history] == ['user']


def test_normalize_is_idempotent() -> None:
    once = normalize_history([
        {'role': 'user', 'content': '在吗'},
        {'role': 'assistant', 'content': '<say>在</say><mood favor="+1"/>'},
    ])
    assert normalize_history(once) == once


def test_char_budget_drops_oldest_and_repairs_structure() -> None:
    history = [
        {'role': 'user', 'content': 'x' * 100},
        {'role': 'assistant', 'content': 'y' * 100},
        {'role': 'user', 'content': '最新的一句'},
    ]
    trimmed = fit_char_budget(history, budget=120)

    assert trimmed[0]['role'] == 'user'          # 裁完仍由 user 起头
    assert trimmed[-1]['content'] == '最新的一句'


# ── 写入端：中断与报错 ──────────────────────────────────────────────────

async def test_interrupted_turn_keeps_what_was_already_said(db) -> None:
    provider = _ScriptedProvider(['<say emotion="smile">前半句', '后半句</say>'])
    chat = _chat(db, provider)

    await chat.send('第一句')
    await chat._inflight
    chat.interrupt()
    await chat.send('第二句')
    await chat._inflight

    roles = _roles(chat.memory)
    # 关键：不能出现连续两条 user——那意味着她看不到自己刚说过什么。
    assert not any(a == b == 'user' for a, b in zip(roles, roles[1:])), roles
    assert 'assistant' in roles


async def test_error_with_no_output_rolls_the_turn_back(db) -> None:
    # raise_at=0：进入循环就抛，一个 chunk 都没吐出来。
    provider = _ScriptedProvider(['永远不会被 yield 的内容'], raise_at=0)
    chat = _chat(db, provider)

    await chat.send('会失败的一句')
    await chat._inflight

    # 一个字都没吐出来，这轮当作没发生。
    assert _roles(chat.memory) == []


async def test_error_after_partial_output_keeps_both_sides(db) -> None:
    provider = _ScriptedProvider(['<say>已经说出口的半句', 'X'], raise_at=1)
    chat = _chat(db, provider)

    await chat.send('提问')
    await chat._inflight

    # 用户已经看到半句回复了，删掉 user 消息会让历史出现「凭空的回复」。
    assert _roles(chat.memory) == ['user', 'assistant']


# ── 组装出的 messages 形状 ─────────────────────────────────────────────

async def test_messages_array_carries_full_alternating_history(db) -> None:
    provider = _ScriptedProvider(['<say>好</say><memory type="偏好">他喜欢咖啡</memory>'])
    chat = _chat(db, provider)

    for text in ['第一句', '第二句', '第三句']:
        await chat.send(text)
        await chat._inflight

    sent = provider.seen
    assert sent[0]['role'] == 'system'
    body = sent[1:]
    assert len(body) >= 3, f'历史没有被带上：{body}'
    assert body[0]['role'] == 'user'
    assert [m['role'] for m in body] == ['user', 'assistant'] * (len(body) // 2) + \
        (['user'] if len(body) % 2 else [])
    # 副作用标签不该回灌——否则等于每轮示范「我又记了一条」。
    assert all('<memory' not in m['content'] and '<mood' not in m['content'] for m in body)
    assert any('<say>' in m['content'] for m in body if m['role'] == 'assistant')


# ── 会话级人设稳定 ─────────────────────────────────────────────────────

async def test_tone_and_samples_stay_stable_within_one_session(db) -> None:
    provider = _ScriptedProvider(['<say>嗯</say>'])
    chat = _chat(db, provider)

    systems: List[str] = []
    for text in ['第一句', '第二句', '第三句']:
        await chat.send(text)
        await chat._inflight
        systems.append(provider.seen[0]['content'])

    tone = chat._session_tone
    assert len({chat._session_seed}) == 1
    if tone:
        assert all(tone in s for s in systems), '同一会话内语气不应逐轮跳变'


async def test_new_session_after_long_gap_rerolls_persona(db) -> None:
    provider = _ScriptedProvider(['<say>嗯</say>'])
    chat = _chat(db, provider)

    await chat.send('第一句')
    await chat._inflight
    first_seed = chat._session_seed

    # 把上一条消息推到会话间隔之外
    from yueli.services.chat import SESSION_GAP_MS
    db.execute('UPDATE messages SET created_at = created_at - ?', (SESSION_GAP_MS * 2,))
    db.commit()

    await chat.send('很久以后的一句')
    await chat._inflight

    assert chat._session_seed != first_seed, '跨过静默间隔后应该重开一段会话'
