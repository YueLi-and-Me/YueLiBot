"""提供工具插件访问宿主的窄视图，避免插件依赖完整配置与内核服务。

宿主用 Config 构造 PluginContext，再通过 Plugin.bind_context 注入。
插件读取 logger、paths、host、MCP 窄入口与媒体识别窄入口；配置只复制三个已约定
字段，不保留 Config 引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

from src.core.config.schema import Config
from src.core.logging.logger import get_logger
from src.core.mcp.compat.types import McpContentItem, McpFailure, McpToolResult, McpUnavailableError
from src.core.platform_io.types import InboundMessage, VideoSource
from src.core.services.media.chat_image import IMAGE_PLACEHOLDER, merge_image_descriptions
from src.core.services.media.chat_video import (
    VIDEO_PLACEHOLDER,
    merge_video_descriptions,
    message_concerns_her,
)

if TYPE_CHECKING:
    from src.core.mcp.manager import McpManager
    from src.core.services.media.chat_image import ChatImageDescriber
    from src.core.services.media.chat_video import ChatVideoDescriber


class PluginMcpAccess:
    """只暴露调用配置中只读工具的入口，权限、计数与连接全部由宿主管理器处理。"""

    __slots__ = ('_manager',)

    def __init__(self, manager: Optional['McpManager']) -> None:
        """绑定宿主服务；未装配时仍提供明确的中文不可用错误。"""
        self._manager = manager

    async def call_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> McpToolResult:
        """调用已列入配置的只读工具，expose 不影响插件调用授权。

        :param server: 已配置服务名。
        :param tool: 服务上的原始工具名。
        :param arguments: 按服务参数结构提供的参数对象。
        :return: 统一结果；工具、超时或服务器错误保留为 is_error=True。
        :raises McpUnavailableError: 未授权、未连上、非只读或今日达到上限。
        """
        if self._manager is None:
            raise McpUnavailableError('宿主 MCP 未启用')
        try:
            return await self._manager.call_tool(server, tool, arguments)
        except McpFailure as exc:
            return McpToolResult(True, (McpContentItem('text', text=str(exc)),))


class PluginMediaAccess:
    """只暴露按来源识别聊天图片与视频的入口，以及识别相关的两项宿主判定。

    下载、防盗链请求头、内容哈希缓存、在途去重、视频时长上限、每项的截止与模型
    路由全部由宿主的聊天图片 / 视频服务处理，与入站正文走同一套实现；插件拿不到
    服务对象本身，也就不能绕开这些约束。返回值是与聊天正文同格式的替换文本，
    格式只在宿主一处定义，插件不自己拼写。

    宿主的在途去重让同一内容的后来者直接等先发起者的任务：等待方被取消时，
    那个共享任务会被一起取消，正在等它的其他调用方随之落空。可能被取消的调用方
    （例如工具执行）应把识别放进自己创建的任务，再用 :func:`asyncio.shield` 或
    :func:`asyncio.wait` 等它。
    """

    __slots__ = ('_images', '_videos')

    def __init__(
        self,
        images: Optional['ChatImageDescriber'],
        videos: Optional['ChatVideoDescriber'],
    ) -> None:
        """绑定宿主服务；任一为 ``None`` 表示宿主没有装配该能力。

        :param images: 聊天图片识别服务。
        :param videos: 聊天视频理解服务；video 路由没有模型时宿主不装配它。
        """
        self._images = images
        self._videos = videos

    def images_available(self) -> bool:
        """判断当前能否识别图片：宿主已装配、主配置开启聊天图片识别且视觉模型可用。"""
        return self._images is not None and self._images.can_describe_images()

    def videos_available(self) -> bool:
        """判断当前能否理解视频：宿主已装配、主配置开启聊天视频理解且 video 路由有模型。"""
        return self._videos is not None and self._videos.can_describe_videos()

    def videos_watched_on_arrival(self, inbound: InboundMessage) -> bool:
        """判断这条入站消息里的视频是否属于收到即看的范围，与入站正文的视频同口径。

        范围是 ``all``，或这条消息跟她有关（私聊、@ 她、叫她名字、回复她）。入站
        正文另有「紧跟 @ 她的上一条」的扩展，它依赖聊天服务记录的发送者状态，
        这里不提供；不在范围内的视频由插件自己决定何时再看。

        :param inbound: 入站消息。
        :return: 收到即看为 ``True``；只表达范围，不检查开关与模型。
        :raises RuntimeError: 宿主没有装配视频理解；调用前应先检查
            :meth:`videos_available`。
        """
        if self._videos is None:
            raise RuntimeError('宿主没有装配聊天视频理解，调用前应先检查 videos_available()')
        return self._videos.watches_on_arrival(message_concerns_her(
            inbound.context.stream.kind,
            inbound.mentioned_me,
            inbound.name_mentioned,
            inbound.replied_to_me,
        ))

    def max_wait_s(self, *, images: bool, videos: bool) -> float:
        """所选类型里，按来源识别一项最长要等多久（秒）。

        每项的上界由宿主服务报告：图片是下载截止加识别截止，视频是识别截止（读
        时长已包在里面）。截止在服务构造时定下、配置热重载不重算，进程内不变。
        没有装配的服务不参与，它的项根本不会被识别。

        :param images: 是否计入图片。
        :param videos: 是否计入视频。
        :return: 所选且已装配类型里的最大值；一个都没有时为 ``0.0``。
        """
        waits = [0.0]
        if images and self._images is not None:
            waits.append(self._images.max_source_wait_s())
        if videos and self._videos is not None:
            waits.append(self._videos.max_source_wait_s())
        return max(waits)

    async def describe_image(self, source: str) -> Optional[str]:
        """按来源识别一张普通聊天图片。

        :param source: 图片来源（``http(s)://``、``file://``、``base64://`` 或本地路径）。
        :return: 与正文同格式的 ``[图片：描述]``；下载、解码或模型失败时为
            ``None``，原因由宿主服务记入日志。
        :raises RuntimeError: 宿主没有装配图片识别；调用前应先检查
            :meth:`images_available`。
        """
        if self._images is None:
            raise RuntimeError('宿主没有装配聊天图片识别，调用前应先检查 images_available()')
        descriptions = await self._images.describe_sources((source,))
        rendered = merge_image_descriptions(IMAGE_PLACEHOLDER, descriptions)
        return None if rendered == IMAGE_PLACEHOLDER else rendered

    async def describe_video(self, source: str, file: str) -> Optional[str]:
        """按来源理解一个聊天视频，不套用入站时的观看范围。

        范围判定见 :meth:`videos_watched_on_arrival`，是否套用由调用方决定。

        :param source: 视频直链；链接带一次性签名，过期后读不出时长，按失败返回。
        :param file: 视频段的内容文件名，宿主按它缓存与去重；为空时退回按链接。
        :return: 与正文同格式的 ``[视频：描述]``，或时长超限时的
            ``[视频：约 N 秒/分钟，超出观看时长上限，未看]``；读不出时长、超时或
            模型失败时为 ``None``，原因由宿主服务记入日志。
        :raises RuntimeError: 宿主没有装配视频理解；调用前应先检查
            :meth:`videos_available`。
        """
        if self._videos is None:
            raise RuntimeError('宿主没有装配聊天视频理解，调用前应先检查 videos_available()')
        outcomes = await self._videos.describe_sources((VideoSource(url=source, file=file),))
        rendered = merge_video_descriptions(VIDEO_PLACEHOLDER, outcomes)
        return None if rendered == VIDEO_PLACEHOLDER else rendered


@dataclass(frozen=True)
class HostView:
    """插件可见的宿主配置窄视图。

    刻意只有三个字段：ctx 暴露出去的每一样都是对插件作者的承诺，加进来容易拿掉难。
    需要更多字段时单独议，不要在实现里顺手加。
    """

    bot_name: str
    https_proxy: str
    data_dir: Path


@dataclass(frozen=True)
class PluginPaths:
    """插件可用的两个目录，不负责创建目录。

    plugin_dir 是插件自己的目录，config.toml 就在这里；data_dir 是宿主数据目录，
    插件要落盘时在其下自建子目录。
    """

    plugin_dir: Path
    data_dir: Path


class PluginContext:
    """插件访问宿主的唯一入口。插件不得直接 import src.core 下的任何模块。"""

    __slots__ = ('_logger', '_paths', '_host', '_mcp', '_media')

    def __init__(self, plugin_id: str, plugin_dir: Path, data_dir: Path, cfg: Config,
                 mcp: Optional['McpManager'] = None,
                 image_describer: Optional['ChatImageDescriber'] = None,
                 video_describer: Optional['ChatVideoDescriber'] = None) -> None:
        """从宿主配置生成只读快照，MCP 与媒体识别服务只通过窄入口提供，不保留完整配置。

        :param plugin_id: 已校验清单中的插件标识，用于日志归属。
        :param plugin_dir: 插件目录，包含入口与插件配置。
        :param data_dir: 宿主确定的数据根目录，与 host.data_dir 使用同一个值。
        :param cfg: 宿主已校验配置，仅取 bot.name 与 advanced.https_proxy。
        :param mcp: 宿主 MCP 管理器；未提供时调用会抛出中文不可用错误。
        :param image_describer: 宿主聊天图片识别服务；未提供时媒体入口报告图片不可用。
        :param video_describer: 宿主聊天视频理解服务；未提供时媒体入口报告视频不可用。
        副作用：构造日志代理；不创建目录，不读写磁盘。
        """
        self._logger = get_logger(f'src.plugin_system.context.{plugin_id}')
        self._mcp = PluginMcpAccess(mcp)
        self._media = PluginMediaAccess(image_describer, video_describer)
        self._paths = PluginPaths(plugin_dir=plugin_dir, data_dir=data_dir)
        self._host = HostView(
            bot_name=cfg.bot.name,
            https_proxy=cfg.advanced.https_proxy,
            data_dir=data_dir,
        )

    @property
    def logger(self) -> Any:
        """结构化日志器，已绑定本插件的 id。"""
        return self._logger

    @property
    def paths(self) -> 'PluginPaths':
        """插件目录与数据目录。"""
        return self._paths

    @property
    def host(self) -> HostView:
        """宿主配置的只读窄视图。"""
        return self._host

    @property
    def mcp(self) -> PluginMcpAccess:
        """调用部署者列出的只读 MCP 工具，与模型调用共用每日次数。"""
        return self._mcp

    @property
    def media(self) -> PluginMediaAccess:
        """按来源识别聊天图片与视频，与入站正文共用宿主的识别服务、缓存和开关。"""
        return self._media
