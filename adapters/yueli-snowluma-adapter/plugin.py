"""YueLi SnowLuma 适配器插件：身份与配置装配，协议实现全部复用协议核心。

SnowLuma 是不依赖可选封包组件的 OneBot 11 协议端，不存在「能力随客户端构建
失效」的动态来源，因此全部能力静态声明、无待探测项；投递万一失败，由协议核心
既有的失败回传链路落账，不在插件里自检。任何后端差异都应表达为清单里的能力
差异，而不是本文件里的代码分支。
"""

from __future__ import annotations

from pathlib import Path
from typing import FrozenSet

from src.core.runtime.backend_runtime import read_backend_runtime
from src.platforms.onebot11.backend import BackendClient
from src.platforms.onebot11.config import read_section_config
from src.platforms.onebot11.runner import OneBot11Runner
from src.platforms.onebot11.transport import OneBot11Transport
from src.plugin_system import AdapterCapability, AdapterManifest, AdapterPlugin

# 连接配置与插件同目录：它描述的是「这个适配器连哪个协议端」，属于适配器自身，
# 放进全局 config/ 只会和主体配置混在一起，还要靠文件名去猜是哪个适配器在用。
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / 'config.toml'


class SnowlumaAdapterPlugin(AdapterPlugin):
    """以纯静态能力声明驱动 SnowLuma 协议端的适配器插件。"""

    def __init__(
        self,
        manifest: AdapterManifest,
        *,
        config_path: Path | None = None,
        runtime_path: Path | None = None,
        transport: OneBot11Transport | None = None,
        backend: BackendClient | None = None,
    ) -> None:
        """保存清单与注入项。

        :param manifest: 已校验的插件清单；连接段名取自其中的 config_section。
        :param config_path: 适配器配置文件路径；缺省为本插件目录下的 ``config.toml``。
        :param runtime_path: 主体运行时信息文件路径；缺省为 ``data/runtime/backend.json``。
        :param transport: 协议传输替身注入位；缺省由运行器构造真实传输。
        :param backend: 主体客户端替身注入位；缺省由运行器构造真实客户端。
        副作用：只保存引用，不读文件也不建对象。
        """
        super().__init__(manifest)
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._runtime_path = runtime_path or Path('data/runtime/backend.json')
        self._transport = transport
        self._backend = backend
        self._runner: OneBot11Runner | None = None

    async def on_load(self) -> None:
        """读取 ``snowluma`` 配置段并构造运行器。

        配置文件与共用协议配置模型同构，只是连接段以本适配器的段名出现；
        读取、段名映射与校验全部复用 ``read_section_config``，本插件只提供
        清单里声明的段名。

        :raises ValueError: 配置文件缺少本适配器的连接段。
        :raises OSError: 配置文件或运行时文件无法读取。
        :raises pydantic.ValidationError: 连接参数不符合共用配置模型。
        副作用：读取两个本地文件；构造运行器但不建立连接。
        """
        config = read_section_config(
            self._config_path,
            self._manifest.config_section,
        )
        runtime = read_backend_runtime(self._runtime_path)
        self._runner = OneBot11Runner(
            config,
            runtime.port,
            runtime.token,
            transport=self._transport,
            backend=self._backend,
            adapter_id=self._manifest.plugin_id,
            capability_probe=self.resolve_capabilities,
        )

    async def probe_capabilities(self) -> FrozenSet[AdapterCapability]:
        """返回空集合。

        清单声明的待探测能力为空：SnowLuma 的能力置信度来自「不依赖封包组件」
        的结构事实，静态声明即是最终结论。方法仍然实现，保持四个生命周期
        入口完整；即使被调用也不发起任何网络请求。
        """
        return frozenset()

    async def on_start(self) -> None:
        """把运行器推进收发循环，并阻塞到停机为止。

        必须阻塞而不是起个任务就返回：宿主用本协程的返回判断「收发已结束」，
        提前返回会让它立刻进入收尾路径，把刚建立的连接取消掉，表现为适配器
        启动后零错误退出。约束的完整说明见基类 ``AdapterPlugin.on_start``。

        :raises RuntimeError: 未先调用 ``on_load``；生命周期顺序由宿主保证，
            此处只拒绝明显乱序的调用。
        :raises asyncio.CancelledError: 宿主取消时原样传播，用于结束收发循环。
        副作用：建立协议端与主体连接并持续收发；连接与重试由运行器自行管理。
        """
        if self._runner is None:
            raise RuntimeError('on_start 在 on_load 之前被调用')
        await self._runner.run()

    async def on_stop(self) -> None:
        """关闭协议端连接。

        幂等：停机与重连两条路径都可能调用，重复调用或先于 ``on_start`` 调用
        都不抛异常。收发循环由宿主取消 ``on_start`` 协程结束，运行器在其收尾
        路径里关闭两条连接；这里只做一次兜底关闭，不持有也不取消任务。

        副作用：关闭协议端传输；已关闭时重复调用无副作用。
        """
        if self._transport is not None:
            await self._transport.close()
