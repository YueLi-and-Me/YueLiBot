"""校验跨进程传递的语音字节，供主体消息类型与适配器共用。

音频以纯 base64 字符串传递，避免协议端容器依赖主体的本地文件系统。
空串表示该位置仍发送文字；非空项与台词逐项对齐，保留混合回复的顺序。
"""

from base64 import b64decode
from binascii import Error
from typing import Sequence


# 单轮音频原始字节总量上限；编码后约 6.7 MiB，适配器接收帧上限为 8 MiB。
MAX_VOICE_BYTES = 5 * 1024 * 1024
MAX_VOICE_FRAME_BYTES = 8 * 1024 * 1024


def validate_voice_data(voice_data: Sequence[str], segment_count: int) -> None:
    """校验音频与台词对齐、编码有效且整轮不超限；无效时抛出 ValueError。"""
    if not voice_data:
        return
    if len(voice_data) != segment_count:
        raise ValueError('语音数据必须与回复台词逐项对齐')
    total = 0
    for encoded in voice_data:
        if not isinstance(encoded, str):
            raise ValueError('语音数据必须是 base64 字符串')
        if not encoded:
            continue
        if len(encoded) > ((MAX_VOICE_BYTES + 2) // 3) * 4:
            raise ValueError('单轮语音数据超过 5 MiB')
        try:
            total += len(b64decode(encoded, validate=True))
        except (Error, ValueError) as exc:
            raise ValueError('语音数据不是有效的 base64 编码') from exc
        if total > MAX_VOICE_BYTES:
            raise ValueError('单轮语音数据超过 5 MiB')
