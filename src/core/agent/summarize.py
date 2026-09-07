"""按可配置角色身份把一段对话压缩为可检索的情节摘要。

本模块清理模型协议标签、按说话人渲染用户与 Bot 的对话文本（群聊里多个说话人
必须以各自名字区分，Bot 行保持第一人称「我」），并通过 `LlmProvider` 请求
结构化 JSON；解析失败或对话过短时返回 `None`，不会写入记忆存储。

依赖：``src.core.agent.fact_extract``（``Participant`` 在场者结构，与事实抽取、
表达学习共用同一套名单语义）、``src.core.prompts.registry``（``summary`` 模板）。
被 ``src.core.services.chat.background``（队列摘要）与
``src.core.services.maintenance.memory_feedback``（情节重建）调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import json
import re

from .fact_extract import Participant

from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata


@dataclass
class Episode:
    """表示一段已完成的对话摘要及其召回线索。

    :ivar summary: 面向后续上下文的简洁摘要，必须为非空文本。
    :ivar recall_cues: 用于相似召回的关键词或短语列表。
    """

    summary: str
    recall_cues: List[str]


def _system_prompt(character_name: str, character_personality: str) -> str:
    """根据角色名称和人格配置渲染摘要专用系统提示词。

    :param character_name: Bot 在摘要提示词中的名称。
    :param character_personality: 当前配置的人格文本。
    :return: 渲染后的系统提示词。
    副作用：读取固定提示词资源，不访问模型。
    """
    return get_prompt('summary').render(
        character_name=character_name,
        character_personality=character_personality,
    )


def _strip_tags(raw: str) -> str:
    """移除模型协议标签，保留可用于摘要的自然语言正文。

    :param raw: 可能含有 `say`、`memory`、`mood` 或思考标签的文本。
    :return: 删除结构化标签及其内容后的去空白文本。
    副作用：不修改输入字符串。
    """
    raw = re.sub(r'<(memory|mood|think|thinking)\b[^>]*>[\s\S]*?</\1>', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'<(memory|mood)\b[^>]*/?>',  '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'</?say\b[^>]*>', '', raw, flags=re.IGNORECASE)
    return raw.strip()


def _speaker_names(participants: Sequence[Participant]) -> Dict[int, str]:
    """把在场者解析成对话行前缀与名单共用的说话人名。

    群聊昵称会重复也会改：两个不同的人顶着同一个展示名时渲染成相同前缀，
    摘要模型只能把两个人的发言按到一个人头上，与全部匿名没有区别。因此同名者
    统一追加从 1 起的序号，且名单与对话行共用这一份映射，两处必然一致。
    展示名为空的人没有可渲染的名字，不进映射，其对话行退回「对方」。

    :param participants: 调用方按批解析出的在场者。
    :return: ``person_id -> 说话人名``；无可用展示名的人不产生条目。
    副作用：纯函数，不修改输入。
    """

    counts: Dict[str, int] = {}
    for person in participants:
        base = person.display_name.strip()
        if base:
            counts[base] = counts.get(base, 0) + 1
    names: Dict[int, str] = {}
    used: set[str] = set()
    ordinals: Dict[str, int] = {}
    for person in participants:
        base = person.display_name.strip()
        if not base:
            continue
        suffix = 0
        if counts[base] > 1:
            ordinals[base] = ordinals.get(base, 0) + 1
            suffix = ordinals[base]
        candidate = f'{base}{suffix}' if suffix else base
        # 库里可能本来就存着「小明1」这样的展示名，装饰后仍撞车时继续加一，
        # 直到本批内唯一；名字不唯一时同名编号反而制造新的混淆。
        while candidate in used:
            suffix += 1
            candidate = f'{base}{suffix}'
        if suffix:
            ordinals[base] = suffix
        used.add(candidate)
        names[person.person_id] = candidate
    return names


def _render(messages: List[Dict[str, Any]], speaker_names: Dict[int, str]) -> str:
    """将消息列表渲染为摘要模型可读的逐行对话。

    :param messages: 按时间顺序排列的消息字典列表，使用 ``role``、``content``
        与 ``sender_person_id`` 字段；发言人 ID 只用于查说话人名，不进正文。
    :param speaker_names: :func:`_speaker_names` 产出的 ``person_id -> 说话人名``
        映射。查不到名字的 user 行退回「对方」而不是「某人」：桌宠、私聊等
        解析不出名单的链路此前就是这样渲染的，保持该现状，避免这些场景的
        摘要里出现「某人说了什么」这类退化表述。
    :return: 每行以说话人名（Bot 行为「我」）为前缀的对话文本；有效内容为空时
        返回空字符串。
    :raises KeyError: 用户消息缺少 ``content`` 时由现有访问方式抛出。
    副作用：不修改消息列表或其中的字典。
    """

    lines: List[str] = []
    for m in messages:
        text = _strip_tags(m['content']) if m.get('role') == 'assistant' else m.get('content', '')
        # Bot 行固定写「我」：摘要是第一人称回想，不照搬事实抽取的 bot_name 前缀。
        speaker = speaker_names.get(m.get('sender_person_id'), '对方') if m.get('role') == 'user' else '我'
        line = f'{speaker}：{text}'
        if len(line) > 3:
            lines.append(line)
    return '\n'.join(lines)


def parse_episode(raw: str) -> Optional[Episode]:
    """从模型输出中提取并校验摘要 JSON。

    :param raw: 可能包含 Markdown 代码围栏或额外说明的模型输出。
    :return: 合法且含非空 `summary` 的 :class:`Episode`；结构不完整或 JSON 无效时返回 `None`。
    副作用：不写入存储，不抛出模型输出解析异常。
    """
    text = re.sub(r'```(?:json)?', '', raw, flags=re.IGNORECASE).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        j = json.loads(text[start:end + 1])
        summary = j.get('summary', '')
        if not isinstance(summary, str) or not summary.strip():
            return None
        cues = [c.strip() for c in j.get('recall_cues', []) if isinstance(c, str) and c.strip()]
        return Episode(summary=summary.strip(), recall_cues=cues if cues else [summary.strip()])
    except Exception:
        return None


async def summarize(
    provider: LlmProvider,
    messages: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    character_name: str,
    character_personality: str,
    participants: Sequence[Participant],
) -> Optional[Episode]:
    """请求模型为足够长的对话生成摘要。

    :param provider: 提供流式文本输出的模型客户端。
    :param messages: 待摘要的按时间顺序排列的消息列表；user 项带
        ``sender_person_id``，只用于查说话人名，不进提示词正文。
    :param temperature: 模型采样温度。
    :param max_tokens: 摘要输出的最大 token 数；`None` 表示由 provider 决定。
    :param character_name: Bot 名称。
    :param character_personality: Bot 人格文本。
    :param participants: 本批消息的在场者，用于把 user 行渲染成带名字的前缀，
        并在 user 段头部生成「在场的人」名单；空序列时全部 user 行退回「对方」，
        与无名单链路的既有渲染一致。
    :return: 成功解析的 :class:`Episode`；对话过短或模型输出不是合法摘要 JSON 时
        返回 `None`。模型调用失败不再折叠成 `None`，而是原样上抛。
    :raises LlmError: 模型候选全部失败时由路由层抛出，交调用方计数与留痕。
    副作用：发起一次流式模型请求并记录 `llm_request` 观测事件，不写入数据库。
    :performance: 请求内容长度与消息总文本长度成正比，模型网络耗时占主要成本。
    """
    speaker_names = _speaker_names(participants)
    # 过短对话缺少可检索上下文，跳过模型调用以节省请求并避免生成空洞摘要。
    body = _render(messages, speaker_names)
    if len(body) < 40:
        return None
    raw = ''
    render_params = {
        'summary': {
            'character_name': character_name,
            'character_personality': character_personality,
        },
    }
    # 名单走 user 段而不是 system 模板：随批变化的内容不进模板，
    # registry 的 TEMPLATE_PLACEHOLDERS['summary'] 与用户覆写校验都无须改动。
    sections: List[str] = []
    if speaker_names:
        sections.append(
            '在场的人：\n' + '\n'.join(f'  {name}' for name in speaker_names.values())
        )
    sections.append(f'要整理的对话：\n{body}')
    request_messages = [
        {
            'role': 'system',
            'content': _system_prompt(character_name, character_personality),
        },
        {'role': 'user', 'content': '\n\n'.join(sections)},
    ]
    # 模型与观测的失败一律上抛，由调用方 :meth:`ChatService._maybe_summarize` 统一
    # 计数、留痕并决定是否放弃这一批。
    #
    # - 现象：一段对话被服务商内容策略拒绝后，日志里只有路由层的 model_switch，
    #   没有任何一条摘要失败记录，外部无法区分「队列卡死」与「这段没什么可记的」。
    # - 原因：这里原本用 ``except Exception: return None`` 把任何失败都折叠成
    #   「没有摘要」，与「模型输出不是合法 JSON」共用同一个返回值。
    # - 后果：真实错因（blocked / auth / timeout）在抵达调用方之前就被销毁，
    #   调用方既没法按错因分流，也没法把原因写进日志。
    # 摘要请求只传清洗后的对话正文，不把原始协议标签交给模型。
    trace.emit(
        'llm_request',
        messages=request_messages,
        temperature=temperature,
        maxTokens=max_tokens,
        renderParams=render_params,
        **prompt_metadata('summary', ('summary',)),
    )
    bind_render_params(render_params)
    async for chunk in provider.stream(
        messages=request_messages,
        temperature=temperature,
        max_tokens=max_tokens,
    ):
        if chunk.get('text'):
            raw += chunk['text']
    # 统一由 parse_episode 校验 JSON 结构、摘要正文和召回线索。
    return parse_episode(raw)
