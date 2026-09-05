# 一次回合的完整链路

一条入站消息到一条出站气泡之间，数据依次穿过：平台适配器、HTTP 入口、归属解析、
入口门控、每会话缓冲、批次门控、上下文组装、Conversation Agent 决策、流式解析与
副作用、出站投递。本文按这个顺序说明每一步的判定依据与失败分支。分层与进程关系
见[架构总览](overview.md)，这里只展开回合内部。

「回合」（turn）是这套链路的基本单位：一批同发送者的消息唤醒一次决策与生成，
全程挂在一个单调递增的回合 ID 下。回合 ID 的起点由事件账本里已出现的最大 ID 播种
（`src/core/services/chat/service.py:472` 的 `max_turn_id`，分配见 `_next_turn`
`service.py:2264`），重启后不与历史回合撞号——观察面板按 turnId 聚合，撞号会把
不同启动的对话并成一张卡。

```
适配器(NapCat/SnowLuma 等协议端)
   |  WS 事件 -> 适配器进程整理
   |  POST /platform/inbound            桌面: POST /chat/send
   v                                    v
+-------------------------------------------+
| 入口: 归属解析 -> 开发者命令截断 -> 入口门控 |
+-------------------------------------------+
   | drop(落库为观察)      | 放行
   v                       v
                     ChatService.send: 落库 + 入缓冲 + 唤醒
                              |
                        _poll_loop / _tick (0.1s 心跳兜底)
                              |
                    取同发送者前缀批次 -> claim_stream 占用
                              |
                        _start_turn: 回合任务(异步)
                              |
              等图片描述 -> 上下文组装 -> 批次门控(三态)
                              |
            +-----------------+------------------+
            | drop            | live / off       | shadow(只记录)
            v                 v                  v
        gate_dropped    ConversationAgent.run   记决策后继续/返回
            结束         (认知轮 ReAct -> 终局动作)
                              |
        silent / wait / poke / react / reply|speak
                              |
            流式解析事件 -> 副作用(人格/约定/表情包) -> 分句
                              |
              桌面: 边解析边推 chat.event   QQ: 攒齐分句 -> broker -> 适配器
```

## 1. 平台入站与归属解析

1. QQ 侧由独立适配器进程连接协议端。`src/platforms/onebot11/runner.py:827` 的
   `_consume_protocol_events` 把协议事件整理成统一结构，`backend.py:156` 的
   `submit_inbound` POST 到主体的 `/platform/inbound`（`backend.py:197`）。
   桌面端走 `/chat/send`（`src/core/api/http.py:499`）。两条入口最终都收敛为
   `InboundMessage` + `ConversationContext`。
2. 归属解析 `StreamRegistry.resolve_inbound`（`src/core/platform_io/registry.py:273`）
   把「平台 + 会话外部 ID + 发送者外部 ID」upsert 成内部稳定的 stream / person /
   identity 主键。它必须先于一切门控（`http.py:543` 的注释明确要求）：后续的
   trace、记忆写入、出站路由全部依赖这组内部 ID，门控拒绝的消息也要落库。
3. 开发者命令在归属解析之后、任何门控与观测之前截断（`http.py:555-562`）。命中后
   直接经 broker 回发，不进聊天缓冲、不创建回合；仅命令成功时把问答两条消息补入
   历史，让后续对话、抽取与摘要都能看到先问后答（`http.py:570-587`）。

## 2. 入口门控：每条消息一次的廉价过滤

入口门控在 HTTP 请求内执行，不调模型，只读确定性事实。核心是
`decide_disposition`（`src/core/agent/conversation_gate.py:205`），输入是
`GateRequest`，输出三态：drop / force / deliberate。

判定优先级（`conversation_gate.py:222-290`）：

0. `is_self_message` 直接 drop，保证永不回环。当前入口与批次两个调用方都不填
   这个字段（适配器侧已过滤 Bot 自己的消息），它是协议层的保险而不是现役判据。
