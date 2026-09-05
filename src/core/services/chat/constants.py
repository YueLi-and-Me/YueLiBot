"""对话编排的模块级常量。

轮询间隔、插件根目录、后台批次重试上限、场景观察窗口、私聊等待超时、群历史
回填的播种参数与失败提示文案都集中在这里。

单独成模块而不是留在 ``service`` 里，是因为各 mixin 也要读同一批常量：留在
``service`` 会让 mixin 反向导入 ``service``，而 ``service`` 又要导入 mixin 类
组装 ``ChatService``，形成导入环。抽出来之后依赖方向单向指向本模块。
"""

from pathlib import Path

from src.core.config.schema import ConversationConfig


# 插件根目录，按发现顺序排列：内置在前、第三方在后。同一插件标识冲突时先扫到的
# 生效，因此随程序发布的内置实现不会被用户目录里的同名插件顶掉。
PLUGIN_ROOTS = (Path('src/plugins/built_in'), Path('plugins'))

CHAT_POLL_INTERVAL_S = 0.1

# 部分 Gemini 兼容网关会把 system 单独提取；保留一条固定的非 system 指令，
# 既满足其 contents 非空约束，也不把触发情境伪装成用户提出的新问题。
PROACTIVE_TRIGGER_MESSAGE = '请按上面的要求开始。'
# 一次事实抽取最多带多少个在场者进提示词。群聊在场者可能数百人，全量会超出
# 名单并拖慢身份解析；近期未发言者在本批对话中也不会有相关事实。
_EXTRACTION_PARTICIPANT_LIMIT = 12

# 同一批后台任务输入连续失败多少次之后放弃这一批。
#
# - 现象：一段对话被服务商内容策略拒绝后，摘要在之后的每个回合重跑同一批 46 条
#   消息、每次都被拒，情节记忆停止产出，队列积压持续增长。
# - 原因：摘要、事实抽取、表达学习的队列游标都只在成功后推进，失败重跑同一批。
#   这对瞬时故障是对的，对确定性失败则是死锁——同样的输入永远得到同样的拒绝。
# - 后果：不设上限就没有出口，一条消息足以让整条记忆线永久停摆，并且每个回合
#   多付一次模型调用。取 3 是因为瞬时故障几乎不会连着三个回合复现，而确定性
#   失败第一次就会把额度用满。
_BACKGROUND_BATCH_RETRY_LIMIT = 3

# 对齐群聊既有回复窗口，在同一窗口内最多发送一张表情包。
EMOJI_MAX_PER_REPLY_WINDOW = 1
# 场景观察读取的历史条数。刻意宽于工作记忆窗口：观察的价值就在于看到比单轮
# 上下文更长的一段，否则它只是把对话模型已经看过的东西再读一遍。
SCENE_WINDOW_MESSAGES = 60

# 一个回合内最多允许的内部轮次。
#
# 多轮回合已经停用，正常路径只执行第一轮；这个上限保留为防御，防止以后调整
# 控制流时意外失控。当前路径永远不会撞到它，详见 ``_run_conversation_turn``。
MAX_TURN_ROUNDS = 10

# 私聊等待的下文超时。群聊在无下文时保持沉默；私聊对方正在等待回应，
# 超时后必须回应，否则等待会变成永久已读不回。取值覆盖连发两条消息的常见
# 间隔（两到五秒），同时避免单句等待时间过长。
DIRECT_WAIT_TIMEOUT_S = 10.0

# 群历史首次回填时用于播种游标的历史条数与时间容差。
_BACKFILL_SEED_EVENT_LIMIT = 500
_BACKFILL_SEED_MESSAGE_LIMIT = 80
_BACKFILL_SEED_TIME_MATCH_MS = 10 * 60_000

# 旧测试和诊断脚本仍会读取这个换算值；唯一默认来源是配置模型。
SESSION_GAP_MS = ConversationConfig().session_gap_minutes * 60_000

_HINTS: dict[str, str] = {
    'auth': 'API Key 无效，检查 providers.toml',
    'billing': '服务商余额不足或免费额度用尽，请检查余额与仅用免费额度设置，或移除对应候选模型',
    'model': '模型 ID 不对，检查 models.toml',
    'quota': '限流或余额不足，稍等一下',
    'network': '连不上模型接口，检查网络或代理',
    'timeout': '模型迟迟不出字，可能在排队；换个模型或调大首字超时',
    'blocked': '这句被内容审核拦了，换个说法',
}
