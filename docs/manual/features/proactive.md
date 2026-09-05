# 主动搭话

除了被点名时回应，她在四种情况下会主动开口：群聊里自开话题、桌面上主动搭话、私聊里静默追问、久等后的催促。
四路相互独立，判据、前提和关法都不同，本页逐路说明。

## 群聊：主动发起话题

默认开（`bot.toml` `group_chat.self_started_topics = true`）。

- 判据：群里暂时没有可直接回应的消息时，她可以选择「自己起个话头」。发起话题和回帖走的是同一套触发与频率闸门，**不会额外增加她开口的次数**——它只是「开口」的候选形式之一。
- 频率闸门：`reply_window_minutes`（默认 10 分钟）窗口内，非点名消息她最多接 `max_replies_in_window` 次（默认 3，0 为不限）。@、叫名字、戳一戳、回复她的消息这类直接点名不受此上限约束。
- 话多自敛：`presence_decay_strength`（默认 3.0，范围 0~20）越大，她在群里说得越多就越倾向于让别人先说；调到 0 等于关掉这层自敛。
- 背景观察：群里每积累 `scene_refresh_messages` 条新消息（默认 15）后台重算一次场景画像，作为她判断「该不该开口、开什么口」的上下文；调 0 关闭观察。
- 关闭：`self_started_topics = false`，她只在被点名或搭得上话时开口。

## 桌面：主动搭话

默认「配置开、前提关」：`models.toml` 的 `generation.proactive.enabled` 默认 true，
但主动搭话的唯一出口是桌宠窗口，所以还要求 `bot.toml` 的 `desktop_pet.enabled = true`（默认 false）。
两个开关取与，桌宠不开则桌面搭话整体停用，连全局键鼠钩子都不会安装。

四类触发意图：

- **场景**：前台窗口变化时（比如你打开了游戏）。
- **闲的**：每分钟评估一次，兴趣值攒满就想说话；精力越低、对你的好感越低，攒得越慢。
- **计划**：日程活动时间线切换活动时（比如到了她自己安排的休息时间）。
- **约定**：聊天里识别出的约定到点了（「等你打完这局叫我」之类）。

每次投放前都要过打扰判定，任一命中就放弃：你在静默场景（疑似全屏，见[屏幕感知](perception.md)）、
她在睡觉、桌宠窗口不可见。预算上每天有投放上限 5 次，其中「场景」类最多用 3 次（给其它意图预留 2 次）；
搭话连续得不到回应时，兴趣累积会越来越慢。
关闭：`generation.proactive.enabled = false`（保留桌宠、只停搭话）。

## 私聊：静默主动追问

默认开（`typing.follow_up.enabled = true`），但**只在 Conversation 行动核心生效的私聊**里工作
——`conversation_agent.mode` 为 `enabled`，或把该会话列入 `selected_streams`。行动核心默认 off，所以全新部署上这路实际不生效。

- 判据：她正常回复后，对方持续 `peer_silence_minutes`（默认 1 分钟）没回话，产生一次追问机会；每次正常回复后至多一次，对方来消息即取消。
- 决策：到点先做一次独立的情景分析，再让行动核心结合聊天历史在「追问 / 保持沉默」之间选——沉默是合法且常见的选择。
- 关闭：`typing.follow_up.enabled = false`。

## 私聊：久等后的催促

默认开（`typing.nudge.enabled = true`），前提与静默追问相同（行动核心生效的私聊）。

- 判据：从她上次正常回复起累计静默超过 `peer_silence_minutes`（默认 3 分钟），**且看到对方开始打字**（输入状态通知），产生一次催促机会，同样由行动核心决定问还是不问。
- 上限：每段静默期最多催 `max_per_silence` 次（默认 2），只统计实际选择追问并成功发出的次数；对方回话后清零重计。调 0 等于关闭。
- 关闭：`typing.nudge.enabled = false`。

## 睡眠时呢

她睡着时：群聊里非点名的主动参与直接被丢弃；桌面主动搭话一律不投放。
点名与私聊的叫醒规则见[日程与睡眠](schedule.md)。

## 想彻底安静

按位置逐一关：

- 群聊自开话题：`group_chat.self_started_topics = false`。
- 桌面主动搭话：`generation.proactive.enabled = false`。
- 私聊追问与催促：`typing.follow_up.enabled = false`、`typing.nudge.enabled = false`（或保持行动核心 `mode = off`，两路自然不生效）。

全关后她只剩被动回应：被 @、被叫名字、私聊消息照常回复，其它时候一言不发。

实现细节见[开发手册·一次回合的完整链路](../../dev/architecture/conversation-turn.md)。
