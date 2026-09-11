# core/services —— 业务服务组合层

把内核各包组合成可运行的业务：对话回合的编排、媒体支路、后台维护、控制台
呈现、子进程宿主与开发者工具。决策的语义在 `src/core/agent/`
（见 [core/agent](core-agent.md)），记忆的算法在 `src/core/memory/`
（见 [core/memory](core-memory.md)），本包负责「什么时候调用谁、失败怎么记账、
结果怎么投递」。

## 职责边界

- 负责：回合编排（`chat/`）、图片理解 / 表情包 / TTS（`media/`）、联想边衰减
  与黑话学习等后台维护（`maintenance/`）、终端与面板的回合呈现（`console/`）、
  适配器与桌宠外壳的子进程拉起（`host/`）、开发者命令与重放（`dev/`）、
  主动消息投放（`proactive.py`）。
- 不负责：回复内容与门控判据（`src/core/agent/`，本包只装配与调度）、主动
  行为的规则计算（`src/core/awareness/` 的纯决策函数，`proactive.py` 只编排）、
  日程生成（`src/core/schedule/`）。

包入口 `src/core/services/__init__.py` 只放包说明，不做再导出；`chat/` 子包
的入口则相反，见下。

## chat/ —— 对话编排

- `src/core/services/chat/__init__.py`
  只做再导出。`chat` 拆包前是单文件模块，全项目以
  `from src.core.services.chat import ChatService` 引用，拆包后保持同一入口。
- `src/core/services/chat/service.py`
  `ChatService` 本体与回合主循环（约 3300 行），由十个 mixin 组合而成。持有
  按角色拆分的 LLM provider、`MemoryStore`、`Persona` 与日程服务，把入站消息
  推进为一次回合，并通过 WebSocket push 把事件推给 Electron 主进程。
- `src/core/services/chat/state.py`
  回合与会话的内部数据结构：缓冲消息、批次门控结果、流式解析状态、召回留痕、
  等待句柄等。只被 `service` 使用，对包外不构成接口。
- `src/core/services/chat/constants.py`
  编排常量集中地。单独成模块是因为各 mixin 也要读同一批常量——留在 `service`
  会让 mixin 反向导入 `service`，与 `service` 导入 mixin 组装类形成导入环。
- `src/core/services/chat/helpers.py`
  不依赖实例的模块级纯函数：召回事实到提示词条目（含同槽冲突整组补齐）、
  解析事件到出站分句的收集、表情命中到助手历史标签的序列化等。
- `src/core/services/chat/gating.py`
  批次门控：把睡眠状态、名字命中、@ 事实与回复必要性评分汇成三态门控结果
  （`DROP` / `FORCE` / `DELIBERATE`），并给出群聊扩展触发模式的读取口径。
- `src/core/services/chat/context_build.py`
  回合上下文组装：关系与人格配置、表达习惯挑选、工作记忆排序、事实召回与
  会话印象、黑话查表、日程描述，渲染成系统提示词与消息序列，产出组装完成的
  回合上下文。
- `src/core/services/chat/agent_protocol.py`
  Agent 协议渲染：把门控事实、可选消息预览与动作空间渲染成规划器读的协议
  文本；预览与历史的消息编号口径必须一致，模型才能指认目标。
- `src/core/services/chat/capabilities.py`
  平台能力判据：把配置开关与协议端实测能力合成一个判据，决定表情包、表情
  回应与戳一戳在当前出口是否可选——两者取与，未收到能力上报按不可用处理。
- `src/core/services/chat/outbound.py`
  解析事件的消费与出站投递：副作用写库、台词按打字节奏切气泡逐条投递、引用
  目标解析、非桌面平台的出站发送。切分前置才能让平台出站、助手历史与控制台
  渲染看到完全一致的气泡。
- `src/core/services/chat/background.py`
  回合之外的四条后台队列：情节摘要、事实抽取、画像刷新、表达学习。按批取
  输入、成功才推进游标；失败计数达上限跳批——这是确定性失败（如同样的输入
  永远得到同样的拒绝）不至死锁的出口。
- `src/core/services/chat/follow_up.py`
  私聊跟进：记录正常回复时刻、按配置安排定时追问、对方开始输入时决定是否
  催促、新消息到达撤掉待发跟进。群聊不走这条路径。
