"""Agent 协议渲染。

本 mixin 把一次回合的门控事实、可选消息预览与动作空间渲染成规划器读的协议
文本与消息序列，并提供输出示例。动作头的 targets 必须落在消息编号上，编号
只有在历史里逐行可见时模型才能指认，因此预览与历史的编号口径必须一致。

由 ``ChatService`` 继承，依赖它的配置与提示词属性。
"""

from src.core.agent.action_protocol import DecisionFrame, GateInputFacts
from src.core.agent.history import strip_say_tags
from src.core.agent.prompt import render_action_protocol, render_tool_protocol
from src.core.platform_io.types import ConversationContext

from .state import _BatchGate, _BufferedMessage


class AgentProtocolMixin:

    def _agent_gate_inputs(
        self,
        frame: DecisionFrame,
        batch_gate: _BatchGate,
    ) -> GateInputFacts:
        """把本批门控事实组装为行动事件第 1 层。"""
        return GateInputFacts(
            stream_kind=frame.stream_kind,
            mentioned_me=batch_gate.mentioned_me,
            name_mentioned=batch_gate.name_mentioned,
            must_reply=frame.disposition == 'force',
            asleep=batch_gate.asleep,
            rate_limited='rate_limited' in batch_gate.result.reason_codes,
            recent_bot_replies=batch_gate.reply_count,
            candidate_message_ids=frame.selectable_message_ids,
            selectable_message_ids=frame.selectable_message_ids,
        )

    def _selectable_message_previews(
        self,
        batch: list[_BufferedMessage],
    ) -> list[tuple[int, str]]:
        """把本批可选消息渲染为「消息 ID + 展示原文」序列。

        群聊原文按历史同一口径带上发送者显示名，使协议块里的清单与模型看到
        的历史行逐字对应；私聊历史本就不带名字，因此只给正文。

        :param batch: 本回合已完成图片描述补齐的批次消息。
        :return: 与 ``selectable_message_ids`` 同序的 ``(消息 ID, 原文)`` 列表。
        :raises ValueError: 群聊消息的发送者在注册表中不存在时由注册表抛出。
        """
        previews: list[tuple[int, str]] = []
        for message in batch:
            context = message.context
            if context.stream.kind == 'group':
                name = self._registry.stream_display_name(
                    context.person.id,
                    context.stream.id,
                )
                previews.append((message.message_id, f'{name}: {message.text}'))
            else:
                previews.append((message.message_id, message.text))
        return previews

    def _render_agent_protocol(
        self,
        frame: DecisionFrame,
        batch: list[_BufferedMessage],
        context: ConversationContext,
    ) -> str:
        """渲染本回合的动作头协议文本。

        该文本由调用方整体替换系统提示词中的直接发言协议；shadow 与 live 共用，
        保证两条灰度路径看到的输出规则完全一致。

        可选消息必须连同原文一起写进协议：消息 ID 是数据库主键，在对话历史里
        没有任何可见锚点，只给一串孤立数字时模型会把 targets 填成「凌白最后
        一条」这类描述，整轮按 illegal_action 失败、用户侧表现为 Bot 不回话。

        :param frame: 本回合固定快照，提供动作空间、可选消息与平台能力。
        :param batch: 与 ``frame.selectable_message_ids`` 同源的批次消息；
            自主回合没有待接消息，传空列表。
        :param context: 当前会话上下文；自主回合没有批次可反查，必须显式给出。
        :return: 已注入运行时动作集与目标锚点清单的协议文本。
        """
        target_person = (
            self._registry.stream_display_name(context.person.id, context.stream.id)
            if context.stream.kind == 'group' and batch
            else ''
        )
        if self._tool_calling:
            # 工具调用模式下动作空间由函数签名承载，提示词只留目标锚点与选择
            # 口径；两套输出协议同时出现会让模型在写 XML 与调工具之间摇摆。
            return render_tool_protocol(
                self._selectable_message_previews(batch),
                quote_supported=frame.capabilities.quote,
                target_person=target_person,
                cognitive_rounds=self._cognitive_rounds,
                available_actions=frame.available_actions,
            )
        return render_action_protocol(
            sorted(frame.available_actions),
            self._selectable_message_previews(batch),
            quote_supported=frame.capabilities.quote,
            emoji_enabled=frame.capabilities.emoji,
            target_person=target_person,
            cognitive_rounds=self._cognitive_rounds,
            available_reactions=frame.capabilities.available_reactions,
            stream_kind=context.stream.kind,
        )

    def _render_agent_messages(
        self,
        frame: DecisionFrame,
        messages: list[dict],
        protocol_text: str | None = None,
    ) -> list[dict]:
        """把已渲染消息整理为 Conversation Agent 实际提交的上下文。

        XML 模式下，助手历史去掉 ``<say>`` 外壳，再在真实用户消息前插入
        reply/silent few-shot，并把输出要求并进末条用户消息。工具模式下不再做
        任何角色重排或合并：system 之后的时间、画像与历史保持独立 user item，
        工具/回复协议作为最后一项。

        :param frame: 本回合固定快照，提供动作空间与可选消息。
        :param messages: ``_render_prepared_context`` 产出的系统与历史消息。
        :param protocol_text: 工具模式必需的末轮协议；XML 模式已在 system 内，
            因此忽略该参数。
        :return: 按当前协议模式整理完成的新消息列表。
        :raises ValueError: 工具模式缺少协议，或上游仍传入 assistant 历史。
        """
        if self._tool_calling:
            if not protocol_text or not protocol_text.strip():
                raise ValueError('工具调用的 item 流缺少末轮协议')
            if not messages or messages[0].get('role') != 'system':
                raise ValueError('工具调用的 item 流必须以 system 开头')
            invalid_roles = [
                item.get('role') for item in messages[1:]
                if item.get('role') != 'user'
            ]
            if invalid_roles:
                raise ValueError(
                    f'工具调用的 item 流只能包含 user 上下文，收到：{invalid_roles}'
                )
            flattened = [dict(item) for item in messages]
            flattened.append({
                'role': 'user',
                'content': protocol_text.rstrip(),
            })
            return flattened

        if len(messages) < 2:
            return messages
        history: list[dict] = []
        for message in messages[1:]:
            item = dict(message)
            if item.get('role') == 'assistant':
                content = item.get('content')
                if isinstance(content, str):
                    item['content'] = strip_say_tags(content)
            history.append(item)
        user_indexes = [
            index for index, message in enumerate(history)
            if message.get('role') == 'user'
        ]
        if not user_indexes:
            return messages
        last_user_index = user_indexes[-1]
        # 上一回合的 assistant 回复可能晚于当前用户消息落库：当前消息在模型
        # 生成期间到达时，落库顺序是 [上一用户, 当前用户, 上一回复]。同 stream
        # 的占用保证尾部 assistant 只可能属于上一回合，必须移到当前用户之前，
        # 而不是截断；否则 Agent 看不见自己刚说过的话，会重复回复。
        trailing_assistants = history[last_user_index + 1:]
        history = [
            *history[:last_user_index],
            *trailing_assistants,
            history[last_user_index],
        ]
        last_user_index = len(history) - 1
        # 输出要求并进候选块所在的这条 user 消息，而不是再追加一条：它必须留在
        # 整个序列的最末，才能成为生成侧最近、最先需要满足的约束。
        output_rule = (
            '[输出要求] 你下一条回复必须先输出 <decision> 动作标签；'
            '正文只能放在其后的 <say> 里，禁止在 <decision> 之前输出 '
            '<say>、普通文字或解释。'
        )
        history[last_user_index] = {
            **history[last_user_index],
            'content': (
                f"{history[last_user_index]['content']}\n\n"
                f'{output_rule}'
            ).rstrip(),
        }
        return [
            messages[0],
            *history[:last_user_index],
            *self._agent_output_examples(frame),
            *history[last_user_index:],
        ]

    def _agent_output_examples(self, frame: DecisionFrame) -> list[dict[str, str]]:
        """构造紧邻当前轮次的 reply 与 silent 输出示例。

        系统提示词末尾的示例离生成位置较远；在真实用户消息前放两条同格式
        few-shot，可显著压低模型退回「先 <say>」旧习惯的概率。

        :param frame: 本回合固定快照，示例目标只取真实可选消息。
        :return: 按当前动作空间生成的 user/assistant 示例消息列表。
        """
        examples: list[dict[str, str]] = []
        targets = frame.selectable_message_ids
        if 'reply' in frame.available_actions and targets:
            examples.extend([
                {
                    'role': 'user',
                    'content': '[输出格式示例] 群里有人问你忙不忙，要不要现在一起上号。',
                },
                {
                    'role': 'assistant',
                    'content': (
                        f'<decision action="reply" targets="{targets[0]}" '
                        'reasons="direct_question" length="brief"/>'
                        '<say emotion="normal">不忙，刚刷完视频。</say>'
                        '<say emotion="smile">上号叫我，我这就来。</say>'
                    ),
                },
            ])
        if 'silent' in frame.available_actions:
            examples.extend([
                {
                    'role': 'user',
                    'content': '[输出格式示例] 群里在聊一个你不认识的人。',
                },
                {
                    'role': 'assistant',
                    'content': '<decision action="silent" reasons="others_conversation"/>',
                },
            ])
        return examples
