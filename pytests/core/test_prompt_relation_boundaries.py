"""钉住两条防止「对某个人的关系」外溢到其他对话的提示词规则。

- ``activity.next``：``mood`` 每轮注入她和所有人的对话，写进「等哥哥回消息」这类
  针对具体人物的内容后，私聊陌生人时规划器会把对方当成那个人。
- ``expression.learn``：「称呼对方为老婆」被当作表达方式学进库、注入回复后，
  她在群里管其他联系人叫老婆。称呼代表关系，不属于说话风格。
"""

from __future__ import annotations

from src.core.prompts.registry import get_prompt


def test_activity_mood_excludes_person_directed_feelings():
    text = get_prompt('activity.next').text

    assert 'mood 会原样带进她和每一个人的对话，所以只写她自己的心情和精神状态' in text
    assert '对某个具体的人的期待、惦记或情绪' in text


def test_expression_learning_excludes_forms_of_address():
    text = get_prompt('expression.learn').text

    assert '不要学对人的称呼' in text
    assert '称呼代表两个人之间的关系，不是说话风格' in text
