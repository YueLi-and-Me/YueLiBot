"""YueLi-NapCat-Adapter 插件实现：NapCat 协议端的身份、配置与能力探测。

本模块只承载三件事：读哪段配置、构造并驱动 ``OneBot11Runner``、连接后实测
``poke`` 能力。协议逻辑全部复用 ``src.platforms.onebot11``，这里不重写任何
协议代码。

``poke`` 必须探测而不能静态声明：它依赖 NapCat 私有 packet 后端对当前 QQ 构建的
匹配程度，QQ 自动更新后可能从可用变成恒定失败（2026-08-31 现场：QQ 9.9.33-52230
超出 packet 后端支持范围，group_poke 恒定 retcode=1400）。探测判据是 NapCat 私有
action ``nc_get_packet_status``，任何失败形态——拒绝、超时、断连、未知
action——一律按不可用处理，不重试、不兜底、绝不当成可用。
"""

from __future__ import annotations

from pathlib import Path
from typing import FrozenSet, Optional

from src.core.common.backend_runtime import read_backend_runtime
from src.core.common.logger import get_logger
from src.platforms.onebot11.config import read_config
from src.platforms.onebot11.runner import OneBot11Runner
from src.platforms.onebot11.transport import OneBot11Transport
from src.plugin_system import AdapterCapability, AdapterManifest, AdapterPlugin

# 插件由宿主按文件路径加载，__name__ 取决于加载器而非包结构；logger 名写成显式
# 常量，并以同名键登记在 logger_colors 的 MODULE_COLORS 与 MODULE_ALIASES。
logger = get_logger('adapters.yueli_napcat_adapter.plugin')

# 探测 poke 所用的私有 action。响应 status 为 ok 表示 packet 后端与当前 QQ 构建
# 匹配，group_poke 才具备实际投递能力；其它协议端没有这个动作，会按未知 action
# 拒绝，同样落入失败分支。
_POKE_PROBE_ACTION = 'nc_get_packet_status'
_POKE_CAPABILITY: AdapterCapability = 'poke'

# 连接配置与插件同目录：它描述的是「这个适配器连哪个协议端」，属于适配器自身，
# 放进全局 config/ 只会和主体配置混在一起，还要靠文件名去猜是哪个适配器在用。
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / 'config.toml'
# 运行时信息属于主体，随数据目录配置变化，默认值与 src.platforms.onebot11.__main__
# 的命令行默认值保持一致。
_DEFAULT_RUNTIME_PATH = Path('data/runtime/backend.json')


class NapCatAdapterPlugin(AdapterPlugin):
    """NapCat 协议端适配器：薄封装 ``OneBot11Runner``，外加 ``poke`` 能力实测。"""

    def __init__(
        self,
        manifest: AdapterManifest,
        *,
        config_path: Path = _DEFAULT_CONFIG_PATH,
        runtime_path: Path = _DEFAULT_RUNTIME_PATH,
        transport: Optional[OneBot11Transport] = None,
    ) -> None:
        """保存路径与可选的传输替身；不做任何 I/O。

        :param manifest: 已校验的插件清单；能力结算以它为上界。
        :param config_path: 协议端连接配置路径，默认为本插件目录下的 ``config.toml``。
        :param runtime_path: 主体后端运行时信息路径，默认 ``data/runtime/backend.json``。
        :param transport: 可选的协议传输实现；为空时由 ``on_load`` 创建真实的
            :class:`OneBot11Transport`，测试可传入替身。
        副作用：只保存参数，不读写文件、不建立连接。
        """
        super().__init__(manifest)
        self._config_path = config_path
        self._runtime_path = runtime_path
        self._transport = transport
        self._runner: Optional[OneBot11Runner] = None

    async def on_load(self) -> None:
        """读取配置与主体运行时信息，构造运行器；不发起任何网络 I/O。

        探测与收发共用同一个传输实例：能力结论属于这条连接指向的协议端，
        另起连接既浪费也让「探测的那一端」和「收发的那一端」可能不是同一个。

        :raises Exception: 配置或运行时信息缺失、非法时原样上抛，由宿主决定
            是否终止启动。
        副作用：读取两个本地文件并构造对象；不建立连接。
        """
        config = read_config(self._config_path)
        runtime = read_backend_runtime(self._runtime_path)
        if self._transport is None:
            self._transport = OneBot11Transport(config.napcat)
        self._runner = OneBot11Runner(
            config,
            runtime.port,
            runtime.token,
            transport=self._transport,
            adapter_id=self._manifest.plugin_id,
            capability_probe=self.resolve_capabilities,
        )

    async def probe_capabilities(self) -> FrozenSet[AdapterCapability]:
        """用 ``nc_get_packet_status`` 实测一次 packet 后端，判定 ``poke`` 可用性。

        探测是一次性判定：不重试、不兜底。任何失败形态——协议端返回
        ``status=failed``、等待超时、连接断开、协议端没有这个动作——都按不可用
        处理并记一条 warning；``ActionError`` 的文本已携带协议端返回的
        message/wording 原文，直接落日志，便于对照现场。

        :return: 实测可用时返回 ``{'poke'}``，否则返回空集合。
        :raises RuntimeError: ``on_load`` 尚未完成时被调用，属于装配错误，必须暴露。
        副作用：在现有连接上发起一次协议 action；不重连、不新建连接。
        """
        if self._transport is None:
            raise RuntimeError('on_load 尚未完成，无法探测能力')
        try:
            await self._transport.call_action(_POKE_PROBE_ACTION)
        except Exception as exc:
            logger.warning(
                'poke 能力探测失败，按不可用处理',
                action=_POKE_PROBE_ACTION,
                error=str(exc),
            )
            return frozenset()
        return frozenset({_POKE_CAPABILITY})

    async def on_start(self) -> None:
        """进入收发循环；连接维持与重试策略由 ``OneBot11Runner`` 负责。

        :raises RuntimeError: ``on_load`` 尚未完成时被调用。
        :raises Exception: 运行器的不可重试错误原样上抛，由宿主处理。
        副作用：建立协议端与主体连接并进入收发循环，直到停机或连接失败。
        """
        if self._runner is None:
            raise RuntimeError('on_load 尚未完成，无法启动')
        await self._runner.run()

    async def on_stop(self) -> None:
        """关闭协议端连接；幂等，重复调用安全。

        宿主在停机与重连两条路径上都会调用本方法；底层传输的 ``close`` 本身
        可重复调用，这里不维护额外状态。宿主应先取消 ``on_start`` 任务再调用
        本方法，否则运行器会把断连当作可重试故障并重连。
        """
        if self._transport is not None:
            await self._transport.close()
