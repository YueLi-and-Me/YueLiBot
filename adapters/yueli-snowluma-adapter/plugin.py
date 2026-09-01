"""YueLi SnowLuma 适配器插件：身份与配置装配，协议实现全部复用协议核心。

SnowLuma 是不依赖可选封包组件的 OneBot 11 协议端，不存在「能力随客户端构建
失效」的动态来源，因此全部能力静态声明、无待探测项；投递万一失败，由协议核心
既有的失败回传链路落账，不在插件里自检。任何后端差异都应表达为清单里的能力
差异，而不是本文件里的代码分支。
"""

from __future__ import annotations

from pathlib import Path
from typing import FrozenSet

from src.core.common.backend_runtime import read_backend_runtime
from src.core.config.toml_io import read_versioned_toml
from src.platforms.onebot11.backend import BackendClient
from src.platforms.onebot11.config import (
    NAPCAT_CONFIG_VERSION,
    AdapterDocument,
)
from src.platforms.onebot11.runner import OneBot11Runner
from src.platforms.onebot11.transport import OneBot11Transport
from src.plugin_system import AdapterCapability, AdapterManifest, AdapterPlugin

# 共用配置模型里的连接段字段名。清单的 config_section 决定磁盘上的段名，两者
# 不同名时在这里做一次显式映射；字段名若由契约层统一改名，改这一处即可。
_PROTOCOL_SECTION_FIELD = 'napcat'


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
        :param config_path: 适配器配置文件路径；缺省为 ``config/<config_section>.toml``。
        :param runtime_path: 主体运行时信息文件路径；缺省为 ``data/runtime/backend.json``。
        :param transport: 协议传输替身注入位；缺省由运行器构造真实传输。
        :param backend: 主体客户端替身注入位；缺省由运行器构造真实客户端。
        副作用：只保存引用，不读文件也不建对象。
        """
        super().__init__(manifest)
        self._config_path = config_path or Path('config') / f'{manifest.config_section}.toml'
        self._runtime_path = runtime_path or Path('data/runtime/backend.json')
        self._transport = transport
        self._backend = backend
        self._runner: OneBot11Runner | None = None

    async def on_load(self) -> None:
        """读取 ``snowluma`` 配置段并构造运行器。

        配置文件与共用协议配置模型同构，只是连接段以本适配器的段名出现；
        校验全部复用共用模型，段名在此处映射。段缺失时上抛明确错误而不是
        回退默认值——连接参数写错段的适配器连不上任何协议端，静默默认只会
        把错误推迟到运行期。

        :raises ValueError: 配置文件缺少本适配器的连接段。
        :raises OSError: 配置文件或运行时文件无法读取。
        :raises pydantic.ValidationError: 连接参数不符合共用配置模型。
        副作用：读取两个本地文件；构造运行器但不建立连接。
        """
        section = self._manifest.config_section
        document = read_versioned_toml(
            self._config_path,
            NAPCAT_CONFIG_VERSION,
            '该文件版本与程序支持的版本不一致，需要用当前版本的模板重写',
        )
        section_payload = document.get(section)
        if not isinstance(section_payload, dict):
            raise ValueError(
                f'{self._config_path} 缺少 [{section}] 配置段，无法确定协议端连接参数'
            )
        payload = {key: value for key, value in document.items() if key != section}
        payload[_PROTOCOL_SECTION_FIELD] = section_payload
        config = AdapterDocument.model_validate(payload)
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
