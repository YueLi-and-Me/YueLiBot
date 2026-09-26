"""人物画像的人名标注：生成时点明写的是谁、owner 关系只归 owner，注入时按人标注。

覆盖 ``PersonProfileMixin`` 的三个取名入口与 ``agent/profile.py`` 生成、注入两端：
情节摘要多为群聊，生成画像时不点明对象，模型会把 Bot 与 owner 的亲属关系安到
其他联系人头上；注入时不标注归属，私聊里 Bot 会以亲属称呼对方。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List

import sqlite3

from src.core.agent.profile import mark_dirty, profiles_for_injection, refresh_profiles
from src.core.agent.prompt import build_system_prompt
from src.core.memory.store import EpisodeInput, MemoryStore
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import ConversationContext
from src.core.services.chat.profiles import PersonProfileMixin

NOW = 1_800_000_000_000


class _Naming(PersonProfileMixin):
    """只挂载取名入口依赖的注册表与配置，不构造完整的 ChatService。"""

    def __init__(self, db: sqlite3.Connection, relationship: str) -> None:
        self._registry = StreamRegistry(db)
        self._cfg = SimpleNamespace(bot=SimpleNamespace(relationship=relationship, user_nickname=''))


class _CapturingProvider:
    """记录每次请求的消息序列，并回放固定印象文本。"""

    def __init__(self) -> None:
        self.requests: List[List[Dict[str, str]]] = []

    async def stream(self, messages: List[Dict[str, str]], **_kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.requests.append(messages)
        yield {'text': '他爱聊游戏。'}


def _seed_people(db: sqlite3.Connection) -> Dict[str, Any]:
    """建 owner 与一个联系人，联系人在群里另设群名片。"""

    registry = StreamRegistry(db)
    owner = registry.owner_person()
    registry.link_identity(owner, 'qq', '787', '凌白')
    contact = registry.create_person('contact', NOW)
    registry.link_identity(contact, 'qq', '257', '雨后')
    group = registry.get_or_create_stream('qq', 'group', '999')
    registry.set_group_card(contact, group, '雨后在摸鱼', NOW)
    direct = registry.get_or_create_stream('qq', 'direct', '257')
    return {'owner': owner, 'contact': contact, 'group': group, 'direct': direct}


def test_subject_lists_group_cards_after_account_name(db):
    people = _seed_people(db)
    naming = _Naming(db, '兄妹')

    assert naming._profile_subject(people['contact'].id) == '雨后（群名片：雨后在摸鱼）'
    assert naming._profile_subject(people['owner'].id) == '凌白'


def test_relation_note_assigns_relationship_to_owner_only(db):
    _seed_people(db)

    assert _Naming(db, '兄妹')._profile_relation_note() == (
        '凌白和她是兄妹关系，这层关系只属于凌白本人。'
    )
    assert _Naming(db, '')._profile_relation_note() == ''


def test_impression_name_matches_history_naming(db):
    people = _seed_people(db)
    naming = _Naming(db, '兄妹')
    contact = people['contact']

    in_group = ConversationContext(stream=people['group'], person=contact)
    in_direct = ConversationContext(stream=people['direct'], person=contact)

    # 群聊记录按群名片称呼，私聊按账号名，标注必须与之一致。
    assert naming._impression_name(contact.id, in_group) == '雨后在摸鱼'
    assert naming._impression_name(contact.id, in_direct) == '雨后'


async def test_generation_prompt_names_subject_and_owner_relation(db):
    people = _seed_people(db)
    contact = people['contact']
    naming = _Naming(db, '兄妹')
    store = MemoryStore(db)
    message_id = store.append_message(people['group'].id, contact.id, 'user', '聊游戏', NOW)
    store.add_episode(
        people['group'].id,
        EpisodeInput(
            summary='哥哥发了游戏截图，雨后在摸鱼也跟着聊了几句', cues=['游戏'],
            started_at=NOW - 60_000, ended_at=NOW, message_ids=[message_id],
        ),
        NOW,
    )
    mark_dirty(db, [contact.id], NOW)
    provider = _CapturingProvider()

    await refresh_profiles(
        db, provider, bot_name='月璃',
        subject_of=naming._profile_subject,
        relation_note=naming._profile_relation_note(),
        temperature=0.3, max_tokens=None, now=NOW + 1,
    )

    system_prompt = provider.requests[0][0]['content']
    assert '整理她对「雨后（群名片：雨后在摸鱼）」的印象' in system_prompt
    assert '凌白和她是兄妹关系，这层关系只属于凌白本人。' in system_prompt


def test_each_injected_profile_is_labeled_with_its_owner(db):
    people = _seed_people(db)
    naming = _Naming(db, '兄妹')
    owner, contact = people['owner'], people['contact']
    for person_id, summary in ((owner.id, '他是我哥'), (contact.id, '他爱聊游戏')):
        db.execute(
            'INSERT OR REPLACE INTO person_profile (person_id, summary) VALUES (?, ?)',
            (person_id, summary),
        )
    db.commit()
    context = ConversationContext(stream=people['group'], person=contact)

    injected = profiles_for_injection(
        db, [contact.id, owner.id],
        name_of=lambda person_id: naming._impression_name(person_id, context),
        priority_ids=(contact.id,),
    )
    prompt = build_system_prompt(
        name='月璃', birthday='', personality='观察细节',
        reply_style='简短接话', now=datetime(2032, 7, 15, 12, 5),
        impressions=injected,
    )

    assert '关于雨后在摸鱼：\n印象：他爱聊游戏' in prompt
    assert '关于凌白：\n印象：他是我哥' in prompt
