"""定义所有插件共有的基类与生命周期。

插件只需继承本模块的基类、写一份清单，宿主负责发现、加载与接线。类型专有的
生命周期在子类里补充：适配器要连协议端并探测能力（见 ``adapter``），工具插件
要贡献工具声明与执行体（见 ``tools``）。

依赖 ``manifest``；被具体插件继承，被宿主与注册表驱动。
"""

from __future__ import annotations

from abc import ABC
from typing import ClassVar, Optional, Type

from .config import PluginConfig
from .context import PluginContext
from .manifest import PluginManifest


class Plugin(ABC):
    """所有插件的共同基类。

    两个生命周期方法都给了空实现而不是留抽象：多数插件在加载期无事可做，强制
    覆写只会逼出一堆空方法，反而让真正做了事的实现淹没在噪声里。
    """

    #: 本插件的配置模型。子类按需覆写并追加字段；不覆写时只有一个启用开关。
    #: 宿主据此在插件目录里生成带注释的 config.toml，声明因此只有这一份。
    config_model: ClassVar[Type[PluginConfig]] = PluginConfig

    def __init__(self, manifest: PluginManifest) -> None:
        """保存清单。

        :param manifest: 已校验的插件清单；类型与本类的子类必须匹配，该判据由
            加载器负责，构造函数不重复校验。
        """
        self._manifest = manifest
        # 配置由宿主在发现阶段读好后注入，构造期还拿不到。
        self._config: PluginConfig = self.config_model()
        self._ctx: Optional[PluginContext] = None

    @property
    def manifest(self) -> PluginManifest:
        """返回本插件的清单。"""
        return self._manifest

    @property
    def config(self) -> PluginConfig:
        """返回本插件的配置。

        :return: :attr:`config_model` 的实例。宿主注入之前是一份全默认值，
            因此在 ``on_load`` 及之后读它总是安全的。
        """
        return self._config

    def bind_config(self, config: PluginConfig) -> None:
        """由宿主在 ``on_load`` 之前注入已读取的配置。

        :param config: 已校验的配置实例。
        :return: ``None``。
        副作用：替换实例持有的配置；插件自身不应调用本方法。

        不走构造函数是因为构造签名是插件契约的一部分：加载器统一以
        ``plugin_class(manifest)`` 构造，加一个位置参数会让所有既有插件失效。
        """
        self._config = config

    @property
    def ctx(self) -> PluginContext:
        """返回宿主在 bind_config 之后、on_load 之前注入的上下文。

        :return: 已绑定本插件日志与路径的上下文；on_load 里可以直接使用 self.ctx。
        :raises AttributeError: 宿主尚未注入上下文，或插件在构造期提前访问。
        """
        if self._ctx is None:
            # 工具收集会反射实例属性；未注入表示此属性尚不可用，因此使用 AttributeError。
            # 若改为 RuntimeError，inspect.getmembers 会中断，尚不需要 ctx 的工具也无法收集。
            # 直接访问仍携带插件标识与注入时序报错，不返回 None 或虚构上下文。
            raise AttributeError(
                f'插件 {self.manifest.plugin_id} 的上下文尚未注入：'
                '宿主必须在 bind_config 之后、on_load 之前调用 bind_context'
            )
        return self._ctx

    def bind_context(self, context: PluginContext) -> None:
        """由宿主在 bind_config 之后、on_load 之前注入上下文。

        :param context: 宿主为本插件构造的 PluginContext。
        :return: None。
        副作用：替换实例持有的上下文；插件自身不应调用此方法。
        on_load 里可以直接用 self.ctx，无需改变既有插件的构造签名。
        """
        self._ctx = context

    async def on_load(self) -> None:
        """读取配置、构造运行期对象。

        此时尚未接入任何外部连接，**不应发起网络 I/O**：加载失败应当是纯粹的
        配置问题，混入网络故障会让「配置写错」与「对端没起来」在现场无法区分。

        :raises Exception: 配置缺失或非法时原样上抛，由宿主决定是否终止启动。
        """

    async def on_unload(self) -> None:
        """释放插件持有的资源。

        必须幂等：宿主在停机与重载两条路径上都会调用，重复调用不得抛异常。
        """
