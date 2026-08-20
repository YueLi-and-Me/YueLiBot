"""解析模型流式响应中的结构化标签，并增量生成文本和副作用事件。

输出协议使用类 XML 标签，以便在标签或属性被拆分到多个网络块时继续解析；
解析器同时支持纯文本、未闭合 ``<say>`` 和跨块标签。未完成标签保留在内部
缓冲区，完整标签转换为事件，普通文本转换为隐式发言事件。

本模块不执行网络 I/O，不写入记忆，也不更新人格状态；调用方负责消费事件并
处理持久化和副作用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Union

# ─────────────────────────────────────────────────────────────────────
# 事件类型定义
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SayEvent:
    """表示一次说话段的开始及可选表情、动作元数据。

    :ivar type: 固定为 `say`。
    :ivar emotion: 可选情绪标识，缺省为 `None`。
    :ivar gesture: 可选动作标识，缺省为 `None`。
    """

    type: str = 'say'
    emotion: str | None = None
    gesture: str | None = None


@dataclass
class TextEvent:
    """表示应展示给用户的一段文本内容。

    :ivar type: 固定为 `text`。
    :ivar value: 原始文本，默认值为空字符串。
    """

    type: str = 'text'
    value: str = ''


@dataclass
class SayEndEvent:
    """表示当前说话段结束。

    :ivar type: 固定为 `sayEnd`。
    """

    type: str = 'sayEnd'


@dataclass
class MemoryEvent:
    """表示模型声明的一条可供记忆层处理的内容。

    :ivar type: 固定为 `memory`。
    :ivar memory_type: 可选记忆类型，例如偏好或事实。
    :ivar content: 去除首尾空白后的记忆正文。
    """

    type: str = 'memory'
    memory_type: str | None = None
    content: str = ''


@dataclass
class MoodEvent:
    """表示模型输出的好感或精力变化量。

    :ivar type: 固定为 `mood`。
    :ivar favor: 可选好感变化数值。
    :ivar energy: 可选精力变化数值。
    """

    type: str = 'mood'
    favor: float | None = None
    energy: float | None = None


@dataclass
class PromiseEvent:
    """表示模型约定在绝对时间提醒一件事。

    :ivar type: 固定为 `promise`。
    :ivar at: Unix 毫秒时间戳。
    :ivar what: 约定内容，必须为非空字符串。
    """

    type: str = 'promise'
    at: int = 0
    what: str = ''


@dataclass
class EmojiEvent:
    """表示模型想用一张表情包表达目标情绪。"""

    type: str = 'emoji'
    emotion: str = ''


@dataclass
class DecisionEvent:
    """表示模型声明的行动决策头，必须先于任何正文出现。

    属性保持解析器产出的原始字符串，语义校验（动作枚举、目标范围、理由码
    分域）由 Conversation Agent 完成，解析器不做判断。

    :ivar type: 固定为 `decision`。
    :ivar action: 动作名原文，例如 `reply`；缺失时为 `None`。
    :ivar targets: 逗号分隔的目标消息 ID 原文；缺失时为 `None`。
    :ivar quote: 引用消息 ID 原文；缺失时为 `None`。
    :ivar reasons: 逗号分隔的理由码原文；缺失时为 `None`。
    :ivar length: 回复篇幅原文；缺失时为 `None`。
    :ivar query: 认知动作的检索词原文；终局动作不携带，缺失时为 `None`。
    :ivar reaction: react 动作的表情回应标识原文；其他动作不携带，缺失时为 `None`。
    :ivar reference: 决策交给回复生成的背景说明原文；只有终局发言动作携带，
        缺失时为 `None`。
    """

    type: str = 'decision'
    action: str | None = None
    targets: str | None = None
    quote: str | None = None
    reasons: str | None = None
    length: str | None = None
    query: str | None = None
    reaction: str | None = None
    reference: str | None = None


ParseEvent = Union[
    SayEvent,
    TextEvent,
    SayEndEvent,
    MemoryEvent,
    MoodEvent,
    PromiseEvent,
    EmojiEvent,
    DecisionEvent,
]

# ─────────────────────────────────────────────────────────────────────
# 内部会处理的标签名。其余一律当普通文本。
# ─────────────────────────────────────────────────────────────────────
_KNOWN = frozenset(['say', 'memory', 'mood', 'promise', 'emoji', 'decision'])

_State = Literal['outside', 'say', 'memory', 'skip']


def _parse_attrs(src: str) -> dict[str, str]:
    """从标签体中提取宽松的键值属性。

    :param src: 不含外层尖括号的标签文本。
    :return: 属性名转小写后的字符串映射；无法匹配的片段被忽略。
    副作用：不修改输入字符串，也不抛出格式异常。
    :performance: 使用一次正则扫描，时间复杂度与标签长度线性相关。
    """
    attrs: dict[str, str] = {}
    for m in re.finditer(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'/>]+))', src):
        attrs[m.group(1).lower()] = m.group(2) or m.group(3) or m.group(4) or ''
    return attrs


def _parse_num(v: str | None) -> float | None:
    """把可选标签属性解析为浮点数。

    :param v: 原始数字文本；`None` 表示属性缺失。
    :return: 解析后的 `float`，缺失或格式非法时返回 `None`。
    副作用：不修改解析器状态。
    """
    if v is None:
        return None
    try:
        return float(v.strip())
    except ValueError:
        return None


def _could_be_known_tag(partial: str) -> bool:
    """判断未闭合标签片段是否仍可能匹配已知协议标签。

    :param partial: 以 ``<`` 开头且通常尚未出现 ``>`` 的标签片段。

    :return: 片段名称是已知标签前缀、已知标签前缀包含片段名称或名称为空时返回
        ``True``；明确不可能匹配时返回 ``False``。

    :raises TypeError: ``partial`` 不是字符串时由字符串操作触发。
    """
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
    """增量解析发言、表情包、状态副作用和行动决策标签。

    实例维护跨网络分片的文本缓冲区和当前标签状态；调用方应持续调用
    :meth:`push`，在流结束时调用 :meth:`flush` 释放残留文本并补齐说话结束事件。
    """

    def __init__(self) -> None:
        """创建处于文档外部状态的空解析器。

        :return: 无返回值。
        副作用：初始化内部缓冲区和标签状态，不执行 I/O。
        """
        self._buf = ''
        self._state: _State = 'outside'
        self._say_open = False
        self._memory_type: str | None = None
        self._memory_buf = ''
        self._skip_until = ''

    def push(self, chunk: str) -> list[ParseEvent]:
        """追加一段模型输出并尽可能产生解析事件。

        :param chunk: 当前收到的文本分片，可以是空字符串。
        :return: 当前分片足以确定的事件列表；未闭合标签会留在内部缓冲区。
        :raises TypeError: `chunk` 不是字符串时由字符串操作暴露类型错误。
        副作用：修改内部缓冲区、状态和已打开的说话段标记。
        :performance: 每次调用只处理当前可推进的缓冲区，超长未闭合标签会保留内存。
        """
        self._buf += chunk
        return self._run()

    def flush(self) -> list[ParseEvent]:
        """结束当前流并输出残留文本及必要的收尾事件。

        :return: 包含待完成记忆、残留文本和 `SayEndEvent` 的最终事件列表。
        副作用：清空文本缓冲区，重置标签状态；重复调用只返回空列表。
        :performance: 处理量与尚未消费的缓冲区长度线性相关。
        """
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
        """反复推进状态机，直到当前缓冲区无法继续解析。

        :return: 本轮新增的解析事件。
        副作用：消费内部缓冲区并更新解析状态。
        :performance: 每个已消费字符只会沿状态机处理，整体为线性复杂度。
        """
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
        """处理外部文本或从文本中识别下一个完整标签。

        :param out: 用于追加解析事件的当前输出列表。
        :return: 本次是否推进了缓冲区；返回 `False` 表示需要等待更多分片。
        副作用：消费文本缓冲区，并可能切换说话状态或调用标签处理逻辑。
        """
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
        """根据标签名和属性更新状态机并追加对应事件。

        :param out: 用于追加结构化事件的当前输出列表。
        :param raw: 不含外层尖括号的原始标签体，允许包含闭合斜杠。
        :return: `None`；未知或属性不完整的标签被忽略。
        副作用：修改说话、记忆和跳过状态，并可能向 `out` 追加事件。
        """
        closing = raw.startswith('/')
        self_closing = raw.rstrip().endswith('/')
        body = raw.lstrip('/').rstrip('/')
        name_match = re.match(r'^([a-zA-Z][\w-]*)', body.lstrip())
        if not name_match:
            return
        name = name_match.group(1).lower()
        attrs = _parse_attrs(body)

        # 每种标签只修改自身状态；恢复目标由当前是否处于 say 状态决定。
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
                # 仅保存非空记忆，避免空标签污染长期记忆表。
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
            # 情绪标签是即时事件，不进入文本状态机，也不要求闭合标签。
            favor = _parse_num(attrs.get('favor'))
            energy = _parse_num(attrs.get('energy'))
            if favor is not None or energy is not None:
                out.append(MoodEvent(favor=favor, energy=energy))
            return

        if name == 'promise':
            if closing:
                return
            # promise 必须同时具备可解析时间和非空原话，避免将不完整意图写入队列。
            at = _parse_promise_at(attrs.get('at'))
            what = attrs.get('what', '').strip()
            if at is not None and what:
                out.append(PromiseEvent(at=at, what=what))
            return

        if name == 'emoji':
            # 表情包是即时的可见产物意图，不进入文本状态机；缺少目标情绪时忽略。
            if closing:
                return
            emotion = attrs.get('emotion', '').strip()
            if emotion:
                out.append(EmojiEvent(emotion=emotion))
            return

        if name == 'decision':
            # 动作头是即时事件，不进文本状态机；属性语义留给 Agent 校验。
            if closing:
                return
            out.append(DecisionEvent(
                action=attrs.get('action'),
                targets=attrs.get('targets'),
                quote=attrs.get('quote'),
                reasons=attrs.get('reasons'),
                length=attrs.get('length'),
                query=attrs.get('query'),
                reaction=attrs.get('reaction'),
                reference=attrs.get('reference'),
            ))
            return

    def _step_memory(self, out: list[ParseEvent]) -> bool:
        """在记忆状态中寻找闭合标签并积累记忆正文。

        :param out: 用于追加完成的 `MemoryEvent` 的当前输出列表。
        :return: 找到并处理闭合标签时返回 `True`，否则保留尾部片段并返回 `False`。
        副作用：消费内部缓冲区并修改记忆正文缓冲区。
        """
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
        """跳过不支持标签的正文，直到找到预设的结束标记。

        :param _out: 为保持状态机接口一致而传入的输出列表，本方法不会写入它。
        :return: 找到结束标记并切回文本状态时返回 `True`，否则返回 `False`。
        副作用：消费或保留内部缓冲区，并恢复说话/外部状态。
        """
        idx = self._buf.lower().find(self._skip_until)
        if idx == -1:
            keep = max(0, len(self._buf) - len(self._skip_until))
            self._buf = self._buf[keep:]
            return False
        self._buf = self._buf[idx + len(self._skip_until):]
        self._state = 'say' if self._say_open else 'outside'
        return True

    def _emit_text(self, out: list[ParseEvent], text: str) -> None:
        """将一段普通文本包装为隐式说话事件。

        :param out: 用于追加 `SayEvent` 和 `TextEvent` 的输出列表。
        :param text: 待输出的原始文本。
        :return: `None`；空文本或外部纯空白文本不会追加事件。
        副作用：必要时打开说话状态并向 `out` 追加事件。
        """
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
    """把精确的绝对时间文本转换为 Unix 毫秒时间戳。

    :param value: `YYYY-MM-DD HH:MM` 格式的时间文本；缺失值为 `None`。
    :return: 解析后的 Unix 毫秒时间戳；格式非法或为空时返回 `None`。
    副作用：不访问系统时钟以外的外部资源，也不替模型推断缺失日期。
    """
    if not value:
        return None
    try:
        return int(datetime.strptime(value, '%Y-%m-%d %H:%M').timestamp() * 1000)
    except ValueError:
        return None
