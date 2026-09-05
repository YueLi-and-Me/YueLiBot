# 可观测性

接续[架构总览](overview.md)的分层，本文只展开 `observe/` 与 `logging/` 两个包，以及它们在
`services/console/`、`services/dev/`、`api/` 里的消费方。回答三件事：日志与事件账本
为什么是两套东西；一条管线事件从产生到呈现走过哪些组件；事后能把一个回合还原到什么程度。

## 两套记录，两种读者

| | 结构化日志 | 事件账本 | 分阶段调用记录 |
| :--- | :--- | :--- | :--- |
| 实现 | `src/core/logging/` | `src/core/observe/` | `src/core/llm_models/snapshot.py`（写）、`src/core/services/dev/prompt_records.py`（读） |
| 载体 | 控制台文本、`logs/app_*.log.jsonl`、WebUI 日志流 | SQLite 表 `pipeline_events` | `logs/prompt/<任务>/*.json` |
| 读者 | 人，扫读 | 程序：观察面板、重放、调优统计 | 人，按份精读单次调用 |
| 保留 | 按文件数与天数滚动（默认 30 个 / 14 天） | 条数加时长（默认 2 万条 / 72 小时） | 每个任务目录 200 份 |
| 回答的问题 | 现在正在发生什么 | 某条会话、某个回合发生过什么 | 这次模型调用到底发了什么、回了什么 |

分开的理由，逐条说：

1. 日志按人的扫读习惯裁剪。`src/core/logging/logger.py` 把模块名换成中文别名、按模块
   着色、把多行字段压成单行（`_flatten`，logger.py:242）；事件名与枚举值只在展示层
   翻译成中文，落盘 JSONL 保留英文机器标识（`src/core/logging/log_display.py` 的模块
   说明）。这套处理的代价是信息有损，不能当数据源用。
2. 账本按程序检索设计。`pipeline_events` 用自增 `seq` 做主键，另有按时间、按
   stream、按 turn 的三个索引（`src/core/db/schema.py:9` 的 `EVENTS_DDL`）；payload
   是完整 JSON，不做任何展示裁剪。消费方都靠它工作：`/ws/events` 增量推流、
   `/events` 历史检索、`/stages` 阶段看板、`/replay` 重放，以及检索调优的统计查询
   （`src/core/memory/tuning.py:489` 直接按 kind 统计 `llm_final` 等事件）。
3. 合并成一套会怎样：把账本写进 JSONL，就没有游标分页和按 turn 的索引；把日志塞进
   SQLite，每条扫读都要先反序列化再去 ANSI。两者的保留周期也天然不同——日志文件
   要留几周备查，账本 72 小时外的细节已经由调用记录文件接管。
4. 两套只有一个交汇点：`emit()` 写完账本后把同一条事件交给 `emit_console_trace`
   （`src/core/observe/events.py:264`），让管线事件以日志的排版出现在控制台。账本里
   是完整字段，控制台上是摘要；方向只有这一个，日志永远不会倒灌进账本。

## 事件账本

1. 表与连接。账本的表建在主库 `memory.db` 里，但 `EventStore` 持有独立连接
   （`src/core/observe/store.py:100`，`check_same_thread=False`），由模块级锁串行化
   写入。这样事件写入从不挤进业务连接的事务里，发布方在事件循环线程还是普通线程
   都安全。连接与保留策略在启动时装配（`src/main.py:620`，取自 features.toml 的
   `[log]` 段），保留按「每写 500 条清理一次」批处理（store.py:22、154），而不是每
   条事件都跑一遍全量检查。
2. `emit()` 是唯一收敛点（`src/core/observe/events.py:217`）。它做三件事：把当前
   上下文绑定的来源元数据合并进事件；剥离 `seq`、`at`、`stage` 等保留字段，调用方
   无法伪造它们（events.py:23、245）；按类型分流——`LIVE_ONLY_KINDS`
   （`llm_chunk`、`foreground`）只实时广播、不落账，其余写入账本。收敛在一个函数
   里的收益是：稳定不变量可以在这里补一次，所有消费方都受益。现成的例子是
   `memory_fact_scope_blocked` 事件在 emit 里被补上 `factOriginKind`
   （events.py:234-241），观测端不必反推可见性规则。
