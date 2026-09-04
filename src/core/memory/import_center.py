"""导入中心：把外部资料成批写入知识层，并按来源批次整批撤销。

第三方 clone 下来的库是空的——知识、情节、事实全部为零，此前唯一的填充
通道是只对作者历史库有效的一次性迁移。本模块给任何部署者一个最小导入
通道：粘贴文本或上传文件，切成条目后逐条走 :func:`add_knowledge`
（去重与全文索引复用既有判据），向量由向量服务按既有链路补齐。

来源批次是撤销的最小单位：每次导入登记一行 ``import_batches``，新建的知识
行携带批次外键；「这批资料过时了」按批次删除即可整批撤掉，NULL 批次的
存量行（一次性迁移与运行期抽取写入的）永不命中。

进度是进程内状态：一次导一批、同步处理，不做任务队列与断点续传。
并发导入直接拒绝——单用户场景下并发导入本来就不该发生，排队只会把
「正在导第二批」这件事藏起来。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.core.common.logger import get_logger
from src.core.observe import events as trace

from .knowledge import add_knowledge

logger = get_logger(__name__)

# 来源标识：与运行期抽取的 fact_extract 区分开，便于核对哪些是导入进来的。
IMPORT_SOURCE = 'import_center'

# 三个上限是常量而非配置项：部署者调它们的需求远低于维护三个配置字段的代价。
# 单次粘贴的字符上限；超过直接拒绝，提示拆分后分批导入。
MAX_PASTE_CHARS = 200_000
# 上传文件的大小上限（字节），与粘贴同量级。
MAX_FILE_BYTES = 2_000_000
# 单批最多产出的条目数；分块后超过即拒绝，防止一次误操作灌爆知识层。
MAX_BATCH_ITEMS = 5_000
# 单条目的目标长度区间（字符）。分块按段落优先，超出上限的段再按句切。
CHUNK_TARGET_CHARS = 600
CHUNK_MAX_CHARS = 1_200

# 批次状态机：running -> done / failed。没有重试态——失败批次留下的条目
# 仍指向该批次，可以按批次撤掉后重导，不需要半途恢复。
STATUS_RUNNING = 'running'
STATUS_DONE = 'done'
STATUS_FAILED = 'failed'

# 进程内的并发闸：True 表示有一个导入正在进行。
_importing = False

EmbedKnowledgeFn = Callable[[int, str], Awaitable[None]]


class ImportBusyError(RuntimeError):
    """已有一个导入正在进行时抛出；调用方应提示稍后再试，不排队。"""


def split_into_chunks(text: str) -> List[str]:
    """把一段长文本切成知识条目，段落优先、超长段按句二切。

    项目里此前没有「长文本 → 多条 knowledge」的现成实现（运行期抽取的
    条目由模型直接产出），因此导入侧补了这个最小分块：空行分段，段落在
    目标区间内整段成条；超过上限的段按句号类标点再切，仍超限的硬切。
    不做重叠窗口与语义切分——去重靠 content_key，导入内容也不该被
    期待成连贯语料，按段切已经保住「一条知识一件事」的粒度。

    :param text: 原始文本。
    :return: 条目列表；全部条目已去空白，空段丢弃。
    副作用：无。
    """

    chunks: List[str] = []
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= CHUNK_MAX_CHARS:
            chunks.append(paragraph)
            continue
        current = ''
        for sentence in re.split(r'(?<=[。！？!?\.])\s*', paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(current) + len(sentence) + 1 > CHUNK_MAX_CHARS and current:
                chunks.append(current)
                current = sentence
            else:
                current = f'{current} {sentence}'.strip() if current else sentence
        if current:
            chunks.append(current)
    # 硬切兜底：无标点的超长段（例如连续粘贴的密文或表格）。
    finalized: List[str] = []
    for chunk in chunks:
        while len(chunk) > CHUNK_MAX_CHARS:
            finalized.append(chunk[:CHUNK_MAX_CHARS])
            chunk = chunk[CHUNK_MAX_CHARS:]
        if chunk:
            finalized.append(chunk)
    return finalized


def _summary_of(text: str) -> str:
    """取粘贴文本的首行摘要，给批次列表一个可辨认的锚点。"""

    first = text.strip().splitlines()[0] if text.strip() else ''
    return first[:80]


def _acquire_import_lock() -> None:
    """取得导入闸；已有导入在进行时抛 :class:`ImportBusyError`。"""

    global _importing
    if _importing:
        raise ImportBusyError('已有导入进行中，请等它完成后再试')
    _importing = True


def _release_import_lock() -> None:
    global _importing
    _importing = False


def import_in_progress() -> bool:
    """读取导入闸状态，供状态端点展示。"""

    return _importing


async def run_import(
    db: sqlite3.Connection,
    text: str,
    origin_name: str,
    now: int,
    *,
    kind: str = 'paste',
    embed_knowledge: Optional[EmbedKnowledgeFn] = None,
) -> Dict[str, Any]:
    """执行一次完整导入：建批次、分块、逐条写入、补向量、收尾计数。

    :param db: 当前库连接。
    :param text: 原始文本（粘贴正文或文件解码后的内容）。
    :param origin_name: 原始文件名或「粘贴」等来源名。
    :param now: 当前毫秒时间戳。
    :param kind: 入口类型，``paste`` 或 ``upload``。
    :param embed_knowledge: 向量服务回调；省略时跳过向量化（检索仍有 BM25）。
    :return: ``{"batch_id", "submitted", "added", "duplicated", "embedded"}``。
    :raises ImportBusyError: 已有导入在进行。
    :raises ValueError: 超过任一上限。
    :raises sqlite3.Error: 写入失败；批次置 failed 后异常继续传播。
    副作用：写 ``import_batches``、``knowledge`` 与 ``knowledge_fts``；
        可选写入知识向量；发出 import_started / import_done（或 import_failed）事件。
    """

    if kind == 'paste' and len(text) > MAX_PASTE_CHARS:
        raise ValueError(f'粘贴内容 {len(text)} 字符超过上限 {MAX_PASTE_CHARS}，请拆分后分批导入')
    chunks = split_into_chunks(text)
    if len(chunks) > MAX_BATCH_ITEMS:
        raise ValueError(f'分块后 {len(chunks)} 条超过单批上限 {MAX_BATCH_ITEMS}，请拆分后分批导入')

    _acquire_import_lock()
    cursor = db.execute(
        '''INSERT INTO import_batches (kind, origin_name, summary, submitted, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (kind, origin_name, _summary_of(text), len(chunks), STATUS_RUNNING, now),
    )
    db.commit()
    batch_id = int(cursor.lastrowid or 0)
    trace.emit('import_started', batchId=batch_id, kind=kind, submitted=len(chunks))
    logger.info('import_started', batch=batch_id, kind=kind, items=len(chunks))

    embedded = 0
    try:
        for chunk in chunks:
            kid = add_knowledge(db, chunk, IMPORT_SOURCE, now, batch_id=batch_id)
            if kid and embed_knowledge is not None:
                await embed_knowledge(kid, chunk)
                embedded += 1
        added = batch_count(db, batch_id)
        db.execute(
            'UPDATE import_batches SET status = ?, added = ?, finished_at = ? WHERE id = ?',
            (STATUS_DONE, added, now, batch_id),
        )
        db.commit()
        result = {
            'batch_id': batch_id,
            'submitted': len(chunks),
            'added': added,
            'duplicated': len(chunks) - added,
            'embedded': embedded,
        }
        trace.emit(
            'import_done',
            batchId=batch_id,
            submitted=len(chunks),
            added=added,
            embedded=embedded,
        )
        logger.info('import_done', batch=batch_id, added=added, duplicated=result['duplicated'])
        return result
    except Exception as exc:
        db.execute(
            'UPDATE import_batches SET status = ?, error = ?, finished_at = ? WHERE id = ?',
            (STATUS_FAILED, str(exc)[:500], now, batch_id),
        )
        db.commit()
        trace.emit('import_failed', batchId=batch_id, error=str(exc)[:200])
        logger.error('import_failed', batch=batch_id, error=str(exc)[:200])
        raise
    finally:
        _release_import_lock()


def batch_count(db: sqlite3.Connection, batch_id: int) -> int:
    """统计一个批次当前实际拥有的知识条数；这是批次计数的唯一真相源。

    :param db: 当前库连接。
    :param batch_id: 批次 ID。
    :return: ``import_batch_id`` 指向该批次的行数。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    row = db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE import_batch_id = ?', (batch_id,)
    ).fetchone()
    return int(row[0])


