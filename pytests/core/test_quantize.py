"""N2 自解释 SQ8 编解码与存量补算回归。"""

from __future__ import annotations

import sqlite3
import struct

import pytest

from src.core.memory.quantize import (
    backfill_quantized_embeddings,
    cosine_q8,
    dequantize,
    pending_quantization_counts,
    quantize,
)


def _pack(values: list[float]) -> bytes:
    """把测试向量编码为生产原列使用的小端 float32。"""

    return struct.pack(f'<{len(values)}f', *values)


def _unpack(vector: bytes) -> tuple[float, ...]:
    """按字节长度读取测试向量，不预设维度。"""

    return struct.unpack(f'<{len(vector) // 4}f', vector)


def test_round_trip_is_self_describing_for_different_dimensions() -> None:
    """★Q-3：两种维度均只靠单条 blob 恢复，不依赖配置或维度常量。"""

    for values in ([0.0, -1.0, 0.25], [3.5, -2.0, 0.0, 1.25, -0.5]):
        encoded = quantize(_pack(list(values)))
        restored = _unpack(dequantize(encoded))
        maximum = max(abs(value) for value in values)
        tolerance = maximum / 127 + 1e-6
        assert len(restored) == len(values)
        assert restored == pytest.approx(values, abs=tolerance)


def test_cosine_q8_matches_direction_and_validates_dimensions() -> None:
    """相同、正交、全零与维度不一致的余弦语义明确。"""

    horizontal = quantize(_pack([1.0, 0.0, 0.0]))
    vertical = quantize(_pack([0.0, 1.0, 0.0]))
    zero = quantize(_pack([0.0, 0.0, 0.0]))
    assert cosine_q8(horizontal, horizontal) == pytest.approx(1.0)
    assert cosine_q8(horizontal, vertical) == pytest.approx(0.0)
    assert cosine_q8(horizontal, zero) == 0.0
    with pytest.raises(ValueError, match='维度不一致'):
        cosine_q8(horizontal, quantize(_pack([1.0, 0.0])))


@pytest.mark.parametrize(
    'raw',
    [b'', b'abc', _pack([float('nan')]), _pack([float('inf')])],
)
def test_quantize_rejects_invalid_raw_vectors(raw: bytes) -> None:
    """坏原向量立即暴露，不能生成无法审计的量化值。"""

    with pytest.raises(ValueError):
        quantize(raw)


def test_decode_rejects_corrupted_blob() -> None:
    """格式版本、头部与载荷损坏均不得静默读取。"""

    valid = quantize(_pack([1.0, 2.0]))
    with pytest.raises(ValueError):
        dequantize(b'')
    with pytest.raises(ValueError, match='格式标识'):
        dequantize(b'BAD!' + valid[4:])
    with pytest.raises(ValueError, match='载荷长度'):
        dequantize(valid[:-1])


@pytest.mark.asyncio
async def test_backfill_both_tables_preserves_raw_bytes() -> None:
    """★Q-2：批量补算覆盖两表，原列不变，第二次运行写入数为零。"""

    db = sqlite3.connect(':memory:')
    db.executescript(
        '''
        CREATE TABLE facts (
          id INTEGER PRIMARY KEY, embedding BLOB, embedding_q8 BLOB
        );
        CREATE TABLE knowledge (
          id INTEGER PRIMARY KEY, embedding BLOB, embedding_q8 BLOB
        );
        '''
    )
    fact_raw = _pack([1.0, -0.5, 0.25])
    knowledge_raw = _pack([-0.75, 0.0, 2.5, 0.125])
    db.execute('INSERT INTO facts VALUES (1, ?, NULL)', (fact_raw,))
    db.execute('INSERT INTO facts VALUES (2, NULL, NULL)')
    db.execute('INSERT INTO knowledge VALUES (1, ?, NULL)', (knowledge_raw,))
    db.commit()

    assert pending_quantization_counts(db) == {'facts': 1, 'knowledge': 1}
    report = await backfill_quantized_embeddings(db, batch_size=1)

    assert (report.facts, report.knowledge, report.total) == (1, 1, 2)
    assert pending_quantization_counts(db) == {'facts': 0, 'knowledge': 0}
    assert db.execute('SELECT embedding FROM facts WHERE id = 1').fetchone()[0] == fact_raw
    assert db.execute('SELECT embedding FROM knowledge WHERE id = 1').fetchone()[0] == knowledge_raw
    assert db.execute('SELECT embedding_q8 FROM facts WHERE id = 1').fetchone()[0]
    assert db.execute('SELECT embedding_q8 FROM knowledge WHERE id = 1').fetchone()[0]

    replay = await backfill_quantized_embeddings(db, batch_size=1)
    assert replay.total == 0
    with pytest.raises(ValueError, match='批大小'):
        await backfill_quantized_embeddings(db, batch_size=0)
    db.close()