3. 广播与持久化解耦。`EventBroadcaster`（events.py:48）给每个订阅者一个容量 200
   的有界队列，发布方只安排 `call_soon_threadsafe` 回调、不等待任何消费者；订阅者
   可用 `exclude_kinds` 在入队前丢弃不关心的类型，高频的 `llm_chunk` 不会灌满队列
   （events.py:108-125）。队列满了置 `overflowed` 标志，由消费方自己决定怎么办——
   `/ws/events` 的选择是以 1013 关闭连接，让前端重连后按游标追平（见下文）。
4. 读取有四个口径，各服务一个消费方：`since()` 游标分页给 `/ws/events` 回放
   （store.py:159）；`search()` 多条件倒序检索给 `/events`（store.py:262）；
   `current_stages()` 从账本重建每条 stream 的阶段快照给 `/stages`（store.py:194）；
   `max_turn_id()` 给回合编号播种（store.py:352，见下节）。注意 `since()` 返回的是
   游标之后**最新**的一页而非最早的一页（SQL 取 `ORDER BY seq DESC LIMIT n+1` 再反
   转）；超出部分由 `truncated`/`from_seq` 标记，WebUI 据此显示跳过条数
   （`webui/src/hooks/use-traces.ts:73`）。要更早的历史得走 `/events` 检索，两条通道
   是互补的。
5. 阶段看板没有独立内存状态。`current_stages()` 用窗口函数按 stream 取最近的
   `stage` 事件序列，向前扫描同名阶段算出连续停留时长（store.py:194-260）。重启后
   看板直接从账本恢复，不需要任何额外的状态同步代码。
6. 来源标签只有一个出处。`src/core/observe/source.py` 的 `source_label()` 把
   「桌面 / 私聊·某人 / 群聊·某群」的拼法收敛成一个纯函数，回合开始时由
   `bind_origin()` 绑进上下文（events.py:139），之后每条事件自动带上。控制台追踪行、
   轮末面板标题、WebUI 观察面板三处显示同一个标签，不会出现各端各拼一套。

## 阶段 ID 为什么冻结

`src/core/observe/stages.py` 定义了八个管线阶段（received、gated、context、
expression、generating、dispatching、replied、failed），冻结有两层含义：

1. 定义不可变。`Stage` 是 frozen dataclass（stages.py:13），业务代码只传递这些常量
   对象（`src/core/services/chat/service.py:105` 的导入列表），字符串字面量不进业务
   代码。改中文标签只需动一张表，不用全库搜索替换。
2. ID 跨版本稳定。阶段 ID 会原样落进账本的 `stage` 列，被 `current_stages()` 按
   `kind='stage'` 重建、被重放透传（`src/core/services/dev/replay.py:220` 起把原事件的
   stage 写进 replay 事件）、被 WebUI
   按 `stageLabel` 展示。一个版本写入的 ID 会在后续版本的查询里出现，所以 ID 是持久
   格式的一部分，不能像普通内部枚举那样随手改名。
3. 展示与存储解耦是这条约束的缓冲带：`label_for()` 对未知 ID 原样返回
   （stages.py:47-57）。账本里躺着旧版本阶段的记录时，看板和日志照常被渲染，只是
   标签退回成机器 ID——读历史不会因为代码升级而断掉。

## 回合追踪怎么分层

回合追踪分三层：编号、上下文绑定、调用收集。每层的机制不同，组合起来才让「按回合
看一条链路」成立。

