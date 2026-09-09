"""内置认知动作的工具执行器适配。

把认知动作的窄执行协议（CognitiveRequest / CognitiveObservation）适配为
统一工具执行协议（ToolInvocation / ToolContext / ToolExecutionResult）：
认知动作绑定进注册表之后，对话代理不再直接持有 CognitiveExecutor，统一经
ToolExecutor.execute 执行检索。适配只做参数搬运与结果转换，检索行为不变。

依赖：``src.core.agent.cognition`` 的请求与结果结构、``spec``；
被 ``src.core.services.chat`` 装配（把三个认知动作包装后绑定进注册表），
不反向依赖聊天服务或模型层。
"""

from __future__ import annotations

from src.core.agent.cognition import CognitiveAction, CognitiveRequest

from .spec import ToolContext, ToolExecutionResult, ToolInvocation

# 工具调用里检索词的参数名。与动作工具的参数 Schema 同一口径：认知动作的
# 声明参数就叫 query，这里只做搬运不做校验（非空校验在动作头）。
_QUERY_ARGUMENT = 'query'


class CognitiveToolExecutor:
    """把一个认知动作包装成统一工具执行器。"""

    def __init__(self, action: CognitiveAction) -> None:
        """保存被包装的认知动作。

        :param action: 已构造的认知动作实现；其 name 必须与注册表登记名一致。
        """
        self._action = action

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """执行一次认知检索并返回观察正文。

        :param invocation: 已解析的工具调用；arguments 里必须携带非空 query。
        :param context: 当前会话与回合上下文，提供检索范围与消息水位。
        :return: 成功时观察正文与命中数进 metadata；缺少 query 属于装配路径
            漏传，按工具失败返回，不进模型失败账本。
        :raises Exception: 检索本身的本机故障（数据库错误等）原样上抛，
            由执行层按「工具故障」记账。
        副作用：与具体认知动作的检索行为一致，不额外引入。
        """
        query = invocation.arguments.get(_QUERY_ARGUMENT)
        if not isinstance(query, str) or not query.strip():
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message='认知工具缺少非空 query 参数',
            )
        observation = await self._action.execute(
            CognitiveRequest(
                action=self._action.name,
                query=query.strip(),
                stream_id=context.stream_id,
                stream_kind=context.stream_kind,
                person_ids=context.person_ids,
                message_watermark=context.frame.message_watermark,
                cross_person=context.cross_person,
            ),
        )
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation=observation.text,
            metadata={'hit_count': observation.hit_count},
        )