- `src/core/services/chat/group_observe.py`
  群聊观察与历史回填：把没轮到她开口的消息记成观察事件；首次进群按协议端
  提供的历史消息补齐空白，识别已见过的外部消息 ID、播种游标、按批写入。
- `src/core/services/chat/profiles.py`
  人物画像与身份呈现：整理画像供管理面板读取；组装回合上下文时解析发言者
  身份（合并多平台身份引用、挑展示名）。
- `src/core/services/chat/scene.py`
  场景观察调度：让观察 Agent 在后台读一段刻意宽于工作记忆的历史，产出场景
  画像并转成可注入提示词的文本行。

## media/ —— 媒体支路

- `src/core/services/media/__init__.py`
  包说明。三条支路彼此不互相依赖，都由对话编排在回合中调用。
- `src/core/services/media/chat_image.py`
  聊天图片的中文客观描述：后台按来源下载，表情包先按 SHA-256 查已有标签，
  未命中才调视觉模型；失败统一返回 None，由调用方保留占位符，不得猜测图片
  内容。
- `src/core/services/media/emoji.py`
  表情包库（全包最大）：按内容 SHA-256 存文件、按情绪语义检索。启动必须
  `verify_integrity` 重算全部文件哈希，不一致即阻止启动；入库有三道闸门
  （封禁表查哈希、大小上限、可选的视觉内容审查），后台按确定性 SQL 淘汰
  最冷条目并清理孤儿文件。
- `src/core/services/media/tts.py`
  台词转语音：从模型路由取 TTS 候选，支持 OpenAI 兼容与厂商协议，同音色同
  文本走进程内缓存；连续失败达上限后停止接受新任务，音频只在内存处理。

## maintenance/ —— 后台维护

- `src/core/services/maintenance/__init__.py`
  包说明。这些服务以后台循环或队列运行，不参与单次回合的同步路径。
- `src/core/services/maintenance/edge_decay.py`
  联想边的后台冻结。边与事实共用衰减语义但时间尺度差一个量级（边半衰期 720
  小时），因此不共用调度点：事实走 `MemoryStore.sweep`，边用低频轮询承载一次
  全表扫描。
- `src/core/services/maintenance/jargon_learn.py`
  黑话学习的后台服务：每会话独立游标、攒批提取、推断调用数与推断词条数都有
  上限常量（不节流就是四位数的调用量）。业务语义全在
  `src/core/agent/jargon_mine.py`，本层只回答何时学、学多少、推断谁。
- `src/core/services/maintenance/jargon_stats.py`
  高频词表快照的后台重建：只重建有新消息的会话，首轮全量一次让新部署立刻
  有表可用。
- `src/core/services/maintenance/memory_feedback.py`
  反馈纠错：事实进过提示词之后被用户纠正时写回库里。链路四段（登记锚点、
  关键词预筛、模型判定、按置信度应用），默认全关；纯否定不造事实，只写
  「已被纠正」标记由注入侧硬过滤。
- `src/core/services/maintenance/vector.py`
  向量服务：事实与知识向量的生成、历史数据补算、查询向量计算。未配置嵌入
  客户端时保持禁用；嵌入失败只影响对应向量操作，不阻断正文写入与关键词
  召回。

## console/ —— 回合呈现

- `src/core/services/console/__init__.py`
  包说明。只负责把已有事实排版成人读得懂的形态，不产生新事实。
- `src/core/services/console/turn_panel.py`
  按回合聚合各级模型调用（决策、回复生成、各次认知检索），渲染成嵌套面板。
  收集走 `ContextVar`：模型路由只管上报调用，不需要知道自己属于哪个回合，
  并发会话互不混淆。写入方是 `src/core/llm_models/router.py`。
- `src/core/services/console/trace_console.py`
  把回合合成嵌套彩色面板，一次写到终端与 `webui_logs`，三端呈现一致；回合外
  调用（视觉、摘要、抽取、日程）独立成框。非彩色或非交互场景全部渲染函数
  保持无操作。

## host/ —— 子进程宿主

- `src/core/services/host/__init__.py`
  包说明。Python 是进程入口之后，适配器与外壳都由本进程拉起，退出按相反
  顺序收走。
- `src/core/services/host/adapter_host.py`
  按 `config/adapter.toml` 声明拉起 QQ 适配器进程：只做「解析声明 → 组装
  命令行」，进程行为由 `src/core/runtime/child_process.py` 提供；适配器可选，
  没有声明时告警跳过。