1. 回合编号跨重启不撞号。编号由进程内计数器分配（`service.py:2264` 的
   `_next_turn`），但构造时用 `max_turn_id()` 从账本播种（service.py:472）。代码注
   释里留了不改回去的理由：曾实测 `turn_id = 33` 同时装着四次不同启动的对话、横跨
   31 小时，WebUI 按 turnId 聚合时把它们并成一张卡。账本会被保留策略清理，而清理掉
   的回合不再是碰撞源，所以「取留存事件的最大值」这个口径是充分的（store.py:352 的
   文档字符串）。
2. 上下文绑定走 ContextVar，不走参数传递。回合开始先分配编号、绑定来源
   （service.py:992-1012 的 `_next_turn()` 与 `bind_origin()`），阶段推进由
   `_mark_stage()` 统一走 `enter_stage()`（service.py:1418-1442）：来源元数据、
   stream、turn、当前
   阶段分别写进四个 ContextVar（events.py:25-28），之后整条调用链上任何一层
   `emit()` 都自动带上这组坐标，中间层不需要知道自己属于哪个回合。`enter_stage()`
   同时发出 `stage` 事件（events.py:268），阶段切换因此天然有账本记录。
3. 模型调用收集也走 ContextVar，但有「关闭」语义。`begin_turn()` 在回合上下文里放
   一个开放缓冲（`src/core/services/console/turn_panel.py:80`），路由层每完成一次调
   用就 `note_model_call()`（turn_panel.py:89）；`take_calls()` 在轮末取走列表并把
   缓冲标记为关闭（turn_panel.py:106-125）。关闭语义针对的是一个具体陷阱：回合收尾
   时派生的后台任务（摘要、事实抽取、表达学习）继承同一份上下文，如果只清空列表，
   这些迟到的调用会追加进旧缓冲，被下一轮的面板当成本轮调用显示。缓冲关闭后它们拿
   到 `False`，改走独立展示。
4. 收集的唯一写入点在路由层。`ExchangeRecorder.write()`
   （`src/core/llm_models/router.py:143`）在每次调用收尾时统一做三件事：`dump_exchange`
   落盘、`emit('prompt_record')` 把路径写回账本、`note_model_call()` 交给回合面板；
   返回 `False` 时立即 `render_model_call()` 独立成框（router.py:188）。「收集与否
   由返回值决定」保证一次响应只打印一次，不会既进轮末面板又单独打印。
5. 渲染出口在轮末。`render_turn()`（`src/core/services/console/trace_console.py:196`）
   把收到的消息、各级调用面板、可见回复、副作用和耗时页脚合成一个嵌套面板；
   `mark_turn_start()`（trace_console.py:112）在回合开始时同时启动计时和收集。失败
   轮走 `render_turn_error()`（trace_console.py:392），同样先取走本回合的调用，否则
   这批调用会残留到下一轮面板里。
6. 两个控制台出口的分工靠 turnId 划界。`emit_console_trace()` 对带 turnId 的事件
   一律不逐条打印（logger.py:519），因为轮末面板会整体呈现这一轮，逐条再打就是同一
   内容出两遍；唯一的例外是 `memory_fact_scope_blocked`，带 turnId 也照打
   （logger.py:519-523）。无 turnId 的管线事件（主动感知等）仍在这里逐条呈现，保住控
   制台可见性。`llm_chunk`、`foreground` 这类高频实时事件在此静音，控制台不被
   token 流淹没（logger.py:283）。

## 提示词记录与重放

提示词侧有两份记录，粒度不同、互相索引：

1. 账本侧的 `llm_request` 事件带完整 messages、采样参数、`renderParams`（每个模板
   的渲染参数）以及 `promptId`/`promptHash`（service.py:1217；指纹由
   `src/core/prompts/registry.py:519` 的 `prompt_metadata()` 对参与模板取组合哈希）。
   它回答「这次调用的输入是什么」。
