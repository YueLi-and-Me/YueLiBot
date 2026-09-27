"""实现文本与多模态向量线格式，复用对话 provider 的 HTTP 连接契约。

EmbeddingClient 负责业务分批及 packed 编码；本层处理图片请求、响应校验和
L2 归一化。任一响应项非法时整批抛出 LlmError，不把错位向量交给存储层。
"""

from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import urlsplit
import asyncio
import base64
import math

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

# 含图片输入时的单次请求条数上限（条），取值 1–``_BATCH``。
# - 现象：预处理已压到最长边 512、20 张合计约 1MB 后，百炼 20 张一批仍要 12–37 秒，
#   同一批不同时刻相差三倍，常规 30 秒连接超时下时成时败；5 张一批 4 秒左右。
# - 原因：耗时主要在服务端逐张处理图片（约 1–2 秒一张且随负载波动），不在上传体积。
# - 后果：调大会让批量补算随服务端负载间歇性整批超时，失败的整批保持 NULL。
_IMAGE_BATCH = 5

# 方舟一条融合输入对应一次请求；限制同批在途连接，避免批量补算打满服务商额度。
ARK_CONCURRENCY = 4
_DASHSCOPE_PATH = '/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding'


@dataclass(frozen=True)
class EmbedInput:
    """一条文本、已预处理图片或两者融合的输入；media_type 对应图片 MIME。"""

    text: str = ''
    image: bytes | None = None
    media_type: str = ''


def _image_uri(item: EmbedInput) -> str:
    """编码已预处理图片；缺少图片或 MIME 时立即报告配置错误。"""
    if item.image is None or not item.media_type:
        raise LlmError('model', '图片向量输入必须同时提供图片字节与 media_type')
    return f'data:{item.media_type};base64,{base64.b64encode(item.image).decode("ascii")}'


def _snapshot(value: Any) -> Any:
    """复制请求体并省略图片 data URI；实际 HTTP 请求仍使用原始值。"""
    if isinstance(value, str) and value.startswith('data:'):
        return f'<data URI 已省略，原始长度 {len(value)}>'
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snapshot(item) for item in value]
    return value


def _normalized(vectors: List, count: int, dim: int) -> List[List[float]]:
    """校验完整批次的数量、维度与数值后归一化；失败不返回任何向量。"""
    if len(vectors) != count:
        raise LlmError('format', f'向量返回条数错误：期望 {count}，实际 {len(vectors)}')
    result = []
    for vector in vectors:
        if not isinstance(vector, list) or not vector or len(vector) != dim:
            raise LlmError('format', f'向量维度错误：期望 {dim} 个非空元素')
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise LlmError('format', '向量必须全部为有限数值')
        scale = max(abs(value) for value in vector)
        if scale == 0:
            raise LlmError('format', '向量不能是零向量')
        # 先缩放再求范数，避免有限但极大的值平方溢出，也避免极小值下溢。
        scaled = [value / scale for value in vector]
        norm = math.hypot(*scaled)
        result.append([value / norm for value in scaled])
    return result


def _ordered(items: List[Dict], count: int) -> List[Dict]:
    """按 index 恢复输入顺序，同时拒绝缺号、重复号和超出输入范围的响应。"""
    if not isinstance(items, list) or len(items) != count:
        raise LlmError('format', f'向量返回条数错误：期望 {count}')
    indices = [item['index'] for item in items]
    if any(type(index) is not int for index in indices) or sorted(indices) != list(range(count)):
        raise LlmError('format', '向量返回 index 不连续或重复')
    return sorted(items, key=lambda item: item['index'])


