"""按消息编号与路径逐层浏览合并转发内容的内置工具插件。

本模块承载工具本体与两类组件声明：``@tool`` 装饰的方法同时给出模型可见声明与
执行体；``@inbound_observe`` 装饰的观察组件在入站落库之后把完整转发树写入
有界会话缓存。``stream_capabilities`` 按会话贡献 ``forward_message`` 能力名，
让工具声明按会话收窄——它不是组件，是能力贡献。除这些挂载点外不依赖聊天服务，
宿主经清单加载本插件后把收集到的工具登记进 ToolRegistry。

工具缓存只保存当前进程已经接收的完整转发树，以 ``(stream_id, message_id)``
隔离会话；模型每次只得到选中层的节点和下一层路径。这样既能访问任意深度，
也不会因为一条大型转发在首轮就挤满上下文。

转发里的图片与视频默认只显示占位。两个配置开关分别打开识图与看视频：收到转发
时就在后台为整棵树所有层的图片发起识别，视频按与普通聊天视频相同的观看范围
收到即看；工具读取时只等这些结果，范围外的视频和收到时没能发起的项在读取时
才发起并等待。识别经宿主注入的 ``ctx.media`` 调用与入站正文同一套服务，每项的
超时由宿主服务自己管，插件不设数量上限、不设并发、不另外计时。一项媒体的文本
在它第一次出现于工具输出之前写定、此后不再变化，同一组参数的重读与按 offset
续读因此拿到同一份渲染文本。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pydantic import Field
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple, cast

import asyncio
import json
import math
import re

from src.plugin_system import (
    ForwardMessagePart,
    ForwardMessageTree,
    InboundMessage,
    PluginConfig,
    PluginManifest,
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolPlugin,
    ToolSpec,
    inbound_observe,
    tool,
)


DEFAULT_FORWARD_CACHE_LIMIT = 128
DEFAULT_FORWARD_OBSERVATION_MAX_CHARS = 16000
MIN_FORWARD_OBSERVATION_MAX_CHARS = 128

# 媒体片段在本条目里的识别身份：(类型, 键)。视频与宿主服务同口径按内容文件名
# 去重，缺文件名时退回直链；图片按来源。同一媒体在树里出现多次只识别一次。
MediaKey = Tuple[str, str]


class ForwardMessageConfig(PluginConfig):
    """两个媒体识别开关，默认都关、保持只显示占位；启用开关由 PluginConfig 提供。

    图片与视频分成两个开关：视频走全模态模型，单个就比识图贵得多，用户可能只开其一。
    """

    read_images: bool = Field(
        default=False,
        title='识别转发里的图片',
        description=(
            '打开后，收到合并转发就在后台识别其中所有层的图片，不论之后是否读取；'
            '每张图片调用一次视觉模型，大的转发会同时发出很多请求，可能被模型服务商限流；'
            '需要主配置同时开启聊天图片识别（vision.chat_image_enabled）'
        ),
    )
    read_videos: bool = Field(
        default=False,
        title='观看转发里的视频',
        description=(
            '打开后，转发里的视频按与普通聊天视频相同的观看范围（vision.chat_video_scope）'
            '在收到时观看，范围外的在读取时观看；每个视频调用一次全模态模型，'
            '费用远高于识图、可能耗时数十秒；需要主配置同时开启聊天视频理解'
            '（vision.chat_video_enabled）并为 video 任务配置模型'
        ),
    )


# 下面两个结构不用 dataclass：本文件可能按路径直接执行、不登记进 sys.modules
# （回归用例就是这样加载的），而推迟求值的注解会让 dataclass 回查所在模块时拿到
# None 并在导入期报错。


class _CachedForward:
    """一条入站消息的转发根树，以及本条目里每项媒体的识别任务与写定文本。

    ``tasks`` 是每项媒体当前的识别任务：收到时发起的，或读取时发起的。``texts``
    是已经写定的文本：成功是与正文同格式的替换文本，失败、被取消或当时不可用是
    ``None``（保留占位）。一项媒体在第一次出现于工具输出之前写定，之后不再改变；
    渲染只读树与 ``texts``，同一组读取参数因此永远得到同一份文本。写定之后才
    结束的任务（只在并发读取交错时出现）结果只进宿主服务的缓存，不改写这里。
    """

    __slots__ = ('trees', 'tasks', 'reissued', 'texts')

    def __init__(self, trees: Tuple[ForwardMessageTree, ...]) -> None:
        """保存根树，识别任务与写定文本从空开始。"""
        self.trees = trees
        self.tasks: Dict[MediaKey, asyncio.Task[Optional[str]]] = {}
        # 读取时已经发起过识别的项。每项在读取时最多发起一次：补发收到时没有发起的，
        # 或重试收到时已经失败的；读取时发起的这一次再失败就写定为占位。
        self.reissued: Set[MediaKey] = set()
        self.texts: Dict[MediaKey, Optional[str]] = {}


class _MediaView:
    """一次渲染对媒体片段的处置：哪些类型参与识别，以及媒体片段显示成什么。

    不识别时显示占位原文；识别时由调用方给出按写定文本取文本的函数。两遍
    渲染（先收集名单、再出正文）共用同一个遍历，名单顺序与正文顺序不会分叉。
    """

    __slots__ = ('kinds', 'text')

    def __init__(
        self,
        kinds: FrozenSet[str],
        text: Callable[[ForwardMessagePart], str],
    ) -> None:
        """绑定参与识别的类型与媒体片段的取文本函数。"""
        self.kinds = kinds
        self.text = text


# 开关全关时的处置：媒体片段与改动前的文本占位逐字相同。
_PLACEHOLDER_VIEW = _MediaView(frozenset(), lambda part: part.text)


class ForwardMessagePlugin(ToolPlugin):
    """有界保存入站转发树，并按路径执行只读查询。"""

    config_model = ForwardMessageConfig

    def __init__(
        self,
        manifest: PluginManifest,
        cache_limit: int = DEFAULT_FORWARD_CACHE_LIMIT,
        observation_max_chars: int = DEFAULT_FORWARD_OBSERVATION_MAX_CHARS,
    ) -> None:
        """创建进程内缓存，并为分页正文和续读提示保留足够观察空间。

        :param manifest: 已校验的插件清单。
        :param cache_limit: 会话缓存容量上限，必须大于 0。
        :param observation_max_chars: 单次观察的字符上限，不得小于
            :data:`MIN_FORWARD_OBSERVATION_MAX_CHARS`，否则分页提示放不下。
        :raises ValueError: 容量或观察上限非法。
        """
        super().__init__(manifest)
        if cache_limit <= 0:
            raise ValueError('合并转发缓存容量必须大于 0')
        if observation_max_chars < MIN_FORWARD_OBSERVATION_MAX_CHARS:
            raise ValueError(
                '合并转发观察字符上限不能小于 '
                f'{MIN_FORWARD_OBSERVATION_MAX_CHARS}'
            )
        self._cache_limit = cache_limit
        self._observation_max_chars = observation_max_chars
        self._cache: Dict[Tuple[int, int], _CachedForward] = {}
        # 全部尚未结束的识别任务。事件循环只弱引用任务，条目被淘汰后它的任务仍要
        # 跑完，强引用由这里持有；卸载时据此全部取消。
        self._tasks: Set[asyncio.Task[Optional[str]]] = set()

    async def on_load(self) -> None:
        """开关打开但宿主当前不能识别时，启动期就说出来，而不是等第一次读取。"""
        config = cast(ForwardMessageConfig, self.config)
        if config.read_images and not self.ctx.media.images_available():
            self.ctx.logger.warning(
                '合并转发识图开关已打开，但宿主当前不能识别图片：'
                '需要开启主配置 vision.chat_image_enabled 并配置可用的视觉模型',
            )
        if config.read_videos and not self.ctx.media.videos_available():
            self.ctx.logger.warning(
                '合并转发看视频开关已打开，但宿主当前不能理解视频：'
                '需要开启主配置 vision.chat_video_enabled 并为 video 任务配置模型',
            )

    async def on_unload(self) -> None:
        """取消全部仍在跑的识别任务并等它们结束；幂等，重复调用只是空操作。"""
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        # 等取消落定：任务里还有宿主服务关闭模型流与下载连接的收尾，卸载返回后
        # 不应再有本插件发起的请求在跑。
        await asyncio.gather(*pending, return_exceptions=True)

    def tools(self) -> List[Tuple[ToolSpec, Any]]:
        """收集工具声明；开关打开时补充模型可见的识别说明，并按宿主的识别上界放宽执行超时。

        开关全关时原样返回装饰器给出的声明，与引入开关之前逐字段相同。

        执行层按声明的 timeout_ms 用 wait_for 截断工具，沿用原声明会在等识别途中
        取消整次调用，模型只拿到一句超时，连已经识别好的内容也看不到。因此执行
        超时 = 工具本来声明的超时（留给渲染与分页）+ 宿主报告的单项最长等待。
        读取时等的任务要么收到时就已发起、比本次调用开始得早，要么在本次调用开始
        时才发起，每一项都由宿主的截止约束，所以全部等完不会超过单项上界。宿主的
        截止在服务构造时定下、配置热重载不重算，插件配置改动又要重启才生效，所以
        在这里算一次就与运行期的实际上界对得上。

        :return: ``(声明, 执行器)`` 列表。
        """
        collected = super().tools()
        config = cast(ForwardMessageConfig, self.config)
        if not (config.read_images or config.read_videos):
            return collected
        note = _media_tool_description(config.read_images, config.read_videos)
        wait_ms = math.ceil(
            self.ctx.media.max_wait_s(images=config.read_images, videos=config.read_videos)
            * 1000
        )
        return [
            (
                replace(
                    spec,
                    description=f'{spec.description}{note}',
                    timeout_ms=spec.timeout_ms + wait_ms,
                ),
                executor,
            )
            for spec, executor in collected
        ]

    @tool(
        name='read_forward_message',
        description=(
            '当聊天记录里出现 [转发消息：发送者：内容预览｜共 N 条] 形态的占位时，'
            '用该行的内部消息编号逐层读取完整内容；[转发消息：内容读取失败] '
            '表示该转发不可读取，无需调用；默认读取一层，返回的 path 可定位子树；'
            '嵌套很深时可设置 depth 在一次调用里展开多层；结果出现 next_offset 时，'
            '保持其他参数不变并传入 offset 可继续读取，避免深层或超长单层内容被截断。'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'message_id': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': '聊天记录里的内部消息编号。',
                },
                'path': {
                    'type': 'array',
                    'items': {'type': 'integer', 'minimum': 0},
                    'description': '上一次结果给出的嵌套路径；首次读取时省略。',
                },
                'depth': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': '本次从选中位置向下展开的层数，默认 1。',
                },
                'offset': {
                    'type': 'integer',
                    'minimum': 0,
                    'description': '结果分页字符游标，首次读取时省略或设为 0。',
                },
            },
            'required': ['message_id'],
            'additionalProperties': False,
        },
        capabilities=('forward_message',),
    )
    async def read_forward_message(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """读取根层或路径指定层，并返回下一层的稳定路径。"""
        try:
            message_id, path, depth, offset = _parse_arguments(invocation.arguments)
        except ValueError as exc:
            return _failure(invocation.tool_name, str(exc))

        # 回合帧是本轮模型可见消息的时间边界。入站处理仍可能在模型思考期间
        # 缓存更新消息，若只按 stream_id 查缓存，模型猜中后续编号就能越过水位
        # 读取尚未进入当前快照的内容。
        if message_id > context.frame.message_watermark:
            return _failure(
                invocation.tool_name,
                f'合并转发消息 {message_id} 晚于当前回合消息水位 '
                f'{context.frame.message_watermark}',
            )

        entry = self._cache.get((context.stream_id, message_id))
        if entry is None:
            # 工具声明按会话给出（见 stream_capabilities），可读性却是按消息的：
            # 同一个会话里有的转发解析成功进了缓存，有的因协议端超时或结构损坏
            # 没进。模型只能看到正文里清一色的 [转发消息] 占位，分不出哪条能读，
            # 因此失败信息必须把可读的编号一并给出，否则它只能继续猜。
            return _failure(
                invocation.tool_name,
                f'当前会话中没有可读取的合并转发消息 {message_id}'
                f'{self._readable_hint(context.stream_id, context.frame.message_watermark)}',
            )
        try:
            selected = _select_trees(entry.trees, path)
        except ValueError as exc:
            return _failure(invocation.tool_name, str(exc))

        kinds = _recognized_kinds(cast(ForwardMessageConfig, self.config))
        if kinds:
            rendered = await self._render_with_media(
                message_id, entry, selected, depth, kinds,
            )
        else:
            rendered = _render_selection(message_id, selected, depth, _PLACEHOLDER_VIEW)
        try:
            observation, next_offset = _page_observation(
                rendered,
                offset,
                self._observation_max_chars,
            )
        except ValueError as exc:
            return _failure(invocation.tool_name, str(exc))
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation=observation,
            metadata={
                'messageId': message_id,
                'path': path,
                'depth': depth,
                'offset': offset,
                'nextOffset': next_offset,
            },
        )

    @inbound_observe()
    def observe_inbound(
        self,
        stream_id: int,
        message_id: int,
        inbound: InboundMessage,
    ) -> None:
        """把一条已落库入站消息的转发根树写入有界会话缓存，开关打开时在后台发起识别。

        入站消息不含转发根树时本组件没有任何效果；容量超限时按最旧条目
        淘汰。宿主在入站处理的协程里落库后直接同步调用观察组件，本方法因此跑在
        事件循环线程上，可以就地创建识别任务、不等它们。
        """
        entry = self._remember(stream_id, message_id, inbound.forward_messages)
        if entry is None:
            return
        kinds = _recognized_kinds(cast(ForwardMessageConfig, self.config))
        if kinds:
            self._recognize_on_arrival(entry, message_id, inbound, kinds)

    def stream_capabilities(self, stream_id: int) -> FrozenSet[str]:
        """只在缓存过转发树的会话贡献 ``forward_message``，其余会话为空。

        能力跟随缓存而不是协议端静态声明：没有可读内容的会话里，工具声明
        出现只会诱导模型调用后必败。
        """
        if self._has_stream(stream_id):
            return frozenset({'forward_message'})
        return frozenset()

    def _readable_hint(self, stream_id: int, watermark: int) -> str:
        """列出该会话当前可读的合并转发编号，供失败信息给出可用取值。

        :param stream_id: 会话编号。
        :param watermark: 本回合消息水位；晚于水位的缓存不列出，否则模型能从
            失败信息里得知尚未进入本轮快照的消息存在。
        :return: 以分号起头的中文补充说明；没有可读内容时说明这一事实。
        """
        readable = sorted(
            cached_message_id
            for cached_stream_id, cached_message_id in self._cache
            if cached_stream_id == stream_id and cached_message_id <= watermark
        )
        if not readable:
            return '；本会话目前没有任何可读取的合并转发'
        listed = '、'.join(str(message_id) for message_id in readable)
        return f'；本会话可读取的是 {listed}'

    def _remember(
        self,
        stream_id: int,
        message_id: int,
        trees: Tuple[ForwardMessageTree, ...],
    ) -> Optional[_CachedForward]:
        """把一条已落库入站消息的转发根树写入有界会话缓存。

        被淘汰的条目里还在跑的识别任务不取消，让它们跑完、结果进宿主服务的缓存。
        请求已经发出、费用已经产生，取消省不下多少；更要紧的是宿主对同一内容的
        在途识别让后来者直接等共享任务，取消这边会把别的条目（或入站正文）正在等的
        同一张图、同一个视频一起取消。淘汰时还有任务在跑，意味着一项识别的截止
        之内又进来了上百条转发，这种情况本身就少见，跑完的代价也被宿主的截止封顶。

        :param stream_id: 会话编号，必须大于 0。
        :param message_id: 内部消息编号，必须大于 0。
        :param trees: 转发根树；为空时不写入。
        :return: 新写入的条目；没有转发根树时为 ``None``。
        :raises ValueError: 编号非法。
        """
        if stream_id <= 0:
            raise ValueError('合并转发缓存的 stream_id 必须大于 0')
        if message_id <= 0:
            raise ValueError('合并转发缓存的 message_id 必须大于 0')
        if not trees:
            return None
        key = (stream_id, message_id)
        entry = _CachedForward(trees)
        # 重写同一键时先删除，确保它重新成为最新条目。
        self._cache.pop(key, None)
        self._cache[key] = entry
        while len(self._cache) > self._cache_limit:
            self._cache.pop(next(iter(self._cache)))
        return entry

    def _has_stream(self, stream_id: int) -> bool:
        """判断当前会话是否至少缓存过一条可读合并转发。"""
        return any(key_stream_id == stream_id for key_stream_id, _ in self._cache)

    def _recognize_on_arrival(
        self,
        entry: _CachedForward,
        message_id: int,
        inbound: InboundMessage,
        kinds: FrozenSet[str],
    ) -> None:
        """收到转发时为整棵树所有层的媒体在后台发起识别，不等结果。

        图片全部发起。视频只发起顶层视频收到时也会看的那部分：范围是 ``all``，
        或这条消息跟她有关，判定由宿主经 ``ctx.media`` 给出，与入站正文同口径。
        范围外的视频、宿主当时不能识别的类型都不发起，留到读取时再发起。

        :param entry: 刚写入缓存的条目。
        :param message_id: 内部消息编号，用于日志定位。
        :param inbound: 入站消息，视频范围按它判定。
        :param kinds: 开关打开的媒体类型。
        :return: ``None``。
        :raises RuntimeError: 不在事件循环线程上调用。
        """
        # 先确认身处事件循环再创建任何识别协程：脱离事件循环时当场报错，而不是
        # 静默地不识别，也不留下从未等待的协程。
        asyncio.get_running_loop()
        media = self.ctx.media
        start = {
            'image': 'image' in kinds and media.images_available(),
            'video': (
                'video' in kinds
                and media.videos_available()
                and media.videos_watched_on_arrival(inbound)
            ),
        }
        started = {'image': 0, 'video': 0}
        for part in _tree_media(entry.trees):
            if not start[part.kind] or _media_key(part) in entry.tasks:
                continue
            self._start_recognition(entry, part)
            started[part.kind] += 1
        if started['image'] or started['video']:
            self.ctx.logger.info(
                '收到合并转发，已在后台发起识别',
                messageId=message_id,
                images=started['image'],
                videos=started['video'],
            )

    def _start_recognition(
        self,
        entry: _CachedForward,
        part: ForwardMessagePart,
    ) -> asyncio.Task[Optional[str]]:
        """为一项媒体发起识别任务，登记进条目与插件的任务集合。

        识别放在插件自己创建的任务里，读取方只等这个任务、从不直接等宿主入口：
        宿主的在途去重让同一内容的后来者直接等共享任务，等待方被取消会连带取消它。

        :param entry: 媒体所在的缓存条目；同一媒体已有的任务被新任务替换。
        :param part: 图片或视频片段。
        :return: 新建的识别任务。
        """
        media = self.ctx.media
        if part.kind == 'image':
            recognition = media.describe_image(part.source)
        else:
            recognition = media.describe_video(part.source, part.file)
        task = asyncio.create_task(recognition)
        entry.tasks[_media_key(part)] = task
        self._tasks.add(task)
        task.add_done_callback(self._finish_task)
        return task

    def _finish_task(self, task: asyncio.Task[Optional[str]]) -> None:
        """识别任务结束时释放引用；入口抛出的异常是缺陷，记 error 而不是吞掉。

        读取方等到这个任务时还会再抛出同一个异常；这里另记一次，是为了没人读取的
        转发里的缺陷也能被看见。
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.ctx.logger.error(
                '合并转发识别任务异常结束',
                error=f'{type(exc).__name__}：{exc}',
            )

    async def _render_with_media(
        self,
        message_id: int,
        entry: _CachedForward,
        selected: List[Tuple[Tuple[int, ...], ForwardMessageTree]],
        depth: int,
        kinds: FrozenSet[str],
    ) -> str:
        """等本次渲染范围内的媒体识别结束、写定文本后出正文。

        先用渲染遍历按正文顺序收集范围内的媒体，尚未写定文本的项由
        :meth:`_pending_media` 定下要等的任务，全部结束后按结果写定，再出正文。
        一项媒体的文本在它第一次出现于输出之前写定、之后不变，所以同一组参数的
        重读与按 offset 续读拿到同一份文本。

        等待用 :func:`asyncio.wait`，它在调用方被取消时不取消被等的任务：回复途中
        被打断会取消整个回合、连带取消本次工具调用，此时识别任务照常跑完，下一次
        读取直接拿到结果，不必重新请求。

        :param message_id: 内部消息编号，用于日志定位。
        :param entry: 该消息的缓存条目，写定的文本写回它的 ``texts``。
        :param selected: 已按 path 选中的子树。
        :param depth: 本次展开层数。
        :param kinds: 开关打开的媒体类型。
        :return: 完整渲染文本，分页由调用方负责。
        :raises Exception: 宿主识别入口本身抛出的异常原样上抛——识别失败由入口
            以 ``None`` 表达，抛出的只能是缺陷，不能被改写成「保留占位」。
        """
        distinct = _distinct_media(_media_in_render_order(message_id, selected, depth, kinds))
        waiting = self._pending_media(entry, distinct, message_id)
        if waiting:
            await asyncio.wait({task for _ordinal, _part, task in waiting})
        for ordinal, part, task in waiting:
            key = _media_key(part)
            # 并发读取可能已经先写定了同一项，以先写定的为准，两边输出一致。
            if key not in entry.texts:
                entry.texts[key] = self._outcome(task, part, ordinal, message_id)

        def settled_text(part: ForwardMessagePart) -> str:
            # 范围内的项刚刚全部写定，取不到说明写定流程有缺口，直接报错。
            settled = entry.texts[_media_key(part)]
            return part.text if settled is None else settled

        return _render_selection(
            message_id, selected, depth, _MediaView(kinds, settled_text),
        )

    def _pending_media(
        self,
        entry: _CachedForward,
        distinct: List[Tuple[int, ForwardMessagePart]],
        message_id: int,
    ) -> List[Tuple[int, ForwardMessagePart, asyncio.Task[Optional[str]]]]:
        """为范围内尚未写定的媒体定下要等的任务，需要时现在发起。

        已有任务且没有失败（还在跑或已经成功）的直接等它；从没发起过的（收到时
        不在视频范围，或当时开关、宿主不可用）现在发起；收到时就已经失败的现在
        重试一次——大的转发收到时同时发出很多请求，失败多半是被限流，读取时只剩
        这几项，值得再试。重试只针对读取开始时已经失败的项：还在跑的项若等它失败
        再重试，等待就会超过宿主的单项上界，工具执行超时也就对不上了。

        宿主当前不能识别某类型时，该类型从没发起过的项直接写定为占位。

        :param entry: 缓存条目；新发起的任务登记进它。
        :param distinct: 本次渲染范围里去重后的媒体及其出现次序。
        :param message_id: 内部消息编号，用于日志定位。
        :return: ``(出现次序, 片段, 要等的任务)``，按首次出现顺序排列。
        """
        media = self.ctx.media
        available = {
            'image': media.images_available(),
            'video': media.videos_available(),
        }
        waiting: List[Tuple[int, ForwardMessagePart, asyncio.Task[Optional[str]]]] = []
        unavailable = {'image': 0, 'video': 0}
        for ordinal, part in distinct:
            key = _media_key(part)
            if key in entry.texts:
                continue
            task = entry.tasks.get(key)
            if key not in entry.reissued and (task is None or _failed(task)):
                if available[part.kind]:
                    task = self._start_recognition(entry, part)
                    entry.reissued.add(key)
                elif task is None:
                    # 不可用也写定为占位：读取中途宿主开关被热重载打开时，已经给出的
                    # 页面不能因为后面几项突然识别成功而改写。
                    entry.texts[key] = None
                    unavailable[part.kind] += 1
                    continue
            assert task is not None
            waiting.append((ordinal, part, task))
        for kind, count in unavailable.items():
            if not count:
                continue
            self.ctx.logger.warning(
                f'合并转发{_kind_label(kind)}识别当前不可用，本次涉及的项保持占位',
                messageId=message_id,
                count=count,
                reason=(
                    '主配置未开启聊天图片识别或视觉模型不可用'
                    if kind == 'image'
                    else '主配置未开启聊天视频理解或 video 任务没有模型'
                ),
            )
        return waiting

    def _outcome(
        self,
        task: asyncio.Task[Optional[str]],
        part: ForwardMessagePart,
        ordinal: int,
        message_id: int,
    ) -> Optional[str]:
        """取一个已结束识别任务的结果，失败与被取消都记 warning 并按占位处理。

        :param task: 已结束的识别任务。
        :param part: 对应的媒体片段。
        :param ordinal: 该类型在本次渲染范围里的出现次序，只用于日志定位。
        :param message_id: 内部消息编号，用于日志定位。
        :return: 替换文本；``None`` 表示保留占位。
        :raises Exception: 宿主识别入口本身抛出的异常原样上抛。
        """
        # 日志不记来源：图片与视频直链都带一次性签名，有效期内谁拿到都能下载；
        # 视频只记内容文件名，图片记本次渲染范围里的出现次序。
        fields: Dict[str, Any] = {'messageId': message_id, 'ordinal': ordinal}
        if part.kind == 'video':
            fields['file'] = part.file
        if task.cancelled():
            # 本插件只在卸载时取消自己的任务；读取时见到被取消的任务，是宿主侧的
            # 共享任务被别的等待方取消、连带这边落空，结果已经拿不到。
            self.ctx.logger.warning(
                f'合并转发{_kind_label(part.kind)}识别任务已被取消，保留占位',
                **fields,
            )
            return None
        text = task.result()
        if text is None:
            self.ctx.logger.warning(
                f'合并转发{_kind_label(part.kind)}识别失败，保留占位；原因见宿主识别服务日志',
                **fields,
            )
        return text


