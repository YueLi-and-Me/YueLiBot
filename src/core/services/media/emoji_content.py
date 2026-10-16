"""表情包画面描述的后台补写。

入库时只生成情绪标签；画面描述（角色、外观、物件、图上文字）由本模块在后台逐张
识别补写，供 ``sticker_search`` 按内容检索。启动后先补齐存量，之后每隔
:data:`CONTENT_INDEX_INTERVAL_S` 秒收一次新入库的表情包。已封禁的表情包不识别。

依赖 ``EmojiLibrary`` 的待识别查询、读文件与写入，以及
``ChatImageDescriber.describe_emoji_content`` 的视觉识别；由 ``main`` 装配并登记进
生命周期。
"""

from __future__ import annotations

from typing import Set

import asyncio
import hashlib

from .chat_image import ChatImageDescriber, _guess_media_type
from .emoji import EmojiLibrary

from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 两轮补写之间的间隔（秒）：新表情包入库后最迟这么久可以按内容检索到。
CONTENT_INDEX_INTERVAL_S = 600
# 每批并发识别张数，与目录自动登记的批大小一致。
CONTENT_INDEX_BATCH = 4


class EmojiContentIndexer:
    """在后台为缺少画面描述的表情包逐张生成描述。"""

    def __init__(
        self,
        library: EmojiLibrary,
        describer: ChatImageDescriber,
        *,
        interval_s: float = CONTENT_INDEX_INTERVAL_S,
    ) -> None:
        """绑定表情包库与视觉识别服务。

        :param library: 表情包库。
        :param describer: 提供画面描述识别的聊天图片服务。
        :param interval_s: 两轮补写之间的间隔，单位秒，必须大于 0。
        :raises ValueError: 间隔非正数。
        """
        if interval_s <= 0:
            raise ValueError('画面描述补写间隔必须大于 0')
        self._library = library
        self._describer = describer
        self._interval_s = interval_s
        # 本进程内识别失败的行不再重试，留待下次启动，避免同一张图反复请求模型。
        self._failed: Set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        """创建后台补写任务并立即返回，不等待模型调用。"""
        self._task = asyncio.create_task(self._run(), name='emoji-content-index')

    async def shutdown(self) -> None:
        """取消后台任务；已写入的描述保留，未完成的行下次继续。"""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        """循环补写；单轮异常记录后等到下一轮，不终止任务。"""
        while True:
            try:
                await self.index_pending()
            except Exception:
                logger.exception('表情包画面描述补写失败')
            await asyncio.sleep(self._interval_s)

    async def index_pending(self) -> int:
        """为当前全部待识别的表情包生成画面描述。

        :return: 成功写入的条数。
        副作用：读取表情包文件、调用视觉模型并写入 description 列；识别关闭或
            模型不可用时不做任何事。
        """
        if not self._describer.can_describe_emoji_content():
            return 0
        completed = failed = 0
        announced = False
        while True:
            rows = [
                row for row in self._library.pending_content(CONTENT_INDEX_BATCH + len(self._failed))
                if row[0] not in self._failed
            ][:CONTENT_INDEX_BATCH]
            if not rows:
                break
            if not announced:
                logger.info('表情包画面描述补写开始')
                announced = True
            results = await asyncio.gather(*(self._describe(digest, ref) for digest, ref in rows))
            for (digest, _ref), description in zip(rows, results, strict=True):
                if description is not None and self._library.set_description(digest, description):
                    completed += 1
                else:
                    self._failed.add(digest)
                    failed += 1
        if announced:
            logger.info('表情包画面描述补写完成', completed=completed, failed=failed)
        return completed

    async def _describe(self, digest: str, send_ref: str) -> str | None:
        """读取一张表情包并请求画面描述；读文件或识别失败时记录警告并返回 None。"""
        try:
            image_bytes = self._library.read_file(send_ref)
        except (OSError, ValueError) as exc:
            logger.warning('表情包画面描述跳过图片', hash=digest, error=str(exc))
            return None
        if hashlib.sha256(image_bytes).hexdigest() != digest:
            logger.warning('表情包画面描述跳过图片', hash=digest, error='文件内容与哈希不一致')
            return None
        description = await self._describer.describe_emoji_content(
            image_bytes, _guess_media_type(image_bytes), digest,
        )
        if description is None:
            logger.warning('表情包画面描述识别失败', hash=digest)
        return description


__all__ = ['CONTENT_INDEX_BATCH', 'CONTENT_INDEX_INTERVAL_S', 'EmojiContentIndexer']
