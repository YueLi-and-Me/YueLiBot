"""验证合并转发读取工具的识图与看视频开关。

两个开关默认关闭，关闭时输出与只有文本占位时逐字相同、不发起任何识别；打开后
收到转发就在后台识别所有层的图片，视频按普通视频的观看范围收到即看、范围外的
读取时再看；工具读取只等结果，被取消不连带取消识别；媒体文本第一次出现于输出前
写定，重读与续读稳定。图片与视频服务都用宿主的真实实现，只把视觉模型、全模态
模型与视频时长读取换成替身：不连网络、不读本机配置。

依赖插件目录、``src.plugin_system`` 的宿主入口与 ``src.core.services.media``。
"""

from __future__ import annotations

from pathlib import Path
from structlog.testing import capture_logs
from typing import Any, AsyncIterator, Callable, Dict, List, Tuple

import asyncio
import base64
import importlib.util

import pytest

from src.core.agent.action_protocol import (
    DecisionFrame,
    PlatformCapabilities,
    available_actions,
)
from src.core.config.schema import Config
from src.core.platform_io.forward import (
    ForwardMessagePart,
    ForwardMessageTree,
    ForwardNode,
)
from src.core.platform_io.types import (
    ConversationContext,
    InboundMessage,
    PersonRef,
    StreamRef,
)
from src.core.services.media.chat_image import IMAGE_DOWNLOAD_TIMEOUT_S, ChatImageDescriber
from src.core.services.media.chat_video import (
    ChatVideoDescriber,
    VideoDurationUnreadableError,
    watches_video_on_arrival,
)
from src.core.tooling.spec import ToolContext, ToolExecutionResult, ToolInvocation
from src.plugin_system import PluginContext, PluginManifest

# 插件目录名含连字符，不是合法 Python 包名；与契约层 loader 同法按文件路径
# 加载入口模块，模块名用插件标识派生避免同名覆盖。
_PLUGIN_DIR = (
    Path(__file__).resolve().parents[2]
    / 'src' / 'plugins' / 'built_in' / 'forward-message'
)
_spec = importlib.util.spec_from_file_location(
    'yueli_forward_message_media', _PLUGIN_DIR / 'plugin.py',
)
_plugin_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin_module)
ForwardMessagePlugin = _plugin_module.ForwardMessagePlugin
ForwardMessageConfig = _plugin_module.ForwardMessageConfig


