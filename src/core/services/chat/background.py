"""回合之外的后台队列任务。

本 mixin 驱动摘要、事实抽取、画像刷新与表达学习四条队列：按批取输入、成功后
推进游标、失败时按统一口径计数并在达到上限时跳过这一批。

队列游标只在成功后推进，这对瞬时故障是对的，对确定性失败（例如内容策略拒绝）
则是死锁——同样的输入永远得到同样的拒绝。失败计数与跳批就是这条死锁的出口。

由 ``ChatService`` 继承，依赖它的 ``_memory`` / ``_models`` 等属性。
"""

from typing import Any, Callable, Dict, List, Sequence

import sqlite3

from src.core.agent.expression_learn import (
    BATCH_MESSAGES as EXPRESSION_LEARN_BATCH,
    TRIGGER_MESSAGES as EXPRESSION_LEARN_TRIGGER,
    advance_cursor as advance_expression_learn_cursor,
    read_cursor as read_expression_learn_cursor,
    run_learning,
)
from src.core.agent.fact_extract import (
    Participant,
    advance_cursor,
    read_cursor,
    run_extraction,
)
from src.core.agent.profile import refresh_profiles
from src.core.agent.summarize import summarize
from src.core.logging.logger import get_logger
from src.core.memory.store import EpisodeInput, MemoryStore, StoredMessage, UNSUMMARIZED_KIND

from .constants import _EXTRACTION_PARTICIPANT_LIMIT
from .state import _BatchFailureTracker

logger = get_logger(__name__)