2. 文件侧的 `dump_exchange()`（snapshot.py:348）按任务分目录写完整 JSON：内部请求、
   实际选中的候选、候选切换的 attempts、脱敏后的 provider 请求、正文/推理/工具调用、
   首字与总耗时、失败信息。密钥字段与请求头在写入前统一替换为占位符
   （snapshot.py:29-33、85）。每个任务目录独立按份数轮转（snapshot.py:60、456），回复
   这种高频任务不会把日程、摘要这种低频任务的记录挤掉。它回答「这一级 Agent 看到
   了什么、答了什么」，也是多级 Agent 下定位「哪一级出问题」的依据。
3. 两份记录由 `prompt_record` 事件缝合：落盘成功后路径被写回账本（router.py:170），
   从观察面板的一条事件能跳到对应的完整记录文件。读取侧
   `src/core/services/dev/prompt_records.py` 只做只读扫描，任务名与文件名先过正则白
   名单再拼路径，挡住路径穿越（prompt_records.py:26-27）；未启用记录与「启用但还没
   记录」用 `RecordsDisabled` 区分开，面板据此提示开开关而不是让人对着空列表等
   （prompt_records.py:30；`src/core/api/http.py:1301`）。

重放建立在这两份记录之上（`src/core/services/dev/replay.py`）：

4. 输入是账本里的一条 `llm_request`。`replay_event()`（replay.py:203）取出原
   messages 和 `renderParams`，用**当前**模板重新渲染提示词（`_render_current_prompt`，
   replay.py:69），然后只替换首条消息里的提示词正文、保留其余历史上下文
   （`_replace_prompt`，replay.py:145）——重放回答的问题是「同样的现场，换成现在的
   提示词会怎么说」，所以历史上下文必须原样、提示词必须重新渲染。
5. 不能准确重放时拒绝，而不是给一个误导性结果：`renderParams` 缺失（早于该字段落
   库的事件）、渲染参数与当前模板结构不符、占位符对不上，都直接抛错并说明原因
   （replay.py:72-136）。
6. 结果用三个哈希对照呈现：`originalPromptHash`（当时实际发送的正文哈希）、
   `templatePromptHash`（事件落账时的模板指纹）、`replayPromptHash`（当前模板渲染结
   果的哈希）。三者是否相等直接区分「模板改了」和「渲染参数变了」。重放的请求与
   结果以 `replay_request`/`replay_final` 写回账本并照常广播（replay.py:220-251），
   原输出用 `first_matching_after()` 在同一 stream/turn 里找当时的 `llm_final` 做
   对照（replay.py:252）。
7. 重放是隔离的：它直接调用 provider 的流式接口，不经过 ChatService——不写记忆、
   不改对话历史、不推进任何回合状态（replay.py 模块说明）。入口是
   `POST /replay`（http.py:1253），按事件的 `promptId` 路由到对应任务槽的模型；
   不在重放白名单里的 `promptId` 直接拒绝（replay.py:28-35、50-56）。

## 控制台呈现与 WebUI 日志流

控制台、Electron 控制台、WebUI 日志面板三端看到的是同一段文本，这不是三次渲染
碰巧一致，而是刻意的单点渲染：

1. 面板只渲染一次。`trace_console` 用固定宽度（100 列）的 rich 捕获控制台把面板渲
   染成带 ANSI 的字符串，一次写 stdout、一次发布到 `webui_logs`
   （trace_console.py:71-87）。固定宽度保证三端换行完全一致，也避免 stdout 是管道时
   rich 回退到窄宽度导致排版跳动。是否渲染在导入期由 `is_color_enabled()` 裁定一次
   （trace_console.py:45）：重定向到文件或测试环境下全部渲染函数直接跳过，调试面板
   不会混进服务日志。
2. 普通日志的 WebUI 支路是 structlog 处理器。`WebUiLogHandler`（logger.py:169）在
   渲染成字符串之后把同一行复制给 `webui_logs`；它在处理器链里的位置在
   `_ConsoleLevelGate` 之前（logger.py:706-707），因此控制台等级压不掉 WebUI 侧已
   收到的行。三条支路先取各自等级的最低值作为全局门槛，控制台与文件支路在此之上
   再按自己的等级筛一遍（logger.py:709-718）；WebUI 支路不做二次过滤，等级设置
   只影响控制台与文件。