def list_batches(db: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """按时间倒序列出批次，附每批的实际条数。

    :param db: 当前库连接。
    :param limit: 最多返回的批次数。
    :return: 批次字典列表；``live_count`` 按库实时数出，与导入时的
        ``added`` 不一致即说明批次被部分或整批删除过。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    rows = db.execute(
        '''SELECT id, kind, origin_name, summary, submitted, added, status,
                  created_at, finished_at, error
           FROM import_batches ORDER BY created_at DESC, id DESC LIMIT ?''',
        (limit,),
    ).fetchall()
    return [
        {
            'id': int(row[0]),
            'kind': str(row[1]),
            'origin_name': str(row[2]),
            'summary': str(row[3]),
            'submitted': int(row[4]),
            'added': int(row[5]),
            'status': str(row[6]),
            'created_at': int(row[7]),
            'finished_at': row[8],
            'error': str(row[9]),
            'live_count': batch_count(db, int(row[0])),
        }
        for row in rows
    ]


def batch_detail(db: sqlite3.Connection, batch_id: int, sample_limit: int = 20) -> Dict[str, Any]:
    """读取一个批次的详情与条目样本。

    :param db: 当前库连接。
    :param batch_id: 批次 ID。
    :param sample_limit: 样本条数上限。
    :return: 批次字段 + ``live_count`` + ``items`` 样本（ID 与正文前 120 字）。
    :raises ValueError: 批次不存在。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    row = db.execute(
        '''SELECT id, kind, origin_name, summary, submitted, added, status,
                  created_at, finished_at, error
           FROM import_batches WHERE id = ?''',
        (batch_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f'批次 {batch_id} 不存在')
    detail = {
        'id': int(row[0]),
        'kind': str(row[1]),
        'origin_name': str(row[2]),
        'summary': str(row[3]),
        'submitted': int(row[4]),
        'added': int(row[5]),
        'status': str(row[6]),
        'created_at': int(row[7]),
        'finished_at': row[8],
        'error': str(row[9]),
        'live_count': batch_count(db, batch_id),
    }
    samples = db.execute(
        '''SELECT id, substr(content, 1, 120) FROM knowledge
           WHERE import_batch_id = ? ORDER BY id LIMIT ?''',
        (batch_id, sample_limit),
    ).fetchall()
    detail['items'] = [{'id': int(r[0]), 'content': str(r[1])} for r in samples]
    return detail


def delete_preview(db: sqlite3.Connection, batch_id: int) -> Dict[str, Any]:
    """预览按批次删除的影响面：会删掉的条数与样本。

    :param db: 当前库连接。
    :param batch_id: 批次 ID。
    :return: ``{"batch_id", "to_delete", "items"}``；批次不存在时 ``to_delete`` 为 0。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    exists = db.execute(
        'SELECT 1 FROM import_batches WHERE id = ?', (batch_id,)
    ).fetchone()
    if exists is None:
        return {'batch_id': batch_id, 'to_delete': 0, 'items': []}
    samples = db.execute(
        'SELECT id, substr(content, 1, 120) FROM knowledge'
        ' WHERE import_batch_id = ? ORDER BY id LIMIT 5',
        (batch_id,),
    ).fetchall()
    return {
        'batch_id': batch_id,
        'to_delete': batch_count(db, batch_id),
        'items': [{'id': int(r[0]), 'content': str(r[1])} for r in samples],
    }


def delete_batch(db: sqlite3.Connection, batch_id: int) -> Dict[str, Any]:
    """按批次删除其全部知识条目，返回实际删除数。

    只删 ``import_batch_id`` 指向该批次的行：NULL 批次的存量与其他批次
    一条不碰。批次行本身保留——它是来源管理的记录，删除后 ``live_count``
    归零即可看出这批已被撤掉。knowledge_fts 是外部内容表，FTS 行不会
    随主表行自动消失，必须显式按同一选择集删一遍，否则检索会命中
    已删除知识的残留行。

    :param db: 当前库连接。
    :param batch_id: 批次 ID。
    :return: ``{"batch_id", "deleted"}``；删除数为删除前后的行数差。
    :raises sqlite3.Error: 删除失败。
    副作用：删除 knowledge 与 knowledge_fts 的对应行并提交；发一条
        import_batch_deleted 事件。
    """

    before = batch_count(db, batch_id)
    if before:
        # content='' 的 FTS5 外部内容表不能直接 DELETE，必须用 'delete' 命令
        # 逐行喂回原文 token（与 memory_feedback 撤销情节索引同一惯例）；
        # tokens_v2 在写入时就已同步保存，这里原样喂回即可。
        rows = db.execute(
            'SELECT id, tokens_v2 FROM knowledge WHERE import_batch_id = ?',
            (batch_id,),
        ).fetchall()
        for kid, tokens in rows:
            db.execute(
                'INSERT INTO knowledge_fts(knowledge_fts, rowid, tokens)'
                " VALUES('delete', ?, ?)",
                (kid, tokens),
            )
        db.execute('DELETE FROM knowledge WHERE import_batch_id = ?', (batch_id,))
        db.commit()
    trace.emit('import_batch_deleted', batchId=batch_id, deleted=before)
    logger.info('import_batch_deleted', batch=batch_id, deleted=before)
    return {'batch_id': batch_id, 'deleted': before}
