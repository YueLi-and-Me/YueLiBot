"""
三层记忆的读写。直接移植自 src/core/memory/store.ts。

接受已打开的 sqlite3.Connection，不自己开文件：
  · core 层不该知道用户数据目录在哪
  · 测试可传入 ':memory:' 拿到隔离库

L1 messages  — 对话原文，最近若干轮直接进 context
L2 episodes  — 每 N 轮压缩成一条摘要，同时是日记内容
L3 facts     — 结构化事实，带强度与遗忘曲线
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import json
import sqlite3

from .decay import (
    FREEZE, DecayState, evaluate, freeze_due_at, half_life_for,
    reinforce, relevance_from_bm25, retention, retention_weight, score,
)
from .similarity import exact_key, is_same_fact
from .tokenize import index_tokens, match_query

from src.common.clock import now as current_time
from src.common.db.schema import DDL, SCHEMA_VERSION, SEED


_PENDING_PROMISES_KEY = 'pending_promises'


@dataclass
class StoredMessage:
    role: str   # 'user' | 'assistant'
    content: str
    created_at: int
    sender_person_id: int | None


@dataclass
class FactInput:
    content: str
    kind: str = '未分类'


@dataclass
class RecalledFact:
    id: int
    kind: str
    content: str
    retention: float
    score: float


@dataclass
class StoredFact(RecalledFact):
    due_at: int = 0
    frozen: bool = False


@dataclass
class RecalledEpisode:
    id: int
    summary: str
    kind: str
    ended_at: int
    score: float


@dataclass
class EpisodeInput:
    summary: str
    cues: list[str]
    started_at: int
    ended_at: int
    message_ids: list[int]
    kind: str = 'conversation'


class MemoryStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        db.executescript(DDL)
        db.executescript(SEED)
        db.execute(
            'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
            ('schema_version', str(SCHEMA_VERSION))
        )
        db.commit()

    # ------------------------------------------------------------------ 属性
    def first_seen_at(self, person_id: int) -> int:
        row = self._db.execute(
            "SELECT first_seen_at FROM persons WHERE id = ?", (person_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"person {person_id} 不存在，必须先经 StreamRegistry 解析")
        return row[0]

    # ------------------------------------------------------------------ L1 工作记忆
    def append_message(
        self,
        stream_id: int,
        sender_person_id: int | None,
        role: str,
        content: str,
        now: int | None = None,
    ) -> int:
        if role == 'user' and sender_person_id is None:
            raise ValueError('user 消息必须携带 sender_person_id')
        now = now if now is not None else current_time()
        cur = self._db.execute(
            '''INSERT INTO messages (stream_id, sender_person_id, role, content, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            (stream_id, sender_person_id, role, content, now)
        )
        self._db.commit()
        return cur.lastrowid or 0

    def delete_message(self, stream_id: int, id: int) -> None:
        self._db.execute('DELETE FROM messages WHERE id = ? AND stream_id = ?', (id, stream_id))
        self._db.commit()

    def working_memory(self, stream_id: int, limit: int = 40) -> list[StoredMessage]:
        rows = self._db.execute(
            '''SELECT role, content, created_at, sender_person_id FROM messages
               WHERE stream_id = ? AND episode_id IS NULL ORDER BY id DESC LIMIT ?''',
            (stream_id, limit)
        ).fetchall()
        return [StoredMessage(role=r[0], content=r[1], created_at=r[2], sender_person_id=r[3])
                for r in reversed(rows)]

    def last_message_at(self, stream_id: int) -> int | None:
        row = self._db.execute(
            'SELECT MAX(created_at) FROM messages WHERE stream_id = ?', (stream_id,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def interaction_density(self, stream_id: int, now: int | None = None) -> str:
        now = now if now is not None else current_time()
        since = now - 3 * 24 * 60 * 60_000
        row = self._db.execute(
            'SELECT COUNT(*) FROM messages WHERE stream_id = ? AND created_at >= ?',
            (stream_id, since),
        ).fetchone()
        n = row[0] if row else 0
        if n >= 40:
            return '最近几天你们聊得很多，生活安排可以留出更多陪伴和放松的空白。'
        if n >= 10:
            return '最近几天你们偶尔聊聊，节奏自然而不拥挤。'
        return '最近几天互动很少，安排更偏向安静地做自己的事。'

    def pending_count(self, stream_id: int) -> int:
        row = self._db.execute(
            'SELECT COUNT(*) FROM messages WHERE stream_id = ? AND episode_id IS NULL',
            (stream_id,),
        ).fetchone()
        return row[0] if row else 0

    def assistant_reply_count_since(self, stream_id: int, since: int) -> int:
        """统计群聊硬频率闸窗口内已经落库的助手回复数。"""
        row = self._db.execute(
            '''SELECT COUNT(*) FROM messages
               WHERE stream_id = ? AND role = 'assistant' AND created_at >= ?''',
            (stream_id, since),
        ).fetchone()
        return row[0] if row else 0

    def oldest_pending(self, stream_id: int, n: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            '''SELECT id, role, content, created_at, sender_person_id FROM messages
               WHERE stream_id = ? AND episode_id IS NULL ORDER BY id ASC LIMIT ?''',
            (stream_id, n)
        ).fetchall()
        return [{'id': r[0], 'role': r[1], 'content': r[2], 'created_at': r[3],
                 'sender_person_id': r[4]}
                for r in rows]

    # ------------------------------------------------------------------ L2 情节记忆
    def add_episode(self, stream_id: int, input: EpisodeInput, now: int | None = None) -> int:
        now = now if now is not None else current_time()
        cur = self._db.execute(
            '''INSERT INTO episodes (stream_id, kind, summary, started_at, ended_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (stream_id, input.kind, input.summary, input.started_at, input.ended_at, now)
        )
        episode_id = cur.lastrowid or 0

        for cue in input.cues:
            c = cue.strip()
            if not c:
                continue
            cue_cur = self._db.execute(
                'INSERT INTO episode_cues (episode_id, cue) VALUES (?, ?)', (episode_id, c)
            )
            cue_id = cue_cur.lastrowid or 0
            self._db.execute(
                'INSERT INTO cues_fts (rowid, tokens) VALUES (?, ?)', (cue_id, index_tokens(c))
            )

        if input.message_ids:
            placeholders = ','.join('?' * len(input.message_ids))
            self._db.execute(
                f'''UPDATE messages SET episode_id = ?
                    WHERE stream_id = ? AND id IN ({placeholders})''',
                (episode_id, stream_id, *input.message_ids)
            )
        self._db.commit()
        return episode_id

    def recent_episodes(self, stream_id: int, limit: int = 4) -> list[RecalledEpisode]:
        rows = self._db.execute(
            '''SELECT id, summary, kind, ended_at FROM episodes
               WHERE stream_id = ? ORDER BY ended_at DESC LIMIT ?''',
            (stream_id, limit)
        ).fetchall()
        return [RecalledEpisode(id=r[0], summary=r[1], kind=r[2], ended_at=r[3], score=1.0)
                for r in rows]

    def all_episodes(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.execute(
            '''SELECT id, kind, summary, ended_at, stream_id FROM episodes
               ORDER BY ended_at DESC LIMIT ?''',
            (limit,)
        ).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        placeholders = ','.join('?' * len(ids))
        cue_rows = self._db.execute(
            f'SELECT episode_id, cue FROM episode_cues WHERE episode_id IN ({placeholders})',
            ids
        ).fetchall()
        by_id: dict[int, list[str]] = {}
        for c in cue_rows:
            by_id.setdefault(c[0], []).append(c[1])
        return [{'id': r[0], 'kind': r[1], 'summary': r[2], 'ended_at': r[3],
                 'streamId': r[4],
                 'cues': by_id.get(r[0], [])} for r in rows]

    def recall_episodes(self, stream_id: int, query: str, limit: int = 3) -> list[RecalledEpisode]:
        match = match_query(query)
        if not match:
            return []
        rows = self._db.execute(
            '''SELECT e.id, e.summary, e.kind, e.ended_at, bm25(cues_fts) AS bm
               FROM cues_fts
               JOIN episode_cues c ON c.id = cues_fts.rowid
               JOIN episodes e     ON e.id = c.episode_id
               WHERE cues_fts MATCH ? AND e.stream_id = ?
               ORDER BY bm ASC LIMIT ?''',
            (match, stream_id, limit * 4)
        ).fetchall()
        best: dict[int, RecalledEpisode] = {}
        for r in rows:
            if r[0] not in best:
                best[r[0]] = RecalledEpisode(
                    id=r[0], summary=r[1], kind=r[2], ended_at=r[3], score=score(r[4], 1.0)
                )
        return list(best.values())[:limit]

    # ------------------------------------------------------------------ L3 语义记忆
    def add_fact(self, person_id: int, input: FactInput, now: int | None = None) -> int:
        now = now if now is not None else current_time()
        content = input.content.strip()
        if not content:
            return 0
        key = exact_key(content)
        if not key:
            return 0
        half_life = half_life_for(input.kind)

        existing = self._find_similar(person_id, content, key)
        if existing:
            cur_ret = retention(existing['strength'], existing['updated_at'], existing['half_life_hours'], now)
            next_strength = reinforce(cur_ret)
            self._db.execute(
                '''UPDATE facts SET strength = ?, updated_at = ?, due_at = ?, active = 1,
                                     hit_count = hit_count + 1 WHERE id = ? AND person_id = ?''',
                (
                    next_strength,
                    now,
                    freeze_due_at(next_strength, now, existing['half_life_hours']),
                    existing['id'],
                    person_id,
                )
            )
            self._db.commit()
            return existing['id']

        due = freeze_due_at(1.0, now, half_life)
        cur = self._db.execute(
            '''INSERT INTO facts (person_id, kind, content, content_key, strength, half_life_hours,
                                  updated_at, created_at, due_at, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)''',
            (person_id, input.kind, content, key, 1.0, half_life, now, now, due)
        )
        fid = cur.lastrowid or 0
        self._db.execute(
            'INSERT INTO facts_fts (rowid, tokens) VALUES (?, ?)', (fid, index_tokens(content))
        )
        self._db.commit()
        return fid

    def _find_similar(self, person_id: int, content: str, key: str) -> dict[str, Any] | None:
        row = self._db.execute(
            '''SELECT id, content, strength, updated_at, half_life_hours
               FROM facts WHERE person_id = ? AND content_key = ?''', (person_id, key)
        ).fetchone()
        if row:
            return {'id': row[0], 'content': row[1], 'strength': row[2],
                    'updated_at': row[3], 'half_life_hours': row[4]}
        match = match_query(content)
        if not match:
            return None
        candidates = self._db.execute(
            '''SELECT f.id, f.content, f.strength, f.updated_at, f.half_life_hours
               FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
               WHERE facts_fts MATCH ? AND f.person_id = ?
               ORDER BY bm25(facts_fts) ASC LIMIT 8''',
            (match, person_id)
        ).fetchall()
        for c in candidates:
            if is_same_fact(content, c[1]):
                return {'id': c[0], 'content': c[1], 'strength': c[2],
                        'updated_at': c[3], 'half_life_hours': c[4]}
        return None

    def recall_facts(self, person_id: int, query: str, limit: int = 6, now: int | None = None,
                      query_embedding: bytes | None = None) -> list[RecalledFact]:
        """
        BM25 召回，可选混合向量打分。

        query_embedding: 查询文本的 float32 packed bytes（由 VectorService 注入）。
                         为 None 时退回纯 BM25，行为与重构前完全一致。
        """
        now = now if now is not None else current_time()
        match = match_query(query)
        if not match:
            return []
        rows = self._db.execute(
            '''SELECT f.id, f.kind, f.content, f.strength, f.updated_at,
                      f.half_life_hours, f.active, bm25(facts_fts) AS bm,
                      f.embedding
               FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
               WHERE facts_fts MATCH ? AND f.person_id = ?
               ORDER BY bm ASC LIMIT ?''',
            (match, person_id, limit * 3)
        ).fetchall()
        scored = []
        for r in rows:
            ret = retention(r[3], r[4], r[5], now)
            relevance = relevance_from_bm25(r[7])
            # ── 向量融合 ───────────────────────────────────────────────────
            # 只有查询向量和事实向量都存在时才做混合，任意一方缺失就纯 BM25。
            # 权重 0.4 BM25 + 0.6 向量：向量召回「换个说法也能找到」的收益更高，
            # 但 BM25 精确词面匹配仍有价值（专名、数字、代码关键字）。
            #
            # ★ 融合发生在**相关度**层面，留存度权重最后统一乘上去。
            #   此前是 0.4*score(bm25, ret) + 0.6*vec_score——留存度只作用于
            #   BM25 那 40%，向量那 60% 完全不受遗忘曲线约束，等于一条已经
            #   衰减到该被忘掉的事实，只要语义相近就能满血召回，跟三层记忆
            #   「会遗忘」的设计意图直接冲突。
            fact_embedding = r[8]
            if query_embedding is not None and fact_embedding is not None:
                try:
                    from .embed import cosine
                    dim = len(query_embedding) // 4  # float32 = 4 bytes
                    cos = cosine(query_embedding, fact_embedding, dim)
                    # cos 归一化到 [0,1]（L2 归一化向量的内积已在[-1,1]，+1后/2）
                    vec_score = (cos + 1) / 2
                    relevance = 0.4 * relevance + 0.6 * vec_score
                except Exception:
                    pass   # 向量打分失败静默降级，不影响 BM25 结果
            final_score = relevance * retention_weight(ret)
            scored.append(RecalledFact(id=r[0], kind=r[1], content=r[2],
                                        retention=ret, score=final_score))
        scored.sort(key=lambda x: x.score, reverse=True)
        result = scored[:limit]
        # 命中即回补
        for h in result:
            row = next((r for r in rows if r[0] == h.id), None)
            if row:
                nxt = reinforce(h.retention)
                self._db.execute(
                    '''UPDATE facts SET strength = ?, updated_at = ?, due_at = ?, active = 1,
                                         hit_count = hit_count + 1, last_hit_at = ?
                       WHERE id = ? AND person_id = ?''',
                    (nxt, now, freeze_due_at(nxt, now, row[5]), now, h.id, person_id)
                )
        if result:
            self._db.commit()
        return result

    def store_embedding(self, fact_id: int, embedding: bytes) -> None:
        """写入事实的向量。由 VectorService 在后台异步填充。"""
        self._db.execute('UPDATE facts SET embedding = ? WHERE id = ?', (embedding, fact_id))
        self._db.commit()

    def facts_without_embedding(self, limit: int = 128) -> list[dict]:
        """取尚未计算 embedding 的事实，供后台批量补算。"""
        rows = self._db.execute(
            'SELECT id, content FROM facts WHERE embedding IS NULL LIMIT ?', (limit,)
        ).fetchall()
        return [{'id': r[0], 'content': r[1]} for r in rows]

    def sweep(self, now: int | None = None) -> int:
        now = now if now is not None else current_time()
        due = self._db.execute(
            'SELECT id, strength, updated_at, half_life_hours, active FROM facts WHERE active = 1 AND due_at <= ?',
            (now,)
        ).fetchall()
        if not due:
            return 0
        n = 0
        for f in due:
            ev = evaluate(DecayState(strength=f[1], updated_at=f[2], half_life_hours=f[3], active=bool(f[4])), now)
            if not ev.active:
                self._db.execute('UPDATE facts SET active = 0, due_at = ? WHERE id = ?', (ev.due_at, f[0]))
                n += 1
        self._db.commit()
        return n

    def top_facts(self, person_id: int, limit: int = 8,
                  now: int | None = None) -> list[RecalledFact]:
        now = now if now is not None else current_time()
        rows = self._db.execute(
            '''SELECT id, kind, content, strength, updated_at, half_life_hours
               FROM facts WHERE person_id = ? AND active = 1''',
            (person_id,)
        ).fetchall()
        result = []
        for r in rows:
            ret = retention(r[3], r[4], r[5], now)
            result.append(RecalledFact(id=r[0], kind=r[1], content=r[2], retention=ret, score=ret))
        result.sort(key=lambda x: x.score, reverse=True)
        return result[:limit]

    def all_facts(self, person_id: int, now: int | None = None) -> list[StoredFact]:
        now = now if now is not None else current_time()
        rows = self._db.execute(
            '''SELECT id, kind, content, strength, updated_at, half_life_hours, due_at, active
               FROM facts WHERE person_id = ?''',
            (person_id,)
        ).fetchall()
        result = []
        for r in rows:
            ret = retention(r[3], r[4], r[5], now)
            result.append(StoredFact(
                id=r[0], kind=r[1], content=r[2], retention=ret, score=ret,
                due_at=r[6], frozen=(r[7] == 0 or ret <= FREEZE)
            ))
        result.sort(key=lambda x: x.retention, reverse=True)
        return result

    def fact_count(self, person_id: int) -> dict[str, int]:
        row = self._db.execute(
            'SELECT COUNT(*), SUM(active) FROM facts WHERE person_id = ?', (person_id,)
        ).fetchone()
        return {'total': row[0] or 0, 'active': row[1] or 0}

    # ------------------------------------------------------------------ 待说的话
    def queue_utterance(self, source: str, text: str, deliver_after: int,
                        expires_at: int, emotion: str | None = None,
                        now: int | None = None) -> int:
        """当前无生产调用；恢复接线后仅服务 desktop 主动搭话，QQ 启用时再分区。"""
        now = now if now is not None else current_time()
        text = text.strip()
        if not text:
            return 0
        cur = self._db.execute(
            '''INSERT INTO pending_utterances (source, emotion, text, deliver_after, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (source, emotion, text, deliver_after, expires_at, now)
        )
        self._db.commit()
        return cur.lastrowid or 0

    def due_utterances(self, now: int | None = None, limit: int = 4) -> list[dict[str, Any]]:
        """当前无生产调用；恢复接线后仅服务 desktop 主动搭话，QQ 启用时再分区。"""
        now = now if now is not None else current_time()
        rows = self._db.execute(
            '''SELECT id, source, emotion, text FROM pending_utterances
               WHERE delivered_at IS NULL AND deliver_after <= ? AND expires_at > ?
               ORDER BY id ASC LIMIT ?''',
            (now, now, limit)
        ).fetchall()
        return [{'id': r[0], 'source': r[1], 'emotion': r[2], 'text': r[3]} for r in rows]

    def mark_delivered(self, ids: list[int], now: int | None = None) -> None:
        """当前无生产调用；恢复接线后仅服务 desktop 主动搭话，QQ 启用时再分区。"""
        if not ids:
            return
        now = now if now is not None else current_time()
        placeholders = ','.join('?' * len(ids))
        self._db.execute(
            f'UPDATE pending_utterances SET delivered_at = ? WHERE id IN ({placeholders})',
            (now, *ids)
        )
        self._db.commit()

    def has_queued_since(self, source: str, since: int) -> bool:
        """当前无生产调用；恢复接线后仅服务 desktop 主动搭话，QQ 启用时再分区。"""
        row = self._db.execute(
            'SELECT 1 FROM pending_utterances WHERE source = ? AND created_at >= ? LIMIT 1',
            (source, since)
        ).fetchone()
        return row is not None

    def pending_utterance_count(self) -> int:
        """当前无生产调用；恢复接线后仅服务 desktop 主动搭话，QQ 启用时再分区。"""
        row = self._db.execute(
            'SELECT COUNT(*) FROM pending_utterances WHERE delivered_at IS NULL'
        ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------ meta JSON 键值
    def load_pending_promises(self) -> list[dict[str, Any]]:
        """读取 desktop 专属的跨重启约定；短期情境意图不在这里存储。"""
        raw = self.read_json(_PENDING_PROMISES_KEY, [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def save_pending_promises(self, promises: list[dict[str, Any]]) -> None:
        """整批覆盖 desktop 专属约定快照，队列的其余意图在进程内自然过期。"""
        self.write_json(_PENDING_PROMISES_KEY, promises)

    def read_json(self, key: str, fallback: Any) -> Any:
        row = self._db.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        if not row:
            return fallback
        try:
            return json.loads(row[0])
        except Exception:
            return fallback

    def write_json(self, key: str, value: Any) -> None:
        self._db.execute(
            'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
            (key, json.dumps(value, ensure_ascii=False))
        )
        self._db.commit()

    def user_spoke_after(self, stream_id: int, at: int) -> bool:
        row = self._db.execute(
            """SELECT 1 FROM messages
               WHERE stream_id = ? AND role = 'user' AND created_at > ? LIMIT 1""",
            (stream_id, at),
        ).fetchone()
        return row is not None