1. 戳一戳超窗口直接丢弃：同一 stream 5 分钟内最多 3 次戳一戳能唤起回合
   （`POKE_SIGNAL_WINDOW_MS` / `POKE_SIGNAL_LIMIT`，`conversation_gate.py:90-91`），
   计数由入口的 `record_poke_arrival`（`src/core/services/chat/capabilities.py:24`）
   按到达时刻登记。排在信号收集之前，是因为连戳的后续必然命中「Bot 刚回复过」的
   自然窗口，只有提前丢弃才有效。
2. 私聊与桌面直接 force（`direct_conversation`）：这是问答契约，不允许沉默。
3. 群聊真实 @ 且配置了必回则 force，先于休眠与频率判断（`conversation_gate.py:228`）。
4. 休眠 drop。
5. 频率硬上限（`max_replies_in_window`）只约束无人点名的自发参与：@、名字、被戳、
   回复 Bot 的消息都先收为抬入码，再判断上限（`conversation_gate.py:244-255`）。
   上限曾经排在信号之前，真机上压掉过直接叫名字的消息。
6. 轻量注意力信号（名字提及、回复 Bot、进行中的话题、Bot 发言后 90 秒自然窗口等）
   任一命中则 deliberate，全部未命中则 `attention_filtered` drop。

两个容易踩的细节：

- 戳一戳的正文是适配器合成的（形如「[揉了揉月璃]」），其中出现的 Bot 名字不算
  点名，入口的名字匹配显式排除它（`http.py:631-637`）。
- 门控读的 `follow_up_declined`（Bot 上一次是否主动选择沉默）与批次门控必须是同
  一份事实（`http.py:656-658`），否则审计事件报告的门控态与实际生效的判定对不上。

drop 分支的语义是「不回复但看见」：消息仍写入 L1 历史并发观察事件
（`record_group_observation`，`src/core/services/chat/group_observe.py:34`），并落
一条 `gate_dropped` 行动事件（turn_id 为 None，`http.py:711-732`），回答「代码
根本没让 Bot 考虑」这类问题。

例外是扩展触发模式：群消息因 `attention_filtered` 被 drop、且该 stream 启用了
frequency / reply_necessity 口径时，入口不丢弃而是放行给批次门控累计
（`plain_group_deferred`，`http.py:660-666`）。

force 且正在休眠时，先保留门控时的 asleep 审计事实，再打断睡眠
（`http.py:739-742`，`ChatService.wake_from_inbound` 在 `service.py:576`）。

## 3. 缓冲与批次合并

`ChatService.send`（`service.py:773`）做四件事后立即返回：

1. 取消该私聊待发的定时追问——对方开口即静默期结束（`service.py:794`）。
2. 消息立刻落库（`service.py:799` 的 `append_message`）。先落库再排队，确认接收与
   持久化是同一个动作；回合失败不回滚这条用户消息。
3. 带图片/表情包的消息创建后台描述任务（`_describe_image_message`，
   `service.py:855`）：先以 `[图片]` 占位符落库并返回，VLM 描述成功后回写同一行
   正文。这样图片下载不拖住 HTTP 入站响应——适配器侧的入站是串行循环。
4. 追加到 `_buffers[stream_id]` 并置唤醒事件（`service.py:821-834`）。入缓冲时
   还没有回合。

消费由 `_poll_loop` / `_tick` 驱动（`service.py:714` / `:728`）：消息到达即唤醒，
0.1 秒心跳兜底（`CHAT_POLL_INTERVAL_S`，`src/core/services/chat/constants.py:20`）。
`_tick` 的关键判定：

- 只取缓冲头部「连续同一发送者」的前缀作为一个批次（`service.py:736-746`）。
  群聊里不同人的关系与事实彼此独立，混批会把 A 的话说给 B 的上下文。
- stream 一次只跑一个回合：`claim_stream`（`service.py:1384`）占用失败就跳过本
  tick，缓冲不动。占用分 reply / proactive 两个驱动源，跨源抢占失败才记
  `turn_competition` 事件；同源空转不记，否则 0.1 秒轮询会淹没有效事件。
- 取批次时发生异常，批次原样放回缓冲头部并释放占用（`service.py:768-771`），
  下轮重试，消息不丢。