def _parse_arguments(
    arguments: Dict[str, Any],
) -> Tuple[int, Tuple[int, ...], int, int]:
    """校验直接调用场景的参数；模型链路还会在注册表提前校验一次。"""
    unknown = sorted(
        str(key)
        for key in arguments
        if key not in {'message_id', 'path', 'depth', 'offset'}
    )
    if unknown:
        raise ValueError(f'合并转发工具包含未知字段：{", ".join(unknown)}')
    message_id = arguments.get('message_id')
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
    ):
        raise ValueError('message_id 必须是大于 0 的整数')
    raw_path = arguments.get('path', [])
    if not isinstance(raw_path, list):
        raise ValueError('path 必须是非负整数数组')
    if any(
        not isinstance(index, int) or isinstance(index, bool) or index < 0
        for index in raw_path
    ):
        raise ValueError('path 必须是非负整数数组')
    depth = arguments.get('depth', 1)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0:
        raise ValueError('depth 必须是大于 0 的整数')
    offset = arguments.get('offset', 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError('offset 必须是非负整数')
    return message_id, tuple(raw_path), depth, offset


def _page_observation(
    rendered: str,
    offset: int,
    page_max_chars: int,
) -> Tuple[str, int | None]:
    """按稳定字符游标分页，使超长平层或单片段的尾部仍然可达。"""
    if offset >= len(rendered):
        raise ValueError(
            f'offset {offset} 已到达或超出合并转发内容末尾'
            f'（总字符数 {len(rendered)}）'
        )
    prefix = f'[合并转发分页，offset={offset}]\n' if offset else ''
    remaining = rendered[offset:]
    if len(prefix) + len(remaining) <= page_max_chars:
        return f'{prefix}{remaining}', None

    # 续读提示也是模型可见观察的一部分，必须从正文预算中预留。用总长度的
    # 位数估算最宽游标，可避免页尾跨十进制位数时预算来回振荡。
    content_limit = page_max_chars - len(prefix) - len(
        _continuation_hint(len(rendered))
    )
    if content_limit <= 0:
        raise ValueError('合并转发观察字符上限过小，无法容纳分页提示')
    page_end = offset + content_limit
    page = rendered[offset:page_end]
    suffix = _continuation_hint(page_end)
    return f'{prefix}{page}{suffix}', page_end


def _continuation_hint(next_offset: int) -> str:
    """生成模型可见的稳定续读游标说明。"""
    return (
        f'\n[内容未完；next_offset={next_offset}。请保持 message_id、path、depth '
        f'不变，并在下一次调用中设置 offset={next_offset}]'
    )


def _select_trees(
    roots: Tuple[ForwardMessageTree, ...],
    path: Tuple[int, ...],
) -> List[Tuple[Tuple[int, ...], ForwardMessageTree]]:
    """沿路径选择一层；空路径返回全部根树。"""
    if not path:
        return [((index,), tree) for index, tree in enumerate(roots)]

    candidates = roots
    selected: ForwardMessageTree | None = None
    for depth, index in enumerate(path):
        if index >= len(candidates):
            raise ValueError(
                f'合并转发路径无效：第 {depth + 1} 级索引 {index} 超出范围'
            )
        selected = candidates[index]
        candidates = _direct_nested_trees(selected)
    assert selected is not None
    return [(path, selected)]


def _direct_nested_trees(tree: ForwardMessageTree) -> Tuple[ForwardMessageTree, ...]:
    """按节点和片段原序收集当前层可继续展开的直接子树。"""
    nested_trees: List[ForwardMessageTree] = []
    for node in tree.nodes:
        for part in node.parts:
            if part.kind == 'forward' and part.nested is not None:
                nested_trees.append(part.nested)
    return tuple(nested_trees)


# 整段都是方括号占位的片段判为非文本内容。用结构判据而不是枚举
# ``[图片]`` / ``[表情包]`` 这些具体词：占位符词表由平台适配器维护，
# 在这里抄一份会让两边各自演进后悄悄对不上。
_PLACEHOLDER_PATTERN = re.compile(r'\[[^\[\]]*\]')


def _is_placeholder(text: str) -> bool:
    """判断一个文本片段是否整段都是方括号占位。"""
    return _PLACEHOLDER_PATTERN.fullmatch(text) is not None


def _is_textless_layer(tree: ForwardMessageTree, kinds: FrozenSet[str]) -> bool:
    """判断一层是否只有非文本内容，且没有可继续展开的子树。

    含嵌套子树的层不算：它下面还有内容，模型仍有展开的理由。开关打开时，
    含该类型媒体的层也不算：图片与视频会被识别成描述，不再是没有信息的占位；
    开关关闭的媒体仍按占位原文判断，与引入开关之前一致。

    :param tree: 待判断的一层。
    :param kinds: 开关打开的媒体类型。
    :return: 全部片段都是方括号占位时为 ``True``。
    """
    for node in tree.nodes:
        for part in node.parts:
            if part.kind == 'forward' or part.kind in kinds:
                return False
            if not _is_placeholder(part.text):
                return False
    return bool(tree.nodes)


def _textless_layer_summary(tree: ForwardMessageTree) -> str:
    """把一层纯非文本内容概括成一行，按出现顺序给出各类占位的条数。

    逐条列出十几行一模一样的 ``[表情包]`` 对模型没有任何信息量，却要占满观察
    预算；真机上出现过为此白花两个认知轮次的情况。概括保留「有多少、都是什么」
    这两件有用的事，丢掉的只是「第几条是谁发的」——在全是占位的层里那不构成信息。
    """
    counts = Counter(
        part.text for node in tree.nodes for part in node.parts
    )
    kinds = '、'.join(f'{text}×{count}' for text, count in counts.items())
    return f'（这一层 {len(tree.nodes)} 条，全部为非文本内容：{kinds}）'


def _nested_hint(tree: ForwardMessageTree, kinds: FrozenSet[str]) -> str:
    """给未展开的嵌套子树附一句规模说明，供模型决定要不要往下钻。

    只给 path 时，模型无从判断展开值不值：真机上它展开一层，拿回十几行
    ``[表情包]``，两个认知轮次白花。条数与「有没有文本」在展开前就能算出来。
    开关打开时再报出这一层直接含有的图片、视频个数：模型据此知道下面有可看的
    内容，识别还没结束时展开它要等一会儿。说明只依赖树本身、不依赖识别进度，
    这一行在识别前后保持不变。

    :param tree: 未展开的嵌套层。
    :param kinds: 开关打开的媒体类型。
    :return: 以中文逗号起头的说明。
    """
    if _is_textless_layer(tree, kinds):
        return f'，{len(tree.nodes)} 条，无文本'
    counts = Counter(
        part.kind for node in tree.nodes for part in node.parts if part.kind in kinds
    )
    media = '、'.join(
        f'{counts[kind]} {_kind_unit(kind)}{_kind_label(kind)}'
        for kind in ('image', 'video')
        if counts[kind]
    )
    return f'，{len(tree.nodes)} 条，含 {media}' if media else f'，{len(tree.nodes)} 条'


def _render_selection(
    message_id: int,
    selected: List[Tuple[Tuple[int, ...], ForwardMessageTree]],
    depth: int,
    view: _MediaView,
) -> str:
    """展示选中位置以下指定层数，并为尚未展开的子树保留可复制路径。

    :param message_id: 内部消息编号，写进首行。
    :param selected: 已按 path 选中的子树及其路径。
    :param depth: 本次展开层数。
    :param view: 媒体片段的处置；开关全关时是 :data:`_PLACEHOLDER_VIEW`。
    :return: 完整渲染文本。
    """
    lines = [f'[合并转发消息 {message_id}，展开深度 {depth}]']
    for root_index, (base_path, tree) in enumerate(selected):
        if len(selected) > 1:
            lines.append(f'\n根转发 {root_index + 1}，path={_path_text(base_path)}')
        _render_tree(lines, tree, base_path, depth, '', view)
    if any(_direct_nested_trees(tree) for _, tree in selected):
        lines.append(
            '\n可继续使用结果中的 path 定位子树，或增大 depth 一次展开更多层。'
        )
    return '\n'.join(lines)


def _render_tree(
    lines: List[str],
    tree: ForwardMessageTree,
    base_path: Tuple[int, ...],
    remaining_depth: int,
    indent: str,
    view: _MediaView,
) -> None:
    """深度优先渲染一棵树；每层先保留原片段顺序，再展开直接子树。"""
    if _is_textless_layer(tree, view.kinds):
        lines.append(f'{indent}{_textless_layer_summary(tree)}')
        return
    nested: List[Tuple[Tuple[int, ...], ForwardMessageTree]] = []
    nested_index = 0
    for node in tree.nodes:
        content: List[str] = []
        for part in node.parts:
            if part.kind == 'text':
                content.append(part.text)
                continue
            if part.kind != 'forward':
                # 图片与视频：开关关闭时就是占位原文，打开时由写定的识别文本决定。
                content.append(view.text(part) if part.kind in view.kinds else part.text)
                continue
            assert part.nested is not None
            nested_path = (*base_path, nested_index)
            if remaining_depth > 1:
                content.append(
                    f'[嵌套合并转发 #{nested_index + 1}，已在下方展开]'
                )
            else:
                content.append(
                    f'[嵌套合并转发{_nested_hint(part.nested, view.kinds)}，'
                    f'path={_path_text(nested_path)}]'
                )
            nested.append((nested_path, part.nested))
            nested_index += 1
        lines.append(f'{indent}【{node.sender_name}】：{"".join(content)}')
    if remaining_depth <= 1:
        return
    for nested_number, (nested_path, nested_tree) in enumerate(nested, start=1):
        lines.append(f'{indent}  [展开嵌套 #{nested_number}]')
        _render_tree(
            lines,
            nested_tree,
            nested_path,
            remaining_depth - 1,
            f'{indent}  ',
            view,
        )


def _path_text(path: Tuple[int, ...]) -> str:
    """把路径渲染为稳定 JSON 数组，便于模型原样复用。"""
    return json.dumps(list(path), ensure_ascii=False)


def _recognized_kinds(config: ForwardMessageConfig) -> FrozenSet[str]:
    """把两个开关折成本次要识别的媒体类型集合；全关时为空集合。"""
    kinds: Set[str] = set()
    if config.read_images:
        kinds.add('image')
    if config.read_videos:
        kinds.add('video')
    return frozenset(kinds)


def _media_key(part: ForwardMessagePart) -> MediaKey:
    """给出媒体片段在缓存条目里的识别身份，见 :data:`MediaKey`。"""
    return (part.kind, part.file.strip() or part.source)


def _failed(task: asyncio.Task[Optional[str]]) -> bool:
    """判断识别任务是否已经结束且没有结果：返回 ``None`` 或被取消。

    抛异常的不算失败：那是宿主入口的缺陷，重试只会把它藏起来，要留给读取方原样上抛。
    """
    if not task.done():
        return False
    if task.cancelled():
        return True
    return task.exception() is None and task.result() is None


def _kind_label(kind: str) -> str:
    """媒体类型的中文名。"""
    return '图片' if kind == 'image' else '视频'


def _kind_unit(kind: str) -> str:
    """媒体类型的中文量词。"""
    return '张' if kind == 'image' else '个'


def _tree_media(trees: Tuple[ForwardMessageTree, ...]) -> Iterator[ForwardMessagePart]:
    """深度优先给出整棵树所有层里的图片与视频片段，同一媒体出现几次就给出几次。"""
    for tree in trees:
        for node in tree.nodes:
            for part in node.parts:
                if part.kind == 'forward':
                    assert part.nested is not None
                    yield from _tree_media((part.nested,))
                elif part.kind in ('image', 'video'):
                    yield part


def _media_in_render_order(
    message_id: int,
    selected: List[Tuple[Tuple[int, ...], ForwardMessageTree]],
    depth: int,
    kinds: FrozenSet[str],
) -> List[ForwardMessagePart]:
    """按正文顺序收集本次渲染范围里要识别的媒体片段。

    直接复用渲染遍历而不是另写一遍树走查：哪些层会被概括、哪些层会被展开，
    判据都在渲染里，另写一份迟早与它对不上，等待的范围就会和正文错开。

    :return: 媒体片段的出现序列，同一媒体出现几次就有几项。
    """
    appearances: List[ForwardMessagePart] = []

    def record(part: ForwardMessagePart) -> str:
        appearances.append(part)
        return part.text

    _render_selection(message_id, selected, depth, _MediaView(kinds, record))
    return appearances


def _distinct_media(
    appearances: List[ForwardMessagePart],
) -> List[Tuple[int, ForwardMessagePart]]:
    """按首次出现顺序去重，并给出每项在同类型里的出现次序。

    :param appearances: :func:`_media_in_render_order` 的结果。
    :return: ``(出现次序, 片段)``；次序按类型分别从 1 计数，只用于日志定位。
    """
    distinct: List[Tuple[int, ForwardMessagePart]] = []
    seen: Set[MediaKey] = set()
    counts = {'image': 0, 'video': 0}
    for part in appearances:
        key = _media_key(part)
        if key in seen:
            continue
        seen.add(key)
        counts[part.kind] += 1
        distinct.append((counts[part.kind], part))
    return distinct


def _media_tool_description(read_images: bool, read_videos: bool) -> str:
    """开关打开时追加在工具描述后的模型可见说明。

    :param read_images: 识图开关。
    :param read_videos: 看视频开关。
    :return: 以句号结尾的说明；至少一个开关打开时才会被调用。
    """
    pieces: List[str] = []
    if read_images:
        pieces.append('图片会被识别并显示为 [图片：描述]')
    if read_videos:
        pieces.append('视频会被观看并显示为 [视频：描述]')
    waiting = '识别还没完成时本次调用会等它完成'
    if read_videos:
        waiting += '，看视频可能需要数十秒'
    return f'转发里的{"，".join(pieces)}；{waiting}；识别失败的保持原占位。'


def _failure(tool_name: str, message: str) -> ToolExecutionResult:
    """构造可回灌模型的显式工具失败结果。"""
    return ToolExecutionResult(
        tool_name=tool_name,
        success=False,
        error_message=message,
    )


__all__ = [
    'DEFAULT_FORWARD_CACHE_LIMIT',
    'DEFAULT_FORWARD_OBSERVATION_MAX_CHARS',
    'MIN_FORWARD_OBSERVATION_MAX_CHARS',
    'ForwardMessageConfig',
    'ForwardMessagePlugin',
]
