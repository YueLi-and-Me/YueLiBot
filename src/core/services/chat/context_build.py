"""回合上下文的组装与渲染。

本 mixin 负责把一次回合需要的全部输入拼起来：关系与提示词配置、表达习惯挑选、
工作记忆排序、事实召回与会话印象、黑话查表、人格与日程描述，产出
``_PreparedTurnContext``；随后把它渲染成系统提示词与消息序列，并在需要时用模型
增强补齐。

由 ``ChatService`` 继承，依赖它的 ``_memory`` / ``_persona`` / ``_models`` 等属性。
"""

from datetime import datetime
from typing import Any

import asyncio

from src.core.agent.expression import (
    ExpressionSample,
    fetch_expression_pool,
    render_expression_habits,
)
from src.core.agent.history import (
    close_dangling_say,
    fit_char_budget,
    normalize_history,
    strip_say_tags,
    strip_side_effect_tags,
)
from src.core.agent.jargon import lookup_jargon
from src.core.agent.profile import profiles_for_injection
from src.core.agent.prompt import build_itemized_system_prompt, build_system_prompt
from src.core.llm_models.openai import LlmError
from src.core.llm_models.snapshot import dump as dump_llm_request
from src.core.logging.logger import get_logger
from src.core.memory.store import RecalledFact, StoredMessage
from src.core.memory.tuning import apply_pool_percentile, tuned_value
from src.core.observe import events as trace
from src.core.observe.stages import EXPRESSION
from src.core.persona.state import describe_acquaintance, describe_persona
from src.core.platform_io.types import ConversationContext
from src.core.runtime.clock import now as current_time
from src.core.schedule.plan import asks_about_activity
from src.core.services.memory_feedback import register_prompt_entries

from .helpers import _facts_for_prompt
from .state import _PreparedTurnContext, _RetrievalTrace

logger = get_logger(__name__)