## 4. 批次门控：合并后重算一次

`_start_turn`（`service.py:952`）把批次合并成一个 InboundMessage：正文按行拼接，
`mentioned_me` / `poked_me` 取 or，`pokes_in_window` 取最大值（`service.py:969-990`）。
戳一戳的事实不重算——入口按每次到达登过记，重算等于重复记账，合成正文的名字
匹配也会复活（`gating.py:39-43` 的说明）。

回合主体是独立 asyncio 任务（`service.py:1363` 的 `create_task`，挂在 `_inflight`），
`_tick` 不等它跑完。任务内顺序（`service.py:1037` 的 `_run`）：

1. 人格作息结算与记忆清扫（`service.py:1054-1057`）。
2. 等待批次的图片描述任务完成（`_materialize_batch_images`，`service.py:922`）。
   等待只阻塞本 stream 的回合任务，不阻塞其它 stream 的消费。
3. 会话刷新（`_refresh_session`，`service.py:1476`）：超过配置的静默间隔则重开
   会话，重摇语气与随机种子，私聊重逢间隔只注入一次提示词
   （`_take_resumption`，`service.py:1523`）。
4. 组装上下文（下一节）与批次门控 `_batch_gate`（`src/core/services/chat/gating.py:28`）。

批次门控用合并后的事实重跑同一个 `decide_disposition`。为什么入口判过还要再判：
批次不是单条消息——缓冲期间新消息到达、Bot 刚回复过、频率窗口计数都变了，合并
后的判断对象是「这一批」而非「那一条」。三态之后的去向由灰度归属
（`_agent_scope`，`service.py:2280`，按 `conversation_agent.mode` 分 off /
shadow / live）决定：

| 门控态 | off（旧管线） | shadow（只记录） | live（真实决策） |
| :--- | :--- | :--- | :--- |
| drop | 不应出现（入口已拦） | 同左 | `_handle_live_drop` 落 `gate_dropped`（`service.py:3137`） |
| force | 旧管线必回 | shadow 不观察 force | Agent 决策，动作集无 silent |
| deliberate | 旧管线策略决定 | 跑一遍 Agent 只落决策，再走旧管线 | Agent 决策 |

shadow 的一个边界：仅由频率预算或必要性评分产生的候选，在旧 signal 口径下本会被
drop，shadow 阶段不因此唤醒旧管线（`service.py:1128-1131`）。

## 5. 上下文组装：一次性取齐，增强延后

`_prepare_turn_context`（`src/core/services/chat/context_build.py:296`）不调模型，
一次取齐本回合全部输入，产出不可变的 `_PreparedTurnContext`
（`src/core/services/chat/state.py:186`）：

1. 事实召回双检索词：当前文本与会话印象各跑一次，按 ID 去重、词面分排序
   （`_recall_turn_facts`，`context_build.py:224`）。群聊短消息当检索词捞不到东西，
   印象（`ConversationImpressions`，`service.py:1086` 取）覆盖面是另一个量级。
   检索范围限定「在场者」（最近发言者 + 当前说话人，`_present_person_ids`
   `service.py:2372`）。
2. 情节记忆：召回与最近两路按 ID 去重后截断（`context_build.py:330-348`）。
3. 人格与关系：熟悉天数等 owner 专属信号只在 `relationship_signals_enabled` 时注入
   （`context_build.py:351-356`）；群聊里不能把这个信号套到每个说话人身上
   （`_prompt_config_kwargs` 的注释，`context_build.py:63-69`）。
4. 日程：具体活动只在用户真的问起时才注入，否则只以情绪和作息影响语气
   （`context_build.py:357-365`）。
5. 工作记忆带水位：`user_message_id_watermark` 隔离批次之后落库的消息
   （`context_build.py:367-371`）。因落库与生成可能交错（新消息先于上一回复入库），
   历史要按批次边界重排（`_order_working_memory_for_batch`，`context_build.py:183`）。
