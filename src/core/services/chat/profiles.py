"""人物画像与身份呈现。

本 mixin 负责把库里的人物画像整理成管理面板可读的结构，并在组装回合上下文时
解析当前发言者的身份：合并多平台身份引用、挑选展示名、汇总画像摘要。

由 ``ChatService`` 继承，依赖它的 ``_memory`` / ``_registry`` 等属性。
"""

from typing import Any, Dict, List

from src.core.platform_io.types import (
    ConversationContext,
    IdentityRef,
    PersonRef,
    StreamRef,
)
from src.core.runtime.clock import now as current_time


class PersonProfileMixin:

    def list_person_profiles(self) -> List[Dict[str, Any]]:
        """列出人物画像索引，附带列表页排序所需的关系与事实计数。

        关系与计数直接并入本列表，不另开汇总路由：``/api/persons`` 已是人物列表的
        唯一入口。逐人取数复用 :meth:`Persona.inspect` 与 :meth:`MemoryStore.fact_count`，
        不另写统计 SQL。两者分别命中
        ``persona_bond`` 主键与 ``idx_facts_person_active``，都是索引查找。

        :return: 每个人物的身份与会话归属摘要，附 ``intimacy``、``factCount``
            与 ``bondUpdatedAt``；不展开事实正文（那是详情路由的职责）。
        :raises ValueError: 人物不存在时由注册表抛出。
        :raises sqlite3.Error: 读取关系或事实计数失败。
        副作用：只读，不创建缺失的 contact 关系记录。
        """
        profiles: List[Dict[str, Any]] = []
        for person in self._registry.list_persons():
            summary = self._person_summary(person)
            # inspect 而非 get：列表是只读视图，不因读取而创建关系记录。
            state = self.persona.inspect(person.id)
            summary.update({
                'intimacy': state.intimacy,
                'factCount': self.memory.fact_count(person.id),
                # 命名取自来源而非语义：对 contact 它确实是最后互动时间（apply_elapsed
                # 对非 owner 提前返回），但 owner 那一行还会被每小时的时间结算推进，
                # 叫 lastInteractionAt 会对那一行说谎。
                'bondUpdatedAt': state.updated_at,
            })
            profiles.append(summary)
        return profiles

    def person_profile(self, person_id: int, now: int | None = None) -> Dict[str, Any]:
        """组装指定人物的身份、关系和事实画像。

        :param person_id: ``persons.id`` 稳定主键。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 包含人物摘要、亲密度和当前事实列表的字典。

        :raises ValueError: 人物不存在时由注册表抛出。
        """
        now = now or current_time()
        person = self._registry.person(person_id)
        summary = self._person_summary(person)
        state = self.persona.inspect(person.id)
        # 关系快照与事实列表使用同一时间点，避免画像字段跨时钟读取产生不一致。
        summary.update({
            'bond': {
                'intimacy': state.intimacy,
                'updatedAt': state.updated_at,
            },
            'facts': [
                {
                    'id': fact.id,
                    'kind': fact.kind,
                    'content': fact.content,
                    'retention': fact.retention,
                    'score': fact.score,
                    'dueAt': fact.due_at,
                    'frozen': fact.frozen,
                }
                for fact in self.memory.all_facts(person.id, now)
            ],
        })
        return summary

    def _person_summary(
        self,
        person: PersonRef,
        preferred_platform: str | None = None,
    ) -> Dict[str, Any]:
        """将人物注册表引用转换为跨语言人物画像摘要。

        :param person: 已解析的人物引用。
        :param preferred_platform: 可选优先显示平台；没有匹配身份时按注册表顺序回退。

        :return: 包含人物基本信息、平台身份、实际发言 stream 和群成员关系的可序列化字典。

        :raises ValueError: 人物不存在时由注册表查询抛出。
        :raises sqlite3.Error: 读取身份、stream 或群成员关系失败。

        副作用：
            只读注册表，不调用模型、不修改人物数据。
        """
        identities = self._registry.list_identities(person.id)
        # 身份、stream 和群成员分开返回，调用方可按平台权限选择展示字段。
        return {
            'id': person.id,
            'kind': person.kind,
            'displayName': self._profile_display_name(person, identities, preferred_platform),
            'firstSeenAt': person.first_seen_at,
            'identities': [
                {
                    'platform': identity.platform,
                    'externalId': identity.external_id,
                    'displayName': identity.display_name,
                }
                for identity in identities
            ],
            'streams': [
                {
                    'id': stream.id,
                    'platform': stream.platform,
                    'kind': stream.kind,
                    'externalId': stream.external_id,
                }
                for stream in self._registry.list_person_streams(person.id)
            ],
            'groupMemberships': [
                {
                    'streamId': membership.stream_id,
                    'groupExternalId': membership.group_external_id,
                    'groupCard': membership.group_card,
                }
                for membership in self._registry.group_memberships(person.id)
            ],
        }

    def _conversation_participant(
        self,
        person: PersonRef,
        stream: StreamRef,
    ) -> Dict[str, Any]:
        """构造当前会话参与人的身份、显示名和群名片摘要。

        :param person: 会话参与人的人物引用。
        :param stream: 当前会话引用。

        :return: 包含人物 ID、类型、会话显示名、平台外部 ID、账号昵称和群名片的字典。

        :raises RuntimeError: 非桌面会话缺少平台身份，或群聊缺少群成员关系。
        :raises ValueError: 人物或 stream 不存在，或平台没有可用显示名。
        :raises sqlite3.Error: 读取归属关系失败。
        """
        identities = self._registry.list_identities(person.id)
        # 非桌面会话必须绑定稳定 identity；仅桌面会话允许使用内部人物资料生成显示名。
        identity = next(
            (item for item in identities if item.platform == stream.platform),
            None,
        )
        if stream.platform != 'desktop' and identity is None:
            raise RuntimeError(
                f'person {person.id} 在会话平台 {stream.platform} 缺少 identity'
            )
        group_card = ''
        if stream.kind == 'group':
            # 群名片属于 person-stream 关系，不能从全局 identity 推导。
            membership = next(
                (
                    item for item in self._registry.group_memberships(person.id)
                    if item.stream_id == stream.id
                ),
                None,
            )
            if membership is None:
                raise RuntimeError(
                    f'person {person.id} 在群 stream {stream.id} 缺少 membership'
                )
            group_card = membership.group_card
        return {
            'id': person.id,
            'kind': person.kind,
            'displayName': (
                self._registry.stream_display_name(person.id, stream.id)
                if stream.platform != 'desktop'
                else self._profile_display_name(person, identities, stream.platform)
            ),
            'externalId': identity.external_id if identity is not None else '',
            'nickname': identity.display_name if identity is not None else '',
            'groupCard': group_card,
        }

    def _sender_metadata(self, context: ConversationContext) -> Dict[str, str]:
        """生成观测和终端展示使用的发送者元数据。

        :param context: 已解析当前 stream、人物、身份和群名片的会话上下文。

        :return: 包含外部 ID、账号昵称、群名片、最终显示名和展示标签的字符串字典；桌面消息
            使用固定的本地用户标签。

        :raises RuntimeError: 非桌面上下文缺少稳定平台身份。
        """
        identity = context.identity
        if context.stream.platform == 'desktop':
            return {
                'senderExternalId': '',
                'senderNickname': '',
                'senderGroupCard': '',
                'senderDisplayName': '你',
                'senderLabel': '你',
            }
        if identity is None:
            raise RuntimeError('非桌面入站上下文缺少稳定平台 identity')
        display_name = context.group_card or identity.display_name
        if identity.platform == 'qq':
            if context.group_card and context.group_card != identity.display_name:
                sender_label = (
                    f'{context.group_card}（QQ昵称：{identity.display_name} · '
                    f'QQ号：{identity.external_id}）'
                )
            else:
                sender_label = f'{identity.display_name}（QQ号：{identity.external_id}）'
        else:
            sender_label = f'{display_name}（{identity.platform}：{identity.external_id}）'
        return {
            'senderExternalId': identity.external_id,
            'senderNickname': identity.display_name,
            'senderGroupCard': context.group_card,
            'senderDisplayName': display_name,
            'senderLabel': sender_label,
        }

    def _profile_display_name(
        self,
        person: PersonRef,
        identities: List[IdentityRef],
        preferred_platform: str | None,
    ) -> str:
        """按平台优先级选择人物画像显示名，并为未绑定身份生成明确占位名。

        :param person: 人物引用。
        :param identities: 已加载的平台身份列表。
        :param preferred_platform: 可选优先平台标识。

        :return: 优先平台显示名、首个身份显示名、owner 用户昵称或未绑定联系人占位名。

        副作用：
            仅读取人物和身份数据，不修改配置或注册表。
        """
        preferred = next(
            (identity for identity in identities if identity.platform == preferred_platform),
            None,
        )
        if preferred is not None:
            return preferred.display_name
        if identities:
            return identities[0].display_name
        if person.kind == 'owner':
            user_nickname = self._cfg.bot.user_nickname.strip()
            if user_nickname:
                return user_nickname
            return '用户本人'
        return f'未绑定联系人 #{person.id}'
