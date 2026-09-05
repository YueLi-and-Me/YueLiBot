"""群聊观察与历史回填。

本 mixin 负责把群里「没轮到她开口」的消息记成观察事件，并在首次进群时用
协议端提供的历史消息补齐这段空白：识别已见过的外部消息 ID、播种回填游标、
按批写入观察记录，并在控制台与事件账本上留痕。

由 ``ChatService`` 继承，依赖它的 ``_memory`` / ``_registry`` 等属性。
"""

from typing import Any

import asyncio
import json

from ..console.trace_console import render_observation

from src.core.logging.logger import get_logger
from src.core.observe import events as trace
from src.core.observe.source import source_label
from src.core.platform_io.types import ConversationContext, InboundMessage
from src.core.runtime.clock import now as current_time

from .constants import (
    _BACKFILL_SEED_EVENT_LIMIT,
    _BACKFILL_SEED_MESSAGE_LIMIT,
    _BACKFILL_SEED_TIME_MATCH_MS,
)

logger = get_logger(__name__)


class GroupObservationMixin:

    def record_group_observation(self, inbound: InboundMessage, reason: str = '') -> int:
        """保存被群聊回复门控拒绝的入站消息及原因。

        :param inbound: 已完成 stream、人物和身份解析的群聊消息。
        :param reason: 门控拒绝原因，默认空字符串。

        :return: 新写入的用户消息 ID。

        :raises ValueError: 入站消息不是群聊，或正文为空。
        :raises sqlite3.Error: 消息写入失败。

        副作用：
            将消息写入 L1 历史，登记 observation 事件并渲染观察输出；不启动模型生成。
        """
        context = inbound.context
        if context.stream.kind != 'group':
            raise ValueError('record_group_observation 只接受群聊消息')
        text = inbound.text.strip()
        if not text:
            raise ValueError('群聊消息正文不能为空')
        message_id = self.memory.append_message(
            context.stream.id,
            context.person.id,
            'user',
            text,
            current_time(),
            inbound.external_message_id,
        )
        self._plugins.observe_inbound(context.stream.id, message_id, inbound)
        if inbound.image_sources or inbound.emoji_sources:
            task = asyncio.create_task(self._describe_image_message(
                context.stream.id,
                message_id,
                text,
                inbound.image_sources,
                inbound.emoji_sources,
                inbound.emoji_sub_types,
            ))
            self._track_background_task(task)
        self._emit_group_observation(inbound, reason, text, inbound.external_message_id)
        # 只观察不回复的群消息同样推进场景：Bot 对群里的理解不该只在自己开口时才更新。
        self._schedule_scene_observation(context)
        return message_id

    def _external_group_message_seen(self, stream_id: int, external_id: str) -> bool:
        """判断某条群消息是否已经作为入站或观察事件进入过主体。"""
        row = self._db.execute(
            """SELECT 1 FROM pipeline_events
               WHERE stream_id = ?
                 AND kind IN ('user_input', 'observation')
                 AND json_extract(payload, '$.externalMessageId') = ?
               LIMIT 1""",
            (stream_id, external_id),
        ).fetchone()
        return row is not None

    def _known_group_external_ids(self, stream_id: int) -> set[str]:
        """读取指定群最近入站/观察事件中已登记的外部消息 ID。

        :param stream_id: 目标群 stream ID。
        :return: 非空 ``externalMessageId`` 集合；旧事件缺失该字段时不会报错。
        副作用：只读 pipeline_events。
        """
        rows = self._db.execute(
            """SELECT payload FROM pipeline_events
               WHERE stream_id = ?
                 AND kind IN ('user_input', 'observation')
               ORDER BY seq DESC LIMIT ?""",
            (stream_id, _BACKFILL_SEED_EVENT_LIMIT),
        ).fetchall()
        known: set[str] = set()
        for (payload,) in rows:
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                external_id = str(data.get('externalMessageId') or '').strip()
                if external_id:
                    known.add(external_id)
        return known

    def _seed_group_backfill_cursor(
        self,
        stream_id: int,
        messages: list[dict[str, Any]],
    ) -> tuple[int, set[str]]:
        """首次回填前用已有数据播种去重游标，减少历史消息重复落库。

        优先使用 pipeline_events 中仍可追溯的外部消息 ID；旧事件没有该字段时，
        再用 ``messages`` 表最近用户消息的正文与时间窗匹配历史条目。两类数据
        均无法确认边界时返回 ``0``，此时仍按原逻辑逐条查重。

        :param stream_id: 目标群 stream ID。
        :param messages: 按时间升序排列的待回填消息。
        :return: ``(可安全跳过的最大 seq, 已知外部消息 ID 集合)``。
        副作用：只读数据库，不写入游标。
        """
        known_ids = self._known_group_external_ids(stream_id)
        seeded = 0
        for raw in messages:
            external_id = str(raw.get('externalMessageId') or '').strip()
            if external_id not in known_ids:
                continue
            try:
                message_seq = int(raw.get('messageSeq') or 0)
            except (TypeError, ValueError):
                message_seq = 0
            seeded = max(seeded, message_seq)

        if not seeded:
            seeded = self._seed_backfill_cursor_from_messages(stream_id, messages)
        return seeded, known_ids

    def _seed_backfill_cursor_from_messages(
        self,
        stream_id: int,
        messages: list[dict[str, Any]],
    ) -> int:
        """用最近已落库用户消息的正文时间窗估计历史回填边界。

        这是上线前旧事件缺少 ``externalMessageId`` 时的一次性兼容路径；正文与
        平台时间需落在 10 分钟窗口内才算命中，宁可少播种也不能跳过停机期间的
        新消息。
        """
        rows = self._db.execute(
            """SELECT content, created_at FROM messages
               WHERE stream_id = ? AND role = 'user'
               ORDER BY id DESC LIMIT ?""",
            (stream_id, _BACKFILL_SEED_MESSAGE_LIMIT),
        ).fetchall()
        if not rows:
            return 0
        recent = [(str(row[0]), int(row[1])) for row in rows]
        seeded = 0
        for raw in messages:
            text = str(raw.get('text') or '').strip()
            if not text:
                continue
            try:
                message_seq = int(raw.get('messageSeq') or 0)
                created_at = int(raw.get('createdAt') or 0)
            except (TypeError, ValueError):
                continue
            if not message_seq or created_at <= 0:
                continue
            for content, saved_at in recent:
                if content != text:
                    continue
                if abs(saved_at - created_at) <= _BACKFILL_SEED_TIME_MATCH_MS:
                    seeded = max(seeded, message_seq)
                    break
        return seeded

    def record_group_backfill(
        self,
        context: ConversationContext,
        messages: list[dict[str, Any]],
    ) -> int:
        """把停机期间错过的群历史落为观察消息，不触发模型回复。

        :param context: 目标群聊归属上下文。
        :param messages: 按时间升序排列的历史消息；每项包含发送者、正文、消息 ID、seq 和时间。
        :return: 本次实际新写入的消息条数。
        :raises sqlite3.Error: 消息或游标写入失败。

        副作用：
            写入用户历史、登记 ``backfill`` 观察事件，并持久化该群的去重游标。
        """
        if context.stream.kind != 'group':
            raise ValueError('record_group_backfill 只接受群聊消息')
        stream_id = context.stream.id
        key = f'group_backfill_cursor_{stream_id}'
        cursor = self.memory.read_json(key, {'last_seq': 0, 'recent_ids': []})
        if not isinstance(cursor, dict):
            cursor = {'last_seq': 0, 'recent_ids': []}
        recent_ids = [
            str(item) for item in cursor.get('recent_ids', [])
            if isinstance(item, (str, int))
        ]
        recent = set(recent_ids)
        last_seq = int(cursor.get('last_seq') or 0)
        if not last_seq and messages:
            seeded_seq, known_ids = self._seed_group_backfill_cursor(stream_id, messages)
            recent.update(known_ids)
            if seeded_seq:
                last_seq = seeded_seq
                logger.info(
                    'QQ 群历史回填游标已从已有数据播种',
                    streamId=stream_id,
                    lastSeq=last_seq,
                )
        written = 0

        for raw in messages:
            external_id = str(raw.get('externalMessageId') or '').strip()
            if not external_id or external_id in recent:
                continue
            try:
                message_seq = int(raw.get('messageSeq') or 0)
            except (TypeError, ValueError):
                message_seq = 0
            if message_seq and message_seq <= last_seq:
                continue
            if self._external_group_message_seen(stream_id, external_id):
                continue
            text = str(raw.get('text') or '').strip()
            if not text:
                continue
            created_at = int(raw.get('createdAt') or 0)
            if created_at <= 0:
                created_at = current_time()
            sender_context = self._registry.resolve_inbound(
                platform=context.stream.platform,
                stream_kind='group',
                stream_external_id=context.stream.external_id,
                sender_external_id=str(raw.get('senderExternalId') or ''),
                sender_nickname=str(raw.get('senderNickname') or ''),
                sender_group_card=str(raw.get('senderGroupCard') or ''),
                first_seen_at=created_at,
            )
            self.memory.append_message(
                stream_id,
                sender_context.person.id,
                'user',
                text,
                created_at,
                external_id,
            )
            sender = self._sender_metadata(sender_context)
            trace.emit(
                'observation',
                streamId=stream_id,
                personId=sender_context.person.id,
                text=text,
                reason='backfill',
                externalMessageId=external_id,
                **sender,
            )
            recent.add(external_id)
            if message_seq > last_seq:
                last_seq = message_seq
            written += 1

        if written:
            self.memory.write_json(
                key,
                {
                    'last_seq': last_seq,
                    'recent_ids': list(recent)[-200:],
                },
            )
        return written

    def _emit_group_observation(
        self,
        inbound: InboundMessage,
        reason: str,
        text: str,
        external_message_id: str | None = None,
    ) -> None:
        """登记已保存群聊消息的观察事件并渲染控制台输出。

        :param inbound: 已完成 stream、人物和身份解析的群聊消息。
        :param reason: 本次不回复消息的策略原因。
        :param text: 已去除首尾空白且已经写入历史的消息正文。

        :raises ValueError: 入站消息不是群聊，或正文为空。

        副作用：
            登记 observation 事件并渲染观察输出，不重复写入消息历史。
        """
        context = inbound.context
        if context.stream.kind != 'group':
            raise ValueError('_emit_group_observation 只接受群聊消息')
        if not text:
            raise ValueError('群聊消息正文不能为空')
        sender = self._sender_metadata(context)
        source = source_label(context.stream, direct_name=sender['senderNickname'])
        trace.emit(
            'observation',
            streamId=context.stream.id,
            personId=context.person.id,
            text=text,
            reason=reason,
            externalMessageId=external_message_id or '',
            streamKind=context.stream.kind,
            streamExternalId=context.stream.external_id,
            sourceLabel=source,
            **sender,
        )
        render_observation(sender['senderLabel'], text, reason, source_label=source)