6. 两份历史并存：旧管线读的 `raw_history` 与 Agent 读的 `agent_history`——后者给
   每条用户消息加 `[消息ID]` 前缀（`_history_for_context`，`context_build.py:599`）。
   动作头的 targets 是数据库主键，编号不在历史里逐行可见时模型会把 targets 写成
   人名，整轮按 illegal_action 失败（`context_build.py:619-623` 的注释）。
7. 黑话查表在组装期执行一次，结果作为不可变字段带下去；决策与回复两次渲染共用，
   副作用每回合只发生一次（`context_build.py:390-403`）。

表达增强（向量重排事实 + 表达样本挑选）不在组装期做，而在确认要回复之后的
`_enrich_prepared_context`（`context_build.py:556`）。拆分模式下决策那次调用用
`decision_only` 渲染，省略回复风格、语调与表达样本三块（`service.py:2630-2640`）：
这些只影响「话怎么说」，Bot 选择沉默时这次模型调用根本不该发生。

## 6. 规划器决策：动作集即约束

live 路径进 `_run_conversation_turn`（`service.py:2496`）。一回合当前只跑一轮：
生成期间到达的新消息必须另开回合取新快照，没有新消息时再问一次只会得到确定的
收束答复（`service.py:2510-2523` 的说明）。

决策的合法性框架在 `src/core/agent/action_protocol.py`：

- `DecisionFrame`（`action_protocol.py:237`）是回合固定快照：可选消息只有本批、
  消息水位、动作集、平台能力，整个回合不变；ReAct 各轮只收窄动作集
  （`with_available_actions`）。
- 动作空间由 `available_actions`（`action_protocol.py:635`）单点计算：私聊/桌面/
  force 只有 reply（可先检索）；群聊 deliberate 有 reply/silent，react/poke 按
  平台实测能力追加，wait 只在未等过的批次出现，speak 按配置开关。认知动作
  （recall/inspect/consult）只在剩余预算大于零时在场。预算耗尽时没有「降级自动
  reply」——认知动作直接不在动作集里，模型再选就撞校验记 illegal_action。
- 平台能力是「配置开关与协议端上报」的与（`capabilities.py:6-7`）：适配器每次连接
  成功后整体替换上报（`capabilities.py:106-122`），未上报按不可用处理。反向处理
  会让 Bot 反复选中一个执行不了的动作，对方收到彻底的沉默。
- 模型输出的决策先过 `ConversationDecision.validate` / `_validate_frame_choice`
  （`action_protocol.py:322`）：FORCE 禁 silent、目标必须在可选消息内且不晚于水位、
  表情回应必须在封闭词表内。校验失败是协议错误，不允许静默降级成普通回复。

`ConversationAgent.run`（`src/core/agent/conversation.py:423`）是 ReAct 回环：
选到认知动作就执行检索、把观察追加进消息序列、再发起一轮（`conversation.py:493-541`）；
选到终局动作则回合结束。失败语义（`conversation.py:18-30` 的模块说明）：

- 正文先于动作头 / 缺动作头 → parse_error；动作头违反协议或回合帧 → illegal_action。
  工具调用模式下这两类先回灌拒绝原因重发一次（预算 1 次，
  `_ACTION_CALL_REPAIR_LIMIT`，`conversation.py:97-102`），因为兼容网关会把工具
  参数整段丢弃，这类错误重发即可自愈；重发仍不合法才落失败状态。
- 模型超时与其余调用失败 → timeout / provider_error。用户主动中断（aborted）原样
  上抛，不写行动决策事件。
- 认知检索本身失败（数据库错误等）原样上抛：那是本机故障，混进 provider_error
  会被当成服务商波动忽略。

提示词侧的配合在 `src/core/services/chat/agent_protocol.py`：协议文本内嵌可选消息
的「ID + 原文」清单（`_render_agent_protocol`，`agent_protocol.py:64`），reply /
silent 的 few-shot 示例紧贴最后一条真实用户消息（`_agent_output_examples`，
`agent_protocol.py:201`），输出要求并进末条用户消息而非追加新消息——它必须留在
整个序列最末才能成为生成侧最近的约束（`agent_protocol.py:180-193`）。

