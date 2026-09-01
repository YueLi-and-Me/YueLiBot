"""定义适配器插件的生命周期契约与能力结算。

适配器插件只负责三件事：读自己那段配置、驱动协议实现收发、如实报告协议端能做什么。
协议实现本身共用（见 ``src.platforms``），插件里不得复制协议代码，也不得出现按后端
名字分支的判断——后端差异应当表达为能力差异，而不是代码分支。

能力结算的方向是刻意单向收窄的：探测只能确认待探测能力是否可用，不能新增能力，
探测失败一律按不可用处理。反方向（失败当可用）会让主体把执行不了的动作放进动作集，
现场表现为「她做了动作但对方什么都没收到」，且账本里查不出来。

依赖 ``capabilities`` 与 ``manifest``；被具体适配器包继承，被插件宿主驱动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import FrozenSet

from src.core.common.logger import get_logger

from .capabilities import AdapterCapability
from .manifest import AdapterManifest


logger = get_logger(__name__)


class AdapterPlugin(ABC):
    """适配器插件基类；子类实现四个生命周期方法。

    生命周期顺序固定为 ``on_load`` → ``probe_capabilities`` → ``on_start``，
    停机或重连前调用 ``on_stop``。宿主保证这个顺序，子类不必自行判断阶段。
    """

    def __init__(self, manifest: AdapterManifest) -> None:
        """保存清单。

        :param manifest: 已校验的插件清单；能力结算以它为上界。
        """
        self._manifest = manifest

    @property
    def manifest(self) -> AdapterManifest:
        """返回本插件的清单。"""
        return self._manifest

    @abstractmethod
    async def on_load(self) -> None:
        """读取配置并构造运行期对象。

        此时尚未连接协议端，**禁止在这里发起任何网络 I/O**：加载失败应当是纯粹的
        配置问题，混入网络故障会让「配置写错」和「协议端没起来」在现场无法区分。

        :raises Exception: 配置缺失或非法时原样上抛，由宿主决定是否终止启动。
        """

    @abstractmethod
    async def probe_capabilities(self) -> FrozenSet[AdapterCapability]:
        """实测清单中 ``probed`` 部分的能力，返回其中确实可用的。

        在协议端连接成功之后、开始收发之前调用。返回值必须是清单
        ``probed_capabilities`` 的子集；返回集合外的能力会被结算层拒绝。

        :return: 实测可用的能力集合；没有待探测能力时返回空集合。
        :raises Exception: 探测过程本身失败时可以上抛，结算层会按全部不可用处理。
        """

    @abstractmethod
    async def on_start(self) -> None:
        """进入收发循环，**并阻塞到停机为止**。

        - 现象：若实现改成 ``create_task`` 后立即返回，宿主会以为适配器已经跑完，
          随即进入收尾路径把刚建的任务取消掉，进程零错误退出（``code=0``），
          日志里什么都没有，看起来像「启动了但什么也没做」。
        - 原因：宿主用 ``await on_start()`` 的返回来判断「收发结束」，这是它唯一
          能等的信号；返回即等于宣告结束。
        - 后果：换成非阻塞实现会让适配器每次启动都立刻退出，且不报错。

        停机由宿主取消本协程实现，因此实现方不必自己持有运行任务，也不必在
        ``on_stop`` 里再取消一次。

        :raises Exception: 连接或循环失败时原样上抛，由宿主按重试策略处理。
        :raises asyncio.CancelledError: 宿主取消时原样传播，用于结束收发循环。
        """

    @abstractmethod
    async def on_stop(self) -> None:
        """停止收发并释放连接。

        必须幂等：宿主在停机与重连两条路径上都会调用，重复调用不得抛异常。
        """

    async def resolve_capabilities(self) -> FrozenSet[AdapterCapability]:
        """结算本次连接最终可用的能力集合。

        结果为「静态能力」并上「探测确认可用的待探测能力」。探测抛异常时，待探测部分
        整体按不可用处理并记一条 warning——不可用是安全方向，可用是危险方向。

        :return: 最终能力集合，恒为清单 ``declared_capabilities`` 的子集。
        :raises ValueError: 探测返回了清单未声明为 ``probed`` 的能力，说明插件与清单
            不同步；这属于装配错误，必须暴露而不是过滤掉。
        副作用：调用子类的 ``probe_capabilities``；探测失败时写一条 warning 日志。
        """
        declared_probed = self._manifest.probed_capabilities
        if not declared_probed:
            # 没有待探测能力时不调用探测，避免插件为了「被调用」而做无谓的网络往返。
            return self._manifest.static_capabilities

        try:
            probed = await self.probe_capabilities()
        except Exception as exc:
            logger.warning(
                '适配器能力探测失败，相关能力按不可用处理',
                plugin=self._manifest.plugin_id,
                probed=sorted(declared_probed),
                error=str(exc),
            )
            return self._manifest.static_capabilities

        unexpected = frozenset(probed) - declared_probed
        if unexpected:
            raise ValueError(
                f'{self._manifest.plugin_id} 探测返回了清单未声明的能力：'
                f'{"、".join(sorted(unexpected))}'
            )
        unavailable = declared_probed - frozenset(probed)
        if unavailable:
            # 探测判定不可用是正常结果而非故障，记 info 便于事后对照现场行为。
            logger.info(
                '适配器部分能力经探测不可用',
                plugin=self._manifest.plugin_id,
                unavailable=sorted(unavailable),
            )
        return self._manifest.static_capabilities | frozenset(probed)
