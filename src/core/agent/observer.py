"""情景分析 Agent：把一段对话消息流提炼成结构化的场景画像。

她现在每一轮都在裸读消息流，靠对话模型在生成的同时顺便理解「这个群此刻是什么
情况」。那件事既贵（每轮重做一遍）又不连贯（上一轮的理解不会留下来）。本模块把它
拆成一个**后台**的一次性子任务：读比工作记忆更宽的一段历史，产出两个字段的场景
画像，缓存起来供后续若干轮直接使用。

两个字段，不多不少：

- ``topic``：当前对话在聊什么，一句话；
- ``atmosphere``：气氛，**封闭枚举**。

气氛用枚举而不是自由文本，是因为它会直接进她的系统提示词——自由文本会把观察模型
的语气带进人格层，而人格只该由 ``bot.toml`` 和她自己的轴决定。话题必须是自由文本
（对话内容无法枚举），因此对它只做长度收窄。

**观察不是决策。** 本模块产出的东西只作为提示词里的一个背景块，不进
``GateInputFacts``（那里只放确定性事实）、不影响动作空间、不参与任何硬边界。
观察失败就没有场景块，对话照常进行。

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

# 气氛的封闭枚举。六个足够区分「该不该插话、用什么调子」，再多只会让观察模型在
# 近义词之间反复横跳，而下游根本用不出差别。
ATMOSPHERES: tuple[str, ...] = ('热闹', '玩闹', '认真', '平淡', '低落', '冷场')
# 话题一句话的字符上限。它会进系统提示词，写长了就变成第二份聊天记录。
TOPIC_MAX_CHARS = 40
# 允许模型返回的最大原文长度，防止它把整段分析当 JSON 吐回来。
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
            raise ValueError(f'场景话题超过 {TOPIC_MAX_CHARS} 字')
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

        枚举收窄之后旧值可能失效，这里**不修正只丢弃**：一份读不懂的画像等于
        没有画像，硬掰回去只会让她按一个谁也说不清来历的气氛说话。
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

    解析失败**不降级成一个默认场景**：编一个「平淡」出来会让她按一个从未观察到的
    气氛说话，比没有场景更糟。调用方应当保留上一份画像或干脆不注入。
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
