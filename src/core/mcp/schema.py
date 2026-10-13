"""把服务参数结构收窄到模型厂商与本地校验器共同支持的子集。

不能无歧义展开的组合或引用会拒绝该工具，防止一个声明破坏整次决策请求。
"""

from copy import deepcopy
from typing import Any, Dict, FrozenSet, Tuple

# 十个已验证的声明关键字；其他约束由服务器校验，不进入模型请求。
ALLOWED_KEYS = frozenset({'type', 'description', 'properties', 'required', 'additionalProperties',
                          'enum', 'items', 'minimum', 'minLength', 'minItems'})
SUPPORTED_TYPES = frozenset({'object', 'array', 'string', 'integer', 'number', 'boolean'})


class McpSchemaError(ValueError):
    """参数结构无法安全收窄；错误消息携带确切关键字。"""


def narrow_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """展开本地引用与单分支组合，返回根为 object 的十关键字子集。

    :param schema: 服务原始 input_schema，不会原地修改。
    :return: 可交给本地参数校验器与模型接口的结构。
    :raises McpSchemaError: 根非对象、循环或外部引用、多分支组合及不支持的类型。
    """
    def visit(node: Any, refs: FrozenSet[str] = frozenset()) -> Dict[str, Any]:
        if not isinstance(node, dict):
            raise McpSchemaError('不支持：参数结构必须是对象')
        node = deepcopy(node)
        if 'patternProperties' in node:
            raise McpSchemaError('不支持：patternProperties')
        if '$ref' in node:
            ref = node.pop('$ref')
            if not isinstance(ref, str) or not ref.startswith(('#/$defs/', '#/definitions/')):
                raise McpSchemaError('不支持：$ref 外部引用')
            if ref in refs:
                raise McpSchemaError('不支持：$ref 循环引用')
            target: Any = schema
            for part in ref[2:].split('/'):
                part = part.replace('~1', '/').replace('~0', '~')
                if not isinstance(target, dict) or part not in target:
                    raise McpSchemaError('不支持：$ref 目标不存在')
                target = target[part]
            resolved = visit(target, refs | {ref})
            resolved.update(node)
            node = resolved
        for keyword in ('anyOf', 'oneOf', 'allOf'):
            if keyword not in node:
                continue
            branches = node.pop(keyword)
            if not isinstance(branches, list):
                raise McpSchemaError(f'不支持：{keyword}')
            if keyword != 'allOf':
                branches = [b for b in branches if not isinstance(b, dict) or b.get('type') != 'null']
            if len(branches) != 1:
                raise McpSchemaError(f'不支持：{keyword} 多分支组合')
            resolved = visit(branches[0], refs)
            resolved.update(node)
            node = resolved
        typ = node.get('type')
        if isinstance(typ, list):
            non_null = [t for t in typ if t != 'null']
            if len(non_null) != 1 or any(t not in SUPPORTED_TYPES for t in non_null):
                raise McpSchemaError('不支持：type 类型数组')
            typ = non_null[0]
        if typ is None and 'properties' in node:
            typ = 'object'
        if typ is not None and typ not in SUPPORTED_TYPES:
            raise McpSchemaError(f'不支持：type {typ}')
        result = {key: deepcopy(value) for key, value in node.items() if key in ALLOWED_KEYS}
        if typ is not None:
            result['type'] = typ
        if 'properties' in result:
            if not isinstance(result['properties'], dict):
                raise McpSchemaError('不支持：properties')
            result['properties'] = {name: visit(child, refs) for name, child in result['properties'].items()}
        if 'items' in result:
            result['items'] = visit(result['items'], refs)
        if isinstance(result.get('additionalProperties'), dict):
            result['additionalProperties'] = visit(result['additionalProperties'], refs)
        if 'required' in result and (not isinstance(result['required'], list) or
                                    not all(isinstance(v, str) for v in result['required'])):
            raise McpSchemaError('不支持：required')
        return result
    narrowed = visit(schema)
    if narrowed.get('type') != 'object':
        raise McpSchemaError('不支持：根必须是 object')
    return narrowed