## 7. 回复生成：决策与表达可拆分

配置打开且 planner / replyer 两路 provider 都在时，决策与表达分成两次调用
（`_split_replyer`，`service.py:342-346`）；缺任一条件保持单次调用，不在运行期
才失败。拆分时决策只产出动作头，正文由回复生成那次写：

- 回复生成的消息序列由调用方按动作头组装（`replyer_messages` 回调，
  `service.py:2671`）。两级共用同一份 `_PreparedTurnContext`，历史、事实候选与
  场景完全同源；差别只在表达增强与协议段——决策依据和说话依据不会各说各话。
- `_stream_body`（`conversation.py:1184`）把回复生成的事件先暂存，整条响应通过
  协议校验（有非空 `<say>`、标签外无裸正文）后才统一放出
  （`conversation.py:1245-1253`）。流式边出边发的话，后段的协议错误会让前面已经
  发出的气泡、TTS、记忆写入无法收回。回复生成流里出现的动作头一律忽略——动作
  已定，不覆盖已通过校验的决策（`conversation.py:1201-1202`）。

旧管线（scope 为 off）是单次调用：动作策略先决定 reply / silent
（`service.py:1151-1164`），reply 时直接流式消费 chat provider
（`service.py:1227-1242`）。两条路径的解析、副作用、投递共用同一套下游。

## 8. 解析与副作用

`ResponseParser`（`src/core/agent/parser.py:200`）增量解析流式输出，产出 say /
text / sayEnd / mood / promise / emoji / decision 事件。`_consume_events`
（`service.py:3227`）逐事件消费：

1. 取消信号优先：`interrupt`（`service.py:1569`）置位后停止读取，已生成的正文仍
   按历史一致性规则保存（先 `_persist_reply` 再判断中断，`service.py:1248-1252`）。
2. 副作用即时生效：mood 事件进人格（权重按会话类型，群聊用配置倍率），promise
   只对 owner 生效——联系人消息不能改变主体计划
   （`_handle_side_effects`，`src/core/services/chat/outbound.py:35-84`）。
3. 表情包：模型只写目标情绪，真实命中才进出站与历史（`service.py:3243-3271`）。
   检索落空会留一条 `emoji_selection_missed` 事件，用于区分「没写」和「写了没中」。
4. 分句切分放在解析侧而不是投递侧（`_collect_outbound_segment`，
   `src/core/services/chat/helpers.py:106`）：`<say>` 闭合时按打字习惯切成气泡
   （`split_into_bubbles`，`src/core/agent/segmentation.py:91`）。平台出站、助手
   历史、控制台渲染共用这份 segments，切分前置才能保证三者看到的气泡完全一致。
5. 历史只落可见产物：动作头不进记忆；`<emoji>` 意图标签被剥掉，替换为实际命中的
   标记（`_persist_reply`，`service.py:1540-1567`；Agent 路径同口径，
   `service.py:2846-2858`）。模型声明只是意图，频控统计依据的是真实发送。

终局动作的分支（`_run_conversation_round`，`service.py:2598`）：

- silent：群聊里把 stream 加入 `_follow_up_declined`，关闭自然回应窗口——「看过
  并决定不接」这个事实替代了回复计数成为窗口的关闭条件（`service.py:2792-2798`）。
  不产生任何用户可见输出，只落行动决策事件。
- wait：批次退回缓冲头部、扩展触发累计加回去、登记等待水位，三件事缺一不可
  （`_hold_batch_for_wait`，`service.py:2910`）。群聊无新消息就一直等；私聊等过
  `DIRECT_WAIT_TIMEOUT_S`（10 秒，`constants.py:55`）由 `_tick` 强制重开，且该轮
  动作集里没有 wait——等一次的约束写在动作空间里，不需要循环计数器。
- poke / react：经 broker 直投（`_apply_poke` / `_apply_reaction`，
  `service.py:2958` / `:3050`），平台确认成功后动作事实才落助手历史；失败追加
  `delivery_failed` 事件再上抛。目标消息没有平台编号时如实失败，不退化成「改成发
  条消息」——那是替 Bot 改主意。