class BackgroundTaskMixin:

    def _batch_failed(
        self,
        tracker: _BatchFailureTracker,
        event: str,
        stream_id: int,
        head_id: int,
        size: int,
        reason: str,
    ) -> bool:
        """记录一次后台批处理失败，返回这一批是否已用尽重试次数。

        计数与留痕合在一处，是因为三个后台队列的失败处理必须口径一致：失败一定
        进日志（否则「队列停摆」与「这段没什么可记的」在外部完全一样），达到上限
        一定返回 ``True`` 让调用方推进队列。跳过动作本身由调用方执行——三个队列
        的推进方式不同（摘要写归档情节，抽取与学习推游标）。

        :param tracker: 该任务的连续失败计数器。
        :param event: 日志事件名，如 ``summary_failed``。
        :param stream_id: 失败所属的会话 ID。
        :param head_id: 本批首条消息的 ID。
        :param size: 本批消息条数，进日志用于判断是否整批卡住。
        :param reason: 失败原因原文。
        :return: 连续失败已达上限、调用方应跳过这一批时为 ``True``。
        副作用：更新计数器并写一条 warning 日志。
        """
        failures = tracker.record(stream_id, head_id)
        logger.warning(
            event,
            streamId=stream_id,
            headMessageId=head_id,
            messages=size,
            failures=failures,
            reason=reason,
        )
        return tracker.exhausted(failures)

    async def _maybe_summarize(self, stream_id: int) -> None:
        """在待摘要消息达到阈值时异步生成并保存 episode。

        :param stream_id: 待检查的会话 stream ID。

        副作用：
            读取待摘要消息、调用摘要模型并写入 episode；同一 stream 同时只允许
            一个摘要任务。摘要异常不会影响已完成的对话回合；同一批连续失败到
            :data:`_BACKGROUND_BATCH_RETRY_LIMIT` 次后归档该批以放行队列。
        """

        if stream_id in self._summarizing or not self._summary_provider:
            return
        if self.memory.pending_count(stream_id) < self._summarize_trigger_messages:
            return
        self._summarizing.add(stream_id)
        # 取批可能自己抛错，失败处理要读它，因此先给一个空批。
        batch: List[Dict[str, Any]] = []
        try:
            # 单个 stream 使用内存集合去重，避免连续回复重复启动摘要任务。
            batch = self.memory.oldest_pending(
                stream_id,
                self._summarize_batch_messages,
            )
            if len(batch) < 4:
                return
            # 摘要输入只保留 role/content，避免将内部消息 ID 暴露给模型。
            msgs = [{'role': m['role'], 'content': m['content']} for m in batch]
            episode = await summarize(
                self._summary_provider,
                msgs,
                temperature=self._summary_temperature,
                max_tokens=self._summary_max_tokens,
                character_name=self._bot_display_name,
                character_personality=self._summary_personality,
            )
            if not episode:
                # 模型没抛异常但也没给出合法摘要 JSON。这与抛异常同属「这一批没能
                # 处理」，必须一并计数：只计异常会让格式性失败继续无声地卡住队列。
                self._handle_summary_failure(stream_id, batch, '模型未返回合法的摘要 JSON')
                return
            # episode 写入后由 MemoryStore 标记对应消息已处理，下一轮从队列继续。
            self.memory.add_episode(
                stream_id,
                EpisodeInput(
                    summary=episode.summary,
                    cues=episode.recall_cues,
                    started_at=batch[0]['created_at'],
                    ended_at=batch[-1]['created_at'],
                    message_ids=[message['id'] for message in batch],
                ),
            )
            self._summary_failures.clear(stream_id)
        except Exception as exc:
            # 摘要是后台附加任务，失败不能回滚已完成的对话或阻断下一轮；但计数与
            # 留痕不能省，否则确定性失败会把队列永久钉死在这一批。
            self._handle_summary_failure(stream_id, batch, f'{type(exc).__name__}：{exc}')
        finally:
            self._summarizing.discard(stream_id)

    def _handle_summary_failure(
        self,
        stream_id: int,
        batch: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """记录一次摘要失败；同一批连续失败到上限时归档它，放行待摘要队列。

        归档写的是一条 :data:`UNSUMMARIZED_KIND` 占位情节：它不带召回线索，也被
        ``recent_episodes`` 排除，因此不会进入工作记忆，只用来占住 ``episode_id``
        让队列前进；代价是丢弃该段的情节记忆，但不归档会阻塞其后全部批次。

        :param stream_id: 失败所属的会话 ID。
        :param batch: 本次送去摘要的消息批，按 ID 正序；为空表示批次都没取到，
            此时只记日志，没有可归档的对象。
        :param reason: 失败原因原文，同时写入日志与占位情节正文。
        :return: 无返回值。
        副作用：写日志；达到重试上限时写入占位情节并归档该批消息。
        """
        if not batch:
            logger.warning('summary_failed', streamId=stream_id, reason=reason)
            return
        if not self._batch_failed(
            self._summary_failures,
            'summary_failed',
            stream_id,
            batch[0]['id'],
            len(batch),
            reason,
        ):
            return
        try:
            self.memory.add_episode(
                stream_id,
                EpisodeInput(
                    summary=f'这一批对话未能生成摘要：{reason}',
                    cues=[],
                    started_at=batch[0]['created_at'],
                    ended_at=batch[-1]['created_at'],
                    message_ids=[message['id'] for message in batch],
                    kind=UNSUMMARIZED_KIND,
                ),
            )
        except sqlite3.Error as exc:
            # 占位归档写不进去时不再上抛：调用方多半正处在上一个失败的处理路径上，
            # 异常逃逸只会变成一条无主的 Task exception，反而盖住真正的原因。
            logger.error('summary_skip_failed', streamId=stream_id, error=str(exc))
            return
        self._summary_failures.clear(stream_id)
        logger.error(
            'summary_batch_skipped',
            streamId=stream_id,
            headMessageId=batch[0]['id'],
            messages=len(batch),
            reason=reason,
        )

    async def _maybe_refresh_profiles(self) -> None:
        """在回合之外批量刷新过期的人物画像。

        与摘要、事实抽取同一纪律：后台任务、失败不阻塞回合。刷新的输入是本地
        已有的事实与情节，不依赖当前会话，不按 stream 分派；同一时刻只
        允许一轮在跑，避免几条会话同时收尾时占满 memory 模型槽。

        副作用：可能发起多次模型请求并写入 ``person_profile``。
        """

        if self._refreshing_profiles or self._memory_provider is None:
            return
        self._refreshing_profiles = True
        try:
            await refresh_profiles(
                self._db,
                self._memory_provider,
                bot_name=self._bot_display_name,
                temperature=self._memory_temperature,
                max_tokens=self._memory_max_tokens,
            )
        except Exception as exc:
            logger.warning('profile_refresh_failed', error=str(exc))
        finally:
            self._refreshing_profiles = False

    def _extraction_participants(self, batch: Sequence[StoredMessage]) -> list[Participant]:
        """从待抽取的这批消息里解析出在场者名单。

        必须按批解析，不能用最近发言名单：抽取游标从 0 起步，第一批取的是这个
        会话最早的消息，当时的发言者未必在最近发言名单中。名单不匹配不会报错，
        模型抽出的事实会全部按归属不明丢弃，静默失败。

        :param batch: 本次交给模型的消息批，按 ID 正序。
        :return: 至多 :data:`_EXTRACTION_PARTICIPANT_LIMIT` 个在场者，按批内首次发言
            顺序排列；解析不出平台身份的人会被跳过——归属仅依据编号，昵称不参与判定。
        副作用：只读 identities，不写任何表。
        """

        seen: list[int] = []
        for message in batch:
            person_id = message.sender_person_id
            if person_id is not None and person_id not in seen:
                seen.append(person_id)
        people: list[Participant] = []
        for person_id in seen[:_EXTRACTION_PARTICIPANT_LIMIT]:
            identities = self._registry.list_identities(person_id)
            if not identities:
                continue
            identity = identities[0]
            people.append(Participant(
                external_id=identity.external_id,
                display_name=identity.display_name,
                person_id=person_id,
            ))
        return people

    def _skip_stuck_batch(
        self,
        tracker: _BatchFailureTracker,
        task: str,
        stream_id: int,
        batch: Sequence[StoredMessage],
        reason: str,
        advance: Callable[[MemoryStore, int, int], None],
    ) -> None:
        """记录一次游标型后台任务的失败；同一批失败到上限时把游标推过这一批。

        与 :meth:`_handle_summary_failure` 对应：摘要靠写归档情节推进队列，抽取与
        学习靠推进各自的 ``meta`` 游标，除此之外两条路径的纪律完全一致。

        :param tracker: 该任务的连续失败计数器。
        :param task: 任务名，用于拼日志事件名（``fact_extract`` / ``expression_learn``）。
        :param stream_id: 失败所属的会话 ID。
        :param batch: 本次处理的消息批，按 ID 正序；为空表示批次都没取到，此时只记日志。
        :param reason: 失败原因原文。
        :param advance: 该任务的游标推进函数，接收 ``(store, stream_id, 末条消息 ID)``。
        :return: 无返回值。
        副作用：写日志；达到重试上限时写 ``meta`` 表推进游标。
        """
        if not batch:
            logger.warning(f'{task}_failed', streamId=stream_id, reason=reason)
            return
        if not self._batch_failed(
            tracker,
            f'{task}_failed',
            stream_id,
            batch[0].message_id,
            len(batch),
            reason,
        ):
            return
        try:
            advance(self.memory, stream_id, batch[-1].message_id)
        except sqlite3.Error as exc:
            # 与摘要占位归档同一条理由：调用方正处在上一个失败的处理路径上，
            # 异常逃逸只会变成一条无主的 Task exception，盖住真正的原因。
            logger.error(f'{task}_skip_failed', streamId=stream_id, error=str(exc))
            return
        tracker.clear(stream_id)
        logger.error(
            f'{task}_batch_skipped',
            streamId=stream_id,
            headMessageId=batch[0].message_id,
            messages=len(batch),
            reason=reason,
        )

    async def _maybe_extract_facts(self, stream_id: int, stream_kind: str) -> None:
        """在待抽取消息达到阈值时后台抽取人物事实并写入长期记忆。

        与 :meth:`_maybe_summarize` 同一条纪律：独立模型任务、回合之后执行、
        同一会话同时只允许一个在飞、失败只丢该批且不影响已完成的对话。

        :param stream_id: 待检查的会话 ID。
        :param stream_kind: 该会话的类型；决定新写事实的来源标记。
        :return: 无返回值。
        副作用：可能发起一次模型请求、写入 facts 并推进抽取游标。
        """

        if stream_id in self._extracting or self._memory_provider is None:
            return
        self._extracting.add(stream_id)
        # 失败处理要读这一批的首尾 ID，取批本身也可能抛错，因此先给一个空批。
        batch: List[StoredMessage] = []
        try:
            # 先按同一口径取出这一批，用它的发言人解析在场者；run_extraction 内部会
            # 再读一次同样的批次。多一次只读查询换取「名单与批次必然对齐」。
            cursor = read_cursor(self.memory, stream_id)
            if self.memory.message_count_after(stream_id, cursor) < self._fact_extract_trigger:
                return
            batch = self.memory.messages_after(stream_id, cursor, self._fact_extract_batch)
            participants = self._extraction_participants(batch)
            if not participants:
                return
            written = await run_extraction(
                self.memory,
                self._memory_provider,
                self._db,
                stream_id=stream_id,
                stream_kind=stream_kind,
                participants=participants,
                bot_name=self._bot_display_name,
                trigger_messages=self._fact_extract_trigger,
                batch_messages=self._fact_extract_batch,
                temperature=self._memory_temperature,
                max_tokens=self._memory_max_tokens,
                embed_fact=self._vector.embed_fact,
                embed_knowledge=self._vector.embed_knowledge,
            )
            if written is None:
                self._skip_stuck_batch(
                    self._extract_failures,
                    'fact_extract',
                    stream_id,
                    batch,
                    '模型未返回合法的事实抽取 JSON',
                    advance_cursor,
                )
                return
            self._extract_failures.clear(stream_id)
        except Exception as exc:
            # 抽取是旁路设施：任何失败都不该回滚已完成的回合。游标只在成功时推进，
            # 这一批下次会重跑；但确定性失败（例如整批被服务商内容策略拒绝）每次
            # 重跑都会原样复现，所以连续失败到上限就把游标推过这一批——宁可丢掉
            # 这一段的事实，也不能让它挡住其后的全部对话。
            self._skip_stuck_batch(
                self._extract_failures,
                'fact_extract',
                stream_id,
                batch,
                f'{type(exc).__name__}：{exc}',
                advance_cursor,
            )
        finally:
            self._extracting.discard(stream_id)

    async def _maybe_learn_expressions(self, stream_id: int) -> None:
        """在待学习消息达到阈值时后台学习表达方式，并顺路执行淘汰。

        与 :meth:`_maybe_extract_facts` 同一条纪律：独立模型任务（复用 memory 槽）、
        回合之后执行、同一会话同时只允许一个在飞、失败只丢该批且不影响已完成的
        对话。游标独立（``expression_learn_cursor``），与事实抽取、摘要队列互不
        消费对方输入。

        :param stream_id: 待检查的会话 ID。
        :return: 无返回值。
        副作用：可能发起一次模型请求、写入并淘汰 expressions 行、推进学习游标。
        """

        if stream_id in self._learning_expressions or self._memory_provider is None:
            return
        self._learning_expressions.add(stream_id)
        # 失败处理要读这一批的首尾 ID，取批本身也可能抛错，因此先给一个空批。
        batch: List[StoredMessage] = []
        try:
            # 先按同一口径取出这一批，用它的发言人解析在场者（名单只用于把对话
            # 渲染成带名字的行）；run_learning 内部会再读一次同样的批次。
            cursor = read_expression_learn_cursor(self.memory, stream_id)
            if self.memory.message_count_after(stream_id, cursor) < EXPRESSION_LEARN_TRIGGER:
                return
            batch = self.memory.messages_after(stream_id, cursor, EXPRESSION_LEARN_BATCH)
            participants = self._extraction_participants(batch)
            report = await run_learning(
                self.memory,
                self._memory_provider,
                self._db,
                stream_id=stream_id,
                participants=participants,
                bot_name=self._bot_display_name,
                temperature=self._memory_temperature,
                max_tokens=self._memory_max_tokens,
            )
            if report is None:
                self._skip_stuck_batch(
                    self._expression_failures,
                    'expression_learn',
                    stream_id,
                    batch,
                    '模型未返回合法的表达学习 JSON',
                    advance_expression_learn_cursor,
                )
                return
            self._expression_failures.clear(stream_id)
        except Exception as exc:
            # 学习是旁路设施：任何失败都不该回滚已完成的回合。游标只在整批成功时
            # 推进，这一批下次会重跑；与事实抽取同一条纪律，连续失败到上限就跳过
            # 这一批，避免一段处理不了的对话永久卡住学习队列。
            self._skip_stuck_batch(
                self._expression_failures,
                'expression_learn',
                stream_id,
                batch,
                f'{type(exc).__name__}：{exc}',
                advance_expression_learn_cursor,
            )
        finally:
            self._learning_expressions.discard(stream_id)
