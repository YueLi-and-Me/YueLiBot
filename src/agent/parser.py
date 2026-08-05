"""
流式响应解析器。直接移植自 src/core/agent/parser.ts。

模型被要求输出这种形态：
  <say emotion="害羞" gesture="捂胸口">诶、诶？你怎么突然……</say>
  <memory type="偏好">玩家喜欢深夜写代码</memory>
  <mood favor="+2" energy="-1"/>

用类 XML 标签而非 JSON，唯一原因是流式：半截 JSON 是非法的，
只能等整段收完才能 parse，会造成 2–3 秒死寂。标签可以边收边解。

必须扛住的现实情况：
 · 标签被网络包切断（`<say emo` + `tion="害羞">` 分两次到）
 · 模型完全不按格式，直接吐纯文本 —— 不能丢，按隐式发言处理
 · 模型漏写 `</say>` —— flush 时兜底收尾
 · 推理模型把思维链混在正文里（`<think>`）—— 必须丢弃
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Union

# ─────────────────────────────────────────────────────────────────────
# 事件类型（对应 TS 的 ParseEvent union）
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SayEvent:
    type: str = 'say'
    emotion: str | None = None
    gesture: str | None = None


@dataclass
class TextEvent:
    type: str = 'text'
    value: str = ''


@dataclass
class SayEndEvent:
    type: str = 'sayEnd'


@dataclass
class MemoryEvent:
    type: str = 'memory'
    memory_type: str | None = None
    content: str = ''


@dataclass
class MoodEvent:
    type: str = 'mood'
    favor: float | None = None
    energy: float | None = None


@dataclass
class PromiseEvent:
    type: str = 'promise'
    at: int = 0
    what: str = ''


ParseEvent = Union[SayEvent, TextEvent, SayEndEvent, MemoryEvent, MoodEvent, PromiseEvent]

# ─────────────────────────────────────────────────────────────────────
# 内部会处理的标签名。其余一律当普通文本。
# ─────────────────────────────────────────────────────────────────────
_KNOWN = frozenset(['say', 'memory', 'mood', 'promise', 'think', 'thinking'])

_State = Literal['outside', 'say', 'memory', 'skip']


def _parse_attrs(src: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in re.finditer(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'/>]+))', src):
        attrs[m.group(1).lower()] = m.group(2) or m.group(3) or m.group(4) or ''
    return attrs


def _parse_num(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v.strip())
    except ValueError:
        return None


def _could_be_known_tag(partial: str) -> bool:
    """partial 以 '<' 开头但还没到 '>'。判断它是否可能长成已知标签。"""
    body = partial[1:].lstrip('/').lower()
    if not body:
        return True
    for k in _KNOWN:
        if k.startswith(body) or body.startswith(k):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# 解析器
# ─────────────────────────────────────────────────────────────────────

class ResponseParser:
    def __init__(self) -> None:
        self._buf = ''
        self._state: _State = 'outside'
        self._say_open = False
        self._memory_type: str | None = None
        self._memory_buf = ''
        self._skip_until = ''

    def push(self, chunk: str) -> list[ParseEvent]:
        self._buf += chunk
        return self._run()

    def flush(self) -> list[ParseEvent]:
        out = self._run()

        if self._state == 'memory' and self._memory_buf.strip():
            out.append(MemoryEvent(
                memory_type=self._memory_type,
                content=self._memory_buf.strip()
            ))
            self._memory_buf = ''
        elif self._state != 'skip' and self._buf:
            # 残留可能是没闭合的正文，也可能是半截标签
            tail = '' if re.match(r'^<[^>]*$', self._buf) else self._buf
            if tail.strip():
                if not self._say_open:
                    out.append(SayEvent())
                    self._say_open = True
                out.append(TextEvent(value=tail))

        self._buf = ''

        if self._say_open:
            out.append(SayEndEvent())
            self._say_open = False
        self._state = 'outside'
        return out

    def _run(self) -> list[ParseEvent]:
        out: list[ParseEvent] = []
        progressed = True
        while progressed:
            if self._state == 'memory':
                progressed = self._step_memory(out)
            elif self._state == 'skip':
                progressed = self._step_skip(out)
            else:
                progressed = self._step_text(out)
        return out

    def _step_text(self, out: list[ParseEvent]) -> bool:
        if not self._buf:
            return False
        lt = self._buf.find('<')
        if lt == -1:
            self._emit_text(out, self._buf)
            self._buf = ''
            return False
        if lt > 0:
            self._emit_text(out, self._buf[:lt])
            self._buf = self._buf[lt:]
            return True
        # buf starts with '<'
        gt = self._buf.find('>')
        if gt == -1:
            if not _could_be_known_tag(self._buf):
                self._emit_text(out, '<')
                self._buf = self._buf[1:]
                return True
            return False
        raw = self._buf[1:gt]
        consumed = gt + 1
        name_match = re.match(r'^/?([a-zA-Z][\w-]*)', raw.replace('/', '').lstrip())
        name = name_match.group(1).lower() if name_match else ''
        if name not in _KNOWN:
            self._emit_text(out, '<')
            self._buf = self._buf[1:]
            return True
        self._buf = self._buf[consumed:]
        self._handle_tag(out, raw)
        return True

    def _handle_tag(self, out: list[ParseEvent], raw: str) -> None:
        closing = raw.startswith('/')
        self_closing = raw.rstrip().endswith('/')
        body = raw.lstrip('/').rstrip('/')
        name_match = re.match(r'^([a-zA-Z][\w-]*)', body.lstrip())
        if not name_match:
            return
        name = name_match.group(1).lower()
        attrs = _parse_attrs(body)

        if name == 'say':
            if closing:
                if self._say_open:
                    out.append(SayEndEvent())
                self._say_open = False
                self._state = 'outside'
                return
            ev = SayEvent()
            if 'emotion' in attrs:
                ev.emotion = attrs['emotion']
            if 'gesture' in attrs:
                ev.gesture = attrs['gesture']
            out.append(ev)
            self._say_open = True
            self._state = 'outside' if self_closing else 'say'
            return

        if name == 'memory':
            if closing:
                if self._memory_buf.strip():
                    out.append(MemoryEvent(
                        memory_type=self._memory_type,
                        content=self._memory_buf.strip()
                    ))
                self._memory_buf = ''
                self._memory_type = None
                self._state = 'say' if self._say_open else 'outside'
                return
            self._memory_type = attrs.get('type')
            self._memory_buf = ''
            if not self_closing:
                self._state = 'memory'
            return

        if name == 'mood':
            favor = _parse_num(attrs.get('favor'))
            energy = _parse_num(attrs.get('energy'))
            if favor is not None or energy is not None:
                out.append(MoodEvent(favor=favor, energy=energy))
            return

        if name == 'promise':
            if closing:
                return
            at = _parse_promise_at(attrs.get('at'))
            what = attrs.get('what', '').strip()
            if at is not None and what:
                out.append(PromiseEvent(at=at, what=what))
            return

        # think / thinking: discard
        if not closing and not self_closing:
            self._skip_until = f'</{name}>'
            self._state = 'skip'

    def _step_memory(self, out: list[ParseEvent]) -> bool:
        # 接受两种闭合写法：<memory> 正式名和遗留的 <system_reminder>
        buf_lower = self._buf.lower()
        idx = -1
        for close_tag in ('</memory', '</system_reminder'):
            pos = buf_lower.find(close_tag)
            if pos != -1 and (idx == -1 or pos < idx):
                idx = pos
        if idx == -1:
            keep = max(0, len(self._buf) - len('</system_reminder>'))
            self._memory_buf += self._buf[:keep]
            self._buf = self._buf[keep:]
            return False
        self._memory_buf += self._buf[:idx]
        gt = self._buf.find('>', idx)
        if gt == -1:
            self._buf = self._buf[idx:]
            return False
        self._buf = self._buf[gt + 1:]
        self._handle_tag(out, '/memory')
        return True

    def _step_skip(self, _out: list[ParseEvent]) -> bool:
        idx = self._buf.lower().find(self._skip_until)
        if idx == -1:
            keep = max(0, len(self._buf) - len(self._skip_until))
            self._buf = self._buf[keep:]
            return False
        self._buf = self._buf[idx + len(self._skip_until):]
        self._state = 'say' if self._say_open else 'outside'
        return True

    def _emit_text(self, out: list[ParseEvent], text: str) -> None:
        if not text:
            return
        if self._state != 'say':
            if not text.strip():
                return
            if not self._say_open:
                out.append(SayEvent())
                self._say_open = True
            self._state = 'say'
        out.append(TextEvent(value=text))


def _parse_promise_at(value: str | None) -> int | None:
    """只接受精确绝对时间；无法解析就丢弃标签，绝不替模型猜日期。"""
    if not value:
        return None
    try:
        return int(datetime.strptime(value, '%Y-%m-%d %H:%M').timestamp() * 1000)
    except ValueError:
        return None
