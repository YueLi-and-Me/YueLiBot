"""持久化可发送表情包，并按情绪语义选择完整性可信的图片。

表情包文件存放在运行数据目录中，文件名使用内容 SHA-256；数据库只保存
可复用的本地 ``file://`` 引用、情绪标签和可选文本向量。启动阶段必须调用
``verify_integrity`` 重算全部文件哈希，任何缺失、越界或内容不一致都会阻止
服务继续启动，避免把损坏文件静默交给平台发送。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Protocol, Sequence
from urllib.parse import unquote

import hashlib
import math
import random
import re
import sqlite3
import struct
import warnings

from PIL import Image, UnidentifiedImageError

from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger

logger = get_logger(__name__)

_IMPORT_SUFFIXES = frozenset({'.gif', '.jpeg', '.jpg', '.png', '.webp'})
_IMPORT_MEDIA_TYPES = {
    'GIF': 'image/gif',
    'JPEG': 'image/jpeg',
    'PNG': 'image/png',
    'WEBP': 'image/webp',
}
_MAX_IMPORT_BYTES = 5 * 1024 * 1024


class EmojiEmbeddingClient(Protocol):
    """表情包库依赖的最小文本嵌入接口。"""

    @property
    def dim(self) -> int:
        """返回序列化向量的浮点维度。"""

        ...

    async def embed_one(self, text: str) -> bytes | None:
        """返回一条文本的序列化向量；调用失败时可返回 ``None``。"""

        ...


@dataclass(frozen=True)
class EmojiIntegrityIssue:
    """一条无法通过启动校验的表情包记录。"""

    content_hash: str
    send_ref: str
    reason: str


class EmojiIntegrityError(RuntimeError):
    """表情包文件缺失、越界或内容哈希不一致。"""

    def __init__(self, issues: Sequence[EmojiIntegrityIssue]) -> None:
        """保存全部损坏记录，并生成可直接排障的中文错误。"""

        self.issues = tuple(issues)
        details = '；'.join(
            f'{issue.content_hash[:12]}：{issue.reason}'
            for issue in self.issues
        )
        super().__init__(f'表情包完整性校验失败（{len(self.issues)} 项）：{details}')


@dataclass(frozen=True)
class EmojiImportSummary:
    """一次视觉自动登记扫描产生的可验收统计。"""

    discovered: int
    added: int
    skipped: int
    failed: int


@dataclass(frozen=True)
class EmojiSelection:
    """一张已命中的表情包发送引用及其 OneBot 子类型。"""

    send_ref: str
    sub_type: int


@dataclass(frozen=True)
class _EmojiImportCandidate:
    """一张已在模型调用前完成格式和哈希预检的素材。"""

    path: Path
    content_hash: str
    media_type: str
    image_bytes: bytes


class EmojiDescription(Protocol):
    """视觉模型成功识别一张表情包后返回的最小数据形状。"""

    content_hash: str
    emotion_tags: str
    image_bytes: bytes
    media_type: str


class EmojiDescriptionProvider(Protocol):
    """自动登记依赖的表情包视觉标注接口。"""

    async def describe_emoji_sources(
        self,
        sources: Sequence[str],
    ) -> list[EmojiDescription | None]:
        """按来源顺序返回情绪标签和已读取的图片内容。"""

        ...


class EmojiLibrary:
    """管理一张最小表情包表、运行时文件与情绪检索。"""

    def __init__(
        self,
        db: sqlite3.Connection,
        directory: Path,
        embed_client: EmojiEmbeddingClient | None = None,
        *,
        choice: Callable[[Sequence[EmojiSelection]], EmojiSelection] = random.choice,
    ) -> None:
        """绑定数据库、表情包目录和可选文本嵌入客户端。

        :param db: 已完成迁移的 SQLite 连接。
        :param directory: 表情包文件专用目录；不得与其他运行文件共用。
        :param embed_client: 文本嵌入客户端；缺失时使用标签包含匹配。
        :param choice: top-K 候选随机选择函数，测试可注入确定性实现。
        副作用：创建表情包目录，但不会在构造阶段读写数据库记录。
        """

        self._db = db
        self._directory = directory.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._embed_client = embed_client
        self._choice = choice

    def has_sendable(self) -> bool:
        """判断库中是否至少存在一条可发送记录。"""

        row = self._db.execute('SELECT 1 FROM emoji LIMIT 1').fetchone()
        return row is not None

    def frequent_tags(self, limit: int = 12) -> tuple[str, ...]:
        """统计覆盖表情最多的情绪标签，供提示词锚定 emotion 词表。

        模型自拟的情绪词与视觉标注的标签词表天然存在偏差，把库内高频标签
        回填进提示词可以显著提高检索命中率；词表保持小规模，避免挤占
        协议文本的注意力。

        :param limit: 返回的最大标签数，必须大于零。
        :return: 按覆盖表情数降序、同数按字典序排列的前若干标签；库为空时
            返回空元组。
        :raises ValueError: limit 非正数。
        副作用：只读 emoji 表，不修改任何记录。
        """

        if limit < 1:
            raise ValueError('表情包高频标签 limit 必须大于零')
        counts: dict[str, int] = {}
        for (tags,) in self._db.execute('SELECT emotion_tags FROM emoji'):
            for tag in str(tags).split(','):
                tag = tag.strip()
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return tuple(tag for tag, _count in ranked[:limit])

    async def auto_register_directory(
        self,
        describer: EmojiDescriptionProvider,
        *,
        batch_size: int = 4,
    ) -> EmojiImportSummary:
        """扫描表情包目录中未登记的内容并调用视觉模型生成标签。

        数据库已有的 SHA-256 和本批次重复内容不会请求视觉模型。识别失败或
        无法解析的原文件保留在目录中，记录明确警告并留待下次启动重试；成功项
        通过 :meth:`register` 写入哈希命名的可信副本和数据库记录。

        :param describer: 项目现有的表情包视觉标注服务。
        :param batch_size: 单批并发调用数量，默认 4，必须大于零。
        :return: 发现、新增、跳过和失败数量。
        :raises ValueError: ``batch_size`` 不是正数。
        副作用：读取 ``data/emojis`` 中的图片，调用视觉与 embedding 接口，
            并为识别成功的新内容写入数据库和哈希命名副本。
        """

        if batch_size < 1:
            raise ValueError('表情包自动登记批大小必须大于零')
        root = self._directory
        paths = [
            path
            for path in sorted(root.rglob('*'), key=lambda item: item.as_posix().casefold())
            if path.is_file() and path.suffix.casefold() in _IMPORT_SUFFIXES
        ]
        if not paths:
            return EmojiImportSummary(discovered=0, added=0, skipped=0, failed=0)

        existing = {
            str(content_hash).lower()
            for (content_hash,) in self._db.execute('SELECT hash FROM emoji').fetchall()
        }
        candidates: list[_EmojiImportCandidate] = []
        batch_hashes: set[str] = set()
        skipped = 0
        failed = 0
        for path in paths:
            try:
                candidate = _inspect_import_candidate(path, root)
            except (OSError, ValueError) as exc:
                failed += 1
                logger.warning('emoji_auto_register_invalid', path=str(path), error=str(exc))
                continue
            if candidate.content_hash in existing or candidate.content_hash in batch_hashes:
                skipped += 1
                continue
            batch_hashes.add(candidate.content_hash)
            candidates.append(candidate)

        added = 0
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset:offset + batch_size]
            results = await describer.describe_emoji_sources(
                [str(candidate.path) for candidate in batch]
            )
            if len(results) != len(batch):
                raise ValueError('视觉标注结果数量与请求图片数量不一致')
            for candidate, result in zip(batch, results):
                if result is None:
                    failed += 1
                    logger.warning(
                        'emoji_auto_register_description_failed',
                        path=str(candidate.path),
                    )
                    continue
                if result.content_hash != candidate.content_hash:
                    failed += 1
                    logger.warning(
                        'emoji_auto_register_changed',
                        path=str(candidate.path),
                    )
                    continue
                await self.register(
                    result.image_bytes,
                    result.emotion_tags,
                    result.media_type,
                    content_hash=result.content_hash,
                )
                added += 1
        return EmojiImportSummary(
            discovered=len(paths),
            added=added,
            skipped=skipped,
            failed=failed,
        )

    async def register(
        self,
        image_bytes: bytes,
        emotion_tags: str,
        media_type: str,
        content_hash: str | None = None,
        sub_type: int = 1,
    ) -> str:
        """保存一张识别成功的表情包并按内容哈希 upsert。

        文件先以临时名写入，再在同一目录原子替换为哈希文件；数据库引用只有
        在文件可用后才提交。相同内容再次出现时 ``seen_count`` 自增，并刷新
        标签和可用向量。

        :param sub_type: 入站 OneBot 表情包子类型；本地导入素材默认使用 ``1``。
        :return: 可交给 OneBot image 段使用的本地 ``file://`` 引用。
        :raises ValueError: 图片、标签或调用方提供的哈希不合法。
        :raises OSError, sqlite3.Error: 文件或数据库写入失败。
        """

        if not image_bytes:
            raise ValueError('登记表情包时图片内容不能为空')
        tags = emotion_tags.strip()
        if not tags:
            raise ValueError('登记表情包时情绪标签不能为空')
        digest = hashlib.sha256(image_bytes).hexdigest()
        if content_hash is not None and content_hash.lower() != digest:
            raise ValueError('调用方提供的表情包哈希与图片内容不一致')
        normalized_sub_type = _validate_emoji_sub_type(sub_type)

        extension = _media_extension(media_type)
        target = self._directory / f'{digest}{extension}'
        if not target.exists():
            temporary = self._directory / f'.{digest}.tmp'
            temporary.write_bytes(image_bytes)
            temporary.replace(target)
        else:
            existing_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing_hash != digest:
                raise EmojiIntegrityError((EmojiIntegrityIssue(
                    content_hash=digest,
                    send_ref=target.as_uri(),
                    reason='同名文件内容与哈希不一致',
                ),))

        vector = await self._embed_tags(tags)
        send_ref = target.resolve().as_uri()
        self._db.execute(
            '''INSERT INTO emoji (
                   hash, send_ref, emotion_tags, emotion_vec, sub_type, seen_count, first_seen_at
               ) VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   send_ref = excluded.send_ref,
                   emotion_tags = excluded.emotion_tags,
                   emotion_vec = COALESCE(excluded.emotion_vec, emoji.emotion_vec),
                   sub_type = excluded.sub_type,
                   seen_count = emoji.seen_count + 1''',
            (digest, send_ref, tags, vector, normalized_sub_type, current_time()),
        )
        self._db.commit()
        logger.info('emoji_registered', hash=digest, tags=tags)
        return send_ref

    async def select(self, emotion: str, top_k: int = 10) -> EmojiSelection | None:
        """按目标情绪取语义最相近的 top-K 并随机返回一张。

        嵌入客户端不可用或查询失败时，按逗号分隔标签执行双向包含匹配；仍无
        候选时返回 ``None``，不硬塞无关表情包。

        :param emotion: 本轮想表达的目标情绪。
        :param top_k: 进入随机池的最大候选数，必须大于零。
        :return: 带 ``file://`` 引用和原始 ``sub_type`` 的命中结果，或 ``None``。
        :raises ValueError: 情绪为空或 top-K 非正数。
        """

        query = emotion.strip()
        if not query:
            raise ValueError('表情包目标情绪不能为空')
        if top_k < 1:
            raise ValueError('表情包 top_k 必须大于零')
        rows = self._db.execute(
            'SELECT send_ref, emotion_tags, emotion_vec, sub_type '
            'FROM emoji ORDER BY first_seen_at, hash'
        ).fetchall()
        if not rows:
            return None

        query_vec = await self._embed_tags(query)
        if query_vec is not None:
            # 【关键】维度必须从查询向量的实际字节数推导，不能读配置的
            # embedding_dim（float32 每分量 4 字节，与事实召回 store 侧同口径）。
            #
            # 原因：
            # 1. 配置未填 embedding_dim 时该值为 0，按配置推导会把库内全部向量
            #    当作维度不符跳过，语义排序永远为空，检索静默退化为字面子串匹配，
            #    现场表现为模型写了 <emoji> 却几乎发不出、且无任何报错。
            # 2. 库内残留其它维度的历史向量时，按查询长度逐条过滤只会跳过不匹配
            #    的记录，不会拖垮整批候选。
            dim = len(query_vec) // 4
            ranked: list[tuple[float, EmojiSelection]] = []
            for send_ref, _tags, vector, sub_type in rows:
                if not isinstance(vector, bytes) or len(vector) != len(query_vec):
                    continue
                ranked.append((
                    _cosine_similarity(query_vec, vector, dim),
                    EmojiSelection(
                        send_ref=str(send_ref),
                        sub_type=_validate_emoji_sub_type(sub_type),
                    ),
                ))
            if ranked:
                ranked.sort(key=lambda item: item[0], reverse=True)
                return self._choice([selection for _score, selection in ranked[:top_k]])

        matched = [
            EmojiSelection(
                send_ref=str(send_ref),
                sub_type=_validate_emoji_sub_type(sub_type),
            )
            for send_ref, tags, _vector, sub_type in rows
            if _tags_overlap(query, str(tags))
        ]
        if not matched:
            return None
        return self._choice(matched[:top_k])

    def verify_integrity(self) -> int:
        """在启动阶段逐项重算已登记表情包的 SHA-256。

        :return: 校验通过的记录数。
        :raises EmojiIntegrityError: 任一记录引用非法、文件缺失或哈希不一致。
        副作用：读取表情包文件内容并记录校验结果，不修改文件或数据库。
        """

        rows = self._db.execute('SELECT hash, send_ref FROM emoji ORDER BY hash').fetchall()
        issues: list[EmojiIntegrityIssue] = []
        for raw_hash, raw_ref in rows:
            expected = str(raw_hash).lower()
            send_ref = str(raw_ref)
            if re.fullmatch(r'[0-9a-f]{64}', expected) is None:
                issues.append(EmojiIntegrityIssue(expected, send_ref, '数据库哈希格式非法'))
                continue
            try:
                path = _file_ref_path(send_ref)
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(self._directory):
                    raise ValueError('文件引用越出表情包目录')
                actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
                if actual != expected:
                    raise ValueError(f'内容哈希不一致，实际为 {actual[:12]}')
            except (OSError, ValueError) as exc:
                issues.append(EmojiIntegrityIssue(expected, send_ref, str(exc)))
        if issues:
            raise EmojiIntegrityError(issues)
        logger.info('emoji_integrity_verified', count=len(rows), directory=str(self._directory))
        return len(rows)

    async def _embed_tags(self, tags: str) -> bytes | None:
        """调用可选嵌入客户端，并把失败收敛为文本匹配信号。"""

        if self._embed_client is None:
            return None
        try:
            return await self._embed_client.embed_one(tags)
        except Exception as exc:
            logger.warning('emoji_embedding_failed', error=str(exc))
            return None


def _file_ref_path(send_ref: str) -> Path:
    """把 OneBot 本地文件引用还原为当前系统路径。"""

    if not send_ref.startswith('file://'):
        raise ValueError('发送引用不是可校验的 file:// 本地文件')
    raw_path = unquote(send_ref[len('file://'):])
    if len(raw_path) >= 3 and raw_path[0] == '/' and raw_path[2] == ':':
        raw_path = raw_path[1:]
    return Path(raw_path)


def _validate_emoji_sub_type(value: object) -> int:
    """校验数据库和调用方提供的 OneBot 表情包子类型。"""

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value in {0, 4, 9}
    ):
        raise ValueError(f'表情包 sub_type 不合法：{value!r}')
    return value


def _inspect_import_candidate(path: Path, root: Path) -> _EmojiImportCandidate:
    """在不调用模型的前提下解析一张素材的格式和 SHA-256。"""

    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError('图片路径越出表情包目录')

    image_bytes = resolved.read_bytes()
    if not image_bytes:
        raise ValueError('图片内容为空')
    if len(image_bytes) > _MAX_IMPORT_BYTES:
        raise ValueError(f'图片超过 {_MAX_IMPORT_BYTES} 字节上限')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as image:
                image.verify()
                image_format = str(image.format or '').upper()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f'图片解析失败：{exc}') from exc
    try:
        media_type = _IMPORT_MEDIA_TYPES[image_format]
    except KeyError as exc:
        raise ValueError(f'不支持的图片格式：{image_format or "未知"}') from exc
    return _EmojiImportCandidate(
        path=resolved,
        content_hash=hashlib.sha256(image_bytes).hexdigest(),
        media_type=media_type,
        image_bytes=image_bytes,
    )


def _media_extension(media_type: str) -> str:
    """把常见图片 MIME 映射为稳定扩展名。"""

    return {
        'image/gif': '.gif',
        'image/png': '.png',
        'image/webp': '.webp',
    }.get(media_type.lower(), '.jpg')


def _tags_overlap(query: str, tags: str) -> bool:
    """判断请求情绪与库标签是否存在双向包含关系。"""

    query_parts = [part.strip() for part in re.split(r'[,，、;；\s]+', query) if part.strip()]
    tag_parts = [part.strip() for part in re.split(r'[,，、;；\s]+', tags) if part.strip()]
    return any(
        query_part in tag_part or tag_part in query_part
        for query_part in query_parts
        for tag_part in tag_parts
    )


def _cosine_similarity(left: bytes, right: bytes, dim: int) -> float:
    """计算两条 packed float32 向量的标准余弦相似度。"""

    if dim < 1 or len(left) != dim * 4 or len(right) != dim * 4:
        raise ValueError('表情包向量维度与序列化字节长度不一致')
    left_values = struct.unpack(f'{dim}f', left)
    right_values = struct.unpack(f'{dim}f', right)
    dot = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return -1.0
    return dot / (left_norm * right_norm)
