"""情景分析 Agent：把一段对话消息流提炼成结构化的场景画像。

本模块是回合后的后台一次性子任务：读取比工作记忆更宽的一段历史，产出两个字段
的场景画像，缓存供后续若干轮使用；对话模型不再在每轮生成时重复理解场景。

- ``topic``：当前对话在聊什么，一句话；
- ``atmosphere``：气氛，封闭枚举。

气氛使用封闭枚举：该字段直接进入系统提示词，自由文本会将观察模型的语气引入
提示词；话题无法枚举，仅做长度收窄。

观察产物仅作为提示词中的背景块：不进 ``GateInputFacts``（那里只放确定性事实）、
不影响动作空间、不参与硬边界。观察失败则本轮无场景块，对话照常进行。

依赖：``sub_agent`` 统一执行器、``prompts.registry`` 的 ``scene.observe`` 模板；
被 ``src.core.services.chat`` 在后台任务里调用，不反向依赖聊天服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Sequence
import json

from .sub_agent import SubAgentCall, run_sub_agent

from src.core.llm_models.protocol import LlmProvider
from src.core.prompts.registry import get_prompt, prompt_metadata

# 气氛的封闭枚举。六个枚举足以区分发言时机与语气差异；继续增加只会使观察模型在
# 近义词之间摇摆，下游无法利用这些差别。
ATMOSPHERES: tuple[str, ...] = ('热闹', '玩闹', '认真', '平淡', '低落', '冷场')
# 话题一句话的字符上限。该内容会进系统提示词，超长相当于在提示词中复制一份聊天记录。
#
# 取值依据（2026-09-08 按真机 58 次观察调整，原值 40）：提示词要求话题写成
# 「主要在聊 X，另有人在聊 Y」的双线句式，光骨架就三十几个字，成功观察的长度
# 落在 25-40（中位 34、47% 不低于 35），上限压在分布中间，26% 的观察因越界被
# 整份丢弃——连带丢掉本来正确的 atmosphere。上限必须落在模型自然输出之外，
# 越界才重新意味着「模型吐了一段分析」而不是「这句话稍微长了点」。
# 输出总长另有 _MAX_OUTPUT_CHARS 兜底，本上限只约束进提示词的那一句。
TOPIC_MAX_CHARS = 100
# 允许模型返回的最大原文长度，防止模型输出整段分析文本而非 JSON。
_MAX_OUTPUT_CHARS = 512


@dataclass(frozen=True)
class SceneSnapshot:
    """一次场景观察的结果。

    :ivar topic: 当前对话在聊什么，一句话。
    :ivar atmosphere: 气氛，取自 ``ATMOSPHERES`` 封闭枚举。
    :ivar observed_message_id: 观察覆盖到的最后一条消息 ID；用于判断画像有多旧。
    """

    topic: str
    atmosphere: str
    observed_message_id: int

    def __post_init__(self) -> None:
        """拒绝空话题、超长话题与枚举外的气氛。"""
        if not self.topic.strip():
            raise ValueError('场景话题不能为空')
        if len(self.topic) > TOPIC_MAX_CHARS:
            raise ValueError(
                f'场景话题超过 {TOPIC_MAX_CHARS} 字（实际 {len(self.topic)} 字）'
            )
        if self.atmosphere not in ATMOSPHERES:
            raise ValueError(f'未知气氛：{self.atmosphere}（封闭枚举）')

    def to_dict(self) -> Dict[str, Any]:
        """转换为可写入 KV 的可序列化字典。"""
        return {
            'topic': self.topic,
            'atmosphere': self.atmosphere,
            'observedMessageId': self.observed_message_id,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> 'SceneSnapshot | None':
        """从 KV 读回场景画像，形状不合法时返回 None。

        :param payload: ``read_json`` 的返回值，可能是任意历史遗留形状。
        :return: 合法的场景画像；缺字段、类型不对或枚举失效时返回 ``None``。

        枚举收窄后旧值可能失效，此处不修正只丢弃：无法解析的画像按无画像处理，
        修正值无法保证与原始观察一致。
        """
        if not isinstance(payload, dict):
            return None
        topic = payload.get('topic')
        atmosphere = payload.get('atmosphere')
        observed = payload.get('observedMessageId')
        if not isinstance(topic, str) or not isinstance(atmosphere, str):
            return None
        if not isinstance(observed, int) or isinstance(observed, bool):
            return None
        try:
            return cls(
                topic=topic,
                atmosphere=atmosphere,
                observed_message_id=observed,
            )
        except ValueError:
            return None


def build_observe_prompt(lines: Sequence[str]) -> str:
    """组装场景观察提示词。

    :param lines: 已渲染为「说话人：正文」的对话历史，按时间正序。
    :return: 含历史、字段约束与封闭气氛枚举的完整提示词。
    :raises KeyError: 模板未注册。
    """
    return get_prompt('scene.observe').render(
        history='\n'.join(lines),
        atmospheres=' / '.join(ATMOSPHERES),
        topic_limit=str(TOPIC_MAX_CHARS),
    )


def parse_scene(raw: str, observed_message_id: int) -> SceneSnapshot:
    """严格解析观察模型返回的 JSON 对象。

    :param raw: 模型原文。
    :param observed_message_id: 本次观察覆盖到的最后一条消息 ID。
    :return: 已通过自检的场景画像。
    :raises ValueError: 原文超长、不是 JSON 对象、缺字段，或气氛不在枚举内。

    解析失败不降级为默认场景：默认气氛并非本会话的真实观察结果，误用比缺失
    更差。调用方应保留上一份画像或不注入。
    """
    if len(raw) > _MAX_OUTPUT_CHARS:
        raise ValueError(f'场景观察输出超过 {_MAX_OUTPUT_CHARS} 字符')
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'场景观察输出不是合法 JSON：{exc}') from exc
    if not isinstance(payload, dict):
        raise ValueError('场景观察输出顶层必须是 JSON 对象')
    topic = payload.get('topic')
    atmosphere = payload.get('atmosphere')
    if not isinstance(topic, str) or not isinstance(atmosphere, str):
        raise ValueError('场景观察输出缺少 topic 或 atmosphere 字符串字段')
    return SceneSnapshot(
        topic=topic.strip(),
        atmosphere=atmosphere.strip(),
        observed_message_id=observed_message_id,
    )


class SceneObserver:
    """调用一次模型，把群聊或私聊历史提炼成场景画像。"""

    def __init__(
        self,
        provider: LlmProvider,
        *,
        temperature: float,
        max_tokens: int | None = None,
    ) -> None:
        """保存观察用的模型提供方与采样参数。

        :param provider: 流式模型客户端；观察是小任务，通常复用摘要任务的路由。
        :param temperature: 采样温度。
        :param max_tokens: 可选输出上限。
        """
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def observe(
        self,
        lines: Sequence[str],
        observed_message_id: int,
    ) -> SceneSnapshot:
        """读一段带说话人标签的对话历史并产出场景画像。

        :param lines: 已渲染为「说话人：正文」的历史，按时间正序；不能为空。
        :param observed_message_id: 本次观察覆盖到的最后一条消息 ID。
        :return: 已通过自检的场景画像。
        :raises ValueError: 历史为空，或模型输出不符合协议。
        :raises Exception: provider 的网络与协议错误原样传播，由调用方决定
            保留旧画像还是放弃本次观察。
        副作用：一次模型调用，并登记 ``llm_request`` 观测事件。
        """
        if not lines:
            raise ValueError('场景观察的历史不能为空')
        prompt = build_observe_prompt(lines)
        result = await run_sub_agent(SubAgentCall(
            task='scene',
            provider=self._provider,
            messages=[{'role': 'system', 'content': prompt}],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            response_format={'type': 'json_object'},
            render_params={'scene.observe': {
                'history': '\n'.join(lines),
                'atmospheres': ' / '.join(ATMOSPHERES),
                'topic_limit': str(TOPIC_MAX_CHARS),
            }},
            trace_extra=prompt_metadata('scene.observe', ('scene.observe',)),
        ))
        return parse_scene(result.text, observed_message_id)