## 9. 出站投递：桌面流式，QQ 整轮

两条出口在解析事件处分叉：

- 桌面：每个解析事件实时推 `chat.event`（`_emit_parse_event`，`outbound.py:131`），
  `<say>` 闭合即触发 TTS（`_track_speech`，`outbound.py:109`），回合末补一个
  `chat.done`。气泡是流式出现的。
- QQ：攒齐整轮 segments 后 `_dispatch_outbound`（`outbound.py:195`）交给
  `PlatformBroker`（`src/core/platform_io/broker.py:56`）单播到
  `QqWebSocketDriver`，一轮回复封装为一个 `qq.send` 事件推给适配器进程
  （`src/core/platform_io/drivers/qq_ws.py:54-90`），适配器的
  `iter_outbound`（`backend.py:428`）收到后按节奏逐条发给协议端。

投递的三个判定：

1. 气泡间停顿在主体侧算好随载荷下发（`_batch_delays_ms`，`outbound.py:250`）：
   打字速度是角色行为参数，不该散落到各平台适配器各算一套。首条停顿恒为 0——
   模型生成本身已占了十几秒，对方视角里 Bot 早就在打字了。
2. 群聊引用由代码强制而非模型选择：目标消息之后已有更新的消息落地时才挂引用
   （`_quote_target`，`outbound.py:168`），判据只有「指认歧义是否存在」一条；
   私聊一律不引用。
3. 表情包使用记录只在拿到投递回执后回写（`outbound.py:238-242`）：「没发出去」
   不能记成「用过」，否则淘汰判据会被污染。

回合收尾（`service.py:2571-2596`）：只有确实产出过可见产物才结算人格，然后以
fire-and-forget 任务触发摘要、事实抽取、表达学习、画像刷新与场景观察——这些是
回合外的后台管线，不阻塞投递。私聊正常回复后挂唯一一轮定时追问机会
（`_arm_direct_follow_up`，`src/core/services/chat/follow_up.py:48`），对方任何
新消息都会取消它。

## 10. 异步边界与失败分支汇总

异步点（不占住调用方）：

| 位置 | 异步形态 |
| :--- | :--- |
| 图片描述（`service.py:812`） | 入站即建后台任务，回合启动时 await 结果 |
| 回合主体（`service.py:1363`） | 独立 asyncio 任务挂 `_inflight`，HTTP 请求早已返回 |
| 摘要 / 事实抽取 / 表达学习 / 画像（`service.py:2583-2595`） | 回复后 fire-and-forget |
| 私聊定时追问（`follow_up.py:75`） | asyncio 定时任务，新消息到达即取消 |
| 气泡节奏停顿（`outbound.py:250`） | 毫秒数随载荷下发，由适配器侧执行 |

失败分支：

| 失败点 | 去向 |
| :--- | :--- |
| 入口门控 drop | 落库为观察消息 + `gate_dropped` 事件，不回模型（`http.py:689-737`） |
| 批次门控 drop（live） | `gate_dropped` 事件 + 阶段看板，不回模型（`service.py:3137`） |
| 动作头协议错误 | illegal_action / parse_error，工具模式先纠错重发一次（`conversation.py:18-30`） |
| 模型超时 / 调用失败 | timeout / provider_error，本回合无可见输出 |
| 用户中断 | 已生成正文照样落历史，不回滚（`service.py:1298-1303`） |
| 回合内一般异常 | 用户消息与已生成助手正文保留，记 FAILED 阶段（`service.py:1328-1356`） |
| 出站投递失败 | `delivery_failed` 事件后上抛，动作事实不落库（`service.py:2885-2900`） |
| 取批次异常 | 批次放回缓冲头部、释放占用，下轮重试（`service.py:768-771`） |

贯穿全链路的不变量：已确认接收的用户消息永不因回合失败而删除；Bot 的动作事实
（回复、戳、表情）只在平台确认后才落库；每一个决定（包括「不决定」——drop 与
silent）都有对应的审计事件可回放。