class ContextBuildMixin:

    def _relationship_kwargs(self) -> dict:
        """读取构建关系提示词所需的用户称呼配置。

        :return: 包含 ``user_nickname`` 和 ``relationship`` 的字典。
        """
        bot_cfg = self._cfg.bot
        return {
            'user_nickname': bot_cfg.user_nickname,
            'relationship': bot_cfg.relationship,
        }

    def _prompt_config_kwargs(self, relationship_enabled: bool) -> dict:
        """读取系统提示词所需的角色与用户配置。

        :param relationship_enabled: 是否注入 owner 专属关系信号（称呼偏好与关系）。
            非 owner（如群聊中的其他成员）必须传 ``False``；否则「对方希望你称呼 X /
            把对方当 Y 看待」会把 owner 的关系错误地套到每一个说话人身上（Bot 会对
            群里所有人叫「哥哥」）。
        :return: 包含角色名、别名、用户称呼、关系、生日、人设和回复风格的字典。
        """
        bot = self._cfg.bot
        personality = self._cfg.personality
        return {
            'name': bot.name,
            'aliases': bot.aliases,
            'user_nickname': bot.user_nickname if relationship_enabled else '',
            'relationship': bot.relationship if relationship_enabled else '',
            'birthday': personality.birthday,
            'personality': personality.personality,
            'reply_style': personality.reply_style,
        }

    async def _pick_expression_habits(
        self,
        context: ConversationContext,
        query: str,
        history: list[dict[str, str]],
        signal: asyncio.Event | None,
    ) -> list[ExpressionSample]:
        """为当前回复从 expressions 表选择表达样本。

        :param context: 当前会话上下文，用于阶段和错误 trace。
        :param query: 当前用户文本或主动情境。
        :param history: 已组装的对话历史。
        :param signal: 可选的取消信号。

        :return: 选择出的表达样本；选择器未配置、候选池为空、输入错误或
            非中断模型错误时返回空列表。

        :raises LlmError: 选择过程被主动中断时向上抛出。
        """
        if self._expression_selector is None:
            trace.emit('expression_select', source='disabled', count=0)
            return []
        # 候选池按会话按轮现取；不足一池（含从未积累过表达方式的会话）时
        # 本轮不选，不调用模型。
        pool, total = fetch_expression_pool(self._db, context.stream.id)
        if not pool:
            trace.emit('expression_select', source='disabled', count=0, pool=0, total=total)
            return []
        self._mark_stage(context, EXPRESSION)
        try:
            # 只把最近历史传给选择器，避免表达习惯选择占用完整上下文预算。
            picked = await self._expression_selector.select(query, history[-8:], pool, signal=signal)
        except LlmError as exc:
            if exc.kind == 'aborted':
                raise
            return self._expression_selection_failed(
                context, type(exc).__name__, str(exc), pool=len(pool), total=total
            )
        except ValueError as exc:
            return self._expression_selection_failed(
                context, 'ValueError', str(exc), pool=len(pool), total=total
            )
        # 选中即进提示词：渲染结果随本轮系统提示词一并发出，因此在这里回写
        # 使用次数与最近使用时间；未选中的行两列都不动。
        if picked:
            used_at = current_time()
            self._db.executemany(
                'UPDATE expressions SET use_count = use_count + 1, last_used_at = ?'
                ' WHERE id = ?',
                [(used_at, sample.id) for sample in picked],
            )
            self._db.commit()
        trace.emit(
            'expression_select',
            source='model',
            count=len(picked),
            pool=len(pool),
            total=total,
            habits=[f'当“{sample.situation}”时，可以用“{sample.style}”来表达。'
                    for sample in picked],
        )
        return picked

    def _expression_selection_failed(
        self,
        context: ConversationContext,
        error_type: str,
        message: str,
        *,
        pool: int,
        total: int,
    ) -> list[ExpressionSample]:
        """记录表达样本选择失败并跳过本轮样本注入。

        :param context: 当前会话上下文。
        :param error_type: 错误类型名称。
        :param message: 错误详情。
        :param pool: 本轮候选池大小。
        :param total: 该会话候选总数。

        :return: 空表达样本列表。

        副作用：
            写入模型请求诊断快照和观察事件。
        """
        snapshot = dump_llm_request('expression', error_type, message, {
            'stage': trace.current_stage_id(),
            'streamId': context.stream.id,
            'turnId': self._active_turns.get(context.stream.id),
        })
        logger.error('expression_select_failed', errorType=error_type, error=message,
                     snapshot=str(snapshot) if snapshot else None)
        trace.emit('expression_select', source='model', count=0,
                   pool=pool, total=total,
                   errorType=error_type, error=message,
                   snapshotPath=str(snapshot) if snapshot else None)
        return []

    @staticmethod
    def _order_working_memory_for_batch(
        messages: list[StoredMessage],
        batch_message_ids: tuple[int, ...] | None,
    ) -> list[StoredMessage]:
        """按本批消息边界重建交错落库后的历史顺序。

        上一轮生成期间到达的当前批消息会先于上一回复写入 messages 表，
        原始顺序形如 ``[上一用户, 当前批用户..., 上一 assistant]``。按当前
        批第一条用户消息把尾部 assistant 插回它前面，得到回合视角的正确
        顺序：``[上一用户, 上一 assistant, 当前批用户...]``。

        :param messages: 记忆服务返回的按落库顺序排列的消息。
        :param batch_message_ids: 本批用户消息主键；非 Agent 路径可传 ``None``。
        :return: 需要重排时返回新列表，否则返回原列表。
        """
        if not batch_message_ids:
            return messages
        batch_ids = set(batch_message_ids)
        last_user_index = max(
            index for index, message in enumerate(messages)
            if message.role == 'user'
        )
        trailing = messages[last_user_index + 1:]
        if not trailing:
            return messages
        first_batch_index = next(
            index for index, message in enumerate(messages)
            if message.role == 'user' and message.message_id in batch_ids
        )
        return [
            *messages[:first_batch_index],
            *trailing,
            *messages[first_batch_index:last_user_index + 1],
        ]

    @property
    def fact_recall_limit(self) -> int:
        """配置提供的进提示词条数初值；检索调优的覆盖在各读取点叠加。"""

        return self._fact_recall_limit

    def _recall_turn_facts(
        self,
        context: ConversationContext,
        query: str,
        impression: str | None,
        now: int,
    ) -> list[RecalledFact]:
        """按在场者召回事实，当前文本与会话印象取并集。

        群聊里当前这条消息经常是短应答，拿它当检索词捞不到东西；印象是对
        一段对话的概括，覆盖面是另一个量级。两个检索词各自跑一次召回，
        候选按 ID 去重后统一排序——当前文本命中的往往更精确，词面分相同
        时排在前面（稳定排序，当前文本的候选先进池）。

        候选池不在这里截断：与单人召回时代一致，保留 ``limit * 3`` 量级的
        池子供确认回复后的向量重排；进提示词的条数由渲染层按
        ``fact_recall_limit`` 截取。

        :param context: 当前会话上下文。
        :param query: 当前用户文本。
        :param impression: 会话印象；``None`` 时退回只用当前文本检索。
        :param now: 当前毫秒时间戳。
        :return: 去重排序后的事实候选池。
        :raises sqlite3.Error: 检索失败。
        副作用：只读；被可见性规则挡下的条数会发一条事件。
        """

        limit = int(tuned_value('fact_recall_limit', self._fact_recall_limit))
        person_ids = self._present_person_ids(context)
        pool: list[RecalledFact] = []
        seen: set[int] = set()
        for text in (query, impression or ''):
            if not text:
                continue
            for fact in self.memory.recall_facts_in_scope(
                person_ids, text, limit, now,
                stream_kind=context.stream.kind,
                private_in_group=self._private_facts_in_group,
                return_candidates=True,
            ):
                if fact.id not in seen:
                    seen.add(fact.id)
                    pool.append(fact)
        pool.sort(key=lambda fact: fact.score, reverse=True)
        # 候选池分数百分位是调优白名单参数：阈值大于零时截掉低分尾部，
        # 让「明显不相关却仍占位」的条目在进重排之前就被挡下。
        kept = apply_pool_percentile([fact.score for fact in pool])
        return pool[:kept]

    async def _conversation_impression(
        self,
        context: ConversationContext,
        now: int,
    ) -> str | None:
        """取当前会话的印象，作为回合组装前的一次性输入。

        :param context: 当前会话上下文。
        :param now: 当前毫秒时间戳。
        :return: 印象正文；未达重算条件时复用缓存，无可概括内容或生成失败
            时返回 ``None``（失败路径的事件由印象服务自己发）。
        副作用：可能发起一次受限流约束的 memory 模型请求。
        """

        return await self._impressions.current(
            context.stream.id,
            bot_name=self._bot_display_name,
            speaker_name=self._registry.stream_display_name,
            temperature=self._memory_temperature,
            max_tokens=self._memory_max_tokens,
            now=now,
        )

    def _prepare_turn_context(
        self,
        context: ConversationContext,
        query: str,
        now: int,
        platform_bot_name: str | None = None,
        user_message_id_watermark: int | None = None,
        batch_message_ids: tuple[int, ...] | None = None,
        impression: str | None = None,
        turn_id: int | None = None,
    ) -> _PreparedTurnContext:
        """组装不依赖模型调用的完整回合上下文。

        :param context: 当前会话上下文。
        :param query: 当前用户文本。
        :param now: 当前毫秒时间戳。
        :param platform_bot_name: 当前平台登录昵称；仅用于当前入站消息的称呼匹配。
        :param user_message_id_watermark: 可选的本批末条用户消息 ID；用于隔离后来落库的用户消息。
        :param batch_message_ids: 可选的本批用户消息主键；用于把上一回合回复
            插回当前批之前的正确历史位置。
        :param impression: 调用方在组装前取到的会话印象；作为事实检索的第二
            检索词与当前文本取并集，``None`` 表示本次只用当前文本检索。
        :param turn_id: 当前回合编号，用于把召回留痕稳定关联到提示词请求。
        :return: 可供动作决策读取、并可在确认回复后继续增强的上下文。

        副作用：
            读取记忆、人格、日程和活动状态，并消费一次重逢提示；不调用模型，
            不强化召回事实。
        """
        fact_candidates = self._recall_turn_facts(context, query, impression, now)
        exclude_rebuild = (
            self._cfg.memory_feedback.enabled
            and self._cfg.memory_feedback.episode_query_block_enabled
        )
        recalled = self.memory.recall_episodes(
            context.stream.id,
            query,
            int(tuned_value('recalled_episode_limit', self._recalled_episode_limit)),
            exclude_pending_rebuild=exclude_rebuild,
        )
        recent = self.memory.recent_episodes(
            context.stream.id,
            int(tuned_value('recent_episode_limit', self._recent_episode_limit)),
            exclude_pending_rebuild=exclude_rebuild,
        )
        seen_ids: set[int] = set()
        episodes = []
        for e in [*recalled, *recent]:
            # 召回结果与最近 episode 可能重叠，按 ID 去重后再限制上下文数量。
            if e.id not in seen_ids:
                seen_ids.add(e.id)
                episodes.append(e)
        episodes = episodes[:self._episode_context_limit]
        state = self.persona.get(context.person.id)
        persona_desc = describe_persona(state)
        # 熟悉程度（认识了多少天）属 owner 专属关系信号，非 owner 不注入。
        acquaintance = (
            describe_acquaintance(self.memory.first_seen_at(context.person.id), now)
            if context.relationship_signals_enabled
            else ''
        )
        # 时段里的具体活动只在他这轮真的问起时才注入，否则日程只以情绪和作息影响本轮语气。
        schedule_desc = (
            self._schedule.describe(
                now,
                self.current_sleep(),
                include_activity=asks_about_activity(query),
            )
            if self._schedule else None
        )
        resumption = self._take_resumption(context.stream.id)
        wm = self.memory.working_memory(
            context.stream.id,
            self._working_memory_messages,
            user_message_id_watermark,
        )
        # raw_history 还服务于旧管线和表达选择器，继续保留角色交替所需的逻辑
        # 顺序；工具 Agent 单独读取原始落库顺序的拍平变体。这样 shadow 开工具
        # 时也不会悄悄改变随后那次旧管线调用的可见行为。
        ordered_wm = self._order_working_memory_for_batch(wm, batch_message_ids)
        raw_history = self._history_for_context(context, ordered_wm)
        agent_wm = wm if self._tool_calling else ordered_wm
        agent_history = self._history_for_context(
            context,
            agent_wm,
            label_message_ids=True,
            flatten=self._tool_calling,
        )
        # 感知开关和 owner 归属分别控制“能否看见”和“是否允许应用用户关系状态”。
        activity = None
        if (context.stream.kind in self._perception_surfaces
                and context.person.kind == 'owner'
                and self._activity is not None):
            activity = self._activity()
        # 黑话只扫他人消息：user 行都是别人说的（Bot 自己的发言是 assistant 行），
        # 本轮批次原文放在最后——它是本轮最新的他人发言。使用原始 content
        # 而非渲染后的历史：渲染行带时间戳与发言人前缀，并非原始输入。
        jargon_scan_texts = [
            message.content for message in wm if message.role == 'user'
        ] + [query]
        jargon = tuple(lookup_jargon(
            self._db,
            context.stream.id,
            jargon_scan_texts,
            protected_names=self._jargon_protected_names,
            injected=self._jargon_injected,
            now=now,
        ))
        return _PreparedTurnContext(
            context=context,
            query=query,
            now=now,
            platform_bot_name=platform_bot_name,
            fact_candidates=fact_candidates,
            retrieval_trace=_RetrievalTrace(
                turn_id=turn_id,
                stream_id=context.stream.id,
                current_text=query,
                conversation_impression=impression if impression is not None else '',
                candidate_pool=tuple(
                    (fact.id, float(fact.score)) for fact in fact_candidates
                ),
            ),
            episodes=[episode.summary for episode in episodes],
            persona=persona_desc,
            acquaintance=acquaintance,
            activity=activity,
            schedule=schedule_desc,
            resumption=resumption,
            raw_history=raw_history,
            agent_history=agent_history,
            jargon=jargon,
        )

    def _render_prepared_context(
        self,
        prepared: _PreparedTurnContext,
        *,
        facts: list[RecalledFact] | None = None,
        expression_habits: str | None = None,
        render_params: dict[str, dict[str, str]] | None = None,
        reply_length: str | None = None,
        protocol_text: str | None = None,
        decision_only: bool = False,
        record_retrieval_trace: bool = True,
    ) -> list[dict]:
        """将同一份已组装上下文渲染为模型消息。

        :param prepared: 决策前已完成一次性组装的回合上下文。
        :param facts: 可选的增强后事实列表；省略时使用词面排序结果。
        :param expression_habits: 可选表达习惯提示词块。
        :param render_params: 可选提示词渲染参数收集字典。
        :param reply_length: 当前轮规划出的回复篇幅。
        :param protocol_text: 可选的 Agent 协议文本。传统角色模式把它整体替换进
            system；工具模式据此选择 item 流，协议本身由 ``_render_agent_messages``
            放在末尾。它同时是「本次渲染属于 Agent 路径」的唯一判据。
        :param decision_only: 本次渲染只用于产出动作决策；透传给系统提示词，
            省略回复风格、语调与表达样本三块。
        :param record_retrieval_trace: 是否把这份提示词选中的事实写入召回留痕；
            影子决策设为 ``False``，避免它抢先冒充真实生产提示词。
        :return: 首项为 system 消息；传统模式后接裁剪历史，工具模式后接独立的
            运行时上下文 item 与裁剪历史。
        副作用：读取配置和会话语调；反馈纠错链路开启时登记「事实进提示词」锚点，
            其余情况不读写数据库、不调用模型。
        """
        selected_facts = (
            prepared.fact_candidates[
                :int(tuned_value('fact_recall_limit', self._fact_recall_limit))
            ]
            if facts is None
            else facts
        )
        prompt_kwargs = self._prompt_config_kwargs(
            prepared.context.relationship_signals_enabled,
        )
        feedback_cfg = self._cfg.memory_feedback
        fact_items = _facts_for_prompt(
            self.memory,
            prepared.context.person.id,
            selected_facts,
            hard_filter_marked=feedback_cfg.enabled and feedback_cfg.hard_filter_enabled,
        )
        # N4 锚点：事实真的进了提示词才登记待观察；链路默认关闭，关闭时零写入。
        if feedback_cfg.enabled and fact_items:
            register_prompt_entries(
                self._db,
                [(item.fact_id, prepared.context.person.id)
                 for item in fact_items if item.fact_id],
                prepared.context.stream.id,
                prepared.now,
            )
        shared_context = {
            'now': datetime.fromtimestamp(prepared.now / 1000),
            'persona': prepared.persona,
            'acquaintance': prepared.acquaintance,
            'facts': fact_items,
            'episodes': prepared.episodes,
            'activity': prepared.activity,
            'schedule': prepared.schedule,
            'expression_habits': expression_habits,
            'reply_length': reply_length,
            'tone': self._session(prepared.context.stream.id).tone,
            'resumption': prepared.resumption,
            'platform_name': prepared.platform_bot_name,
            'scene': self._scene_for_prompt(prepared.context),
            # 只注入组装期查得的黑话命中；匹配、打分与截断在 agent/jargon.py，
            # 决策与回复两次渲染共用同一份结果，副作用每回合只发生一次。
            'jargon': prepared.jargon,
            # 只注入本轮在场者的画像；按亲密度取前 N、空画像不算数与确凿/印象
            # 两档的取数都在 profile.py，渲染分两档呈现在 prompt.py。
            'impressions': profiles_for_injection(
                self._db, self._present_person_ids(prepared.context),
                skip_dirty=(
                    feedback_cfg.enabled
                    and feedback_cfg.profile_force_refresh_on_read
                ),
            ),
            'render_params': render_params,
            'decision_only': decision_only,
            **prompt_kwargs,
        }
        if self._tool_calling and protocol_text is not None:
            # 工具模式的运行时背景不并入单个 system：时间、画像、记忆等
            # 各自保留 item 边界，历史也不做角色合并。协议由
            # _render_agent_messages 放在整个序列末尾，确保它始终是最近的约束。
            system, context_items = build_itemized_system_prompt(**shared_context)
            history = fit_char_budget(
                prepared.agent_history,
                preserve_items=True,
            )
            if record_retrieval_trace:
                prepared.retrieval_trace.emit_once(
                    [item.fact_id for item in fact_items if item.fact_id]
                )
            return [
                {'role': 'system', 'content': system},
                *({'role': 'user', 'content': item} for item in context_items),
                *history,
            ]

        emoji_enabled = self._emoji_available(prepared.context)
        system = build_system_prompt(
            protocol_text=protocol_text,
            emoji_enabled=emoji_enabled,
            emoji_tags=self._emoji_prompt_tags(emoji_enabled),
            **shared_context,
        )
        # Agent 路径读带 [编号] 前缀的历史变体，动作头的 targets 才有可指认的
        # 锚点；旧管线仍读不带编号的原始历史，可见行为完全不受影响。
        source_history = (
            prepared.agent_history if protocol_text is not None else prepared.raw_history
        )
        # 读取历史时再次规范化，兼容早期中断留下的悬空标签；该操作对干净历史幂等。
        history = normalize_history(source_history)
        if record_retrieval_trace:
            prepared.retrieval_trace.emit_once(
                [item.fact_id for item in fact_items if item.fact_id]
            )
        return [{'role': 'system', 'content': system}, *fit_char_budget(history)]

    async def _enrich_prepared_context(
        self,
        prepared: _PreparedTurnContext,
        signal: asyncio.Event | None,
        render_params: dict[str, dict[str, str]],
        reply_length: str | None,
        protocol_text: str | None = None,
    ) -> list[dict]:
        """确认回复后，在既有上下文上附加向量与表达模型增强。

        :param prepared: 动作决策实际读取的同一份上下文。
        :param signal: 可选的表达选择取消信号。
        :param render_params: 提示词渲染参数收集字典。
        :param reply_length: 规划器选出的回复篇幅。
        :param protocol_text: 可选的 Agent 动作协议文本；透传给系统提示词
            渲染，使动作头先于正文成为唯一输出协议。
        :return: 使用增强后事实排序和表达习惯渲染的最终模型消息。
        副作用：调用向量与表达模型，并强化最终实际用于回复的事实 ID。
        """
        query_embedding = await self._vector.embed_query(prepared.query)
        facts = self.memory.rank_recalled_facts(
            prepared.fact_candidates,
            query_embedding,
            int(tuned_value('fact_recall_limit', self._fact_recall_limit)),
        )
        self.memory.reinforce_recalled_facts(facts, prepared.now)
        expression_habits = render_expression_habits(
            await self._pick_expression_habits(
                prepared.context,
                prepared.query,
                prepared.raw_history,
                signal,
            )
        )
        return self._render_prepared_context(
            prepared,
            facts=facts,
            expression_habits=expression_habits,
            render_params=render_params,
            reply_length=reply_length,
            protocol_text=protocol_text,
        )

    def _history_for_context(
        self,
        context: ConversationContext,
        messages: list[Any],
        *,
        label_message_ids: bool = False,
        flatten: bool = False,
    ) -> list[dict]:
        """将记忆消息转换为模型历史，补充发言时刻，并在群聊中补充发送者显示名。

        用户消息带 ``HH:MM`` 前缀（跨天时带 ``MM-DD HH:MM``），使消息的发言时刻
        与消息间隔成为 Bot 可直接读取的信息。传统角色模式下 Bot 自己的历史回复
        不加时刻：assistant 行仍是输出范例，行首前缀可能被模仿进动作头。扁平模式
        没有这种格式示范职责，因而每一方都带时间与说话人，落库顺序就是可直接
        阅读的事件顺序。

        :param context: 当前会话上下文。
        :param messages: 记忆服务返回的消息对象列表。
        :param label_message_ids: 是否给用户消息加 ``[编号] ``、给自己的消息加
            ``[我] `` 前缀。仅
            Conversation Agent 上下文需要：动作头的 targets 是消息主键，主键
            不逐行可见时模型无法指认，会把 targets 写成人名或「最后一条」这类
            描述，整轮按 illegal_action 失败。自己的历史回复用固定标记而不用编号：
            本回合只允许把批次内的用户消息作为目标，给助手行编号会诱导越界。
        :param flatten: 把 Bot 自己的发言也渲染成 ``user`` 角色，用显示名区分
            说话人，并在转角色前清掉历史协议与副作用标签。工具调用模式专用：
            那里动作由函数签名承载，助手行不再承担输出格式示范作用。拍平消除了
            「角色必须交替」约束带来的重排需求：重排可能使助手行位于开头而被
            ``normalize_history`` 丢弃；拍平后不存在开头 assistant 行，
            该问题不再出现。

        :return: 仅含 ``role`` 和 ``content`` 的模型消息列表；原始记忆对象不被修改。

        :raises RuntimeError: 群聊用户消息缺少发送者人物 ID。
        """
        history: list[dict] = []
        last_stamped_date = None
        for message in messages:
            content = message.content
            role = message.role
            if flatten and message.role == 'assistant':
                # 转成 user 之前先按 assistant 语义清理；一旦改完角色，
                # normalize_history 就不会再替它剥副作用标签与 <say> 外壳。
                content = strip_say_tags(
                    close_dangling_say(strip_side_effect_tags(content)),
                )
                if not content:
                    continue
                content = f'{self._bot_display_name}: {content}'
                role = 'user'
            if context.stream.kind == 'group' and message.role == 'user':
                if message.sender_person_id is None:
                    raise RuntimeError('群聊 user 历史缺少 sender_person_id')
                name = self._registry.stream_display_name(
                    message.sender_person_id,
                    context.stream.id,
                )
                content = f'{name}: {content}'
            if message.role == 'user' or (flatten and message.role == 'assistant'):
                spoken_at = datetime.fromtimestamp(message.created_at / 1000)
                # 跨天才带日期：工作记忆可能横跨若干天，但同一天内逐行重复日期
                # 只会挤占上下文；系统提示词已经给出「现在是几点」，Bot 据此就能算出
                # 每条消息离现在多久、彼此间隔多长。
                if last_stamped_date == spoken_at.date():
                    stamp = spoken_at.strftime('%H:%M')
                else:
                    stamp = spoken_at.strftime('%m-%d %H:%M')
                    last_stamped_date = spoken_at.date()
                content = f'{stamp} {content}'
            if label_message_ids:
                if message.role == 'user':
                    content = f'[{message.message_id}] {content}'
                elif message.role == 'assistant':
                    content = f'[我] {content}'
            history.append({'role': role, 'content': content})
        return history
