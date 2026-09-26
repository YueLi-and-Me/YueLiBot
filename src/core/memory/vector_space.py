"""登记消费方的向量空间，并在配置变化时原子清空旧空间的向量。

由 main 装配已启用的客户端后调用，早于任何后台补算任务。首次登记保留旧向量，
仅能假定旧数据来自当前配置；变更后的 NULL 行由对应服务负责续算。
"""

from dataclasses import dataclass
import sqlite3

from src.core.config.schema import ModelCandidate
from src.core.logging.logger import get_logger
from src.core.runtime.clock import now

logger = get_logger(__name__)

# SQL 只从封闭消费方映射选择，不拼接来自配置或数据库的表名。
_CLEAR_SQL = {
    'facts': 'UPDATE facts SET embedding=NULL, embedding_q8=NULL WHERE embedding IS NOT NULL OR embedding_q8 IS NOT NULL',
    'knowledge': 'UPDATE knowledge SET embedding=NULL, embedding_q8=NULL WHERE embedding IS NOT NULL OR embedding_q8 IS NOT NULL',
    'emoji': 'UPDATE emoji SET emotion_vec=NULL WHERE emotion_vec IS NOT NULL',
}


@dataclass(frozen=True)
class VectorSpace:
    """一个可比较空间的模型 ID、协议、维度与输入配方；不含厂商连接。"""

    model: str
    api_format: str
    dim: int
    recipe: str

    @classmethod
    def from_candidate(cls, candidate: ModelCandidate, recipe: str) -> 'VectorSpace':
        """从已通过加载期一致性校验的候选和实际输入配方构造空间。"""
        return cls(candidate.identifier, candidate.api_format, candidate.embedding_dim, recipe)


def reconcile_space(db: sqlite3.Connection, consumer: str, space: VectorSpace) -> int:
    """比对消费方登记并返回清空条数；首次登记或配置不变返回零。

    :param db: 已迁移的共享连接；本函数不让出线程或事件循环。
    :param consumer: facts、knowledge 或 emoji；其它值报 ValueError。
    :param space: 当前实际输入对应的空间。
    :raises sqlite3.Error: SQL 失败时回滚清空和登记，向启动方暴露错误。
    副作用：首次登记写入一行；空间变化时在同一事务更新登记并清空向量。
    """
    if consumer not in _CLEAR_SQL:
        raise ValueError(f'未知向量消费方：{consumer}')
    row = db.execute('SELECT model, api_format, dim, recipe FROM vector_space WHERE consumer=?', (consumer,)).fetchone()
    old = VectorSpace(*row) if row is not None else None
    if old == space:
        return 0
    values = (space.model, space.api_format, space.dim, space.recipe, now(), consumer)
    count = 0
    with db:
        if old is None:
            db.execute('INSERT INTO vector_space (model,api_format,dim,recipe,updated_at,consumer) VALUES (?,?,?,?,?,?)', values)
        else:
            count = db.execute(_CLEAR_SQL[consumer]).rowcount
            db.execute('UPDATE vector_space SET model=?,api_format=?,dim=?,recipe=?,updated_at=? WHERE consumer=?', values)
    if old is None:
        logger.info('首次登记向量空间', consumer=consumer, space=str(space), assumption='假定已有向量来自当前配置，保留不清空')
    else:
        logger.warning('向量空间变更，旧向量已清空', consumer=consumer, old_space=str(old), new_space=str(space), count=count)
    return count
