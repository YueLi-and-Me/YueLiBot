"""定义所有插件共有的基类与生命周期。

插件只需继承本模块的基类、写一份清单，宿主负责发现、加载与接线。类型专有的
生命周期在子类里补充：适配器要连协议端并探测能力（见 ``adapter``），工具插件
要贡献工具声明与执行体（见 ``tools``）。

依赖 ``manifest``；被具体插件继承，被宿主与注册表驱动。
"""

from __future__ import annotations

from abc import ABC

from .manifest import PluginManifest


class Plugin(ABC):
    """所有插件的共同基类。

    两个生命周期方法都给了空实现而不是留抽象：多数插件在加载期无事可做，强制
    覆写只会逼出一堆空方法，反而让真正做了事的实现淹没在噪声里。
    """

    def __init__(self, manifest: PluginManifest) -> None:
        """保存清单。

        :param manifest: 已校验的插件清单；类型与本类的子类必须匹配，该判据由
            加载器负责，构造函数不重复校验。
        """
        self._manifest = manifest

    @property
    def manifest(self) -> PluginManifest:
        """返回本插件的清单。"""
        return self._manifest

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
