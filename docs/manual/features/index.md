# 功能总览

她装好就能聊天、记事、按日程作息；语音、看图、向量召回这类能力默认关闭，需要你显式打开。
本页回答三个问题：哪些默认开、哪些要显式打开、彼此有什么依赖。每项的细调方法见对应篇目。

## 默认开启的功能

| 功能 | 开关位置 | 前提 |
| :--- | :--- | :--- |
| 对话与工作记忆、事实与情节召回 | `bot.toml` `[conversation]` | 配好对话模型 |
| 事实抽取与长期记忆摘要 | `bot.toml` `[conversation]` | memory、summary 任务档可用 |
| 日程、活动与睡眠 | `bot.toml` `[schedule]` | schedule 任务档（留空沿用对话模型） |
| 好感度、精力与心情 | 无总开关 | 随对话自然运转 |
| 表达方式学习 | 无开关 | memory 任务档可用 |
| 黑话学习 | 无开关 | memory 任务档可用 |
| 表情包收集与发送 | `bot.toml` `[emoji]` | 语义标签需要视觉模型 |
| 群聊表情回应（贴表情） | `bot.toml` `group_chat.reactions_enabled` | 无 |
| 群聊主动发起话题 | `bot.toml` `group_chat.self_started_topics` | 受群聊回复频率闸门约束 |

「无开关」不等于「关不掉」，各篇里写了让它们实际上停摆的办法（比如表达方式不确认任何一条就不会被使用）。

## 默认开、但前提不满足就等于没开的

这两项的配置默认值是「开」，但它们还各自压着一个默认关的前提，所以全新部署上实际不生效：

- 私聊跟进（静默追问与久等催促）：`bot.toml` 的 `typing.follow_up` / `typing.nudge` 默认开，
  但只在 Conversation 行动核心启用（`conversation_agent.mode` 为 `enabled` 或把会话列入 `selected_streams`）的私聊生效，而行动核心默认 `off`。
- 桌面主动搭话：`models.toml` 的 `generation.proactive.enabled` 默认开，
  但主动搭话的唯一出口是桌宠窗口，所以还要求 `bot.toml` 的 `desktop_pet.enabled = true`，而桌宠默认关。

## 默认关闭、要显式打开的功能

| 功能 | 开关位置 | 还需要什么 |
| :--- | :--- | :--- |
| 桌宠外壳（窗口、前台采集） | `bot.toml` `desktop_pet.enabled` | 构建好的桌面前端，改动需重启 |
| 语音合成 | `features.toml` `[tts]` | tts 任务档与对应厂商，改动需重启 |
| 屏幕视觉 | `features.toml` `vision.enabled` | vision 任务档与桌宠，改动需重启 |
| QQ 聊天图片描述 | `features.toml` `vision.chat_image_enabled` | vision 任务档，与屏幕视觉各自独立 |
| 向量混合召回 | `features.toml` `vector.enabled` | 安装 vector 可选依赖与 embedding 任务档，改动需重启 |
| 记忆反馈纠错 | `features.toml` `[memory_feedback]` | memory 任务档；整条链路默认零写入，改动需重启 |
| 群聊戳一戳 | `bot.toml` `group_chat.pokes_enabled` | 无；扰动较大所以默认关 |

## 模型任务档是大多数功能的前提

`models.toml` 把模型按用途分成十二个任务槽：chat、planner、replyer、scene、proactive、summary、
memory、schedule、vision、expression、tts、embedding。除 chat 外每个槽留空就沿用 chat 的候选；
vision 留空则表示「不可用」，依赖它的功能（屏幕视觉、图片描述、表情包打标签、表情包内容审查）随之停摆。
对应关系一览：

- 对话回复：chat；行动决策与正文分离时另用 planner、replyer。
- 长期记忆摘要：summary；事实抽取、表达学习、黑话推断、反馈纠错判定：memory。
- 群场景画像与私聊跟进前的情景分析：scene；主动搭话生成：proactive。
- 日程与活动推进：schedule；表达方式挑选：expression。
- 屏幕扫视、聊天图片描述、表情包语义标签与内容审查：vision。
- 语音合成：tts；向量召回与表情包语义匹配：embedding。

某项功能不工作时，先到面板「模型与厂商」页确认对应任务槽有可用候选，再查[常见问题](../troubleshooting.md)。

## 配置在哪改、改完要不要重启

两个入口，改的是同一组文件：

- 面板「月璃设置」页：按表单改五份 TOML，保存时整体校验，任一文件不合法就整体回滚。
- 直接编辑配置目录下的 TOML 文件。

大部分键保存后热生效；以下键改动后需要重启后端（桌宠开关需重启整个应用）：
`tts.enabled`、`vision.enabled`、`vision.fullscreen_silent`、`vision.capture_mode`、
`vector.enabled`、`memory_feedback` 整段、`log` 段、`desktop_pet.enabled`。

## 各功能篇目

- [记忆](memory.md) —— 三层记忆、召回、遗忘与面板上的查改。
- [表达方式与黑话](expression-jargon.md) —— 学什么、从哪学、怎么审核与撤销。
- [表情包](emoji.md) —— 收录、语义标签、发送配额与内容审核。
- [主动搭话](proactive.md) —— 群聊自开话题、桌面搭话、私聊跟进的判据与频率。
- [人格与状态](persona.md) —— 好感度按人漂移，精力与心情如何影响表达。
- [日程与睡眠](schedule.md) —— 一天的节奏怎么生成、睡着时什么能叫醒她。
- [屏幕感知](perception.md) —— 视觉与感知出口两个正交开关。
- [生图管线](sprite.md) —— 从一张参考图跑出整套角色素材。

实现层面的架构与模块说明在[开发手册](../../dev/architecture/overview.md)。
