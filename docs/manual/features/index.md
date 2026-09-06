# 功能总览

本页列出各项功能的默认开关状态、前置条件与相互依赖。各项功能的详细行为与调参方法见对应篇目。

## 默认开启的功能

| 功能 | 配置位置 | 前置条件 |
| :--- | :--- | :--- |
| 对话与工作记忆、事实与情节召回 | `bot.toml` `[conversation]` | chat 任务档可用 |
| 事实抽取与长期记忆摘要 | `bot.toml` `[conversation]` | memory、summary 任务档可用 |
| 日程、活动与睡眠 | `bot.toml` `[schedule]` | schedule 任务档（留空沿用 chat） |
| 好感度、精力与心情 | 无总开关 | 随对话运行 |
| 表达方式学习 | 无开关 | memory 任务档可用 |
| 黑话学习 | 无开关 | memory 任务档可用 |
| 表情包收集与发送 | `bot.toml` `[emoji]` | 语义标签依赖 vision 任务档 |
| 群聊表情回应 | `bot.toml` `group_chat.reactions_enabled` | 无 |
| 群聊主动发起话题 | `bot.toml` `group_chat.self_started_topics` | 受群聊回复频率闸门约束 |

表中标注「无开关」的功能可按各篇所述方式停用。例如表达方式不经人工确认即不会被使用，等于停用注入。

## 默认开启、但前置条件默认不满足

以下两项配置默认值为开，但各自依赖一个默认关闭的开关，因此在全新部署下不实际生效：

- 私聊跟进（静默追问、久等催促）：`bot.toml` 的 `typing.follow_up` / `typing.nudge` 默认开，
  仅在 Conversation 行动核心启用（`conversation_agent.mode` 为 `enabled`，或会话列入 `selected_streams`）的私聊中生效；
  行动核心初始配置为 `enabled`。
- 桌面主动搭话：`models.toml` 的 `generation.proactive.enabled` 默认开，
  但主动搭话的唯一出口为桌宠窗口，还要求 `bot.toml` 的 `desktop_pet.enabled = true`；桌宠默认关闭。

## 默认关闭的功能

| 功能 | 配置位置 | 前置条件 |
| :--- | :--- | :--- |
| 桌宠外壳（窗口、前台采集） | `bot.toml` `desktop_pet.enabled` | 桌面前端；改动需重启应用 |
| 语音合成 | `features.toml` `[tts]` | tts 任务档与对应厂商；改动需重启 |
| 屏幕视觉 | `features.toml` `vision.enabled` | vision 任务档与桌宠；改动需重启 |
| QQ 聊天图片描述 | `features.toml` `vision.chat_image_enabled` | vision 任务档；与屏幕视觉相互独立 |
| 向量混合召回 | `features.toml` `vector.enabled` | embedding 任务档；初始配置即开启；改动需重启 |
| 记忆反馈纠错 | `features.toml` `[memory_feedback]` | memory 任务档；初始配置即开启，关闭后整条链路零写入；改动需重启 |
| 群聊戳一戳 | `bot.toml` `group_chat.pokes_enabled` | 无 |

## 模型任务档与功能的对应关系

`models.toml` 将模型按用途划分为十二个任务槽：chat、planner、replyer、scene、proactive、summary、
memory、schedule、vision、expression、tts、embedding。除 chat 外，任务槽留空即沿用 chat 的候选；
vision 留空表示不可用，依赖它的功能（屏幕视觉、图片描述、表情包语义标签、表情包内容审查）随之停用。

- 对话回复：chat；行动决策与正文分离时另用 planner、replyer。
- 长期记忆摘要：summary；事实抽取、表达学习、黑话推断、反馈纠错判定：memory。
- 群场景画像与私聊跟进前的情景分析：scene；主动搭话生成：proactive。
- 日程与活动推进：schedule；表达方式挑选：expression。
- 屏幕扫视、聊天图片描述、表情包语义标签与内容审查：vision。
- 语音合成：tts；向量召回与表情包语义匹配：embedding。

某项功能不工作时，先在面板「模型与厂商」页确认对应任务槽存在可用候选，再查阅[常见问题](../troubleshooting.md)。

## 配置修改方式与生效时机

两个入口修改同一组文件：面板「月璃设置」页（按表单编辑五份 TOML，保存时整体校验，任一文件不合法则整体回滚），
或直接编辑配置目录下的 TOML 文件。

多数键保存后热生效。以下键改动后需重启后端：`tts.enabled`、`vision.enabled`、`vision.fullscreen_silent`、
`vision.capture_mode`、`vector.enabled`、`memory_feedback` 整段、`log` 段；`desktop_pet.enabled` 需重启整个应用。

## 各功能篇目

- [记忆](memory.md)：三层记忆、召回、遗忘与面板上的查改。
- [表达方式与黑话](expression-jargon.md)：学习内容、来源、审核与撤销。
- [表情包](emoji.md)：收录、语义标签、发送配额与内容审核。
- [主动搭话](proactive.md)：群聊自开话题、桌面搭话、私聊跟进的判据与频率。
- [人格与状态](persona.md)：好感度按人漂移，精力与心情对表达的影响。
- [日程与睡眠](schedule.md)：每日节奏的生成、睡眠中的叫醒规则。
- [屏幕感知](perception.md)：视觉与感知出口两个正交开关。
- [生图管线](sprite.md)：从参考图生成整套角色素材。

实现层面的架构与模块说明见[开发手册](../../dev/architecture/overview.md)。
