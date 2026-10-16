"""按画面内容查找表情包的只读工具。

对方点名要某个角色、某种梗图时，决策模型调用 ``find_sticker`` 工具：先由
``EmojiLibrary.content_candidates`` 按画面描述命中数与画面向量取候选，再把候选缩放
拼成一张带编号的宫格图，交给视觉模型挑出最符合的一张或弃权；挑中的那张再单独送一次
视觉模型复核，复核不通过按没找到处理。找到时观察正文给出表情包编号，回复生成在台词后写
``<emoji id="编号"/>`` 发出；没找到时如实告知。

选图交给视觉模型而不是取相似度第一名：实测文字与画面的向量相似度挤在很窄的区间
（第一名 0.63、中位数 0.53），第一名常常与要求无关，发错比不发更糟。
单图复核的原因：宫格里每格只有 256 像素，且候选总是凑满 9 张，视觉模型在没有一张
对得上时仍倾向挑一张（实测「猫猫 敲键盘」在 9 张都没有键盘时选了第 9 张）；单独看
一张图只需答是否，误判少得多。

依赖 ``EmojiLibrary``、视觉模型提供者与 ``tooling`` 的工具协议；由 ``main`` 装配后
交给 ``ChatService`` 登记进工具注册表。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, List, Sequence

from PIL import Image, ImageDraw, ImageFont

import base64
import math
import re

from .chat_image import strip_model_markup
from .emoji import EmojiLibrary, sticker_id

from src.core.logging.logger import get_logger
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt
from src.core.tooling.spec import ToolContext, ToolExecutionResult, ToolInvocation, ToolSpec

logger = get_logger(__name__)

STICKER_SEARCH_TOOL = 'find_sticker'
# 宫格候选张数：3×3 一张图。候选多则更可能含对的图，但单格变小，视觉模型更难辨认
# 图上的文字；9 张时每格 256 像素，整图 768 像素见方。
STICKER_CANDIDATES = 9
# 宫格列数。
_GRID_COLUMNS = 3
# 宫格单格边长（像素），含四周留白。
_GRID_CELL = 256
# 单格内图片四周的留白（像素），避免相邻两张图贴在一起被当成一张。
_GRID_PADDING = 8
# 编号字号（像素）。编号画在每格左上角的黑底白字方块里。
_LABEL_FONT_SIZE = 36
# 复核单图的最长边（像素）：只缩小不放大，图上小字仍能看清。
_VERIFY_MAX_EDGE = 768
# 工具超时（毫秒）：一次查询向量请求、读图拼图与至多两次视觉模型调用。
STICKER_SEARCH_TIMEOUT_MS = 45000

STICKER_SEARCH_SPEC = ToolSpec(
    name=STICKER_SEARCH_TOOL,
    description=(
        '在自己的表情包库里按画面内容找一张表情包。对方点名要某个角色、某种梗图或'
        '某张表情包时使用；找到后在回复里把它发出去，没找到就如实说没有。'
    ),
    parameters={
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': (
                    '画面里应有的内容：角色名、外观特征、物件或图上的文字，用空格分隔，'
                    '例如「DeepSeek娘 蓝发 鲸鱼尾 女仆」；不写情绪。对方用了昵称或黑话时，'
                    '换成它指代的角色与外观再写'
                ),
            },
        },
        'required': ['query'],
        'additionalProperties': False,
    },
    capabilities=frozenset({'emoji'}),
    timeout_ms=STICKER_SEARCH_TIMEOUT_MS,
)


class StickerSearch:
    """``find_sticker`` 工具的执行器。"""

    def __init__(
        self,
        library: EmojiLibrary,
        provider: Any | None,
        *,
        temperature: float,
        max_tokens: int | None,
    ) -> None:
        """绑定表情包库与视觉模型。

        :param library: 表情包库。
        :param provider: 视觉路由选出的模型提供者；为 ``None`` 时每次调用都按失败返回。
        :param temperature: 挑选调用的采样温度。
        :param max_tokens: 挑选调用的输出上限；``None`` 表示沿用模型默认。
        """
        self._library = library
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """检索候选、请视觉模型挑选，并把结果写成观察正文。

        :param invocation: 已通过参数校验的调用，``query`` 为画面内容检索词。
        :param context: 当前会话与回合上下文。
        :return: 找到或弃权都是成功结果；检索词为空、视觉模型不可用或挑选、复核
            输出无法解析时为失败结果，失败原因回灌模型。
        :raises OSError: 候选表情包文件不可读；启动校验已核对过全部文件，出现即为
            本机故障，原样上抛。
        副作用：一次查询向量请求、一次挑选调用，挑中时再加一次复核调用；写一条
            sticker_search 观察事件。
        """
        query = str(invocation.arguments.get('query', '')).strip()
        if not query:
            return _failure('检索词为空')
        if self._provider is None:
            logger.warning('sticker_search_vision_unavailable')
            return _failure('视觉模型不可用，没法翻表情包')
        candidates = await self._library.content_candidates(query, STICKER_CANDIDATES)
        if not candidates:
            return _success('你的表情包库是空的，没有可发的表情包。')
        images = [self._library.read_file(candidate.send_ref) for candidate in candidates]
        prompt = get_prompt('emoji.pick').render(query=query, count=str(len(candidates)))
        raw = await self._ask(query, prompt, compose_sticker_grid(images))
        if raw is None:
            return _failure('看表情包时视觉模型调用失败')
        try:
            choice = parse_pick(raw, len(candidates))
        except ValueError as exc:
            logger.warning('sticker_pick_unparsable', query=query, output=raw[:80], error=str(exc))
            return _failure('看表情包时没有得到有效的挑选结果')
        picked = candidates[choice - 1] if choice else None
        rejected = ''
        if picked is not None:
            raw = await self._ask(
                query,
                get_prompt('emoji.verify').render(query=query),
                render_sticker_frame(images[choice - 1]),
            )
            if raw is None:
                return _failure('看表情包时视觉模型调用失败')
            try:
                confirmed = parse_verdict(raw)
            except ValueError as exc:
                logger.warning('sticker_verify_unparsable', query=query, output=raw[:80], error=str(exc))
                return _failure('看表情包时没有得到有效的复核结果')
            if not confirmed:
                rejected = sticker_id(picked.content_hash)
                picked = None
        trace.emit(
            'sticker_search',
            turnId=context.turn_id,
            streamId=context.stream_id,
            query=query,
            candidates=[sticker_id(candidate.content_hash) for candidate in candidates],
            picked=sticker_id(picked.content_hash) if picked is not None else '',
            rejected=rejected,
        )
        if picked is None:
            return _success(f'你翻了翻表情包库，没有符合「{query}」的表情包。')
        # 描述多以句号结尾，去掉后再统一补一个，避免出现连续两个句号；以图上文字的
        # 问号、叹号结尾时不再补句号。
        label = (picked.description or picked.emotion_tags).rstrip('。.')
        ending = '' if label.endswith(('？', '！', '?', '!', '…')) else '。'
        return _success(
            f'你翻了翻表情包库，找到一张符合「{query}」的表情包，'
            f'编号 {sticker_id(picked.content_hash)}，画面：{label}{ending}'
        )

    async def _ask(self, query: str, prompt: str, image: bytes) -> str | None:
        """把一张 JPEG 图与提示词交给视觉模型，返回原始输出；调用失败时记录警告并返回 None。"""
        encoded = base64.b64encode(image).decode('ascii')
        raw = ''
        try:
            stream = self._provider.stream(
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {
                            'type': 'image_url',
                            # 图上的文字与角色细节是挑选依据，按高分辨率送入。
                            'image_url': {'url': f'data:image/jpeg;base64,{encoded}', 'detail': 'high'},
                        },
                    ],
                }],
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                require_text=True,
            )
            async for chunk in stream:
                if chunk.get('text'):
                    raw += chunk['text']
        except Exception as exc:
            logger.warning('sticker_pick_failed', query=query, error=str(exc))
            return None
        return raw


def compose_sticker_grid(images: Sequence[bytes]) -> bytes:
    """把若干张表情包缩放拼成一张宫格图，并在每格左上角画上从 1 开始的编号。

    编号必须画在图上：让视觉模型按阅读顺序数格子最容易数错，且错了无从核查。

    :param images: 表情包原始字节，按候选顺序排列；动图取首帧。
    :return: JPEG 编码的宫格图。
    :raises ValueError: 未给出任何图片。
    :raises PIL.UnidentifiedImageError: 某张图片无法解码。
    """
    if not images:
        raise ValueError('宫格图至少需要一张图片')
    rows = math.ceil(len(images) / _GRID_COLUMNS)
    columns = min(len(images), _GRID_COLUMNS)
    canvas = Image.new('RGB', (columns * _GRID_CELL, rows * _GRID_CELL), 'white')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=_LABEL_FONT_SIZE)
    inner = _GRID_CELL - 2 * _GRID_PADDING
    for index, image_bytes in enumerate(images):
        left = (index % _GRID_COLUMNS) * _GRID_CELL
        top = (index // _GRID_COLUMNS) * _GRID_CELL
        with Image.open(BytesIO(image_bytes)) as image:
            image.seek(0)
            frame = image.convert('RGBA')
        frame.thumbnail((inner, inner), Image.Resampling.LANCZOS)
        offset = (
            left + _GRID_PADDING + (inner - frame.width) // 2,
            top + _GRID_PADDING + (inner - frame.height) // 2,
        )
        canvas.paste(frame, offset, frame)
        label = str(index + 1)
        box = draw.textbbox((0, 0), label, font=font)
        width, height = box[2] - box[0], box[3] - box[1]
        draw.rectangle(
            (left, top, left + width + 16, top + height + 16),
            fill='black',
        )
        draw.text((left + 8 - box[0], top + 8 - box[1]), label, fill='white', font=font)
    output = BytesIO()
    canvas.save(output, format='JPEG', quality=90)
    return output.getvalue()


def render_sticker_frame(image_bytes: bytes) -> bytes:
    """把一张表情包转成供复核用的 JPEG：取首帧，透明处垫白底，最长边不超过 768 像素。

    :param image_bytes: 表情包原始字节。
    :return: JPEG 编码的单帧图。
    :raises PIL.UnidentifiedImageError: 图片无法解码。
    """
    with Image.open(BytesIO(image_bytes)) as image:
        image.seek(0)
        frame = image.convert('RGBA')
    frame.thumbnail((_VERIFY_MAX_EDGE, _VERIFY_MAX_EDGE), Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', frame.size, 'white')
    canvas.paste(frame, (0, 0), frame)
    output = BytesIO()
    canvas.save(output, format='JPEG', quality=90)
    return output.getvalue()


def parse_pick(raw: str, count: int) -> int:
    """解析视觉模型给出的编号。

    :param raw: 模型原始输出，应只含一个数字。
    :param count: 宫格候选张数。
    :return: 选中的编号（1 至 count），弃权为 0。
    :raises ValueError: 输出不是单个整数，或编号超出 0 至 count。
    """
    text = strip_model_markup(raw).strip()
    if not re.fullmatch(r'\d+', text):
        raise ValueError(f'输出不是单个数字：{text[:40]}')
    choice = int(text)
    if choice > count:
        raise ValueError(f'编号 {choice} 超出候选范围 1 至 {count}')
    return choice


def parse_verdict(raw: str) -> bool:
    """解析复核输出。

    :param raw: 模型原始输出，应只含「是」或「否」，允许带句号。
    :return: 「是」为 True，「否」为 False。
    :raises ValueError: 输出不是这两个字之一。
    """
    text = strip_model_markup(raw).strip().rstrip('。.')
    if text not in ('是', '否'):
        raise ValueError(f'输出不是「是」或「否」：{text[:40]}')
    return text == '是'


def _success(observation: str) -> ToolExecutionResult:
    """构造成功的工具结果。"""
    return ToolExecutionResult(tool_name=STICKER_SEARCH_TOOL, success=True, observation=observation)


def _failure(reason: str) -> ToolExecutionResult:
    """构造失败的工具结果；原因会回灌决策模型。"""
    return ToolExecutionResult(tool_name=STICKER_SEARCH_TOOL, success=False, error_message=reason)


__all__: List[str] = [
    'STICKER_CANDIDATES',
    'STICKER_SEARCH_SPEC',
    'STICKER_SEARCH_TOOL',
    'StickerSearch',
    'compose_sticker_grid',
    'parse_pick',
    'parse_verdict',
    'render_sticker_frame',
]
