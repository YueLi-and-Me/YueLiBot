"""向量协议的 HTTP 传输，由记忆客户端通过模型路由调用。

鉴权、地址解析、请求快照和错误分类复用对话 provider；本层只处理线格式。
"""

from typing import List

import httpx

from .openai import LlmError, OpenAiChatProvider, resolve_base_url

from src.core.config.schema import ModelCandidate

# 单次请求的文本条数上限。这是服务端硬限制，不是调优值：
# - 现象：批量回填时 provider 返回 400，正文写着
#   `batch size is invalid, it should not be larger than 20`；小批次同样的请求 200。
# - 原因：兼容模式的 embeddings 端点对 input 条数设了 20 的上限，96 条一律拒收。
# - 后果：长期无人发现，是因为 facts 表一直只有个位数条目，不足以构成一个大批次；
#   2026-08-24 迁入 22375 条知识后才第一次触发，当时 22368 条全部留 NULL。
#   调大这个值会让整个向量层静默退回 BM25——失败只记 warning，不中断调用方。
_BATCH = 20



async def request_embeddings(candidate: ModelCandidate, texts: List[str]) -> List[List[float]]:
    """请求一批文本向量；HTTP 与连接失败转换为路由可识别的 LlmError。

    :param candidate: 已装配的厂商连接及模型配置。
    :param texts: 最多二十条文本，由 EmbeddingClient 分批。
    :return: 按服务端 index 排序的浮点向量，不写数据库。
    """
    provider = OpenAiChatProvider(
        resolve_base_url(candidate.kind, candidate.base_url), candidate.api_key, candidate.identifier,
        headers=candidate.default_headers, query=candidate.default_query,
        auth_type=candidate.auth_type, auth_name=candidate.auth_name,
        timeout_ms=candidate.timeout_ms,
    )
    url = provider.request_url('/embeddings')
    headers = provider.request_headers()
    body = {'input': texts, 'model': candidate.identifier}
    provider.record_request(url, headers, body)
    async with httpx.AsyncClient(timeout=candidate.timeout_ms / 1000) as client:
        try:
            response = await client.post(url, headers=headers, json=body)
            await provider.check_response(response)
            data = response.json()
            items = sorted(data['data'], key=lambda item: item['index'])
            return [item['embedding'] for item in items]
        except httpx.TimeoutException as exc:
            raise LlmError('network', f'请求超时（{candidate.timeout_ms / 1000}s）') from exc
        except httpx.RequestError as exc:
            raise LlmError('network', f'连不上 {provider.base_url}', str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise LlmError('format', f'向量响应结构错误：{exc}') from exc
