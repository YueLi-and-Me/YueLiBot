"""事实的人工管理：标失效、恢复、永久保留、人工取代、冲突裁决与操作撤销。

人工管理不改写召回判据——失效仍是 ``superseded_by`` 非空，召回入口无需知道
人工操作的存在。「标失效」用自指哨兵（``superseded_by = 自己的 id``）编码，
与取代链指向他行区分开，全库消费方只测 NULL/NOT NULL，因此哨兵安全；
「永久保留」是 :data:`~src.core.memory.decay.PIN_HALF_LIFE_HOURS` 的半衰期
编码，不是新状态列——pinned 行被召回强化命中时天然安全（reinforce(1.0)=1.0、
due_at 按行自身半衰期重算），无需特判。

每一次人工操作都在 ``fact_operations`` 留一条流水：``prev`` 记操作前的值，
``undone_by`` / ``undo_of`` 构成撤销链。自动链路的显式取代（运行期抽取与
N4 反馈纠错）经由 ``MemoryStore.add_fact`` 落同一张流水表，人工界面因此能
看到全部改动，也能撤销自动操作。

SQL 全部落在 ``MemoryStore`` 的方法里，本模块只做状态判定、快照与事件。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import json

from src.core.common.clock import now as current_time
from src.core.observe import events as trace

from .decay import PIN_HALF_LIFE_HOURS, freeze_due_at, half_life_for, is_pinned, retention
from .similarity import exact_key, is_same_fact
from .store import FactInput, FactWrite, MemoryStore

# 人工操作在流水里的统一来源标记；自动链路（add_fact 取代路径）按各自 actor 落库。
ACTOR_MANUAL = 'manual'

# 撤销时可逆的失效类操作：逆操作都是把 superseded_by 写回 prev 值。
_SUPERSEDED_OPS = ('invalidate', 'restore', 'adjudicate')
# 撤销时回写衰减五字段的操作。
_DECAY_OPS = ('pin', 'unpin')
# 撤销时除回写旧行外，还要处理取代产生的新行的操作。
_REPLACE_OPS = ('supersede', 'replace')


class FactNotFoundError(RuntimeError):
    """目标事实或操作流水不存在时抛出；HTTP 层映射为 404。"""


class FactStateError(RuntimeError):
    """目标事实当前状态不允许该操作时抛出；HTTP 层映射为 409。"""


def _require_row(store: MemoryStore, fact_id: int) -> Dict[str, Any]:
    """读取事实行，不存在时抛 :class:`FactNotFoundError`。"""

    row = store.fact_row(fact_id)
    if row is None:
        raise FactNotFoundError(f'事实 {fact_id} 不存在')
    return row


def _require_valid_row(store: MemoryStore, fact_id: int, action: str) -> Dict[str, Any]:
    """读取事实行并要求其当前有效（未被取代或标失效）。"""

    row = _require_row(store, fact_id)
    if row['superseded_by'] is not None:
        raise FactStateError(f'事实 {fact_id} 已失效，不能{action}')
    return row


def _decay_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    """取一行的衰减五字段快照，作为 pin/unpin 与其撤销的 prev。"""

    return {
        'strength': row['strength'],
        'half_life_hours': row['half_life_hours'],
        'updated_at': row['updated_at'],
        'due_at': row['due_at'],
        'active': row['active'],
    }


def invalidate_fact(store: MemoryStore, fact_id: int, now: int) -> int:
    """把一条有效事实人工标为失效：``superseded_by`` 置为自指哨兵。

    :param store: 记忆存储实例。
    :param fact_id: 目标事实 ID。
    :param now: 操作发生的 Unix 毫秒时间戳。
    :return: 本次操作写入的流水行 ID。
    :raises FactNotFoundError: 事实不存在。
    :raises FactStateError: 事实已处于失效状态。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：更新 facts.superseded_by、写一条 op=``invalidate`` 的流水并提交；
        发出 ``memory_fact_invalidated`` 事件。不动 strength/updated_at/half_life_hours。
    """

    row = _require_row(store, fact_id)
    if row['superseded_by'] is not None:
        raise FactStateError(f'事实 {fact_id} 已处于失效状态，无需重复标失效')
    op_id = store.set_fact_superseded_with_log(
        fact_id, fact_id, at=now, actor=ACTOR_MANUAL, op='invalidate',
        person_id=row['person_id'], prev={'superseded_by': None},
    )
    trace.emit(
        'memory_fact_invalidated',
        factId=fact_id, personId=row['person_id'], operationId=op_id,
    )
    return op_id


def restore_fact(store: MemoryStore, fact_id: int, now: int) -> int:
    """恢复一条已失效事实：``superseded_by`` 清回 NULL。

    对取代链与自指哨兵同样生效——救回 N4 或抽取置的失效是特性而非漏洞。

    :param store: 记忆存储实例。
    :param fact_id: 目标事实 ID。
    :param now: 操作发生的 Unix 毫秒时间戳。
    :return: 本次操作写入的流水行 ID。
    :raises FactNotFoundError: 事实不存在。
    :raises FactStateError: 事实当前有效，无需恢复。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：更新 facts.superseded_by、写一条 op=``restore`` 的流水并提交；
        发出 ``memory_fact_restored`` 事件。不动 strength/updated_at/half_life_hours。
    """

    row = _require_row(store, fact_id)
    if row['superseded_by'] is None:
        raise FactStateError(f'事实 {fact_id} 当前有效，无需恢复')
    op_id = store.set_fact_superseded_with_log(
        fact_id, None, at=now, actor=ACTOR_MANUAL, op='restore',
        person_id=row['person_id'], prev={'superseded_by': row['superseded_by']},
    )
    trace.emit(
        'memory_fact_restored',
        factId=fact_id, personId=row['person_id'], operationId=op_id,
    )
    return op_id


def pin_fact(store: MemoryStore, fact_id: int, now: int) -> int:
    """把一条活跃事实永久保留：强度拉满、半衰期推远到 PIN 编码。

    永久保留不衰减：``due_at`` 按 100 年半衰期推远，sweep 在人的时间尺度上
    不会评估它；即便被召回强化命中，reinforce(1.0)=1.0 且 due_at 按行自身
    半衰期重算，pin 状态天然稳定，无需在召回路径特判。

    :param store: 记忆存储实例。
    :param fact_id: 目标事实 ID。
    :param now: 操作发生的 Unix 毫秒时间戳。
    :return: 本次操作写入的流水行 ID。
    :raises FactNotFoundError: 事实不存在。
    :raises FactStateError: 事实已失效、已冻结或已是永久保留状态。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：更新 facts 衰减五字段、写一条 op=``pin`` 的流水并提交；
        发出 ``memory_fact_pinned`` 事件。
    """

    row = _require_valid_row(store, fact_id, '永久保留')
    if is_pinned(row['half_life_hours']):
        raise FactStateError(f'事实 {fact_id} 已是永久保留状态')
    if not row['active']:
        raise FactStateError(f'事实 {fact_id} 已冻结，不能永久保留')
    op_id = store.set_fact_decay_with_log(
        fact_id,
        strength=1.0,
        half_life_hours=PIN_HALF_LIFE_HOURS,
        updated_at=now,
        due_at=freeze_due_at(1.0, now, PIN_HALF_LIFE_HOURS),
        active=1,
        at=now, actor=ACTOR_MANUAL, op='pin',
        person_id=row['person_id'], prev=_decay_snapshot(row),
    )
    trace.emit(
        'memory_fact_pinned',
        factId=fact_id, personId=row['person_id'], operationId=op_id,
    )
    return op_id


def unpin_fact(store: MemoryStore, fact_id: int, now: int) -> int:
    """取消一条事实的永久保留：半衰期回到其类型的自然值，强度不动。

    :param store: 记忆存储实例。
    :param fact_id: 目标事实 ID。
    :param now: 操作发生的 Unix 毫秒时间戳。
    :return: 本次操作写入的流水行 ID。
    :raises FactNotFoundError: 事实不存在。
    :raises FactStateError: 事实当前不是永久保留状态。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：更新 facts 衰减五字段、写一条 op=``unpin`` 的流水并提交；
        发出 ``memory_fact_unpinned`` 事件。
    """

    row = _require_row(store, fact_id)
    if not is_pinned(row['half_life_hours']):
        raise FactStateError(f'事实 {fact_id} 当前不是永久保留状态')
    natural_half_life = half_life_for(row['kind'])
    op_id = store.set_fact_decay_with_log(
        fact_id,
        strength=row['strength'],
        half_life_hours=natural_half_life,
        updated_at=now,
        due_at=freeze_due_at(row['strength'], now, natural_half_life),
        active=1,
        at=now, actor=ACTOR_MANUAL, op='unpin',
        person_id=row['person_id'], prev=_decay_snapshot(row),
    )
    trace.emit(
        'memory_fact_unpinned',
        factId=fact_id, personId=row['person_id'], operationId=op_id,
    )
    return op_id


def replace_fact(store: MemoryStore, fact_id: int, new_content: str, now: int) -> FactWrite:
    """人工取代：以新正文写新行，并在同一事务里把旧行标为由新行取代。

    流水由 ``add_fact`` 的取代路径写入（actor=``manual`` 时记 op=``replace``），
    本函数不重复写；返回的 :class:`FactWrite` 携带 ``operation_id`` 与新行 ID。

    :param store: 记忆存储实例。
    :param fact_id: 被取代的旧事实 ID。
    :param new_content: 新事实正文，去空白后不能为空。
    :param now: 操作发生的 Unix 毫秒时间戳。
    :return: 写入结果；``fact_id`` 是新行 ID，``conflict_with`` 报告同槽冲突。
    :raises FactNotFoundError: 旧事实不存在。
    :raises FactStateError: 旧事实已失效。
    :raises ValueError: 新正文为空，或与原事实表达同一件事。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：写入 facts/facts_fts 与一条 op=``replace`` 的流水并提交；
        发出 ``memory_fact_replaced`` 事件。
    """

    row = _require_valid_row(store, fact_id, '取代')
    content = new_content.strip()
    if not content:
        raise ValueError('新正文不能为空')
    if is_same_fact(content, row['content']):
        raise ValueError('新正文与原事实表达的是同一件事，无需取代')
    # 新正文与一条已失效行撞去重键时，add_fact 的精确键路径会命中那条死行：
    # 强化不清洗 superseded_by，取代结果落进一条仍失效的行里，永远召不回。
    # 这种局面必须显式拒绝，引导先恢复那条事实。
    duplicate = store.fact_row_by_content_key(row['person_id'], exact_key(content))
    if duplicate is not None and duplicate['superseded_by'] is not None:
        raise FactStateError(
            f"新正文与已失效的事实 {duplicate['id']} 完全相同，请先恢复那条事实",
        )
    written = store.add_fact(
        row['person_id'],
        FactInput(
            content=content,
            kind=row['kind'],
            slot=row['slot'],
            origin_kind=row['origin_kind'],
            supersedes=fact_id,
            actor=ACTOR_MANUAL,
        ),
        now,
    )
    trace.emit(
        'memory_fact_replaced',
        factId=fact_id,
        newFactId=written.fact_id,
        personId=row['person_id'],
        operationId=written.operation_id,
    )
    return written


def list_facts(store: MemoryStore, person_id: int, now: int) -> List[Dict[str, Any]]:
    """列出人物的全部事实并计算展示字段：当前留存度、失效与永久保留标记。

    :param store: 记忆存储实例。
    :param person_id: 目标人物 ID。
    :param now: 计算留存度的 Unix 毫秒时间戳。
    :return: 行字典列表；在原始行字段上附加 ``retention`` / ``invalid`` / ``pinned``。
    :raises sqlite3.Error: 查询失败。
    副作用：只读 facts 表。
    """

    return [
        {
            **row,
            'retention': retention(
                row['strength'], row['updated_at'], row['half_life_hours'], now,
            ),
            'invalid': row['superseded_by'] is not None,
            'pinned': is_pinned(row['half_life_hours']),
        }
        for row in store.list_fact_rows(person_id)
    ]


def conflict_groups(
    store: MemoryStore,
    person_id: Optional[int] = None,
    now: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """聚合全库或单人的槽位冲突组，供人工裁决界面列出待决事实。

    :param store: 记忆存储实例。
    :param person_id: 可选人物过滤；``None`` 表示全库聚合。
    :param now: 计算成员留存度的时间戳；省略时读取当前时钟。
    :return: 冲突组列表，members 按事实 ID 升序。
    :raises sqlite3.Error: 查询失败。
    副作用：只读 facts 与 identities 表。
    """

    return store.conflict_group_rows(
        now if now is not None else current_time(), person_id,
    )


def adjudicate(store: MemoryStore, keep_fact_id: int, drop_fact_id: int, now: int) -> int:
    """裁决一组槽位冲突：保留 keep，把 drop 标为失效（自指哨兵）。

    :param store: 记忆存储实例。
    :param keep_fact_id: 保留的事实 ID。
    :param drop_fact_id: 废弃的事实 ID。
    :param now: 操作发生的 Unix 毫秒时间戳。
    :return: 本次操作写入的流水行 ID。
    :raises FactNotFoundError: 任一事实不存在。
    :raises ValueError: 两 ID 相同、不属于同一人、或不在同一非空槽位。
    :raises FactStateError: 任一事实已不是活跃有效状态。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：更新 drop 行 superseded_by、写一条 op=``adjudicate`` 的流水并提交；
        发出 ``memory_conflict_resolved`` 事件。
    """

    if keep_fact_id == drop_fact_id:
        raise ValueError('保留与废弃不能是同一条事实')
    keep = store.fact_row(keep_fact_id)
    drop = store.fact_row(drop_fact_id)
    missing = [
        fid for fid, row in ((keep_fact_id, keep), (drop_fact_id, drop)) if row is None
    ]
    if missing:
        raise FactNotFoundError(f'事实 {missing} 不存在')
    assert keep is not None and drop is not None
    if keep['person_id'] != drop['person_id']:
        raise ValueError('两条事实不属于同一人，不能裁决')
    if not keep['slot'] or keep['slot'] != drop['slot']:
        raise ValueError('两条事实不在同一非空槽位，不能裁决')
    for row in (keep, drop):
        if row['superseded_by'] is not None or not row['active']:
            raise FactStateError(f"事实 {row['id']} 已不是活跃有效状态，不能裁决")
    op_id = store.set_fact_superseded_with_log(
        drop_fact_id, drop_fact_id, at=now, actor=ACTOR_MANUAL, op='adjudicate',
        person_id=drop['person_id'], related_fact_id=keep_fact_id,
        prev={'superseded_by': None},
    )
    trace.emit(
        'memory_conflict_resolved',
        keepFactId=keep_fact_id,
        dropFactId=drop_fact_id,
        personId=drop['person_id'],
        operationId=op_id,
    )
    return op_id


def operation_log(
    store: MemoryStore,
    person_id: Optional[int] = None,
    fact_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """按时间倒序读取事实操作流水，prev 解析为字典并附被操作事实正文。

    :param store: 记忆存储实例。
    :param person_id: 可选人物过滤。
    :param fact_id: 可选事实过滤。
    :param limit: 最多返回的条数。
    :return: 流水行字典列表。
    :raises sqlite3.Error: 查询失败。
    副作用：只读 fact_operations 与 facts 表。
    """

    return store.list_fact_operations(person_id=person_id, fact_id=fact_id, limit=limit)


def undo_operation(store: MemoryStore, op_id: int, now: int) -> int:
    """撤销一条操作流水：把受影响的 facts 行写回操作前的值，并互链撤销链。

    逆操作按原条类型分三类：失效类（invalidate/restore/adjudicate）把
    ``superseded_by`` 写回 prev 值；pin/unpin 写回 prev 的衰减五字段；
    supersede/replace 除回写旧行外，若当时新建了行且该行当前仍有效，把它
    标为自指失效——撤销产生的新行不物理删除，只退出召回。

    :param store: 记忆存储实例。
    :param op_id: 被撤销的流水行 ID。
    :param now: 撤销发生的 Unix 毫秒时间戳。
    :return: 新写入的 undo 流水行 ID。
    :raises FactNotFoundError: 流水或其指向的事实不存在。
    :raises FactStateError: 该操作已被撤销，或其本身是一条撤销操作。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：回写 facts 行、写入 op=``undo`` 的流水并把原条 ``undone_by``
        回填为新行 ID，一次提交；发出 ``memory_operation_undone`` 事件。
        撤销只还原因操作改变的字段，不动留存强度之外的语义。
    """

    op = store.fact_operation_row(op_id)
    if op is None:
        raise FactNotFoundError(f'操作 {op_id} 不存在')
    if op['undone_by'] is not None:
        raise FactStateError(f'操作 {op_id} 已被撤销，不能重复撤销')
    if op['op'] == 'undo':
        raise FactStateError('撤销操作本身不能再撤销')
    prev = json.loads(op['prev'] or '{}')

    superseded_writes: List[tuple[int, Optional[int]]] = []
    decay_writes: List[tuple[int, float, float, int, int, int]] = []
    if op['op'] in _SUPERSEDED_OPS:
        row = _require_row(store, op['fact_id'])
        undo_prev: Dict[str, Any] = {'superseded_by': row['superseded_by']}
        superseded_writes.append((op['fact_id'], prev.get('superseded_by')))
    elif op['op'] in _DECAY_OPS:
        row = _require_row(store, op['fact_id'])
        undo_prev = _decay_snapshot(row)
        decay_writes.append((
            op['fact_id'],
            float(prev['strength']),
            float(prev['half_life_hours']),
            int(prev['updated_at']),
            int(prev['due_at']),
            int(prev['active']),
        ))
    elif op['op'] in _REPLACE_OPS:
        row = _require_row(store, op['fact_id'])
        undo_prev = {'superseded_by': row['superseded_by']}
        superseded_writes.append((op['fact_id'], prev.get('superseded_by')))
        related_id = op['related_fact_id']
        if prev.get('new_row_created') and related_id:
            related = store.fact_row(related_id)
            if related is not None and related['superseded_by'] is None:
                superseded_writes.append((related_id, related_id))
                undo_prev['related_fact_invalidated'] = related_id
    else:
        raise FactStateError(f"操作 {op_id} 的类型 {op['op']} 不支持撤销")

    new_op_id = store.apply_operation_undo(
        original_op_id=op_id,
        at=now,
        actor=ACTOR_MANUAL,
        person_id=op['person_id'],
        fact_id=op['fact_id'],
        related_fact_id=op['fact_id'],
        prev=undo_prev,
        superseded_writes=superseded_writes,
        decay_writes=decay_writes,
    )
    trace.emit(
        'memory_operation_undone',
        operationId=new_op_id,
        undoOf=op_id,
        factId=op['fact_id'],
        personId=op['person_id'],
    )
    return new_op_id
