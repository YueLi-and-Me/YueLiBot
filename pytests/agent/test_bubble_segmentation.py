"""台词到气泡的切分验收。"""

from __future__ import annotations

from src.core.agent.segmentation import split_into_bubbles, typing_delay_seconds
from src.core.config.schema import TypingConfig

TYPING = TypingConfig()


def test_short_line_stays_one_bubble() -> None:
    """短台词不切：一口气说得完的话，人不会分两条发。"""
    # 末尾句号被去掉：中文聊天里几乎没人用句号收尾。
    assert split_into_bubbles('好经典的段子，笑死。', TYPING) == ['好经典的段子，笑死']


def test_long_line_splits_at_punctuation() -> None:
    """超过目标长度的台词按标点切开，并去掉气泡结尾的逗号。"""
    bubbles = split_into_bubbles('不过某人不是连额度都没了吗，哪来的1314，梦里啥都有。', TYPING)

    assert bubbles == ['不过某人不是连额度都没了吗', '哪来的1314，梦里啥都有']
    assert not any(bubble.endswith('，') for bubble in bubbles)


def test_very_long_line_keeps_bubble_count_bounded() -> None:
    """台词越长每条气泡越长，条数收敛，不会碎成刷屏。"""
    text = (
        '肯定有影响啊，深度学习本来不就是机器学习的分支嘛。就好比你不学基础的写作文法，'
        '直接去开坑写几百万字的奇幻大长篇。虽然也能硬着头皮写，但遇到逻辑崩坏或者卡文的时候，'
        '你连最基础的原理都不懂，怎么去修补？'
    )
    bubbles = split_into_bubbles(text, TYPING)

    assert len(bubbles) <= 4
    # 长台词的气泡明显长于短台词的目标值，说明目标随总长放宽而不是无限切碎。
    assert max(len(bubble) for bubble in bubbles) > TYPING.bubble_target_chars


def test_decimal_and_version_numbers_are_not_split() -> None:
    """英文单词内部的句点不是断句点，否则模型名和版本号会被切碎。"""
    bubbles = split_into_bubbles('gemini-2.5-pro 跟 v4.0 比差多少？我也不知道啊', TYPING)

    assert bubbles == ['gemini-2.5-pro 跟 v4.0 比差多少？', '我也不知道啊']


def test_consecutive_marks_stay_together() -> None:
    """连续标点属于同一个语气单位，不能被拆成独立气泡。"""
    assert split_into_bubbles('哈？！你认真的吗', TYPING) == ['哈？！你认真的吗']


def test_text_without_separators_is_left_intact() -> None:
    """没有断句点时宁可留成一条长气泡，也不在句子中间硬断。"""
    text = '这一整句话完全没有任何标点符号所以切不开只能整条留着不动它'

    assert split_into_bubbles(text, TYPING) == [text]


def test_blank_text_produces_no_bubble() -> None:
    """空白台词不产生气泡，避免向平台投递空消息。"""
    assert split_into_bubbles('   \n  ', TYPING) == []


def test_typing_delay_grows_with_length() -> None:
    """打字停顿随字数增长，短气泡不会等出机器感。"""
    short = typing_delay_seconds('在呢', TYPING)
    long = typing_delay_seconds('在呢在呢，干嘛呀，怎么突然找我', TYPING)

    assert 0 < short < long


def test_latin_text_types_faster_than_chinese() -> None:
    """同样字数下拉丁字母连打更快，不能和中文同价。"""
    assert typing_delay_seconds('abcdefgh', TYPING) < typing_delay_seconds('中文八个字符测试', TYPING)


def test_typing_delay_is_capped() -> None:
    """超长气泡按字数线性算会让对方干等，必须截断在上限。"""
    assert typing_delay_seconds('长' * 200, TYPING) == TYPING.max_delay_seconds


def test_empty_text_needs_no_wait() -> None:
    """空文本不产生等待，避免在没有内容时白白拖延投递。"""
    assert typing_delay_seconds('', TYPING) == 0.0
