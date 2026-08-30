"""统一工具执行协议。

本模块只定义执行器的最小异步接口：一次调用进、一个结果出。执行器不负责参数
校验（消费方在构造 ToolInvocation 之前已完成）、不负责超时与账本（由执行层
统一处理），只表达「这个工具怎么执行」。

依赖：``spec`` 的调用与结果结构；被 ``registry`` 登记、由消费方在
执行阶段调用。本模块当前只有协议，没有实现——认知动作执行器的迁移与多工具
执行语义属于后续批次，一期只把接口定下来。
"""

from __future__ import annotations

from typing import Protocol

from .spec import ToolContext, ToolExecutionResult, ToolInvocation


class ToolExecutor(Protocol):
    """工具执行器的窄协议。"""

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """执行一次工具调用。

        :param invocation: 已解析并通过参数校验的调用请求。
        :param context: 当前会话与回合的只读上下文。
        :return: 执行结果；失败时 error_message 必须给出可回灌模型的原因。
        :raises Exception: 本机故障（数据库、网络等）原样上抛，由执行层按
            「工具故障」记账，不转成模型失败状态。
        副作用：取决于具体工具实现；协议不约束副作用范围。
        """
        ...
