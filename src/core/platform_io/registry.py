"""
stream / person / identity 的唯一读写入口。

业务层只能持有这里返回的引用，不能自行拼接数据库 ID 或直接读写归属表。desktop 与
外部平台的 stream 都必须从这里取得，平台适配器不自行编造数据库 ID。
"""

from __future__ import annotations

from typing import List

import sqlite3

from src.core.common.logger import get_logger
from src.core.platform_io.types import (
    ConversationContext,
    GroupMembershipRef,
    IdentityRef,
    PersonKind,
    PersonRef,
    StreamKind,
    StreamRef,
)

logger = get_logger(__name__)

_OWNER_PERSON_ID = 1
_DESKTOP_STREAM_ID = 1
_DESKTOP_PLATFORM = "desktop"
_DESKTOP_KIND = "desktop"
_DESKTOP_EXTERNAL_ID = "desktop"


class StreamRegistry:
    """集中维护 persons、identities、streams 及群成员关系的数据库引用。

    业务服务通过本类解析稳定主键和外部身份，避免直接拼接 ID 或绕过归属约束。
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        """绑定一个已完成迁移的 SQLite 连接。

        :param db: 用于读取和更新归属表的 SQLite 连接；事务提交由本类写入方法负责。
        """

        self._db = db

    def owner_person(self) -> PersonRef:
        """读取由迁移和种子数据确定的唯一 owner person。

        :return: ``kind`` 为 ``owner`` 的 owner person 引用。

        :raises RuntimeError: owner 记录不存在或其 ``kind`` 不是 ``owner``。
        :raises sqlite3.Error: 查询人物表失败。
        """
        person = self.person(_OWNER_PERSON_ID)
        if person.kind != "owner":
            raise RuntimeError("owner person 不存在或 kind 不正确，确认 v6 迁移已完成")
        return person

    def person(self, person_id: int) -> PersonRef:
        """按稳定数据库主键读取人物引用并校验人物类型。

        :param person_id: ``persons`` 表中的人物 ID。

        :return: 包含 ID、人物类型和首次出现时间的 ``PersonRef``。

        :raises ValueError: 人物不存在。
        :raises RuntimeError: 人物类型不是当前支持的 ``contact`` 或 ``owner``。
        :raises sqlite3.Error: 查询失败。
        """
        row = self._db.execute(
            "SELECT id, kind, first_seen_at FROM persons WHERE id = ?",
            (person_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"person {person_id} 不存在，必须先经 StreamRegistry 创建或解析")
        if row[1] not in ("contact", "owner"):
            raise RuntimeError(f"person {person_id} 的 kind 不受支持：{row[1]}")
        return PersonRef(id=row[0], kind=row[1], first_seen_at=row[2])

    def list_persons(self, stream_id: int | None = None) -> List[PersonRef]:
        """列出全部人物，或列出指定 stream 中实际发送过消息的人物。

        :param stream_id: 可选 stream ID；省略时查询全部人物，传入时仅返回该 stream 的
                user 消息发送者。

        :return: 按人物 ID 升序排列的 ``PersonRef`` 列表。

        :raises ValueError: 指定 stream 不存在。
        :raises sqlite3.Error: 查询失败。
        """
        if stream_id is None:
            rows = self._db.execute(
                "SELECT id, kind, first_seen_at FROM persons ORDER BY id ASC"
            ).fetchall()
        else:
            stream = self.stream(stream_id)
            rows = self._db.execute(
                """SELECT DISTINCT p.id, p.kind, p.first_seen_at
                   FROM messages AS m
                   JOIN persons AS p ON p.id = m.sender_person_id
                   WHERE m.stream_id = ? AND m.role = 'user'
                   ORDER BY p.id ASC""",
                (stream.id,),
            ).fetchall()
        return [
            PersonRef(id=row[0], kind=row[1], first_seen_at=row[2])
            for row in rows
        ]

    def list_identities(self, person_id: int) -> List[IdentityRef]:
        """列出指定人物的全部平台身份。

        :param person_id: 目标人物 ID。

        :return: 按平台和外部 ID 排序的 ``IdentityRef`` 列表；没有身份时返回空列表。

        :raises ValueError: 人物不存在。
        :raises sqlite3.Error: 查询身份表失败。
        """
        person = self.person(person_id)
        rows = self._db.execute(
            """SELECT platform, external_id, display_name FROM identities
               WHERE person_id = ? ORDER BY platform ASC, external_id ASC""",
            (person.id,),
        ).fetchall()
        return [
            IdentityRef(platform=row[0], external_id=row[1], display_name=row[2])
            for row in rows
        ]

    def group_memberships(self, person_id: int) -> List[GroupMembershipRef]:
        """列出人物在各群聊 stream 中的当前群名片。

        :param person_id: 目标人物 ID。

        :return: 按 stream ID 排序的 ``GroupMembershipRef`` 列表；空群名片表示当前未设置。

        :raises ValueError: 人物不存在。
        :raises sqlite3.Error: 查询群成员关系失败。
        """
        person = self.person(person_id)
        rows = self._db.execute(
            '''SELECT gm.stream_id, s.external_id, gm.group_card, gm.updated_at
               FROM group_memberships AS gm
               JOIN streams AS s ON s.id = gm.stream_id
               WHERE gm.person_id = ?
               ORDER BY gm.stream_id ASC''',
            (person.id,),
        ).fetchall()
        return [
            GroupMembershipRef(
                stream_id=row[0],
                group_external_id=row[1],
                group_card=row[2],
                updated_at=row[3],
            )
            for row in rows
        ]

    def desktop_stream(self) -> StreamRef:
        """读取由迁移和种子数据确定的唯一 desktop stream。

        :return: 标识为 ``desktop/desktop/desktop`` 的 ``StreamRef``。

        :raises RuntimeError: desktop stream 缺失或其平台、类型、外部 ID 不符合固定约定。
        :raises sqlite3.Error: 查询失败。
        """
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
        """按稳定数据库主键读取会话引用。

        :param stream_id: ``streams`` 表中的会话 ID。

        :return: 包含平台、会话类型和外部 ID 的 ``StreamRef``。

        :raises ValueError: stream 不存在。
        :raises sqlite3.Error: 查询失败。
        """
        row = self._db.execute(
            """SELECT id, platform, kind, external_id FROM streams
               WHERE id = ?""",
            (stream_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"stream {stream_id} 不存在，必须先经 StreamRegistry 创建或解析")
        return StreamRef(id=row[0], platform=row[1], kind=row[2], external_id=row[3])

    def list_streams(self) -> List[StreamRef]:
        """列出全部可观察 stream。

        :return: 按 stream ID 升序排列的 ``StreamRef`` 列表。

        :raises sqlite3.Error: 查询失败。
        """
        rows = self._db.execute(
            """SELECT id, platform, kind, external_id FROM streams
               ORDER BY id ASC"""
        ).fetchall()
        return [
            StreamRef(id=row[0], platform=row[1], kind=row[2], external_id=row[3])
            for row in rows
        ]

    def list_person_streams(self, person_id: int) -> List[StreamRef]:
        """列出指定人物在 user 消息中实际发言过的会话。

        :param person_id: 目标人物 ID。

        :return: 按 stream ID 升序排列的会话引用；仅有身份绑定但没有消息的会话不会返回。

        :raises ValueError: 人物不存在。
        :raises sqlite3.Error: 查询消息和会话表失败。
        """
        person = self.person(person_id)
        rows = self._db.execute(
            """SELECT DISTINCT s.id, s.platform, s.kind, s.external_id
               FROM messages AS m
               JOIN streams AS s ON s.id = m.stream_id
               WHERE m.sender_person_id = ? AND m.role = 'user'
               ORDER BY s.id ASC""",
            (person.id,),
        ).fetchall()
        return [
            StreamRef(id=row[0], platform=row[1], kind=row[2], external_id=row[3])
            for row in rows
        ]

    def desktop_context(self) -> ConversationContext:
        """返回桌面 stream 与 owner person 组成的完整会话上下文。

        :return: 包含固定 desktop stream 和 owner person 的 ``ConversationContext``。

        :raises RuntimeError: 数据库缺少或损坏固定 desktop/owner 记录。
        :raises sqlite3.Error: 查询失败。
        """
        return ConversationContext(stream=self.desktop_stream(), person=self.owner_person())

    def resolve_inbound(
        self,
        platform: str,
        stream_kind: StreamKind,
        stream_external_id: str,
        sender_external_id: str,
        sender_nickname: str,
        sender_group_card: str,
        first_seen_at: int,
    ) -> ConversationContext:
        """按平台和外部标识解析人物、会话及当前显示信息。

        :param platform: 平台标识，不能为空。
        :param stream_kind: 会话类型，只支持 ``direct`` 和 ``group``。
        :param stream_external_id: 平台侧会话外部 ID。
        :param sender_external_id: 平台侧发送者外部 ID。
        :param sender_nickname: 平台侧账号昵称。
        :param sender_group_card: 当前群聊中的群名片；私聊时可为空。
        :param first_seen_at: 新人物首次出现的 Unix 毫秒时间戳。

        :return: 包含稳定 stream、person、账号身份和群名片的 ``ConversationContext``。

        :raises ValueError: 会话类型或任一必需外部标识为空、格式不支持。
        :raises sqlite3.Error: stream、person、identity 或群成员关系写入失败。

        副作用：
            可能创建 stream/person，更新平台账号昵称和当前群名片，并提交对应事务。
        """
        if stream_kind not in ('direct', 'group'):
            raise ValueError('平台入站仅支持 direct 或 group stream')
        platform = _require_text(platform, 'platform')
        stream = self.get_or_create_stream(platform, stream_kind, stream_external_id)
        person = self.find_person_by_identity(platform, sender_external_id)
        if person is None:
            person = self.create_person('contact', first_seen_at)
        self.link_identity(person, platform, sender_external_id, sender_nickname)
        group_card = sender_group_card.strip()
        if stream.kind == 'group':
            self.set_group_card(person, stream, group_card, first_seen_at)
        identity = IdentityRef(
            platform=platform,
            external_id=_require_text(sender_external_id, 'sender_external_id'),
            display_name=_require_text(sender_nickname, 'sender_nickname'),
        )
        return ConversationContext(
            stream=stream,
            person=person,
            identity=identity,
            group_card=group_card,
        )

    def create_person(self, kind: PersonKind, first_seen_at: int) -> PersonRef:
        """创建一个联系人人物记录。

        :param kind: 人物类型；当前仅允许 ``contact``。
        :param first_seen_at: 人物首次出现的 Unix 毫秒时间戳。

        :return: 新建人物的 ``PersonRef``。

        :raises ValueError: ``kind`` 不是 ``contact``。
        :raises RuntimeError: 插入后未获得人物主键。
        :raises sqlite3.Error: 插入或提交失败。

        副作用：
            向 ``persons`` 表插入一行并提交事务；owner 不由该方法创建。
        """
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
        """按平台和外部身份查找其归属人物。

        :param platform: 平台标识。
        :param external_id: 平台侧外部身份 ID。

        :return: 已绑定身份对应的 ``PersonRef``；没有匹配身份时返回 ``None``。

        :raises sqlite3.Error: 查询身份或人物表失败。
        """
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
        """读取人物在指定平台上的当前账号昵称，不混入群名片。

        :param person_id: 目标人物 ID。
        :param platform: 目标平台标识，不能为空。

        :return: 该人物在平台上的账号显示名。

        :raises ValueError: 平台为空，或人物在平台上没有账号身份。
        :raises sqlite3.Error: 查询失败。
        """
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

    def stream_display_name(self, person_id: int, stream_id: int) -> str:
        """读取指定会话内的显示名，并按群名片优先级回退到账号昵称。

        :param person_id: 目标人物 ID。
        :param stream_id: 目标 stream ID。

        :return: 群聊中非空群名片，或该人物在 stream 平台上的账号显示名。

        :raises ValueError: stream 或人物不存在，或平台上没有账号身份。
        :raises sqlite3.Error: 查询失败。
        """
        stream = self.stream(stream_id)
        self.person(person_id)
        if stream.kind == 'group':
            row = self._db.execute(
                '''SELECT group_card FROM group_memberships
                   WHERE stream_id = ? AND person_id = ?''',
                (stream.id, person_id),
            ).fetchone()
            if row is not None and row[0]:
                return row[0]
        return self.display_name(person_id, stream.platform)

    def set_group_card(
        self,
        person: PersonRef,
        stream: StreamRef,
        group_card: str,
        updated_at: int,
    ) -> None:
        """按人物和群聊 stream 更新当前群名片。

        :param person: 目标人物引用。
        :param stream: 目标群聊 stream 引用。
        :param group_card: 当前群名片；空字符串表示清除名片。
        :param updated_at: 名片更新时间的 Unix 毫秒时间戳。

        :raises ValueError: stream 不是群聊，或人物不存在。
        :raises sqlite3.Error: 群成员关系插入、更新或提交失败。

        副作用：
            插入或更新 ``group_memberships`` 记录并提交事务；清空名片也会保留更新时间记录。
        """
        stored_stream = self.stream(stream.id)
        if stored_stream.kind != 'group':
            raise ValueError('群名片只能绑定到 group stream')
        self.person(person.id)
        self._db.execute(
            '''INSERT INTO group_memberships (
                   stream_id, person_id, group_card, updated_at
               ) VALUES (?, ?, ?, ?)
               ON CONFLICT(stream_id, person_id) DO UPDATE SET
                   group_card = excluded.group_card,
                   updated_at = excluded.updated_at''',
            (stored_stream.id, person.id, group_card.strip(), updated_at),
        )
        self._db.commit()

    def link_identity(
        self,
        person: PersonRef,
        platform: str,
        external_id: str,
        display_name: str,
    ) -> None:
        """将平台外部身份绑定到既有人物，并更新其账号显示名。

        :param person: 已存在的目标人物引用。
        :param platform: 平台标识，不能为空。
        :param external_id: 平台侧外部身份 ID，不能为空。
        :param display_name: 平台侧账号显示名，不能为空。

        :raises ValueError: 人物不存在、标识为空，或身份已绑定到其他人物。
        :raises sqlite3.Error: 身份插入、显示名更新或提交失败。

        副作用：
            新身份会插入 ``identities``；既有同人物身份只更新显示名，并提交事务。
        """
        platform = _require_text(platform, "platform")
        external_id = _require_text(external_id, "external_id")
        display_name = _require_text(display_name, "display_name")
        # 先确认人物存在，再检查平台外部身份是否已归属于其他人物。
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
        # 同一 person 的既有身份只更新展示名，不改变稳定外部标识或归属关系。
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
        """使人物在指定平台只保留一个外部身份，并解除同平台旧绑定。

        该方法用于配置层声明为单值的平台身份。保留旧身份会使历史外部账号继续
        解析到当前人物，造成账号归属错误和记忆越权风险。

        :param person: 目标人物引用。
        :param platform: 平台标识，不能为空。
        :param external_id: 要保留的平台外部身份 ID，不能为空。
        :param display_name: 要保留身份的显示名，不能为空。

        :raises ValueError: 参数为空、人物不存在，或新身份已绑定到其他人物。
        :raises sqlite3.Error: 旧身份删除、新身份写入或提交失败。

        副作用：
            删除该人物在平台上的其他身份，记录解绑日志，再通过 ``link_identity``
            写入或更新指定身份。
        """
        platform = _require_text(platform, "platform")
        external_id = _require_text(external_id, "external_id")

        # 先解除同平台其他外部账号的旧绑定，再执行统一绑定流程。
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
        """按平台、会话类型和外部标识读取或创建稳定 stream。

        :param platform: 平台标识，不能为空。
        :param kind: 会话类型，只支持 ``desktop``、``direct`` 和 ``group``。
        :param external_id: 平台侧会话外部 ID，不能为空。

        :return: 已存在或新建的 ``StreamRef``。

        :raises ValueError: 外部标识为空，或会话类型不受支持。
        :raises RuntimeError: 新建 stream 后未获得数据库主键。
        :raises sqlite3.Error: 查询、插入或提交失败。

        副作用：
            stream 不存在时向 ``streams`` 表插入记录并提交事务；存在时只读查询。
        """
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
    """校验并规范化数据库外部标识文本。

    :param value: 待校验的字符串。
    :param name: 错误信息中使用的字段名。

    :return: 去除首尾空白后的非空字符串。

    :raises ValueError: 字符串为空或只包含空白。
    """

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized
