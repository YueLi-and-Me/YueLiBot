"""
stream / person / identity 的唯一读写入口。

业务层只能持有这里返回的引用，不能自行拼接数据库 ID 或直接读写归属表。desktop 与
外部平台的 stream 都必须从这里取得，平台适配器不自行编造数据库 ID。
"""

from __future__ import annotations

from typing import List

import sqlite3

from src.common.logger import get_logger
from src.platform_io.types import ConversationContext, PersonKind, PersonRef, StreamKind, StreamRef

logger = get_logger(__name__)

_OWNER_PERSON_ID = 1
_DESKTOP_STREAM_ID = 1
_DESKTOP_PLATFORM = "desktop"
_DESKTOP_KIND = "desktop"
_DESKTOP_EXTERNAL_ID = "desktop"


class StreamRegistry:
    """集中维护归属表，阻止业务层绕过 identity 自行编造 ID。"""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def owner_person(self) -> PersonRef:
        """读取迁移/SEED 确定的唯一 owner；缺失说明数据库形态损坏。"""
        person = self.person(_OWNER_PERSON_ID)
        if person.kind != "owner":
            raise RuntimeError("owner person 不存在或 kind 不正确，确认 v6 迁移已完成")
        return person

    def person(self, person_id: int) -> PersonRef:
        """按稳定主键取得 person，供需要显式人物归属的业务服务校验引用。"""
        row = self._db.execute(
            "SELECT id, kind, first_seen_at FROM persons WHERE id = ?",
            (person_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"person {person_id} 不存在，必须先经 StreamRegistry 创建或解析")
        if row[1] not in ("contact", "owner"):
            raise RuntimeError(f"person {person_id} 的 kind 不受支持：{row[1]}")
        return PersonRef(id=row[0], kind=row[1], first_seen_at=row[2])

    def desktop_stream(self) -> StreamRef:
        """读取迁移/SEED 确定的唯一 desktop stream。"""
        row = self._db.execute(
            """SELECT id, platform, kind, external_id FROM streams
               WHERE id = ?""",
            (_DESKTOP_STREAM_ID,),
        ).fetchone()
        expected = (_DESKTOP_PLATFORM, _DESKTOP_KIND, _DESKTOP_EXTERNAL_ID)
        if row is None or tuple(row[1:]) != expected:
            raise RuntimeError("desktop stream 不存在或标识不正确，确认 v6 迁移已完成")
        return StreamRef(id=row[0], platform=row[1], kind=row[2], external_id=row[3])

    def stream(self, stream_id: int) -> StreamRef:
        """按稳定主键读取 stream；不存在时直接暴露调用方传错的分区。"""
        row = self._db.execute(
            """SELECT id, platform, kind, external_id FROM streams
               WHERE id = ?""",
            (stream_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"stream {stream_id} 不存在，必须先经 StreamRegistry 创建或解析")
        return StreamRef(id=row[0], platform=row[1], kind=row[2], external_id=row[3])

    def list_streams(self) -> List[StreamRef]:
        """列出所有可观察 stream，供只读面板选择分区。"""
        rows = self._db.execute(
            """SELECT id, platform, kind, external_id FROM streams
               ORDER BY id ASC"""
        ).fetchall()
        return [
            StreamRef(id=row[0], platform=row[1], kind=row[2], external_id=row[3])
            for row in rows
        ]

    def observation_context(self, stream_id: int) -> ConversationContext:
        """解析只读面板查看一个 stream 时对应的人物上下文。

        desktop 固定属于 owner；direct 的 external_id 就是对端平台身份。
        group 没有唯一人物，因此使用该 stream 最近一条用户消息的发送者，
        并在尚无用户消息时明确报错，不虚构 owner 作为兜底。
        """
        stream = self.stream(stream_id)
        if stream.kind == "desktop":
            if stream.id != _DESKTOP_STREAM_ID:
                raise RuntimeError(f"发现非预期的 desktop stream：{stream.id}")
            return ConversationContext(stream=stream, person=self.owner_person())

        if stream.kind == "direct":
            person = self.find_person_by_identity(stream.platform, stream.external_id)
            if person is None:
                raise ValueError(
                    f"stream {stream.id} 尚未绑定平台身份，不能确定观察对象"
                )
            return ConversationContext(stream=stream, person=person)

        row = self._db.execute(
            """SELECT sender_person_id FROM messages
               WHERE stream_id = ? AND role = 'user' AND sender_person_id IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (stream.id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"group stream {stream.id} 尚无用户消息，不能确定观察对象")
        return ConversationContext(stream=stream, person=self.person(row[0]))

    def desktop_context(self) -> ConversationContext:
        """返回桌面唯一且完整的入站上下文。"""
        return ConversationContext(stream=self.desktop_stream(), person=self.owner_person())

    def resolve_inbound(
        self,
        platform: str,
        stream_kind: StreamKind,
        stream_external_id: str,
        sender_external_id: str,
        sender_name: str,
        first_seen_at: int,
    ) -> ConversationContext:
        """把平台入站字段解析为唯一的业务归属上下文。"""
        if stream_kind not in ('direct', 'group'):
            raise ValueError('平台入站仅支持 direct 或 group stream')
        platform = _require_text(platform, 'platform')
        stream = self.get_or_create_stream(platform, stream_kind, stream_external_id)
        person = self.find_person_by_identity(platform, sender_external_id)
        if person is None:
            person = self.create_person('contact', first_seen_at)
        self.link_identity(person, platform, sender_external_id, sender_name)
        return ConversationContext(stream=stream, person=person)

    def create_person(self, kind: PersonKind, first_seen_at: int) -> PersonRef:
        """创建非 owner person；owner 只能由迁移或 SEED 确定性创建。"""
        if kind != "contact":
            raise ValueError("只能通过 Registry 创建 kind='contact' 的 person")
        cur = self._db.execute(
            "INSERT INTO persons (kind, first_seen_at) VALUES (?, ?)",
            (kind, first_seen_at),
        )
        self._db.commit()
        if cur.lastrowid is None:
            raise RuntimeError("创建 person 后未获得主键")
        return PersonRef(id=cur.lastrowid, kind=kind, first_seen_at=first_seen_at)

    def find_person_by_identity(self, platform: str, external_id: str) -> PersonRef | None:
        """按平台身份查 person；不存在时返回 None，由上层决定是否创建联系人。"""
        row = self._db.execute(
            """SELECT p.id, p.kind, p.first_seen_at
               FROM identities AS i
               JOIN persons AS p ON p.id = i.person_id
               WHERE i.platform = ? AND i.external_id = ?""",
            (platform, external_id),
        ).fetchone()
        if row is None:
            return None
        return PersonRef(id=row[0], kind=row[1], first_seen_at=row[2])

    def display_name(self, person_id: int, platform: str) -> str:
        """读取指定平台上的显示名，供群聊历史在读取时标识说话人。"""
        platform = _require_text(platform, "platform")
        row = self._db.execute(
            '''SELECT display_name FROM identities
               WHERE person_id = ? AND platform = ?
               ORDER BY external_id ASC LIMIT 1''',
            (person_id, platform),
        ).fetchone()
        if row is None:
            raise ValueError(f"person {person_id} 在平台 {platform} 没有可用显示名")
        return row[0]

    def link_identity(
        self,
        person: PersonRef,
        platform: str,
        external_id: str,
        display_name: str,
    ) -> None:
        """把平台身份绑定到既有 person；跨 person 冲突直接暴露。"""
        platform = _require_text(platform, "platform")
        external_id = _require_text(external_id, "external_id")
        display_name = _require_text(display_name, "display_name")
        existing_person = self._db.execute(
            "SELECT id FROM persons WHERE id = ?",
            (person.id,),
        ).fetchone()
        if existing_person is None:
            raise ValueError("不能把 identity 绑定到不存在的 person")

        existing = self._db.execute(
            """SELECT person_id FROM identities
               WHERE platform = ? AND external_id = ?""",
            (platform, external_id),
        ).fetchone()
        if existing is not None and existing[0] != person.id:
            raise ValueError("该平台 identity 已绑定到另一 person")
        if existing is None:
            self._db.execute(
                """INSERT INTO identities (person_id, platform, external_id, display_name)
                   VALUES (?, ?, ?, ?)""",
                (person.id, platform, external_id, display_name),
            )
        else:
            self._db.execute(
                """UPDATE identities SET display_name = ?
                   WHERE platform = ? AND external_id = ?""",
                (display_name, platform, external_id),
            )
        self._db.commit()

    def set_sole_identity(
        self,
        person: PersonRef,
        platform: str,
        external_id: str,
        display_name: str,
    ) -> None:
        """让 person 在该平台只保留这一个身份，旧的解绑。

        用于 owner 这类「配置里只能填一个号」的绑定：配置项是单值的，
        数据也该跟着是单值的。留着旧号的后果是它永远解析成 owner——
        万一曾经填错成别人的号，那个人会一直读得到桌主的记忆。
        """
        platform = _require_text(platform, "platform")
        external_id = _require_text(external_id, "external_id")

        # 先解绑同平台上的其他号，再走正常绑定
        stale = self._db.execute(
            """SELECT external_id FROM identities
               WHERE person_id = ? AND platform = ? AND external_id != ?""",
            (person.id, platform, external_id),
        ).fetchall()
        if stale:
            self._db.execute(
                """DELETE FROM identities
                   WHERE person_id = ? AND platform = ? AND external_id != ?""",
                (person.id, platform, external_id),
            )
            self._db.commit()
            logger.info(
                '已解绑该 person 在此平台的旧身份',
                personId=person.id,
                platform=platform,
                removed=[row[0] for row in stale],
                kept=external_id,
            )
        self.link_identity(person, platform, external_id, display_name)

    def get_or_create_stream(
        self,
        platform: str,
        kind: StreamKind,
        external_id: str,
    ) -> StreamRef:
        """按平台、场所类别和外部标识取得稳定 stream。"""
        platform = _require_text(platform, "platform")
        external_id = _require_text(external_id, "external_id")
        if kind not in ("desktop", "direct", "group"):
            raise ValueError(f"不支持的 stream kind：{kind}")

        row = self._db.execute(
            """SELECT id, platform, kind, external_id FROM streams
               WHERE platform = ? AND kind = ? AND external_id = ?""",
            (platform, kind, external_id),
        ).fetchone()
        if row is not None:
            return StreamRef(id=row[0], platform=row[1], kind=row[2], external_id=row[3])

        cur = self._db.execute(
            """INSERT INTO streams (platform, kind, external_id)
               VALUES (?, ?, ?)""",
            (platform, kind, external_id),
        )
        self._db.commit()
        if cur.lastrowid is None:
            raise RuntimeError("创建 stream 后未获得主键")
        return StreamRef(
            id=cur.lastrowid,
            platform=platform,
            kind=kind,
            external_id=external_id,
        )


def _require_text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized
