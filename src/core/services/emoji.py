"""持久化可发送表情包，并按情绪语义选择完整性可信的图片。

表情包文件存放在运行数据目录中，文件名使用内容 SHA-256；数据库只保存
可复用的本地 file:// 引用、情绪标签和可选文本向量。启动阶段必须调用
verify_integrity 重算全部文件哈希，任何缺失、越界或内容不一致都会阻止
服务继续启动，避免把损坏文件静默交给平台发送。

除启动校验外，本模块还承担表情包库的三道入库闸门与两条后台维护：
- 封禁表按内容哈希独立存在，入库第一件事就是查它（行被淘汰后封禁依然生效）；
- max_file_size_mb 拒绝超大文件；content_filtration 开启时先过视觉模型
  审查，模型不可用或审查不过都拒绝入库；
- 后台维护按 max_count 淘汰最冷条目（use_count 升序、last_used_at 升序，
  确定性 SQL，不调用模型），并按保留期清理目录里的孤儿文件；
- verify_integrity 补上「文件→库」方向巡检，孤儿文件只报不删，删除由
  清理任务与一次性清理脚本负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import unquote

import base64
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
from src.core.config.schema import EmojiConfig
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt

logger = get_logger(__name__)

_IMPORT_SUFFIXES = frozenset({'.gif', '.jpeg', '.jpg', '.png', '.webp'})
_IMPORT_MEDIA_TYPES = {
    'GIF': 'image/gif',
    'JPEG': 'image/jpeg',
    'PNG': 'image/png',
    'WEBP': 'image/webp',
}

_MB = 1024 * 1024


class EmojiEmbeddingClient(Protocol):
    """表情包库依赖的最小文本嵌入接口。"""

    @property
    def dim(self) -> int:
        """返回序列化向量的浮点维度。"""

        ...

    async def embed_one(self, text: str) -> bytes | None:
        """返回一条文本的序列化向量；调用失败时可返回 None。"""

        ...


class EmojiContentFilter(Protocol):
    """入库内容审查依赖的最小视觉模型接口。

    与文本嵌入接口一样只是协议：调用方注入已有 vision 模型槽的实现，
    本模块不关心提示词与网络细节，只认三态结果。
    """

    async def filter_emoji_content(
        self,
        image_bytes: bytes,
        media_type: str,
    ) -> bool | None:
        """判断一张图是否适合作为表情包入库。

        :param image_bytes: 图片原始字节。
        :param media_type: 图片 MIME 类型。
        :return: True 适合入库；False 审查不通过；None 表示模型不可用或
            判定失败，调用方必须按拒绝处理而不是静默放行。
        """

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


class EmojiBannedError(RuntimeError):
    """入库内容命中封禁表。

    封禁表以内容哈希为键独立于 emoji 行存在，因此这张图即使行被淘汰、
    文件被删除后仍不允许入库。
    """

    def __init__(self, content_hash: str, reason: str = '') -> None:
        """保存命中封禁的哈希与封禁原因。"""

        self.content_hash = content_hash
        self.reason = reason
        super().__init__(f'表情包已被封禁：{content_hash[:12]}')


class EmojiContentRejectedError(RuntimeError):
    """入库内容未通过大小上限或视觉模型审查。

    与 EmojiBannedError 分开抛，让调用方可以分别记录拒绝原因而不必
    解析异常文本。
    """

    def __init__(self, reason: str) -> None:
        """保存拒绝原因。"""

        self.reason = reason
        super().__init__(f'表情包拒绝入库：{reason}')


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
class EmojiEvictionRecord:
    """一条被后台维护淘汰的表情包记录，字段足够观察面板回放取舍。"""

    content_hash: str
    send_ref: str
    use_count: int
    last_used_at: int | None
    file_bytes: int


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


class VisionEmojiContentFilter:
    """用既有 [model_tasks.vision] 槽做入库内容审查。

    内容过滤复用既有视觉任务槽，不新增模型槽。过滤关闭时本类不会被调用；
    开启但 provider 为 None（视觉路由无候选）时 filter_emoji_content 返回
    None，调用方据此拒绝入库并告警，不放行未审查的图片。
    """

    def __init__(
        self,
        provider: Any | None,
        temperature: float = 0.3,
        max_tokens: int = 32,
    ) -> None:
        """绑定视觉模型提供者与生成参数。

        :param provider: 视觉路由选出的模型提供者；可为 None。
        :param temperature: 审查调用的采样温度，默认沿用视觉任务的低值。
        :param max_tokens: 审查输出上限；判定只需要两个词，给足 32 足够。
        """

        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def filter_emoji_content(
        self,
        image_bytes: bytes,
        media_type: str,
    ) -> bool | None:
        """调用视觉模型判断图片是否适合入库。

        :param image_bytes: 图片原始字节。
        :param media_type: 图片 MIME 类型。
        :return: 判定结果；模型不可用、调用失败或输出无法解析时返回 None。
        副作用：调用一次视觉模型；不写入任何缓存。
        """

        if self._provider is None:
            return None
        try:
            encoded = base64.b64encode(image_bytes).decode('ascii')
            prompt = get_prompt('emoji.filter').render()
            raw = ''
            stream = self._provider.stream(
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {
                            'type': 'image_url',
                            'image_url': {
                                'url': f'data:{media_type};base64,{encoded}',
                                'detail': 'low',
                            },
                        },
                    ],
                }],
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                require_text=True,
            )
            async for chunk in stream:
                if chunk.get('text'):
                    raw += chunk['text']
        except Exception as exc:
            logger.warning('emoji_content_filter_failed', error=str(exc))
            return None
        verdict = raw.strip()
        # 只认明确的「适合」，无法判断与其他输出均按拒绝处理：未完成审查的
        # 图片入库的代价高于拒绝一张可重新收集的图片。
        if '不适合' in verdict:
            return False
        if '适合' in verdict:
            return True
        logger.warning('emoji_content_filter_unparsable', verdict=verdict)
        return None


class EmojiLibrary:
    """管理一张最小表情包表、运行时文件与情绪检索。"""

    def __init__(
        self,
        db: sqlite3.Connection,
        directory: Path,
        embed_client: EmojiEmbeddingClient | None = None,
        *,
        choice: Callable[[Sequence[EmojiSelection]], EmojiSelection] = random.choice,
        config: EmojiConfig | None = None,
        content_filter: EmojiContentFilter | None = None,
    ) -> None:
        """绑定数据库、表情包目录、可选文本嵌入客户端和库管理配置。

        :param db: 已完成迁移的 SQLite 连接。
        :param directory: 表情包文件专用目录；不得与其他运行文件共用。
        :param embed_client: 文本嵌入客户端；缺失时使用标签包含匹配。
        :param choice: top-K 候选随机选择函数，测试可注入确定性实现。
        :param config: 表情包库管理配置；缺省时使用全默认值（0 上限不淘汰、
            5 MB 单文件上限、不过滤、自动收集）。
        :param content_filter: 内容审查实现；只在配置开启过滤时被调用。
        副作用：创建表情包目录，但不会在构造阶段读写数据库记录。
        """

        self._db = db
        self._directory = directory.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._embed_client = embed_client
        self._choice = choice
        self._config = config or EmojiConfig()
        self._content_filter = content_filter
        # 单文件上限从配置换算成字节；0 表示不限。
        self._max_file_size_bytes = (
            int(self._config.max_file_size_mb * _MB)
            if self._config.max_file_size_mb > 0
            else 0
        )

    def has_sendable(self) -> bool:
        """判断库中是否至少存在一条可发送记录。"""

        row = self._db.execute('SELECT 1 FROM emoji LIMIT 1').fetchone()
        return row is not None

    def emotion_tags_for_hash(self, content_hash: str) -> str | None:
        """按内容哈希读取可直接复用的入站表情包标签。

        :param content_hash: 图片内容的 SHA-256 十六进制字符串。
        :return: 数据库中非空的 ``emotion_tags``；未登记或标签为空时返回 ``None``。
        :raises ValueError: 内容哈希格式不合法。
        :raises sqlite3.Error: 查询表情包表失败。
        副作用：只读 emoji 表，不修改标签、计数或文件。
        """
        normalized = _normalize_hash(content_hash)
        row = self._db.execute(
            'SELECT emotion_tags FROM emoji WHERE hash = ?',
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        tags = str(row[0]).strip()
        return tags or None

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
        通过 register 写入哈希命名的可信副本和数据库记录。

        :param describer: 项目现有的表情包视觉标注服务。
        :param batch_size: 单批并发调用数量，默认 4，必须大于零。
        :return: 发现、新增、跳过和失败数量。
        :raises ValueError: batch_size 不是正数。
        副作用：读取 data/emojis 中的图片，调用视觉与 embedding 接口，
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
            # 文件名就是内容哈希时直接跳过，不读内容也不过 PIL。
            #
            # - 现象：目录里全是已登记的表情时，扫描仍会把每个文件完整读一遍、
            #   算一次 SHA-256、再用 PIL 打开验格式，然后才发现哈希已在库里丢弃。
            #   真机 369 个文件（97 MB）为此花掉约 0.3 秒，占整个启动的可观份额。
            # - 原因：命中判断放在 _inspect_import_candidate 之后，而那次 inspect
            #   的唯一产物（content_hash）对这批文件是已知的——库写文件时就用
            #   哈希做文件名。
            # - 后果：跳过是否安全，取决于「文件名等于哈希」是否可信。可信：
            #   构造 EmojiLibrary 时的 verify_integrity() 已经把每一行的文件重算过
            #   一遍哈希并比对，不一致会直接阻止启动。名字对不上哈希的文件（用户
            #   手工投放的）不满足这个条件，仍走完整 inspect。
            if path.stem.casefold() in existing:
                skipped += 1
                continue
            try:
                candidate = _inspect_import_candidate(path, root, self._max_file_size_bytes)
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
                try:
                    await self.register(
                        result.image_bytes,
                        result.emotion_tags,
                        result.media_type,
                        content_hash=result.content_hash,
                    )
                    added += 1
                except (EmojiBannedError, EmojiContentRejectedError) as exc:
                    # 封禁与审查拒绝按失败计数，但不应中断其余素材的登记。
                    failed += 1
                    logger.warning('emoji_auto_register_rejected', error=str(exc))
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

        入库闸门按顺序执行：先查封禁表，再按 max_file_size_mb
        拒绝超大文件，最后在开启 content_filtration 时过视觉模型审查。
        全部通过后文件先以临时名写入，再在同一目录原子替换为哈希文件；
        数据库引用只有在文件可用后才提交。相同内容再次出现时 seen_count
        自增，并刷新标签和可用向量。

        :param sub_type: 入站 OneBot 表情包子类型；本地导入素材默认使用 1。
        :return: 可交给 OneBot image 段使用的本地 file:// 引用。
        :raises ValueError: 图片、标签或调用方提供的哈希不合法。
        :raises EmojiBannedError: 内容命中封禁表。
        :raises EmojiContentRejectedError: 超过大小上限、审查不通过或过滤开启
            但视觉模型不可用。
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

        # 封禁按内容哈希独立存在，入库第一件事就查它；行被淘汰后封禁依然生效。
        banned = self._db.execute(
            'SELECT reason FROM emoji_banned WHERE hash = ?', (digest,),
        ).fetchone()
        if banned is not None:
            logger.warning(
                'emoji_banned_rejected',
                hash=digest,
                reason=banned[0] or '',
            )
            trace.emit(
                'emoji_banned',
                hash=digest,
                outcome='blocked',
                reason=banned[0] or '',
            )
            raise EmojiBannedError(digest, str(banned[0] or ''))

        if self._max_file_size_bytes and len(image_bytes) > self._max_file_size_bytes:
            limit_mb = self._config.max_file_size_mb
            logger.warning(
                'emoji_too_large',
                hash=digest,
                bytes=len(image_bytes),
                limitBytes=self._max_file_size_bytes,
            )
            trace.emit(
                'emoji_registration_rejected',
                hash=digest,
                reason='too_large',
                bytes=len(image_bytes),
            )
            raise EmojiContentRejectedError(
                f'图片超过 max_file_size_mb = {limit_mb} MB 上限'
            )

        if self._config.content_filtration:
            verdict = None
            if self._content_filter is not None:
                verdict = await self._content_filter.filter_emoji_content(
                    image_bytes, media_type,
                )
            if verdict is None:
                # 开启过滤但视觉模型不可用时拒绝入库并明确告警，
                # 不把未审查的图片静默放行。
                logger.warning(
                    'emoji_content_filter_unavailable',
                    hash=digest,
                )
                trace.emit(
                    'emoji_registration_rejected',
                    hash=digest,
                    reason='filter_unavailable',
                )
                raise EmojiContentRejectedError('内容过滤已开启但视觉模型不可用')
            if verdict is not True:
                logger.warning(
                    'emoji_content_filter_rejected',
                    hash=digest,
                )
                trace.emit(
                    'emoji_registration_rejected',
                    hash=digest,
                    reason='filter_rejected',
                )
                raise EmojiContentRejectedError('内容审查未通过')

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
            """INSERT INTO emoji (
                   hash, send_ref, emotion_tags, emotion_vec, sub_type, seen_count, first_seen_at
               ) VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   send_ref = excluded.send_ref,
                   emotion_tags = excluded.emotion_tags,
                   emotion_vec = COALESCE(excluded.emotion_vec, emoji.emotion_vec),
                   sub_type = excluded.sub_type,
                   seen_count = emoji.seen_count + 1""",
            (digest, send_ref, tags, vector, normalized_sub_type, current_time()),
        )
        self._db.commit()
        logger.info('emoji_registered', hash=digest, tags=tags)
        return send_ref

    def record_use(self, send_ref: str) -> bool:
        """发送成功后回写一次使用记录。

        :param send_ref: 已发送表情包的 file:// 引用，与 emoji 表 send_ref
            逐字对应。
        :return: 命中并更新一行时返回 True；引用已不在库中时返回 False。
        副作用：use_count 加一并刷新 last_used_at；发送失败不得调用本方法。
        """

        row = self._db.execute(
            'UPDATE emoji SET use_count = use_count + 1, last_used_at = ? '
            'WHERE send_ref = ?',
            (current_time(), send_ref),
        )
        self._db.commit()
        return row.rowcount > 0

    def ban(self, content_hash: str, reason: str = '') -> bool:
        """按内容哈希封禁一张图，封禁与 emoji 行解耦。

        :param content_hash: 图片 SHA-256；必须为 64 位十六进制。
        :param reason: 可选封禁原因，随封禁记录保留。
        :return: 新增封禁时返回 True；此前已封禁时返回 False。
        :raises ValueError: 哈希格式非法。
        """

        normalized = _normalize_hash(content_hash)
        row = self._db.execute(
            'INSERT OR IGNORE INTO emoji_banned (hash, banned_at, reason) VALUES (?, ?, ?)',
            (normalized, current_time(), reason.strip()),
        )
        self._db.commit()
        banned = row.rowcount > 0
        if banned:
            logger.info('emoji_ban_recorded', hash=normalized, reason=reason.strip())
            trace.emit('emoji_banned', hash=normalized, outcome='banned', reason=reason.strip())
        return banned

    def unban(self, content_hash: str) -> bool:
        """解除一条封禁记录。

        :param content_hash: 图片 SHA-256；必须为 64 位十六进制。
        :return: 删除一行时返回 True；没有对应封禁时返回 False。
        :raises ValueError: 哈希格式非法。
        """

        normalized = _normalize_hash(content_hash)
        row = self._db.execute(
            'DELETE FROM emoji_banned WHERE hash = ?', (normalized,),
        )
        self._db.commit()
        if row.rowcount > 0:
            logger.info('emoji_unbanned', hash=normalized)
        return row.rowcount > 0

    def remove(self, content_hash: str) -> bool:
        """删除一条表情包记录及其磁盘文件。

        :param content_hash: 图片 SHA-256；必须为 64 位十六进制。
        :return: 删除一行记录时返回 True；没有对应记录时返回 False。
        :raises ValueError: 哈希格式非法。
        副作用：删除数据库行并尝试删除目录内的同名文件；文件删除失败只记录
            警告，由孤儿清理任务兜底。
        """

        normalized = _normalize_hash(content_hash)
        row = self._db.execute(
            'SELECT send_ref FROM emoji WHERE hash = ?', (normalized,),
        ).fetchone()
        if row is None:
            return False
        self._db.execute('DELETE FROM emoji WHERE hash = ?', (normalized,))
        self._db.commit()
        _delete_emoji_file(Path(_file_ref_path(str(row[0]))), self._directory)
        logger.info('emoji_removed', hash=normalized)
        return True

    def page(
        self,
        limit: int = 20,
        offset: int = 0,
        banned_only: bool | None = None,
    ) -> list[dict[str, Any]]:
        """读取一页表情包记录供管理页展示。

        :param limit: 页大小，必须大于零。
        :param offset: 起始偏移，必须非负。
        :param banned_only: ``True`` 只取已封禁、``False`` 只取未封禁、
            ``None``（默认）不筛选。封禁记录独立于 emoji 行存在，因此筛选按
            两表的哈希交集判断，而不是 emoji 表上的某一列。
        :return: 按「最少用、最久未用」顺序排列的记录字典列表，与淘汰排序
            同口径，页面顺序即淘汰顺序。
        :raises ValueError: 分页参数非法。
        """

        if limit < 1:
            raise ValueError('表情包页大小必须大于零')
        if offset < 0:
            raise ValueError('表情包页偏移不能为负')
        banned = {
            str(row).lower()
            for (row,) in self._db.execute('SELECT hash FROM emoji_banned').fetchall()
        }
        rows = self._db.execute(
            'SELECT hash, send_ref, emotion_tags, sub_type, seen_count, use_count, last_used_at '
            f'FROM emoji WHERE {_banned_predicate(banned_only)} '
            'ORDER BY use_count ASC, last_used_at ASC, first_seen_at ASC, hash ASC '
            'LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
        return [
            {
                'hash': str(raw_hash),
                'sendRef': str(send_ref),
                'emotionTags': str(tags),
                'subType': int(sub_type),
                'seenCount': int(seen_count),
                'useCount': int(use_count),
                'lastUsedAt': int(last_used_at) if last_used_at is not None else None,
                'banned': str(raw_hash).lower() in banned,
            }
            for raw_hash, send_ref, tags, sub_type, seen_count, use_count, last_used_at in rows
        ]

    def file_path(self, content_hash: str) -> Path | None:
        """返回一条记录的磁盘文件路径，供缩略图等只读展示使用。

        :param content_hash: 图片 SHA-256；必须为 64 位十六进制。
        :return: 文件路径；哈希不存在、引用非法或文件缺失时返回 None。
        :raises ValueError: 哈希格式非法。
        副作用：只读数据库与目录元数据，不修改任何文件。
        """

        normalized = _normalize_hash(content_hash)
        row = self._db.execute(
            'SELECT send_ref FROM emoji WHERE hash = ?', (normalized,),
        ).fetchone()
        if row is None:
            return None
        try:
            path = Path(_file_ref_path(str(row[0]))).resolve(strict=True)
        except (OSError, ValueError):
            return None
        if not path.is_relative_to(self._directory):
            return None
        return path

    def count_entries(self, banned_only: bool | None = None) -> int:
        """按封禁筛选统计 emoji 行数，供列表分页的总数使用。

        :param banned_only: 与 :meth:`page` 同义的筛选开关。
        :return: 符合筛选的记录条数。
        """

        return int(self._db.execute(
            f'SELECT COUNT(*) FROM emoji WHERE {_banned_predicate(banned_only)}'
        ).fetchone()[0])

    def stats(self) -> dict[str, Any]:
        """汇总库容量与磁盘占用，供管理页总览与维护事件使用。

        :return: 记录数、封禁数、计入上限的记录数、目录文件数与字节数的字典。

        ``count`` 是 emoji 行总数，``countedCount`` 才是拿去和 ``maxCount``
        比的数——已封禁的记录不占容量（见 :meth:`evict_to_limit`）。两者都要
        给出：容量条要用后者，而「库里一共存着多少条」仍然是前者。
        ``bannedCount`` 数的是封禁表的行，它可能大于 ``bannedInLibrary``——
        封禁独立于 emoji 行存在，被封的图可以早已不在库里。

        副作用：只读数据库与目录元数据，不读取图片内容。
        """

        count = self._db.execute('SELECT COUNT(*) FROM emoji').fetchone()[0]
        banned_count = self._db.execute(
            'SELECT COUNT(*) FROM emoji_banned'
        ).fetchone()[0]
        banned_in_library = self.count_entries(banned_only=True)
        orphans = self.scan_orphans()
        files = [
            path for path in self._directory.rglob('*')
            if path.is_file()
        ]
        directory_bytes = sum(path.stat().st_size for path in files)
        return {
            'count': int(count),
            'bannedCount': int(banned_count),
            'bannedInLibrary': int(banned_in_library),
            'countedCount': int(count) - int(banned_in_library),
            'maxCount': int(self._config.max_count),
            'fileCount': len(files),
            'directoryBytes': directory_bytes,
            'orphanCount': len(orphans),
            'orphanBytes': sum(size for _path, size in orphans),
        }

    def scan_orphans(self) -> list[tuple[Path, int]]:
        """巡检「文件→库」方向，找出目录里有、库里没记录的文件。

        :return: (路径, 字节数) 列表，按路径字典序排列。
        副作用：只读目录元数据与 emoji 表哈希列，不修改任何文件。
        """

        registered = {
            str(row).lower()
            for (row,) in self._db.execute('SELECT hash FROM emoji').fetchall()
        }
        orphans: list[tuple[Path, int]] = []
        for path in self._directory.rglob('*'):
            if not path.is_file():
                continue
            if path.stem.casefold() in registered:
                continue
            orphans.append((path, path.stat().st_size))
        orphans.sort(key=lambda item: item[0].as_posix().casefold())
        return orphans

    def cleanup_orphans(self, retention_days: int) -> tuple[int, int]:
        """删除超过保留期的孤儿文件，返回清除数量与释放字节数。

        :param retention_days: 文件至少保留多少天；0 表示立即清理全部孤儿。
        :return: (删除文件数, 释放字节数)。
        :raises ValueError: retention_days 为负。
        副作用：删除目录内超过保留期且库里无记录的文件；保留期内的孤儿
            原样保留。
        """

        if retention_days < 0:
            raise ValueError('孤儿保留天数不能为负')
        removed = 0
        freed_bytes = 0
        cutoff = current_time() - retention_days * 24 * 3600 * 1000
        for path, size in self.scan_orphans():
            # 文件修改时间晚于保留期才跳过；stat 失败视为刚改动，跳过。
            try:
                modified_ms = int(path.stat().st_mtime * 1000)
            except OSError:
                continue
            if modified_ms > cutoff:
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.warning('emoji_orphan_remove_failed', path=str(path), error=str(exc))
                continue
            removed += 1
            freed_bytes += size
        if removed:
            logger.info(
                'emoji_cleanup',
                removed=removed,
                freedBytes=freed_bytes,
                retentionDays=retention_days,
            )
            trace.emit(
                'emoji_cleanup',
                removed=removed,
                freedBytes=freed_bytes,
                freedMb=round(freed_bytes / _MB, 2),
            )
        return removed, freed_bytes

    def evict_to_limit(self, max_count: int) -> list[EmojiEvictionRecord]:
        """按淘汰顺序把库容量收回到上限以内。

        淘汰采用确定性策略：按 (use_count 升序, last_used_at 升序) 单条 SQL
        取最冷条目。可解释、可回放、零模型调用。

        已封禁的记录不计入容量也不参与淘汰：封禁行的作用是在界面上保留该判定；
        若计入容量，等于以一个可用名额为代价保留一条不可发送的记录。

        两个条件必须同时满足，否则死循环：仅将封禁行排除出计数、仍允许其进入
        淘汰候选时，封禁行通常 ``use_count = 0`` 排在最前，会被逐条删除而计数
        不变，直到封禁行删光才开始淘汰真正超限的条目。

        :param max_count: 目标容量上限；0 或负值表示不设限，直接返回空列表。
        :return: 被淘汰的记录列表；未超限时为空列表。
        副作用：删除 emoji 行并尝试删除对应文件；文件删除失败由孤儿清理兜底。
        """

        if max_count < 1:
            return []
        unbanned = _banned_predicate(banned_only=False)
        evicted: list[EmojiEvictionRecord] = []
        while True:
            count = self._db.execute(
                f'SELECT COUNT(*) FROM emoji WHERE {unbanned}').fetchone()[0]
            if count <= max_count:
                break
            row = self._db.execute(
                'SELECT hash, send_ref, use_count, last_used_at FROM emoji '
                f'WHERE {unbanned} '
                'ORDER BY use_count ASC, last_used_at ASC, first_seen_at ASC, hash ASC '
                'LIMIT 1',
            ).fetchone()
            if row is None:
                break
            content_hash, send_ref, use_count, last_used_at = row
            file_bytes = _delete_emoji_file(
                Path(_file_ref_path(str(send_ref))), self._directory,
            )
            self._db.execute('DELETE FROM emoji WHERE hash = ?', (content_hash,))
            self._db.commit()
            record = EmojiEvictionRecord(
                content_hash=str(content_hash),
                send_ref=str(send_ref),
                use_count=int(use_count),
                last_used_at=int(last_used_at) if last_used_at is not None else None,
                file_bytes=file_bytes,
            )
            evicted.append(record)
            logger.info(
                'emoji_evicted',
                hash=record.content_hash,
                useCount=record.use_count,
                lastUsedAt=record.last_used_at,
                freedBytes=record.file_bytes,
            )
            trace.emit(
                'emoji_evicted',
                hash=record.content_hash,
                useCount=record.use_count,
                lastUsedAt=record.last_used_at,
                freedBytes=record.file_bytes,
            )
        return evicted

    async def select(self, emotion: str, top_k: int = 10) -> EmojiSelection | None:
        """按目标情绪取语义最相近的 top-K 并随机返回一张。

        嵌入客户端不可用或查询失败时，按逗号分隔标签执行双向包含匹配；仍无
        候选时返回 None，不强行返回无关表情包。

        :param emotion: 本轮想表达的目标情绪。
        :param top_k: 进入随机池的最大候选数，必须大于零。
        :return: 带 file:// 引用和原始 sub_type 的命中结果，或 None。
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
            # 维度必须从查询向量的实际字节数推导，不能读配置的
            # embedding_dim（float32 每分量 4 字节，与事实召回 store 侧同口径）。
            #
            # 原因：
            # 1. 配置未填 embedding_dim 时该值为 0，按配置推导会把库内全部向量
            #    当作维度不符跳过，语义排序恒为空，检索静默退化为字面子串匹配，
            #    表现为模型写出 <emoji> 却几乎不发送、且无任何报错。
            # 2. 库内残留其它维度的历史向量时，按查询长度逐条过滤只会跳过不匹配
            #    的记录，不影响其余候选。
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
        """在启动阶段逐项重算已登记表情包的 SHA-256，并巡检孤儿文件。

        「文件→库」方向的孤儿只报告数量与字节数，不删除任何文件——
        启动期校验静默删文件是灾难；删除由后台清理任务与一次性清理脚本负责。

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
        orphans = self.scan_orphans()
        logger.info(
            'emoji_integrity_verified',
            count=len(rows),
            directory=str(self._directory),
            orphanCount=len(orphans),
            orphanBytes=sum(size for _path, size in orphans),
        )
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


def _delete_emoji_file(path: Path, directory: Path) -> int:
    """删除一条表情包记录对应的磁盘文件，返回释放的字节数。

    文件删除失败不视为库操作失败：孤儿巡检会再次发现它，由清理任务兜底；
    这里的返回值只用于观察面板统计释放空间。
    """

    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(directory):
            logger.warning('emoji_file_out_of_directory', path=str(path))
            return 0
        size = resolved.stat().st_size
        resolved.unlink()
        return size
    except OSError as exc:
        logger.warning('emoji_file_remove_failed', path=str(path), error=str(exc))
        return 0


def _banned_predicate(banned_only: bool | None) -> str:
    """按封禁筛选生成 WHERE 子句片段，供 emoji 表的查询拼接。

    返回的是固定字面量，不含任何调用方数据，拼进 SQL 文本没有注入面；
    参数化做不到这件事——要变的是子句结构而不是值。

    哈希两侧都套 ``LOWER``：封禁表的键由 ``_normalize_hash`` 归一化过，而
    emoji 表的 hash 是入库时原样写的，直接比较会漏掉大小写不同的行。表只有
    千级，放弃索引换取判定正确是合理的取舍。

    :param banned_only: ``True`` 只要已封禁、``False`` 只要未封禁、``None``
        不筛选。
    :return: 可直接放在 ``WHERE`` 之后的布尔表达式。
    """

    if banned_only is None:
        return '1 = 1'
    op = 'IN' if banned_only else 'NOT IN'
    return f'LOWER(hash) {op} (SELECT LOWER(hash) FROM emoji_banned)'


def _normalize_hash(content_hash: str) -> str:
    """规范化并校验一个 64 位十六进制内容哈希。"""

    normalized = content_hash.strip().lower()
    if re.fullmatch(r'[0-9a-f]{64}', normalized) is None:
        raise ValueError(f'表情包内容哈希不合法：{content_hash!r}')
    return normalized


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


def _inspect_import_candidate(
    path: Path,
    root: Path,
    max_bytes: int,
) -> _EmojiImportCandidate:
    """在不调用模型的前提下解析一张素材的格式和 SHA-256。"""

    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError('图片路径越出表情包目录')

    image_bytes = resolved.read_bytes()
    if not image_bytes:
        raise ValueError('图片内容为空')
    if max_bytes and len(image_bytes) > max_bytes:
        raise ValueError(f'图片超过 {max_bytes} 字节上限')
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

    query_parts = [part.strip() for part in re.split(r'[,，、;； ]+', query) if part.strip()]
    tag_parts = [part.strip() for part in re.split(r'[,，、;； ]+', tags) if part.strip()]
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
