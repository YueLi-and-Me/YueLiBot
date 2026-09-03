"""按「事实被听见的场合」判定它在当前会话里是否可见。

判据一句话：记忆的可见范围由它被听见的场合决定。人物事实是关于某个人的一句话，
那个人在场就不算泄露；私聊里听到的话换个群复述出来一定出事。``origin_kind``
只记录事实最初落库时所在的场合，不随读取变化。

本模块只提供纯函数：配置开关 ``conversation.private_facts_in_group`` 由调用侧
读取后以参数传入，这里不碰任何配置或存储。
"""

from __future__ import annotations

from src.core.platform_io.types import StreamKind

# facts.origin_kind 的三个合法取值。
ORIGIN_LEGACY = 'legacy'
ORIGIN_DIRECT = 'direct'
ORIGIN_GROUP = 'group'

# 读取入口 stream_kind 参数的旁路值：抽取去重清单要看到全部事实，不参与可见性
# 判定。只允许出现在 fact_extract.render_known_facts 这一个调用点。
SCOPE_ALL = 'all'

StreamKindOrAll = str
"""stream_kind 参数的宽类型：``StreamKind`` 三值或旁路值 ``SCOPE_ALL``。"""


def origin_kind_for_stream(stream_kind: StreamKind) -> str:
    """把会话类型映射成事实写入时的来源标记。

    :param stream_kind: 写入发生时所在会话的类型。
    :return: 群聊返回 ``group``；私聊与桌面端统一返回 ``direct``。
    副作用：无。
    """

    return ORIGIN_GROUP if stream_kind == 'group' else ORIGIN_DIRECT


def fact_visible_in_stream(
    origin_kind: str,
    stream_kind: StreamKind,
    *,
    private_in_group: bool = False,
) -> bool:
    """判定一条事实在指定类型的会话里是否可见。

    规则表（``private_in_group`` 默认关闭）：

    ============  ==========  ===========
    事实来源       群聊可见     私聊/桌面可见
    ============  ==========  ===========
    group          是          是
    direct         否          是
    legacy         是          是
    ============  ==========  ===========

    :param origin_kind: 事实的来源标记，取值为 :data:`ORIGIN_LEGACY` /
        :data:`ORIGIN_DIRECT` / :data:`ORIGIN_GROUP`。
    :param stream_kind: 当前读取发生的会话类型。
    :param private_in_group: ``conversation.private_facts_in_group`` 的当前值；
        打开后 ``direct`` 事实在群聊同样可见。
    :return: 可见返回 ``True``。
    副作用：无。
    """

    # 非法来源值按 legacy 处理：该列 NOT NULL DEFAULT 'legacy'，正常写入只会
    # 产生三个合法值，出现别的值意味着库被外部改过——按存量行口径放行，
    # 与改造前的行为保持一致，让问题停留在数据侧而不是让记忆整批消失。
    if origin_kind not in (ORIGIN_LEGACY, ORIGIN_DIRECT, ORIGIN_GROUP):
        return True
    if origin_kind == ORIGIN_DIRECT:
        return stream_kind != 'group' or private_in_group
    return True