3. `webui_logs` 是行级积压。`WebUiLogStream` 在内存里保留最近 500 行并给每行编号
   （`src/core/webui/logs.py:26`），多行面板在 `publish()` 里拆成逐行再入队
   （logs.py:49-77）。注释里留了必须拆行的实测：一块两级回合面板约 11.5 KB、99 行、
   922 个 ANSI 转义序列，按「块」发布会让积压与订阅队列的容量上限同时失去意义，
   前端按「条」预算的保留窗口也会被一块面板冲掉。`/ws/logs` 端点先发积压再推实时
   （`src/core/api/ws.py:203`），打开日志面板立即有上下文。
4. 观察面板走的不是这条文本流，而是结构化频道 `/ws/events`（ws.py:231）：先订阅
   实时广播、再按 `since` 游标从账本回放，随后按 seq 去重——这个顺序堵住了订阅与
   读取之间漏事件的窗口（ws.py:259-275）。订阅时排除 `LIVE_ONLY_KINDS`，注释里同样
   有实测依据：`llm_chunk` 按 token 触发，全量转发会让观察面板每个分片重渲染整棵
   事件树（ws.py:261-266）。订阅队列溢出时服务端以 1013 关闭连接，前端指数退避重连
   并用上次的 seq 追平（ws.py:278-292；use-traces.ts:49-99）。
5. 颜色裁定全进程只有一处。`initialize_logging()` 用「探测终端能力」而不是「读取
   上次裁定」来决定是否着色，再把结论写回色表模块（logger.py:664-667；
   `src/core/logging/logger_colors.py:198-238`）。注释记录了不这么做的后果：先跑过
   一次无色初始化的进程里，后续初始化会读到上一次留下的裁定值，新配置永远生效不了。
   Electron 以管道启动 Python 时注入 `YUELI_FORCE_COLOR=1`，因此管道转发同样着色
   （logger_colors.py:209-226）。

## 一条数据从哪走到哪

```
ChatService._start_turn            src/core/services/chat/service.py
  _next_turn()      分配回合号（启动时由 max_turn_id 从账本播种）
  bind_origin()     来源元数据写入 ContextVar
  enter_stage()     更新阶段上下文并 emit('stage')
       |
       v
observe.events.emit(kind, **fields)          src/core/observe/events.py
  |-- LIVE_ONLY (llm_chunk/foreground) ----> 只广播, seq=None, 不落账
  |-- 其余 --------------------------------> EventStore.append
  |       |
  |       v
  |   SQLite pipeline_events (memory.db, 跨重启)
  |
  |-- EventBroadcaster.publish ----> /ws/events ---> WebUI 观察面板 (结构化事件)
  +-- emit_console_trace ----------> stdout ------> 终端 / Electron 控制台
                              \---> webui_logs ---> /ws/logs -> WebUI 日志面板
                              (带 turnId 的事件在此止步, 由轮末面板整体呈现)

模型路由收尾 ExchangeRecorder.write          src/core/llm_models/router.py
  |-- snapshot.dump_exchange ----> logs/prompt/<任务>/*.json ---> /prompt-records
  |-- emit('prompt_record', path)            (路径写回账本, 两份记录互索引)
  +-- turn_panel.note_model_call -> 回合缓冲 -> trace_console.render_turn (轮末面板)
        +-- 不在回合内 / 缓冲已关闭 --------> render_model_call (独立成框)

事后回看
  /replay -> services/dev/replay.py
      账本 llm_request(messages + renderParams) x 当前模板 -> provider.stream
      replay_request / replay_final 写回账本, 与同回合原始 llm_final 对照
```

相关配置集中在 `features.toml` 的 `[log]` 段（等级、着色、文件轮转、事件保留、两类
提示词落盘开关，见[配置示例](../../config.example/features.toml)）；面板侧的页面入口
见[管理面板](../guide/webui.md)。
