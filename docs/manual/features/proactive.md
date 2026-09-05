# 主动搭话

除被动回应外，系统在四种情形下会主动发起消息：群聊自开话题、桌面主动搭话、私聊静默追问、私聊久等催促。
四路相互独立，判据、前提与停用方式各不相同，本页逐路说明。

## 群聊：主动发起话题

默认开启（`bot.toml` `group_chat.self_started_topics = true`）。

- 判据：群内暂无可直接回应的消息时，可以选择主动发起话题。发起话题与普通回帖共用同一套触发与频率闸门，
  不额外增加开口次数，仅为「开口」的候选形式之一。
- 频率闸门：`reply_window_minutes`（默认 10 分钟）窗口内，非点名消息最多回应 `max_replies_in_window` 次
  （默认 3，0 为不限）。@、称呼名字、戳一戳、回复其消息等直接点名不受此上限约束。
- 话多自敛：`presence_decay_strength`（默认 3.0，范围 0~20）越大，在群内发言越多越倾向于让他人先说；
  置 0 即关闭该约束。
- 背景观察：群内每累计 `scene_refresh_messages` 条新消息（默认 15），后台重算一次场景画像，
  作为判断是否开口及开口内容的上下文；置 0 关闭观察。
- 停用：`self_started_topics = false`，此后仅在被点名或存在可回应消息时开口。

## 桌面：主动搭话

默认为「配置开启、前提关闭」：`models.toml` 的 `generation.proactive.enabled` 默认 true，
但主动搭话的唯一出口为桌宠窗口，还需 `bot.toml` 的 `desktop_pet.enabled = true`（默认 false）。
两个开关取与；桌宠未开启时桌面搭话整体停用，全局键鼠钩子亦不安装。

四类触发意图：

- **场景**：前台窗口变化时触发。
- **空闲**：每分钟评估一次，兴趣值累积满时产生开口意图；精力越低、对主人的好感越低，累积越慢。
- **计划**：日程活动时间线切换活动时触发。
- **约定**：对话中识别出的约定到点时触发。

每次投放前需通过打扰判定，命中任一条件即放弃：处于静默场景（疑似全屏，见[屏幕感知](perception.md)）、
处于睡眠状态、桌宠窗口不可见。预算上每日投放上限 5 次，其中「场景」类最多使用 3 次（为其它意图预留 2 次）；
搭话连续未获回应时，兴趣累积速率随之下降。
停用：`generation.proactive.enabled = false`（保留桌宠，仅停用搭话）。

## 私聊：静默主动追问

默认开启（`typing.follow_up.enabled = true`），但仅在 Conversation 行动核心生效的私聊中工作：
`conversation_agent.mode` 为 `enabled`，或该会话列入 `selected_streams`。行动核心默认 off，
因此全新部署下此路不实际生效。

- 判据：正常回复后，对方持续 `peer_silence_minutes`（默认 1 分钟）未回复，产生一次追问机会；
  每次正常回复后至多一次，对方发来新消息即取消。
- 决策：到点后先执行一次独立的情景分析，再由行动核心结合聊天历史在「追问」与「保持沉默」之间选择；
  沉默是合法且常见的选择。
- 停用：`typing.follow_up.enabled = false`。

## 私聊：久等后的催促

默认开启（`typing.nudge.enabled = true`），前提与静默追问相同（行动核心生效的私聊）。

- 判据：自上次正常回复起累计静默超过 `peer_silence_minutes`（默认 3 分钟），且观察到对方正在输入
  （输入状态通知），产生一次催促机会，同样由行动核心决定追问或沉默。
- 上限：每段静默期最多催促 `max_per_silence` 次（默认 2），仅统计实际选择追问并成功发出的次数；
  对方回话后计数清零。置 0 等于关闭。
- 停用：`typing.nudge.enabled = false`。

## 睡眠状态下的行为

睡眠状态下：群聊中非点名的主动参与直接丢弃；桌面主动搭话一律不投放。
点名与私聊的叫醒规则见[日程与睡眠](schedule.md)。

## 全部停用

按位置逐项关闭：

- 群聊自开话题：`group_chat.self_started_topics = false`。
- 桌面主动搭话：`generation.proactive.enabled = false`。
- 私聊追问与催促：`typing.follow_up.enabled = false`、`typing.nudge.enabled = false`
  （或保持行动核心 `mode = off`，两路自然不生效）。

全部关闭后仅剩被动回应：被 @、被称呼名字、私聊消息照常回复，其余时间不开口。

实现细节见[开发手册·一次回合的完整链路](../../dev/architecture/conversation-turn.md)。
