"""最小可运行的工具插件示例，演示写一个插件必须做的四件事。

四件事：一份 ``_manifest.json`` 清单、一个继承 :class:`ToolPlugin` 的类、至少一个用
``@tool`` 装饰的 async 方法、以及按需覆写的 ``on_load`` / ``on_unload``。除此之外没有
别的必需品——没有注册调用，也不需要改动任何主体代码：宿主扫到目录、读清单、导入
本文件、找出其中唯一的 ToolPlugin 子类，接线就完成了。

配置是第五件可选的事：声明一个 :class:`PluginConfig` 子类挂到 ``config_model``，
宿主会据此在本目录生成带注释的 ``config.toml``。**目录里没有手写的 TOML**——
声明只有模型这一份，两份声明必然随演进漂移。

**本插件默认关闭**（``HelloConfig.enabled`` 的默认值是 ``False``）。它只是教学素材，
没有理由占用模型每一轮的工具声明预算。想试就把生成出的 ``config.toml`` 里那一行改成
``true`` 再重启。

本文件刻意只做一件事（把参数拼成一句问候），因为它的用途是让人看清骨架。真实工具
的写法参考同目录下的 ``forward-message``：那里演示了有状态缓存、``observe_inbound``
入站观察，以及用 ``stream_capabilities`` 按会话收窄工具可见性。

依赖 ``src.plugin_system`` 的基类与装饰器、``src.core.tooling.spec`` 的协议数据结构；
被 ``PluginRegistry`` 在启动期发现并加载，不被任何主体代码直接引用。
"""

from __future__ import annotations

from src.core.tooling.spec import (
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
)
from pydantic import Field

from src.plugin_system import PluginConfig, PluginManifest, ToolPlugin, tool

# 未提供 name 参数时的称呼。
DEFAULT_GREETING_TARGET = '你'

# 单次问候允许的最大称呼长度，单位为字符。参数来自模型输出，长度必须自己兜住：
# observation 会原样回灌进下一轮提示词，不设上限等于把提示词预算交给模型决定。
MAX_NAME_LENGTH = 32


class HelloConfig(PluginConfig):
    """本插件的配置。

    每个字段的 ``description`` 会原样成为生成出的 TOML 里的注释——那是用户唯一能
    看到的字段解释，所以要写成给人看的话，不是复述字段名。
    """

    enabled: bool = Field(
        default=False,
        description='是否启用示例插件；它只是教学素材，默认关着',
    )
    greeting_suffix: str = Field(
        default='好',
        description='跟在称呼后面的问候词，例如「好」会拼成「月璃好」',
    )


class HelloYueLiPlugin(ToolPlugin):
    """贡献一个只读问候工具的示例插件。

    **本模块必须只有一个 ToolPlugin 子类。** 加载器用「模块内唯一实现」定位入口类，
    出现第二个会直接报错。这是刻意的：多入口意味着加载顺序决定行为，而那是查起来
    最费劲的一类问题。
    """

    # 声明配置模型。宿主据此生成 config.toml，并把读到的实例注入 self.config。
    config_model = HelloConfig

    def __init__(self, manifest: PluginManifest) -> None:
        """保存清单并初始化调用计数。

        :param manifest: 已由宿主校验的插件清单；基类存下它，供 ``self.manifest`` 读取。

        构造函数只接受清单一个参数——工具插件由加载器统一构造，不像适配器那样可以
        接收额外关键字参数。配置不走构造函数，由宿主在 ``on_load`` 之前注入。
        """
        super().__init__(manifest)
        self._greeted = 0
        self._suffix = HelloConfig().greeting_suffix

    async def on_load(self) -> None:
        """插件加载时的准备工作：把配置读进运行期字段。

        :return: ``None``。
        :raises Exception: 配置缺失或非法时原样上抛；后果是本插件被移出注册表，
            其余插件与主体照常启动。

        **此时不应发起网络 I/O。** 加载失败应当是纯粹的配置问题，混进网络故障会让
        「配置写错」与「对端没起来」在现场分不出来。

        ``self.config`` 由宿主在本方法之前注入，类型就是 :attr:`config_model`。
        """
        self._greeted = 0
        assert isinstance(self.config, HelloConfig)
        self._suffix = self.config.greeting_suffix

    async def on_unload(self) -> None:
        """释放插件持有的资源。

        :return: ``None``。

        **必须幂等。** 宿主在停机与重载两条路径上都会调用它，重复调用不得抛异常。
        """
        self._greeted = 0

    @tool(
        name='hello_yueli',
        # 描述的口径是「什么时候用它」，不是复述工具名——模型只凭这一句决定要不要调。
        description='当用户明确要求演示插件工具时调用，返回一句问候，用于确认插件链路已经打通。',
        parameters={
            'type': 'object',
            'properties': {
                'name': {
                    'type': 'string',
                    'description': (
                        f'称呼，最长 {MAX_NAME_LENGTH} 字；'
                        f'省略则用「{DEFAULT_GREETING_TARGET}」。'
                    ),
                },
            },
            'required': [],
            # 关掉额外字段：模型传错参数名时当场失败，好过静默忽略后返回一个
            # 看似正常、实则没按要求执行的结果。
            'additionalProperties': False,
        },
        # readonly 是默认值，显式写出以示范三档副作用分级的存在：
        # reversible 需要显式启用，irreversible 一律被注册表拒绝登记。
        side_effect='readonly',
    )
    async def hello_yueli(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """拼出一句问候并回灌给模型。

        :param invocation: 本次调用；``arguments`` 是已解析的参数对象。
        :param context: 会话与回合上下文，只读。
        :return: 成功时 ``observation`` 为模型可见的观察正文；失败时必须给出
            ``error_message``，否则 :class:`ToolExecutionResult` 自己会拒绝构造。

        执行体必须是 ``async``：装饰器在导入期就检查这一点，同步方法当场报错。
        """
        raw_name = invocation.arguments.get('name', DEFAULT_GREETING_TARGET)
        if not isinstance(raw_name, str):
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message='name 必须是字符串',
            )
        name = raw_name.strip() or DEFAULT_GREETING_TARGET
        if len(name) > MAX_NAME_LENGTH:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=f'name 最长 {MAX_NAME_LENGTH} 字，实际 {len(name)} 字',
            )

        self._greeted += 1
        # context 带着会话与回合信息，工具可据此改变行为。这里只用会话类型举例，
        # 完整字段见 src/core/tooling/spec.py 的 ToolContext。
        scene = '群聊' if context.stream_kind == 'group' else '私聊'
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation=(
                f'{name}{self._suffix}，我是示例插件 {self.manifest.name}。'
                f'当前是{scene}，本次进程内第 {self._greeted} 次调用。'
            ),
            # metadata 不进模型视野，用于把结构化信息交给调用方与日志。
            metadata={'greeted': self._greeted, 'streamId': context.stream_id},
        )


__all__ = [
    'DEFAULT_GREETING_TARGET',
    'MAX_NAME_LENGTH',
    'HelloConfig',
    'HelloYueLiPlugin',
]