async def request_embeddings(candidate: ModelCandidate, items: List[EmbedInput]) -> List[List[float]]:
    """根据显式 api_format 发送一批输入，返回已校验且归一化的向量。

    :param candidate: 厂商连接、模型、协议与维度配置。
    :param items: 单批最多二十条输入；方舟按条请求并限制并发为四。
    :raises LlmError: 协议冲突、HTTP 失败、响应数量或数值不合法。
    副作用：网络请求及脱敏快照；不写入数据库。
    """
    if not items:
        return []
    if len(items) > _BATCH:
        raise LlmError('model', f'向量单批输入不能超过 {_BATCH} 条')
    protocol = candidate.api_format
    if protocol == 'openai' and any(item.image is not None for item in items):
        raise LlmError('model', 'OpenAI 文本向量协议不接受图片输入')
    reserved = {'model', 'input'} if protocol == 'dashscope_multimodal' else {'model', 'input', 'encoding_format'}
    if protocol != 'openai' and reserved & candidate.extra_body.keys():
        raise LlmError('model', f'{protocol} extra_body 不得覆盖独占字段：{sorted(reserved & candidate.extra_body.keys())}')
    base_url = resolve_base_url(candidate.kind, candidate.base_url)
    if protocol == 'dashscope_multimodal':
        parsed = urlsplit(base_url)
        base_url = f'{parsed.scheme}://{parsed.netloc}'
    provider = OpenAiChatProvider(
        base_url, candidate.api_key, candidate.identifier,
        headers=candidate.default_headers, query=candidate.default_query,
        auth_type=candidate.auth_type, auth_name=candidate.auth_name,
        timeout_ms=candidate.timeout_ms,
    )
    headers = provider.request_headers()
    async with httpx.AsyncClient(timeout=candidate.timeout_ms / 1000) as client:
        async def post(path: str, body: Dict) -> Dict:
            url = provider.request_url(path)
            provider.record_request(url, headers, _snapshot(body))
            response = await client.post(url, headers=headers, json=body)
            await provider.check_response(response)
            return response.json()

        try:
            if protocol == 'openai':
                data = await post('/embeddings', {'input': [item.text for item in items], 'model': candidate.identifier})
                vectors = [item['embedding'] for item in _ordered(data['data'], len(items))]
            elif protocol == 'dashscope_multimodal':
                contents = []
                for item in items:
                    content = {'text': item.text} if item.text or item.image is None else {}
                    if item.image is not None:
                        content['image'] = _image_uri(item)
                    contents.append(content)
                body = {'model': candidate.identifier, 'input': {'contents': contents},
                        'parameters': {'dimension': candidate.embedding_dim}, **candidate.extra_body}
                data = await post(_DASHSCOPE_PATH, body)
                entries = _ordered(data['output']['embeddings'], len(items))
                vectors = []
                for entry, item in zip(entries, items, strict=True):
                    expected = 'text' if item.image is None else ('fused' if item.text else 'image')
                    if entry['type'] != expected:
                        raise LlmError('format', '该模型不支持融合向量，请用 2026-03-06 及以后的版本'
                                       f'（期望 {expected}，实际 {entry["type"]}）')
                    vectors.append(entry['embedding'])
            elif protocol == 'ark_multimodal':
                semaphore = asyncio.Semaphore(ARK_CONCURRENCY)

                async def fused(item: EmbedInput) -> List[float]:
                    content = [{'type': 'text', 'text': item.text}] if item.text or item.image is None else []
                    if item.image is not None:
                        content.append({'type': 'image_url', 'image_url': {'url': _image_uri(item)}})
                    async with semaphore:
                        # 保留套餐路径；改写成普通 /api/v3 会导致套餐凭据无权限。
                        data = await post('/embeddings/multimodal', {
                            'model': candidate.identifier, 'input': content,
                            'encoding_format': 'float', **candidate.extra_body,
                        })
                    return data['data']['embedding']

                # 等待整批结束再传播错误，避免关闭连接后其它在途请求仍访问客户端。
                vectors = await asyncio.gather(*(fused(item) for item in items), return_exceptions=True)
                for vector in vectors:
                    if isinstance(vector, BaseException):
                        raise vector
            else:
                raise LlmError('model', f'不支持的向量协议：{protocol}')
            return _normalized(vectors, len(items), candidate.embedding_dim)
        except httpx.TimeoutException as exc:
            raise LlmError('network', f'请求超时（{candidate.timeout_ms / 1000}s）') from exc
        except httpx.RequestError as exc:
            raise LlmError('network', f'连不上 {provider.base_url}', str(exc)) from exc
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise LlmError('format', f'向量响应结构错误：{exc}') from exc