class _ContentVision:
    """按图片内容作答的视觉模型替身：图片字节形如 ``IMG:<描述>``。

    描述取自内容而不是调用次序，并发识别时结果仍然确定；描述为空表示模型
    返回空文本，宿主服务据此判为失败。``failures`` 给出某内容开头几次调用返回
    空文本，模拟被限流后恢复。``gate`` 未放行前每次调用都挂起。
    """

    model = 'vision-test'

    def __init__(
        self,
        gate: asyncio.Event | None = None,
        failures: Dict[str, int] | None = None,
    ) -> None:
        self.calls: List[str] = []
        self._gate = gate
        self._failures = dict(failures or {})

    async def stream(
        self,
        messages: List[Dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        assert messages is not None
        data_url = messages[0]['content'][1]['image_url']['url']
        content = base64.b64decode(data_url.split(',', 1)[1]).decode('utf-8')
        self.calls.append(content)
        if self._gate is not None:
            await self._gate.wait()
        if self._failures.get(content, 0) > 0:
            self._failures[content] -= 1
            yield {'text': ''}
            return
        yield {'text': content.removeprefix('IMG:')}


class _ContentOmni:
    """按视频直链作答的全模态模型替身；``gate`` 未放行前每次调用都挂起。"""

    model = 'omni-test'

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.urls: List[str] = []
        self._gate = gate

    async def stream(
        self,
        messages: List[Dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        assert messages is not None
        url = messages[0]['content'][0]['video_url']['url']
        self.urls.append(url)
        if self._gate is not None:
            await self._gate.wait()
        yield {'text': f'画面里是{_video_name(url)}'}


def _host_config(scope: str = 'related') -> Config:
    """主配置只在内存里改：开启聊天图片识别与视频理解。"""
    config = Config()
    config.vision.chat_image_enabled = True
    config.vision.chat_video_enabled = True
    config.vision.chat_video_max_seconds = 180
    config.vision.chat_video_scope = scope
    return config


def _image_describer(vision: _ContentVision) -> ChatImageDescriber:
    return ChatImageDescriber(_host_config(), vision)


def _video_describer(
    monkeypatch: pytest.MonkeyPatch,
    omni: _ContentOmni,
    durations: Dict[str, float | None],
    scope: str = 'related',
) -> Tuple[ChatVideoDescriber, List[str]]:
    """构造真实的视频理解服务，只把读时长换成按直链查表；``None`` 表示链接已过期。"""
    describer = ChatVideoDescriber(_host_config(scope), omni)
    reads: List[str] = []

    async def read_duration(url: str) -> float:
        reads.append(url)
        seconds = durations[url]
        if seconds is None:
            raise VideoDurationUnreadableError('HTTP 400')
        return seconds

    monkeypatch.setattr(describer, '_read_duration', read_duration)
    return describer, reads


def _image_source(description: str) -> str:
    return 'base64://' + base64.b64encode(f'IMG:{description}'.encode('utf-8')).decode('ascii')


def _video_url(name: str) -> str:
    return f'https://multimedia.nt.qq.com.cn/download/{name}?rkey=r'


def _video_name(url: str) -> str:
    return url.rsplit('/', 1)[-1].split('?', 1)[0]


def _image(description: str) -> ForwardMessagePart:
    return ForwardMessagePart.image_part('[图片]', _image_source(description))


def _video(name: str) -> ForwardMessagePart:
    return ForwardMessagePart.video_part('[视频]', _video_url(name), f'{name}.mp4')


def _single(sender: str, *parts: ForwardMessagePart) -> ForwardMessageTree:
    return ForwardMessageTree(nodes=(ForwardNode(sender_name=sender, parts=parts),))


def _media_as_text(tree: ForwardMessageTree) -> ForwardMessageTree:
    """把树里的媒体片段换回同文本的文本片段，得到引入媒体片段之前的等价树。"""
    nodes = []
    for node in tree.nodes:
        parts = []
        for part in node.parts:
            if part.kind == 'forward':
                assert part.nested is not None
                parts.append(ForwardMessagePart.forward_part(_media_as_text(part.nested)))
            elif part.kind in ('image', 'video'):
                parts.append(ForwardMessagePart.text_part(part.text))
            else:
                parts.append(part)
        nodes.append(ForwardNode(sender_name=node.sender_name, parts=tuple(parts)))
    return ForwardMessageTree(nodes=tuple(nodes))


def _deep_media_tree(levels: int = 4) -> ForwardMessageTree:
    """每层都有文本、一张图片、一个视频，并嵌套下一层；最深层没有嵌套。"""
    tree: ForwardMessageTree | None = None
    for level in reversed(range(levels)):
        parts = [
            ForwardMessagePart.text_part(f'第{level}层'),
            _image(f'第{level}层的猫'),
            _video(f'v{level}'),
        ]
        if tree is not None:
            parts.append(ForwardMessagePart.forward_part(tree))
        tree = ForwardMessageTree(nodes=(
            ForwardNode(sender_name=f'用户{level}', parts=tuple(parts)),
        ))
    assert tree is not None
    return tree


def _manifest() -> PluginManifest:
    return PluginManifest(
        plugin_id='yueli.forward-message',
        plugin_type='tool',
        name='YueLi-Forward-Message',
        version='0.1.0',
        description='按消息编号与路径逐层浏览合并转发内容',
    )


def _plugin(
    tmp_path: Path,
    *,
    read_images: bool = False,
    read_videos: bool = False,
    images: ChatImageDescriber | None = None,
    videos: ChatVideoDescriber | None = None,
    cache_limit: int = _plugin_module.DEFAULT_FORWARD_CACHE_LIMIT,
    observation_max_chars: int = _plugin_module.DEFAULT_FORWARD_OBSERVATION_MAX_CHARS,
) -> Any:
    """按宿主的注入顺序装配插件：先配置、再宿主入口。"""
    plugin = ForwardMessagePlugin(
        _manifest(), cache_limit=cache_limit, observation_max_chars=observation_max_chars,
    )
    plugin.bind_config(ForwardMessageConfig(read_images=read_images, read_videos=read_videos))
    plugin.bind_context(PluginContext(
        plugin.manifest.plugin_id,
        _PLUGIN_DIR,
        tmp_path,
        Config(),
        image_describer=images,
        video_describer=videos,
    ))
    return plugin


def _inbound(
    stream_id: int,
    trees: Tuple[ForwardMessageTree, ...],
    *,
    stream_kind: str = 'group',
    mentioned_me: bool = False,
) -> InboundMessage:
    return InboundMessage(
        text='转发',
        context=ConversationContext(
            stream=StreamRef(
                id=stream_id,
                platform='qq',
                kind=stream_kind,
                external_id=f'{stream_kind}-{stream_id}',
            ),
            person=PersonRef(id=1, kind='contact', first_seen_at=0),
        ),
        mentioned_me=mentioned_me,
        forward_messages=trees,
    )


def _context(stream_id: int = 7, message_watermark: int = 200) -> ToolContext:
    caps = PlatformCapabilities(plugin_capabilities=frozenset({'forward_message'}))
    frame = DecisionFrame(
        turn_id=3,
        snapshot_id='turn-3',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101,),
        message_watermark=message_watermark,
        available_actions=available_actions(
            'group', 'deliberate', caps, cognitive_rounds_left=2,
        ),
        capabilities=caps,
    )
    return ToolContext(
        stream_id=stream_id,
        stream_kind='group',
        frame=frame,
        turn_id=3,
        snapshot_id='turn-3',
    )


async def _execute(plugin: Any, arguments: Dict[str, Any]) -> ToolExecutionResult:
    """经 tools() 收集出的执行体调用工具，与宿主登记后的执行路径一致。"""
    executor = dict(
        (spec.name, bound) for spec, bound in plugin.tools()
    )['read_forward_message']
    return await executor.execute(
        ToolInvocation(tool_name='read_forward_message', arguments=arguments),
        _context(),
    )


async def _observe(plugin: Any, arguments: Dict[str, Any]) -> str:
    result = await _execute(plugin, arguments)
    assert result.success is True, result.error_message
    return result.observation


async def _read_all_pages(plugin: Any, arguments: Dict[str, Any]) -> List[str]:
    """沿 nextOffset 读完一份渲染，返回各页观察。"""
    pages: List[str] = []
    offset = 0
    for _ in range(100):
        result = await _execute(plugin, {**arguments, 'offset': offset})
        assert result.success is True, result.error_message
        pages.append(result.observation)
        next_offset = result.metadata['nextOffset']
        if next_offset is None:
            return pages
        offset = next_offset
    raise AssertionError('合并转发分页未在 100 次读取内结束')


async def _drain(plugin: Any) -> None:
    """等插件发起的识别任务全部结束，并让完成回调执行完。"""
    await asyncio.wait_for(
        asyncio.gather(*list(plugin._tasks), return_exceptions=True),
        timeout=10,
    )
    await asyncio.sleep(0)
    assert not plugin._tasks


async def _until(condition: Callable[[], bool]) -> None:
    """让事件循环转到条件成立：后台识别要经过几次让出才走到模型调用。"""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.001)
    raise AssertionError('条件没有在预期时间内成立')


def _page_body(page: str) -> str:
    """去掉分页前缀与续读提示，只留正文切片。"""
    if page.startswith('[合并转发分页，offset='):
        page = page.split('\n', 1)[1]
    marker = '\n[内容未完；next_offset='
    return page.split(marker, 1)[0] if marker in page else page


async def test_tool_timeout_is_derived_from_host_deadlines(tmp_path: Path) -> None:
    """开关关闭时声明不变；打开后执行超时 = 原声明超时 + 所开类型里宿主报告的单项最长等待。"""
    assert ForwardMessageConfig().read_images is False
    assert ForwardMessageConfig().read_videos is False
    base = ForwardMessagePlugin(_manifest()).tools()[0][0]
    config = _host_config()
    config.routing.vision.first_token_timeout_ms = 50_000
    config.routing.video.first_token_timeout_ms = 40_000
    images = ChatImageDescriber(config, _ContentVision())
    videos = ChatVideoDescriber(config, _ContentOmni())
    # 图片：下载截止 + 首字窗口 + 5 秒收尾；视频：首字窗口 + 5 秒收尾。
    assert images.max_source_wait_s() == IMAGE_DOWNLOAD_TIMEOUT_S + 55.0
    assert videos.max_source_wait_s() == 45.0

    def spec(**switches: bool) -> Any:
        return _plugin(tmp_path, images=images, videos=videos, **switches).tools()[0][0]

    images_only = spec(read_images=True)
    videos_only = spec(read_videos=True)
    both = spec(read_images=True, read_videos=True)

    assert _plugin(tmp_path, images=images, videos=videos).tools()[0][0] == base
    assert images_only.timeout_ms == base.timeout_ms + 65_000
    assert videos_only.timeout_ms == base.timeout_ms + 45_000
    assert both.timeout_ms == base.timeout_ms + 65_000
    # 宿主没装配服务时没有要等的识别，超时与原声明相同。
    assert _plugin(tmp_path, read_images=True).tools()[0][0].timeout_ms == base.timeout_ms
    # 截止在服务构造时定下：热重载换了首字窗口，上界与声明都不变。
    reloaded = _host_config()
    reloaded.routing.vision.first_token_timeout_ms = 5_000
    reloaded.routing.video.first_token_timeout_ms = 5_000
    images.apply_config(reloaded)
    videos.apply_config(reloaded)
    assert spec(read_images=True, read_videos=True).timeout_ms == both.timeout_ms

    assert images_only.description.startswith(base.description)
    assert '图片会被识别并显示为 [图片：描述]' in images_only.description
    assert '视频会被观看' not in images_only.description
    assert '视频会被观看并显示为 [视频：描述]' in videos_only.description
    assert '图片会被识别' not in videos_only.description
    assert '上限' not in both.description
    assert both.parameters == base.parameters


async def test_switches_off_render_exactly_like_text_placeholders_and_never_recognize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭时，深层媒体片段的渲染与引入媒体片段之前逐字一致，入站与读取都不发起识别。"""
    vision = _ContentVision()
    omni = _ContentOmni()
    videos, reads = _video_describer(monkeypatch, omni, {}, scope='all')
    plugin = _plugin(tmp_path, images=_image_describer(vision), videos=videos)
    baseline = ForwardMessagePlugin(_manifest())
    tree = _deep_media_tree()
    plugin.observe_inbound(7, 101, _inbound(7, (tree,), mentioned_me=True))
    baseline.observe_inbound(7, 101, _inbound(7, (_media_as_text(tree),)))

    assert plugin._tasks == set()
    for arguments in ({'message_id': 101}, {'message_id': 101, 'depth': 4}):
        observation = await _observe(plugin, arguments)
        assert observation == await _observe(baseline, arguments)

    assert (await _observe(plugin, {'message_id': 101, 'depth': 4})).count('[图片]') == 4
    await asyncio.sleep(0.01)
    assert vision.calls == [] and omni.urls == [] and reads == []


async def test_inbound_recognizes_media_in_every_layer_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收到即识别：四层嵌套里每层的图片与范围内的视频在读取之前就已发起，读取不再重复请求。"""
    vision = _ContentVision()
    omni = _ContentOmni()
    videos, _reads = _video_describer(
        monkeypatch, omni, {_video_url(f'v{level}'): 30.0 for level in range(4)},
    )
    plugin = _plugin(
        tmp_path,
        read_images=True,
        read_videos=True,
        images=_image_describer(vision),
        videos=videos,
    )

    with capture_logs() as logs:
        plugin.observe_inbound(7, 101, _inbound(7, (_deep_media_tree(),), mentioned_me=True))
    await _drain(plugin)

    assert sorted(vision.calls) == sorted(f'IMG:第{level}层的猫' for level in range(4))
    assert sorted(omni.urls) == sorted(_video_url(f'v{level}') for level in range(4))
    assert any(
        log['event'] == '收到合并转发，已在后台发起识别'
        and log['images'] == 4 and log['videos'] == 4
        for log in logs
    )

    deep = await _observe(plugin, {'message_id': 101, 'depth': 4})
    top = await _observe(plugin, {'message_id': 101})

    for level in range(4):
        assert f'【用户{level}】：第{level}层[图片：第{level}层的猫][视频：画面里是v{level}]' in deep
    assert '[嵌套合并转发，1 条，含 1 张图片、1 个视频，path=[0, 0]]' in top
    assert len(vision.calls) == 4 and len(omni.urls) == 4


async def test_images_only_leaves_videos_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只开识图：视频即使在观看范围内也保持占位，入站与读取都不调用视频服务。"""
    vision = _ContentVision()
    omni = _ContentOmni()
    videos, reads = _video_describer(monkeypatch, omni, {}, scope='all')
    plugin = _plugin(
        tmp_path, read_images=True, images=_image_describer(vision), videos=videos,
    )
    plugin.observe_inbound(7, 101, _inbound(7, (_deep_media_tree(),), mentioned_me=True))

    observation = await _observe(plugin, {'message_id': 101, 'depth': 4})

    for level in range(4):
        assert f'【用户{level}】：第{level}层[图片：第{level}层的猫][视频]' in observation
    assert omni.urls == [] and reads == []


async def test_read_waits_for_in_flight_recognition_without_new_requests(
    tmp_path: Path,
) -> None:
    """读取时识别还在跑：工具等它结束再出正文，不另发请求。"""
    gate = asyncio.Event()
    vision = _ContentVision(gate)
    plugin = _plugin(tmp_path, read_images=True, images=_image_describer(vision))
    plugin.observe_inbound(7, 101, _inbound(7, (
        _single('甲', _image('猫'), ForwardMessagePart.forward_part(_single('乙', _image('狗')))),
    )))
    await _until(lambda: len(vision.calls) == 2)

    reading = asyncio.create_task(_observe(plugin, {'message_id': 101, 'depth': 2}))
    await asyncio.sleep(0.02)
    assert not reading.done()
    gate.set()
    observation = await asyncio.wait_for(reading, timeout=5)

    assert '【甲】：[图片：猫]' in observation
    assert '【乙】：[图片：狗]' in observation
    assert sorted(vision.calls) == ['IMG:狗', 'IMG:猫']


async def test_cancelled_read_does_not_cancel_shared_recognition(tmp_path: Path) -> None:
    """工具调用在等待中被取消（回合被打断）：收到时发起的识别照常跑完，下次读取直接用结果。"""
    gate = asyncio.Event()
    vision = _ContentVision(gate)
    plugin = _plugin(tmp_path, read_images=True, images=_image_describer(vision))
    plugin.observe_inbound(7, 101, _inbound(7, (_single('甲', _image('猫'), _image('狗')),)))
    await _until(lambda: len(vision.calls) == 2)
    tasks = set(plugin._tasks)

    reading = asyncio.create_task(_observe(plugin, {'message_id': 101}))
    await asyncio.sleep(0.02)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading

    assert len(tasks) == 2
    assert not any(task.done() for task in tasks)
    gate.set()
    await _drain(plugin)
    assert all(task.result() is not None for task in tasks)

    observation = await _observe(plugin, {'message_id': 101})
    assert '【甲】：[图片：猫][图片：狗]' in observation
    assert sorted(vision.calls) == ['IMG:狗', 'IMG:猫']


async def test_videos_in_scope_watched_on_arrival_others_on_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视频与顶层视频同口径：跟她有关的消息收到即看，无关的读取时才看；all 范围全部收到即看。"""
    names = ('at', 'dm', 'later')
    omni = _ContentOmni()
    videos, _reads = _video_describer(
        monkeypatch, omni, {_video_url(name): 30.0 for name in names},
    )
    plugin = _plugin(tmp_path, read_videos=True, videos=videos)

    plugin.observe_inbound(7, 101, _inbound(7, (_single('甲', _video('at')),), mentioned_me=True))
    plugin.observe_inbound(8, 102, _inbound(8, (_single('乙', _video('dm')),), stream_kind='direct'))
    plugin.observe_inbound(7, 103, _inbound(7, (_single('丙', _video('later')),)))
    await _drain(plugin)

    assert sorted(omni.urls) == [_video_url('at'), _video_url('dm')]
    later = await _observe(plugin, {'message_id': 103})
    assert '【丙】：[视频：画面里是later]' in later
    assert '【甲】：[视频：画面里是at]' in await _observe(plugin, {'message_id': 101})
    assert sorted(omni.urls) == sorted(_video_url(name) for name in names)

    everything = _ContentOmni()
    all_videos, _all_reads = _video_describer(
        monkeypatch, everything, {_video_url('any'): 30.0}, scope='all',
    )
    eager = _plugin(tmp_path, read_videos=True, videos=all_videos)
    eager.observe_inbound(7, 104, _inbound(7, (_single('丁', _video('any')),)))
    await _drain(eager)
    assert everything.urls == [_video_url('any')]


async def test_video_scope_entry_reads_the_host_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ctx.media`` 的视频范围入口按宿主当前配置与「跟她有关」判定，热重载后即生效。"""
    videos, _reads = _video_describer(monkeypatch, _ContentOmni(), {})
    media = PluginContext('test.media', tmp_path, tmp_path, Config(), video_describer=videos).media
    tree = (_single('甲', _video('x')),)

    assert media.videos_watched_on_arrival(_inbound(7, tree)) is False
    assert media.videos_watched_on_arrival(_inbound(7, tree, mentioned_me=True)) is True
    assert media.videos_watched_on_arrival(_inbound(7, tree, stream_kind='direct')) is True
    videos.apply_config(_host_config('all'))
    assert media.videos_watched_on_arrival(_inbound(7, tree)) is True
    assert watches_video_on_arrival('related', False) is False
    assert watches_video_on_arrival('all', False) is True


async def test_render_stays_stable_across_rereads_offsets_and_later_cache_hits(
    tmp_path: Path,
) -> None:
    """文本在第一次输出前写定：续读拼回同一份渲染，之后宿主缓存里出现了描述也不改写它。"""
    inner = _single('内层', _image('内层图片'))
    root = ForwardMessageTree(nodes=(
        *(
            ForwardNode(
                sender_name=f'群友{index}',
                parts=(
                    ForwardMessagePart.text_part(f'这是第 {index} 张，' + '说明' * 10),
                    _image(f'第{index}张'),
                ),
            )
            for index in range(10)
        ),
        ForwardNode(sender_name='转发者', parts=(ForwardMessagePart.forward_part(inner),)),
    ))
    # 第 3 张收到时失败、读取时重试成功；第 5 张两次都失败，第三次才成功。
    vision = _ContentVision(failures={'IMG:第3张': 1, 'IMG:第5张': 2})
    plugin = _plugin(
        tmp_path, read_images=True, images=_image_describer(vision), observation_max_chars=300,
    )
    plugin.observe_inbound(7, 101, _inbound(7, (root,)))
    await _drain(plugin)
    assert len(vision.calls) == 11

    first = await _execute(plugin, {'message_id': 101})
    assert first.metadata['nextOffset'] is not None
    calls_after_first = len(vision.calls)
    pages = await _read_all_pages(plugin, {'message_id': 101})
    whole = ''.join(_page_body(page) for page in pages)

    assert pages[0] == first.observation
    assert calls_after_first == 13
    assert len(vision.calls) == calls_after_first
    assert '【群友3】：这是第 3 张，' + '说明' * 10 + '[图片：第3张]' in whole
    assert '【群友5】：这是第 5 张，' + '说明' * 10 + '[图片]' in whole
    # 各页首行的游标正好是前面各页正文的累计长度：页与页首尾相接，没有重叠或缺口。
    offset = 0
    for page in pages:
        if offset:
            assert page.startswith(f'[合并转发分页，offset={offset}]\n')
        offset += len(_page_body(page))

    # 另一条转发里的同一张图第三次识别成功、进了宿主缓存；本条已经写定，不受影响。
    plugin.observe_inbound(7, 102, _inbound(7, (_single('戊', _image('第5张')),)))
    await _drain(plugin)
    assert '【戊】：[图片：第5张]' in await _observe(plugin, {'message_id': 102})
    assert '【内层】：[图片：内层图片]' in await _observe(plugin, {'message_id': 101, 'path': [0, 0]})
    assert await _read_all_pages(plugin, {'message_id': 101}) == pages
    assert (await _execute(plugin, {'message_id': 101})).observation == first.observation


async def test_inbound_failure_is_retried_once_on_first_read(tmp_path: Path) -> None:
    """收到时失败的项在第一次被读到时重试一次；仍失败的保留 [图片] 并记 warning，之后不再重试。"""
    vision = _ContentVision(failures={'IMG:偶发': 1})
    plugin = _plugin(tmp_path, read_images=True, images=_image_describer(vision))
    tree = _single(
        '甲',
        ForwardMessagePart.text_part('看图'),
        _image(''),
        ForwardMessagePart.image_part('[图片]', str(tmp_path / '不存在.jpg')),
        _image('一只狗'),
        _image('偶发'),
    )
    plugin.observe_inbound(7, 101, _inbound(7, (tree,)))
    await _drain(plugin)
    assert sorted(vision.calls) == ['IMG:', 'IMG:一只狗', 'IMG:偶发']

    with capture_logs() as logs:
        first = await _observe(plugin, {'message_id': 101})
    second = await _observe(plugin, {'message_id': 101})

    assert '【甲】：看图[图片][图片][图片：一只狗][图片：偶发]' in first
    assert second == first
    assert sorted(vision.calls) == ['IMG:', 'IMG:', 'IMG:一只狗', 'IMG:偶发', 'IMG:偶发']
    failures = [
        log for log in logs
        if log['log_level'] == 'warning' and '合并转发图片识别失败' in log['event']
    ]
    assert sorted(log['ordinal'] for log in failures) == [1, 2]


async def test_in_flight_failure_is_not_retried_within_the_same_read(tmp_path: Path) -> None:
    """读取开始时还在跑的项失败了不在本次重试：等待因此不超过宿主的单项上界。"""
    gate = asyncio.Event()
    vision = _ContentVision(gate, failures={'IMG:慢': 1})
    plugin = _plugin(tmp_path, read_images=True, images=_image_describer(vision))
    plugin.observe_inbound(7, 101, _inbound(7, (_single('甲', _image('慢')),)))
    await _until(lambda: len(vision.calls) == 1)

    reading = asyncio.create_task(_observe(plugin, {'message_id': 101}))
    await asyncio.sleep(0.02)
    gate.set()
    observation = await asyncio.wait_for(reading, timeout=5)

    assert '【甲】：[图片]' in observation
    assert await _observe(plugin, {'message_id': 101}) == observation
    assert vision.calls == ['IMG:慢']


async def test_video_over_duration_uses_host_wording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过时长上限的视频沿用正文的超限写法，不调用全模态模型。"""
    omni = _ContentOmni()
    videos, _reads = _video_describer(monkeypatch, omni, {_video_url('long'): 600.0})
    plugin = _plugin(tmp_path, read_videos=True, videos=videos)
    plugin.observe_inbound(7, 101, _inbound(7, (_single('甲', _video('long')),)))

    observation = await _observe(plugin, {'message_id': 101})

    assert '【甲】：[视频：约 10 分钟，超出观看时长上限，未看]' in observation
    assert omni.urls == []


async def test_expired_video_link_keeps_placeholder_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直链过期读不出时长：收到时失败、读取时重试一次仍失败，保留 [视频] 并记 warning。"""
    omni = _ContentOmni()
    videos, reads = _video_describer(monkeypatch, omni, {_video_url('old'): None})
    plugin = _plugin(tmp_path, read_videos=True, videos=videos)
    tree = _single('甲', ForwardMessagePart.text_part('昨天的'), _video('old'))

    with capture_logs() as logs:
        plugin.observe_inbound(7, 101, _inbound(7, (tree,), mentioned_me=True))
        await _drain(plugin)
        first = await _observe(plugin, {'message_id': 101})
        second = await _observe(plugin, {'message_id': 101})

    assert '【甲】：昨天的[视频]' in first
    assert second == first
    assert reads == [_video_url('old'), _video_url('old')]
    assert omni.urls == []
    warnings = [log for log in logs if log['log_level'] == 'warning']
    assert any(log['event'] == 'chat_video_duration_unreadable' for log in warnings)
    assert any(
        '合并转发视频识别失败' in log['event'] and log['file'] == 'old.mp4'
        for log in warnings
    )
    # 日志不能带一次性签名直链。
    assert all('rkey' not in str(log) for log in logs)


async def test_unavailable_host_settles_placeholders_that_survive_hot_reload(
    tmp_path: Path,
) -> None:
    """主配置没开聊天图片识别：入站不发起、读取写定为占位；热重载打开后本条照旧，新转发才识别。"""
    vision = _ContentVision()
    describer = ChatImageDescriber(Config(), vision)
    plugin = _plugin(tmp_path, read_images=True, images=describer)
    tree = _single('甲', _image('a'), _image('b'))

    with capture_logs() as logs:
        await plugin.on_load()
        plugin.observe_inbound(7, 101, _inbound(7, (tree,)))
        observation = await _observe(plugin, {'message_id': 101})

    assert plugin._tasks == set()
    assert '【甲】：[图片][图片]' in observation
    assert vision.calls == []
    events = [log['event'] for log in logs if log['log_level'] == 'warning']
    assert sum('识图开关已打开' in event for event in events) == 1
    assert sum('识别当前不可用' in event for event in events) == 1

    describer.apply_config(_host_config())
    assert await _observe(plugin, {'message_id': 101}) == observation
    assert vision.calls == []
    plugin.observe_inbound(7, 102, _inbound(7, (tree,)))
    await _drain(plugin)
    assert '【甲】：[图片：a][图片：b]' in await _observe(plugin, {'message_id': 102})


async def test_textless_summary_and_hint_follow_switches(tmp_path: Path) -> None:
    """开关打开后含图片的层不再概括成「全部为非文本内容」，表情包层照旧概括。"""
    images_layer = ForwardMessageTree(nodes=tuple(
        ForwardNode(sender_name=f'甲{index}', parts=(_image(f'图{index}'),))
        for index in range(3)
    ))
    emoji_layer = ForwardMessageTree(nodes=tuple(
        ForwardNode(sender_name='乙', parts=(ForwardMessagePart.text_part('[表情包]'),))
        for _ in range(3)
    ))
    root = _single(
        '丙',
        ForwardMessagePart.forward_part(images_layer),
        ForwardMessagePart.forward_part(emoji_layer),
    )
    off = _plugin(tmp_path)
    on = _plugin(tmp_path, read_images=True, images=_image_describer(_ContentVision()))
    for plugin in (off, on):
        plugin.observe_inbound(7, 101, _inbound(7, (root,)))

    off_root = await _observe(off, {'message_id': 101})
    on_root = await _observe(on, {'message_id': 101})
    on_images = await _observe(on, {'message_id': 101, 'path': [0, 0]})
    on_emoji = await _observe(on, {'message_id': 101, 'path': [0, 1]})

    assert '[嵌套合并转发，3 条，无文本，path=[0, 0]]' in off_root
    assert '[嵌套合并转发，3 条，含 3 张图片，path=[0, 0]]' in on_root
    assert '[嵌套合并转发，3 条，无文本，path=[0, 1]]' in on_root
    assert '全部为非文本内容' not in on_images
    assert '【甲2】：[图片：图2]' in on_images
    assert '这一层 3 条，全部为非文本内容：[表情包]×3' in on_emoji


async def test_evicted_entry_lets_recognition_finish_into_host_cache(tmp_path: Path) -> None:
    """条目被淘汰时在跑的识别不取消：跑完进宿主缓存，同一张图再出现时不再调用模型。"""
    gate = asyncio.Event()
    vision = _ContentVision(gate)
    plugin = _plugin(tmp_path, read_images=True, images=_image_describer(vision), cache_limit=1)
    plugin.observe_inbound(7, 101, _inbound(7, (_single('甲', _image('猫')),)))
    await _until(lambda: len(vision.calls) == 1)
    (evicted_task,) = plugin._tasks

    plugin.observe_inbound(7, 102, _inbound(7, (_single('乙', ForwardMessagePart.text_part('只有字')),)))
    assert not (await _execute(plugin, {'message_id': 101})).success
    assert not evicted_task.done()
    gate.set()
    await _drain(plugin)
    assert evicted_task.result() == '[图片：猫]'

    plugin.observe_inbound(7, 103, _inbound(7, (_single('丙', _image('猫')),)))
    assert '【丙】：[图片：猫]' in await _observe(plugin, {'message_id': 103})
    assert vision.calls == ['IMG:猫']


async def test_unload_cancels_every_recognition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """卸载取消全部在跑的识别（含已被淘汰条目的），并且幂等。"""
    vision = _ContentVision(asyncio.Event())
    omni = _ContentOmni(asyncio.Event())
    videos, _reads = _video_describer(monkeypatch, omni, {_video_url('v'): 30.0})
    plugin = _plugin(
        tmp_path,
        read_images=True,
        read_videos=True,
        images=_image_describer(vision),
        videos=videos,
        cache_limit=1,
    )
    plugin.observe_inbound(
        7, 101, _inbound(7, (_single('甲', _image('猫'), _video('v')),), mentioned_me=True),
    )
    plugin.observe_inbound(7, 102, _inbound(7, (_single('乙', _image('狗')),)))
    await _until(lambda: len(vision.calls) == 2 and len(omni.urls) == 1)
    tasks = set(plugin._tasks)

    await plugin.on_unload()
    await plugin.on_unload()

    assert len(tasks) == 3
    assert all(task.cancelled() for task in tasks)
    assert plugin._tasks == set()


def test_inbound_recognition_requires_the_event_loop(tmp_path: Path) -> None:
    """脱离事件循环调用观察组件且开关打开：当场报错而不是静默不识别，条目仍写入缓存。"""
    plugin = _plugin(tmp_path, read_images=True, images=_image_describer(_ContentVision()))

    with pytest.raises(RuntimeError, match='no running event loop'):
        plugin.observe_inbound(7, 101, _inbound(7, (_single('甲', _image('猫')),)))

    assert plugin._tasks == set()
    assert plugin.stream_capabilities(7) == frozenset({'forward_message'})


async def test_media_access_reports_unwired_host(tmp_path: Path) -> None:
    """宿主没装配识别服务时，媒体入口如实报告不可用，直接调用则报错而不是返回空结果。"""
    media = PluginContext('test.media', tmp_path, tmp_path, Config()).media

    assert media.images_available() is False
    assert media.videos_available() is False
    assert media.max_wait_s(images=True, videos=True) == 0.0
    with pytest.raises(RuntimeError, match='没有装配聊天图片识别'):
        await media.describe_image('base64://AA==')
    with pytest.raises(RuntimeError, match='没有装配聊天视频理解'):
        await media.describe_video(_video_url('x'), 'x.mp4')
    with pytest.raises(RuntimeError, match='没有装配聊天视频理解'):
        media.videos_watched_on_arrival(_inbound(7, (_single('甲', _video('x')),)))
