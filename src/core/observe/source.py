"""将会话流引用转换为各观测端共用的消息来源标签。"""

from __future__ import annotations

from src.core.platform_io.types import StreamRef


def source_label(stream: StreamRef, *, direct_name: str = '') -> str:
    """仅根据传入的 stream 行生成一眼可分的来源标签。

    :param stream: 已解析的 stream 引用。
    :param direct_name: 上游已解析的私聊对方显示名；群聊和桌面忽略该值。
    :return: 桌面返回 ``桌面``；私聊优先带对方名；群聊优先带可读展示名，
        尚未取得名称时退回外部标识。
    :raises ValueError: 非桌面 stream 的外部标识为空，或收到未知 stream kind。
    副作用：不查数据库、不读取配置，也不修改传入引用。
    """

    if stream.kind == 'desktop':
        return '桌面'
    external_id = stream.external_id.strip()
    if not external_id:
        raise ValueError(f'{stream.kind} stream 缺少外部标识')
    if stream.kind == 'direct':
        return f'私聊·{direct_name.strip() or external_id}'
    if stream.kind == 'group':
        return f'群聊·{stream.display_name.strip() or external_id}'
    raise ValueError(f'未知 stream kind：{stream.kind}')


__all__ = ['source_label']