- `src/core/services/host/desktop_shell.py`
  按桌宠开关决定是否拉起 Electron 外壳并解析启动命令行：有开发依赖走
  `npm run dev`，只有构建产物直接用本地 Electron 可执行文件，不做猜测。
  `[desktop_pet] enabled = false` 时什么都不产出——这正是无头部署形态。
- `src/core/services/host/lifecycle.py`
  `LifecycleManager`：按注册顺序启动服务、异常即停、关闭按逆序并记录单个
  错误。模块级 `lifecycle` 是进程范围的默认管理器。

## dev/ —— 开发者工具

- `src/core/services/dev/__init__.py`
  包说明。只服务于调试与观察，正常对话路径不依赖它们。
- `src/core/services/dev/dev_commands.py`
  开发者命令 `/git` 与 `/version` 的只读处理：只负责解析与执行，匹配与鉴权
  属于 `src/core/commands/` 的命令通道。`/git` 是全项目唯一 fork 外部进程的
  位置，「用户文本不进入子进程」的边界不得放宽。
- `src/core/services/dev/install_stats.py`
  开发者命令 `/inst`：读遥测服务端的 `GET /stats`，用 matplotlib 画折线图，
  经出站图片契约发到聊天里。绘图依赖在可选 extra `chart`，缺失时退回纯文字；
  服务端地址或 `YUELI_STATS_TOKEN` 为空时命令不注册。取数走
  `src/core/runtime/telemetry.py` 的 `TELEMETRY_ENDPOINT`，即自建域名；命令
  本身不做端点回退，与心跳链路的多端点尝试是两件事。
- `src/core/services/dev/prompt_records.py`
  分阶段模型调用记录的读取侧：把落盘目录扫成可分页摘要、按任务读单份完整
  内容；写入方是 `src/core/llm_models/snapshot.py`。
- `src/core/services/dev/replay.py`
  不进入业务服务的隔离模型重放：拿历史事件与当时的提示词资源重建一次调用，
  用于改提示词后的对照。

## proactive.py —— 主动消息

- `src/core/services/proactive.py`
  `AwarenessService`：接收前台窗口事件，调用 `src/core/awareness/` 的纯决策
  函数（意图、兴趣、预算、睡眠），维护待投放意图与每日预算，经 `ChatService`
  生成桌面主动消息。规则计算与平台协议都不在这里。

## 对外接口与调用方

- `ChatService`：`src/main.py` 装配构造（测试另有 40 个文件直接引用）；
  `proactive.py` 经它投放主动消息，`InboundMessage` 是入站消息的统一形态。
  认知动作由 `service` 包装成执行器、连同工具插件的声明一起登记进它持有的
  `src/core/tooling/` 注册表——插件侧只认工具协议，不直接 import 本包。
- 各后台服务（`VectorService`、`TtsService`、`EdgeDecayService`、
  `JargonStatsService`、`JargonLearnService`、`MemoryFeedbackService`）：
  `src/main.py` 构造并经 `lifecycle` 注册启停。
- `register_prompt_entries` / `marked_fact_ids`：被 `chat/context_build.py` 与
  `chat/helpers.py` 调用——锚点登记发生在事实真正进提示词的时刻。
- `note_model_call` / `begin_turn`：模型路由写入、`trace_console` 渲染；
  `render_observation` 被 `chat/group_observe.py` 复用。
- `replay_event` 与 `prompt_records` 的三个读取函数：被
  `src/core/api/http.py` 的开发者路由调用。
- `build_adapter_process` / `build_desktop_shell_process` / `lifecycle`：
  `src/main.py` 的启动编排。

## 依赖方向

- 本包是全库最大的组合点，依赖 `agent`、`memory`、`persona`、`awareness`、
  `schedule`、`llm_models`、`observe`、`platform_io`、`config`、`commands`、
  `tooling`、`prompts`、`runtime` 与 `src/core/webui/logs.py`。
- 反向不允许：`agent` 与 `memory` 不 import 本包；协作经由函数参数与注入。
  包内 `chat/` 的 mixin 只依赖 `state`、`constants`、`helpers`，不允许反向
  导入 `service`（导入环）。
- `console/` 只读不写：它消费模型路由上报的调用记录，不产生任何业务事实；
  `dev/` 的命令全部只读，不改仓库状态、不写数据库（`/git` 的提交计数除外，
  它只读本地仓库）。
