"""重排任务的线格式：把一组候选文本按与检索词的相关度逐条打分。

通用重排协议为 ``POST {endpoint}``，请求体 ``{model, query, documents}``，响应体
``results[].index`` 与 ``results[].relevance_score``，用量在响应体顶层的 ``usage``。各厂商只有端点路径不同：
厂商的 ``base_url`` 写到版本号（如 ``/v1``）时补 ``/rerank``；以 ``/rerank`` 或
``/reranks`` 结尾时视为完整端点、原样使用，用于路径写法不同的厂商。

对外接口：

- :func:`request_rerank`：用一个候选发送一次请求，返回与输入等长的分数；
- :func:`rerank_with`：经任务路由器按候选顺序重试，供认知动作装配。

依赖 ``llm_models.openai`` 的 HTTP 连接契约与错误分类；由 ``services.chat`` 装配给
``agent.cognition`` 的 recall 与 consult。
"""

from typing import Awaitable, Callable, Dict, List, Sequence
from urllib.parse import urlsplit
import math

import httpx

from .openai import LlmError, OpenAiChatProvider, resolve_base_url
from .router import ModelRouter
from .usage import report_usage

from src.core.config.schema import ModelCandidate

# 请求体里由协议独占的字段，``extra_body`` 不得覆盖。
_RESERVED_FIELDS = frozenset({'model', 'query', 'documents'})

# 检索词与候选文本列表进、等长分数列表出的重排回调。
Reranker = Callable[[str, Sequence[str]], Awaitable[List[float]]]


def _endpoint_path(base_url: str) -> str:
    """按 ``base_url`` 是否已是完整端点决定要补的路径。

    :param base_url: 已解析的厂商基地址。
    :return: 需要拼接到 ``base_url`` 之后的路径；完整端点时为空串。
    副作用：无。
    """
    path = urlsplit(base_url).path.rstrip('/')
    return '' if path.endswith(('/rerank', '/reranks')) else '/rerank'


def _scores(results: object, count: int) -> List[float]:
    """按 ``index`` 还原输入顺序并校验分数。

    :param results: 响应体的 ``results`` 字段。
    :param count: 输入候选条数。
    :return: 与输入等长、按输入顺序排列的分数。
    :raises LlmError: 条数不符、index 缺号或重复、分数不是有限数值。
    副作用：无。
    """
    if not isinstance(results, list) or len(results) != count:
        raise LlmError('format', f'重排返回条数错误：期望 {count}')
    scores: Dict[int, float] = {}
    for item in results:
        index = item['index']
        score = item['relevance_score']
        if type(index) is not int or not 0 <= index < count or index in scores:
            raise LlmError('format', '重排返回 index 越界或重复')
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise LlmError('format', '重排分数必须是有限数值')
        scores[index] = float(score)
    return [scores[index] for index in range(count)]


async def request_rerank(
    candidate: ModelCandidate,
    query: str,
    documents: Sequence[str],
) -> List[float]:
    """用一个候选模型给一组文本打相关度分。

    :param candidate: 厂商连接与模型配置。
    :param query: 检索词。
    :param documents: 待打分的候选文本；为空时不发请求。
    :return: 与 ``documents`` 等长的分数，顺序与输入一致。
    :raises LlmError: ``extra_body`` 覆盖协议字段、HTTP 失败、网络错误或响应结构非法。
    副作用：一次网络请求，向观测链路写一条脱敏请求快照，并把响应体里的 ``usage``
        上报给当前候选尝试。
    """
    if not documents:
        return []
    conflict = _RESERVED_FIELDS & candidate.extra_body.keys()
    if conflict:
        raise LlmError('model', f'重排模型的 extra_body 不得覆盖协议字段：{sorted(conflict)}')
    base_url = resolve_base_url(candidate.kind, candidate.base_url)
    provider = OpenAiChatProvider(
        base_url, candidate.api_key, candidate.identifier,
        headers=candidate.default_headers, query=candidate.default_query,
        auth_type=candidate.auth_type, auth_name=candidate.auth_name,
        timeout_ms=candidate.timeout_ms,
    )
    headers = provider.request_headers()
    body = {
        **candidate.extra_body,
        'model': candidate.identifier, 'query': query, 'documents': list(documents),
    }
    url = provider.request_url(_endpoint_path(provider.base_url))
    provider.record_request(url, headers, body)
    async with httpx.AsyncClient(timeout=candidate.timeout_ms / 1000) as client:
        try:
            response = await client.post(url, headers=headers, json=body)
            await provider.check_response(response)
            data = response.json()
            # 先上报用量再校验分数：结构不合格的响应服务商照样计费。
            report_usage(data.get('usage') if isinstance(data, dict) else None, input_only=True)
            return _scores(data['results'], len(documents))
        except httpx.TimeoutException as exc:
            raise LlmError('network', f'请求超时（{candidate.timeout_ms / 1000}s）') from exc
        except httpx.RequestError as exc:
            raise LlmError('network', f'连不上 {provider.base_url}', str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise LlmError('format', f'重排响应结构错误：{exc}') from exc


def rerank_with(router: ModelRouter) -> Reranker:
    """把重排任务路由器包装成认知动作使用的回调。

    :param router: ``model_tasks.rerank`` 的路由器，调用方须先确认 ``ready``。
    :return: 按候选顺序重试、全部失败时抛出最后一次异常的重排回调。
    副作用：构造时无；每次调用经路由器发起网络请求并更新厂商健康状态。
    """
    async def rerank(query: str, documents: Sequence[str]) -> List[float]:
        if not documents:
            return []
        return await router.run(lambda candidate: request_rerank(candidate, query, documents))

    return rerank
