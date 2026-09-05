"""反馈纠错：事实进过提示词之后被用户纠正时，把纠正写回库里。

链路分四段，全部默认关闭（``memory_feedback.enabled = false``）：

1. 锚点：一条事实真的进了提示词时，``register_prompt_entries`` 把它登记为
   待观察项（事实、stream、时刻）。锚点不是「被检索到」——检索到但没注入的
   事实用户根本没机会反驳。
2. 预筛：观察窗口内的用户消息先过关键词（不对 / 错了 / 记错 / 不是……），
   未过筛的消息不触发模型调用，否则每条闲聊都要烧一次判定。
3. 判定：模型给出「是否被否定」与置信度；过 ``auto_apply_threshold`` 才动手。
4. 应用：有更正正文时走事实账本的 ``superseded_by``（写新行、回填旧行）；
   纯否定没有新说法时不造事实，只按配置写「已被纠正」标记，由注入侧硬过滤。

画像与情节的后续处理复用既有机制：画像置脏走 ``person_profile.dirty``，
情节重建走 ``episodes.needs_rebuild``，都不新增队列表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import asyncio
import json
import sqlite3

from src.core.agent import profile
from src.core.agent.summarize import summarize
from src.core.runtime.clock import now as current_time
from src.core.logging.logger import get_logger
from src.core.config.schema import MemoryFeedbackConfig
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params
from src.core.memory.store import UNSUMMARIZED_KIND, FactInput, MemoryStore
from src.core.memory.tokenize import bigrams, index_tokens
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 纠正信号关键词：命中才进入模型判定。宁宽勿窄——误中只多一次判定调用，
# 漏掉则纠正永远到不了判定环节。
CORRECTION_SIGNALS = (
    '不对', '错了', '记错', '弄错', '搞错', '不是', '应该是', '改成', '说反了',
)

# 模型或输出格式连续失败这么多次后放弃该待观察项，防止确定性失败把队列钉死。
_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class FeedbackJudgment:
    """模型对一条待观察事实的判定结果。

    :ivar negated: 用户消息是否明确否定了这条事实。
    :ivar confidence: 判定置信度，0～1。
    :ivar corrected_content: 更正后的事实正文；纯否定（没给新说法）时为空串。
    """

    negated: bool
    confidence: float
    corrected_content: str


def has_correction_signal(text: str) -> bool:
    """关键词预筛：消息里是否带有纠正信号。

    :param text: 用户消息正文。
    :return: 命中任一纠正信号词时返回 ``True``。
    副作用：纯函数。
    """

    return any(signal in text for signal in CORRECTION_SIGNALS)


def parse_judgment(raw: str) -> Optional[FeedbackJudgment]:
    """解析模型判定输出；不是合法 JSON 或缺字段时返回 ``None``。

    :param raw: 模型原始输出，允许带代码围栏。
    :return: 解析出的判定；格式不合法时为 ``None``。
    副作用：纯函数。
    """

    text = raw.strip()
    if text.startswith('```'):
        # 容忍代码围栏：去掉首行 ```json 与结尾 ```。
        lines = text.splitlines()
        lines = lines[1:] if lines and lines[0].startswith('```') else lines
        lines = lines[:-1] if lines and lines[-1].strip() == '```' else lines
        text = '\n'.join(lines).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or 'negated' not in data:
        return None
    try:
        confidence = float(data.get('confidence', 0.0))
    except (TypeError, ValueError):
        return None
    corrected = data.get('corrected_content') or ''
    return FeedbackJudgment(
        negated=bool(data['negated']),
        confidence=max(0.0, min(1.0, confidence)),
        corrected_content=str(corrected).strip(),
    )


def register_prompt_entries(
    db: sqlite3.Connection,
    entries: Sequence[Tuple[int, int]],
    stream_id: int,
    now: Optional[int] = None,
) -> int:
    """把「本轮真的进了提示词」的事实登记为待观察项，返回新登记条数。

    同一 ``(fact_id, stream_id)`` 只保留一行：仍处于待观察状态时保留最早的
    进入时刻（观察窗口从第一次注入起算，随后消息都在窗口内）；已有结论
    （done / expired / failed）的重新进入则开启新一轮观察。

    :param db: 当前库连接。
    :param entries: ``(fact_id, person_id)`` 列表。
    :param stream_id: 事实进入提示词所在的 stream ID。
    :param now: 可选当前毫秒时钟。
    :return: 本次新插入的行数（刷新既有行不计入）。
    :raises sqlite3.Error: 写入失败时传播。
    副作用：写入 memory_feedback_pending 并提交事务。
    """

    if not entries:
        return 0
    now = now if now is not None else current_time()
    inserted = 0
    for fact_id, person_id in entries:
        cur = db.execute(
            '''INSERT INTO memory_feedback_pending
                 (fact_id, person_id, stream_id, entered_at, status, attempts, updated_at)
               VALUES (?, ?, ?, ?, 'pending', 0, ?)
               ON CONFLICT(fact_id, stream_id) DO UPDATE SET
                 entered_at = CASE
                   WHEN memory_feedback_pending.status = 'pending'
                   THEN memory_feedback_pending.entered_at
                   ELSE excluded.entered_at
                 END,
                 status = 'pending',
                 attempts = 0,
                 updated_at = excluded.updated_at''',
            (int(fact_id), int(person_id), int(stream_id), now, now),
        )
        inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    db.commit()
    return inserted


def _bigrams(text: str) -> frozenset[str]:
    """取正文的 CJK bigram 集合，用于情节与事实的内容重叠判断。"""

    return frozenset(bigrams(text))


def _episode_affected(fact_content: str, summary: str) -> bool:
    """判断情节摘要是否可能引用了被纠正的事实。

    事实正文的 bigram 至少有两成、且至少两个落进摘要里才算受影响：
    单个重叠词（「深圳」）太常见，会把不相关的情节也排进重建。
    """

    fact_terms = _bigrams(fact_content)
    if len(fact_terms) < 2:
        return False
    overlap = len(fact_terms & _bigrams(summary))
    return overlap >= 2 and overlap / len(fact_terms) >= 0.2


class MemoryFeedbackService:
    """反馈纠错后台服务：纠错轮询 + 一致性协调两个循环。

    构造只存依赖；``startup`` 创建两个任务后立即返回，``shutdown`` 置位停止
    信号并等待退出。两个循环各自独立节拍（``check_interval_minutes`` 与
    ``reconcile_interval_minutes``），单轮失败只记日志，不终止轮询。
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        cfg: MemoryFeedbackConfig,
        *,
        judge_provider: Optional[LlmProvider] = None,
        summary_provider: Optional[LlmProvider] = None,
        bot_name: str = '',
        bot_personality: str = '',
        judge_temperature: float = 0.1,
        judge_max_tokens: Optional[int] = 256,
        summary_temperature: float = 0.3,
        summary_max_tokens: Optional[int] = None,
    ) -> None:
        """初始化服务依赖；不做任何 IO。

        :param db: 当前库连接。
        :param cfg: 反馈纠错配置段。
        :param judge_provider: 判定用的模型路由；为 ``None`` 时轮询空转。
        :param summary_provider: 情节重建摘要用的模型路由；为 ``None`` 时重建空转。
        :param bot_name: Bot 名称，用于重摘要提示词。
        :param bot_personality: Bot 人格文本，用于重摘要提示词。
        :param judge_temperature: 判定采样温度。
        :param judge_max_tokens: 判定输出上限。
        :param summary_temperature: 重摘要采样温度。
        :param summary_max_tokens: 重摘要输出上限。
        副作用：只初始化内存状态。
        """

        self._db = db
        self._store = MemoryStore(db)
        self._cfg = cfg
        self._judge_provider = judge_provider
        self._summary_provider = summary_provider
        self._bot_name = bot_name
        self._bot_personality = bot_personality
        self._judge_temperature = judge_temperature
        self._judge_max_tokens = judge_max_tokens
        self._summary_temperature = summary_temperature
        self._summary_max_tokens = summary_max_tokens
        self._stop = asyncio.Event()
        self._tasks: List[asyncio.Task[None]] = []

    async def startup(self) -> None:
        """创建两个轮询任务后立即返回；循环本身不阻塞启动序列。

        :return: 无返回值。
        副作用：启动后台任务。
        """

        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._check_loop(), name='memory-feedback-check'),
            asyncio.create_task(self._reconcile_loop(), name='memory-feedback-reconcile'),
        ]

    async def shutdown(self) -> None:
        """停止两个轮询任务并等待退出。

        :return: 无返回值。
        副作用：停止后台任务。
        """

        self._stop.set()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    async def _check_loop(self) -> None:
        """按 ``check_interval_minutes`` 节拍轮询待观察项。"""

        interval = max(1, self._cfg.check_interval_minutes) * 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self.check_once()
            except Exception as exc:
                logger.error('memory_feedback_check_failed', error=str(exc))

    async def _reconcile_loop(self) -> None:
        """按 ``reconcile_interval_minutes`` 节拍过期锚点、重建情节。"""

        interval = max(1, self._cfg.reconcile_interval_minutes) * 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self.reconcile_once()
            except Exception as exc:
                logger.error('memory_feedback_reconcile_failed', error=str(exc))

    def _window_ms(self) -> int:
        """观察窗口的毫秒数。"""

        return int(self._cfg.window_hours * 3600_000)

    async def check_once(self, now: Optional[int] = None) -> Dict[str, int]:
        """跑一轮纠错轮询：预筛、判定、应用。

        :param now: 可选当前毫秒时钟。
        :return: 本轮统计：``checked`` 处理的锚点数、``prefiltered`` 过了预筛的
            消息批数、``judged`` 实际调用模型的次数、``applied`` 应用纠正的次数。
        :raises sqlite3.Error: 读写失败时传播。
        副作用：读取窗口内用户消息，可能调用模型并写入 facts / 结果表。
        """

        now = now if now is not None else current_time()
        stats = {'checked': 0, 'prefiltered': 0, 'judged': 0, 'applied': 0}
        if self._judge_provider is None:
            return stats
        items = self._db.execute(
            '''SELECT p.id, p.fact_id, p.person_id, p.stream_id, p.entered_at,
                      f.content, f.kind, f.slot
               FROM memory_feedback_pending p
               JOIN facts f ON f.id = p.fact_id
               WHERE p.status = 'pending' AND p.entered_at >= ?
               ORDER BY p.entered_at
               LIMIT ?''',
            (now - self._window_ms(), self._cfg.batch_size),
        ).fetchall()
        for row in items:
            pending_id, fact_id, person_id, stream_id, entered_at = (
                int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]),
            )
            fact_content, fact_kind, fact_slot = str(row[5]), str(row[6]), str(row[7])
            stats['checked'] += 1
            messages = self._db.execute(
                '''SELECT content FROM messages
                   WHERE stream_id = ? AND role = 'user' AND created_at >= ?
                   ORDER BY id LIMIT ?''',
                (stream_id, entered_at, self._cfg.max_feedback_messages),
            ).fetchall()
            texts = [str(message[0]) for message in messages]
            if self._cfg.prefilter_enabled:
                texts = [text for text in texts if has_correction_signal(text)]
            if not texts:
                # 窗口内没有带纠正信号的消息：本轮不打扰模型，锚点留待窗口过期。
                continue
            stats['prefiltered'] += 1
            stats['judged'] += 1
            judgment = await self._judge(fact_content, texts)
            if judgment is None:
                attempts = self._db.execute(
                    'SELECT attempts FROM memory_feedback_pending WHERE id = ?',
                    (pending_id,),
                ).fetchone()[0] + 1
                self._db.execute(
                    '''UPDATE memory_feedback_pending
                       SET attempts = ?, status = ?, updated_at = ? WHERE id = ?''',
                    (attempts, 'failed' if attempts >= _MAX_ATTEMPTS else 'pending', now, pending_id),
                )
                self._db.commit()
                continue
            if judgment.negated and judgment.confidence >= self._cfg.auto_apply_threshold:
                self._apply(
                    fact_id, person_id, stream_id, fact_content, fact_kind, fact_slot,
                    judgment, now,
                )
                stats['applied'] += 1
            self._db.execute(
                "UPDATE memory_feedback_pending SET status = 'done', updated_at = ? WHERE id = ?",
                (now, pending_id),
            )
            self._db.commit()
        return stats

    async def _judge(self, fact_content: str, messages: Sequence[str]) -> Optional[FeedbackJudgment]:
        """调用模型判定一条事实是否被否定；故障返回 ``None`` 不留痕迹。

        :param fact_content: 被校对的事实正文。
        :param messages: 过了预筛的用户消息。
        :return: 判定结果；模型故障或输出不合法时为 ``None``。
        副作用：发起一次流式模型请求并记录 ``llm_request`` 观测事件。
        """

        rendered = get_prompt('memory.feedback').render(
            fact=fact_content,
            messages='\n'.join(f'- {text}' for text in messages),
        )
        request_messages = [{'role': 'system', 'content': rendered}]
        render_params: Dict[str, Dict[str, str]] = {}
        raw = ''
        try:
            trace.emit(
                'llm_request',
                messages=request_messages,
                temperature=self._judge_temperature,
                maxTokens=self._judge_max_tokens,
                renderParams=render_params,
                **prompt_metadata('memory.feedback', ('memory.feedback',)),
            )
            bind_render_params(render_params)
            async for chunk in self._judge_provider.stream(
                messages=request_messages,
                temperature=self._judge_temperature,
                max_tokens=self._judge_max_tokens,
            ):
                if chunk.get('text'):
                    raw += chunk['text']
        except Exception:
            # 判定是旁路设施：模型故障不阻塞任何回合，留待下一轮重试。
            return None
        return parse_judgment(raw)

    def _apply(
        self,
        fact_id: int,
        person_id: int,
        stream_id: int,
        fact_content: str,
        fact_kind: str,
        fact_slot: str,
        judgment: FeedbackJudgment,
        now: int,
    ) -> None:
        """应用一次判定成立的纠正。

        有更正正文时按事实账本写新行并取代旧行；纯否定只留「已被纠正」标记。
        之后按配置置脏画像、把受影响情节排进重建。
        """

        new_fact_id = 0
        if judgment.corrected_content:
            written = self._store.add_fact(
                person_id,
                FactInput(
                    content=judgment.corrected_content,
                    kind=fact_kind,
                    slot=fact_slot,
                    supersedes=fact_id,
                    actor='n4',
                ),
                now,
            )
            new_fact_id = written.fact_id
        self._db.execute(
            '''INSERT INTO memory_feedback_results
                 (fact_id, person_id, stream_id, confidence,
                  corrected_content, new_fact_id, marked, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                fact_id, person_id, stream_id, judgment.confidence,
                judgment.corrected_content, new_fact_id,
                1 if self._cfg.mark_enabled else 0, now,
            ),
        )
        self._db.commit()
        if self._cfg.profile_refresh_enabled:
            profile.mark_dirty(self._db, [person_id], now)
        if self._cfg.episode_rebuild_enabled:
            self._queue_episode_rebuild(stream_id, fact_content)
        trace.emit(
            'memory_fact_corrected',
            factId=fact_id,
            newFactId=new_fact_id,
            streamId=stream_id,
            confidence=judgment.confidence,
        )
        logger.info(
            'memory_fact_corrected',
            fact_id=fact_id,
            new_fact_id=new_fact_id,
            confidence=judgment.confidence,
        )

    def _queue_episode_rebuild(self, stream_id: int, fact_content: str) -> None:
        """把可能引用了被纠正事实的情节置为待重建。

        占位情节的正文是诊断信息，不参与召回，也不进重建队列。
        """

        rows = self._db.execute(
            'SELECT id, summary FROM episodes '
            'WHERE stream_id = ? AND needs_rebuild = 0 AND kind != ?',
            (stream_id, UNSUMMARIZED_KIND),
        ).fetchall()
        for episode_id, summary in rows:
            if _episode_affected(fact_content, str(summary)):
                self._db.execute(
                    'UPDATE episodes SET needs_rebuild = 1 WHERE id = ?',
                    (int(episode_id),),
                )
        self._db.commit()

    async def reconcile_once(self, now: Optional[int] = None) -> Dict[str, int]:
        """跑一轮一致性协调：过期窗口外锚点，重建排队情节。

        :param now: 可选当前毫秒时钟。
        :return: 本轮统计：``expired`` 过期的锚点数、``rebuilt`` 重建成功的
            情节数。
        副作用：更新待观察项状态；可能调用摘要模型并改写情节。
        """

        now = now if now is not None else current_time()
        cur = self._db.execute(
            '''UPDATE memory_feedback_pending
               SET status = 'expired', updated_at = ?
               WHERE status = 'pending' AND entered_at < ?''',
            (now, now - self._window_ms()),
        )
        self._db.commit()
        stats = {'expired': cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0, 'rebuilt': 0}
        if self._summary_provider is None or not self._cfg.episode_rebuild_enabled:
            return stats
        rows = self._db.execute(
            'SELECT id FROM episodes WHERE needs_rebuild = 1 ORDER BY id LIMIT ?',
            (self._cfg.reconcile_batch_size,),
        ).fetchall()
        for (episode_id,) in rows:
            if await self._rebuild_episode(int(episode_id)):
                stats['rebuilt'] += 1
        return stats

    async def _rebuild_episode(self, episode_id: int) -> bool:
        """用原始消息重摘要一条情节；失败保留标记等下一轮。

        消息太短不足以成摘要时认为没有更好的版本可写：保留旧摘要、清掉标记，
        否则这批会永久卡在重建队列里。
        """

        messages = self._db.execute(
            "SELECT role, content FROM messages WHERE episode_id = ? ORDER BY id",
            (episode_id,),
        ).fetchall()
        msgs = [{'role': str(m[0]), 'content': str(m[1])} for m in messages]
        try:
            episode = await summarize(
                self._summary_provider,
                msgs,
                temperature=self._summary_temperature,
                max_tokens=self._summary_max_tokens,
                character_name=self._bot_name,
                character_personality=self._bot_personality,
            )
        except Exception as exc:
            logger.error('memory_feedback_episode_rebuild_failed',
                         episode_id=episode_id, error=str(exc))
            return False
        if episode is None:
            self._db.execute(
                'UPDATE episodes SET needs_rebuild = 0 WHERE id = ?', (episode_id,),
            )
            self._db.commit()
            return False
        self._db.execute(
            'UPDATE episodes SET summary = ?, needs_rebuild = 0 WHERE id = ?',
            (episode.summary, episode_id),
        )
        # 线索与摘要保持同一份正文：cues_fts 是无内容表，删除要带回原分词。
        old_cues = self._db.execute(
            'SELECT id, cue FROM episode_cues WHERE episode_id = ?', (episode_id,),
        ).fetchall()
        for cue_id, cue_text in old_cues:
            self._db.execute(
                "INSERT INTO cues_fts(cues_fts, rowid, tokens) VALUES('delete', ?, ?)",
                (int(cue_id), index_tokens(str(cue_text))),
            )
        self._db.execute('DELETE FROM episode_cues WHERE episode_id = ?', (episode_id,))
        for cue in episode.recall_cues:
            text = cue.strip()
            if not text:
                continue
            cue_cur = self._db.execute(
                'INSERT INTO episode_cues (episode_id, cue) VALUES (?, ?)',
                (episode_id, text),
            )
            self._db.execute(
                'INSERT INTO cues_fts (rowid, tokens) VALUES (?, ?)',
                (cue_cur.lastrowid or 0, index_tokens(text)),
            )
        self._db.commit()
        return True


def marked_fact_ids(db: sqlite3.Connection, fact_ids: Sequence[int]) -> set[int]:
    """返回给定事实里带「已被纠正」标记的 ID 集合，供注入侧硬过滤。

    :param db: 当前库连接。
    :param fact_ids: 待检查的事实 ID。
    :return: 其中有生效标记的事实 ID 集合。
    :raises sqlite3.Error: 查询失败。
    副作用：只读 memory_feedback_results。
    """

    if not fact_ids:
        return set()
    marks = ', '.join('?' for _ in fact_ids)
    rows = db.execute(
        f'SELECT DISTINCT fact_id FROM memory_feedback_results '
        f'WHERE marked = 1 AND fact_id IN ({marks})',
        [int(fact_id) for fact_id in fact_ids],
    ).fetchall()
    return {int(row[0]) for row in rows}
