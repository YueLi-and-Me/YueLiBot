"""定义兼容层对外的数据与错误，禁止把 SDK 对象传入工具或插件层。

所有结果为不可变数据；协议与包版本范围在此声明，由 sdk 在连接前核对。
"""

from dataclasses import dataclass
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

# SDK 包版本左闭右开；协议版本按 ISO 日期字符串比较，两个端点均包含。
SDK_VERSION_RANGE = ((2, 3, 0), (3, 0, 0))
PROTOCOL_VERSION_MIN = '2024-11-05'
PROTOCOL_VERSION_MAX = '2026-07-28'
# 凭证子串脱敏的最短长度，单位字符。短于 8 字的值不可能是有效凭证，
# 按子串替换只会破坏无关文本；这些字段在管理页仍按字段置空。
SECRET_MIN_CHARS = 8


@dataclass(frozen=True)
class McpServerInfo:
    """保存服务自述名称、版本与实际协商的协议版本。"""

    name: str
    version: str
    protocol_version: str


@dataclass(frozen=True)
class McpTool:
    """保存发现的工具原文；未声明只读时 read_only 为 None，不推断副作用。"""

    name: str
    title: Optional[str]
    description: Optional[str]
    input_schema: Dict[str, Any]
    read_only: Optional[bool]


@dataclass(frozen=True)
class McpContentItem:
    """保存显示所需的内容；未知块把类名放在 text 中，不读取其私有字段。"""

    kind: Literal['text', 'image', 'audio', 'resource_link', 'resource', 'unknown']
    text: str = ''
    mime_type: str = ''
    uri: str = ''
    title: str = ''


@dataclass(frozen=True)
class McpToolResult:
    """统一工具成功与工具内部错误；text 只拼接文本块，structured 保留其形状。"""

    is_error: bool
    items: Tuple[McpContentItem, ...] = ()
    structured: Any = None

    @property
    def text(self) -> str:
        """按服务返回顺序用换行拼接全部文本块，供插件解析。"""
        return '\n'.join(item.text for item in self.items if item.kind == 'text')


class McpUnavailableError(RuntimeError):
    """部署者未授权、服务尚未连接或今日额度耗尽；消息直接向插件展示。"""


class McpFailure(RuntimeError):
    """兼容层收口的连接、协议版本、超时或服务器错误，不包含 SDK 对象。"""

    def __init__(self, category: str, message: str) -> None:
        """保存五类失败之一与已遮去凭证的原因，生成固定中文前缀。"""
        self.category = category
        self.message = message
        super().__init__(f'MCP 工具失败（{category}）：{message}')


def connection_secrets(api_key: str, *, headers: Mapping[str, str], env: Mapping[str, str]) -> Tuple[str, ...]:
    """统一收集连接的有效凭证，供兼容层、运行状态和临时测试脱敏。

    :param api_key: 已解析厂商引用的实际密钥，未设置时为空字符串。
    :param headers: 原始请求头；带 {api_key} 的模板只由解析后的密钥参与脱敏。
    :param env: 部署者显式配置的子进程环境值。
    :return: 去重并过滤短值后的凭证元组，不包含凭证模板。
    """
    values = (api_key, *[value for value in headers.values() if '{api_key}' not in value], *env.values())
    return tuple(sorted({value for value in values if len(value) >= SECRET_MIN_CHARS}, key=len, reverse=True))


def redact(text: str, secrets: Tuple[str, ...]) -> str:
    """遮去达到最短凭证长度的已知值；短环境开关不能改坏正文与协议版本。"""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= SECRET_MIN_CHARS:
            text = text.replace(secret, '[已隐藏]')
    return text
