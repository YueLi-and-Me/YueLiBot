# core/agent —— 对话代理

人格与提示词组装、行动协议与决策校验、ReAct 回环、流式解析、表达与黑话学习、
事实抽取与画像派生。本包是「她怎么决定说话与怎么说」的核心；把决策变成一次
完整的回合编排是 `src/core/services/chat/` 的事，本包被它组合，不反向依赖。

## 职责边界

- 负责：系统提示词的文本组装、行动协议定义与合法性校验、对话模型回环与流式
  解析、三态门控判据、回合后的一次性模型子任务（事实抽取、情节摘要、场景
  观察、画像生成、表达与黑话学习）。
- 不负责：模型调用本身的路由与流式实现（`src/core/llm_models/`）、回合编排与
  并发（`src/core/services/chat/`）、提示词模板的存储与版本（
  `src/core/prompts/`）、记忆的存取算法（`src/core/memory/`，本包是它的调用方）。

包入口 `src/core/agent/__init__.py` 只放包说明，不转出符号；以下按职责分组
逐个说明各文件。

## 行动协议与决策

- `src/core/agent/action_protocol.py`
  全包被引用最多的模块。定义「决策外壳 + reply 负载」两层协议：终局与认知动作
  枚举、封闭的理由码、平台能力、回合固定快照 `DecisionFrame` 与
  `ConversationDecision` 自检、四层可审计行动事件。全部纯确定性逻辑：模型产出
  必须先通过 `ConversationDecision.validate` 的硬边界校验，失败按协议错误处理，
  不允许静默降级成普通回复。`available_actions` 按 stream、平台能力与剩余认知
  轮次收窄动作空间——预算归零时认知动作直接不在动作集里，不存在「预算耗尽
  降级」路径；这一判据全库只有这一处。
- `src/core/agent/action.py`
  一轮上下文完成后的可选动作决策：`TurnAction`、`ReplyDecision`、动作与回复
  策略的 Protocol 及 `TurnPlanner`，是协议化之前的轻量决策层，仍被非 Agent
  模式的链路使用。
- `src/core/agent/conversation_gate.py`
  三态门控：DROP 只处理确定无争议的过滤（自己的消息、休眠、频率硬上限、无
  信号群聊噪声），FORCE 保证私聊、桌面交互与 @必回必须回应且不允许沉默，
  DELIBERATE 把其余候选交给 Agent 自主选择。只决定「是否进入意识」，不决定
  「回不回」；名字与别名由调用方动态传入，本模块不读配置、不写死称呼，
  不访问数据库、不调用模型。
- `src/core/agent/reply_necessity.py`
  群聊无信号批次的确定性触发口径：`frequency` 按发言预算折算候选阈值，
  `reply_necessity` 按内容信号计算 0~100 的回复必要性评分。只决定是否值得
  进入 DELIBERATE，进入后仍由 Agent 自主选择。
- `src/core/agent/conversation.py`
  Conversation Agent：行动核心中唯一真正调用模型的模块。一个回合由若干轮
  组成，每轮流式消费模型输出，解析器在动作头完整且通过回合帧校验之前不放出
  任何正文。终局动作结束回合；认知动作执行检索、回灌观察、回合继续，认知轮
  对用户不可见。失败语义分八种行动事件状态（parse_error、illegal_action、
  timeout、provider_error 等），部分错误先按限次纠错重发、把拒绝原因回灌给
  模型；用户主动中断原样上抛、不落行动事件。
- `src/core/agent/cognition.py`
  认知动作的内置实现：`recall` 检索长期记忆、`inspect` 检索本会话水位之前的
  聊天原文、`consult` 检索知识层。认知动作不产生用户可见产物，只把检索结果
  渲染成观察文本回灌给模型；三个实现由聊天服务包装成工具执行器绑进注册表。
- `src/core/agent/tool_schema.py`
  把回合动作空间翻译成 OpenAI 兼容工具声明的换算层：同一套约束从「XML 动作头
  + 提示词文字」改写成「函数签名 + JSON Schema」。枚举值全部引用
  `action_protocol` 的常量，不重新抄一份，保证两种表达同源。

## 解析与出站节奏

- `src/core/agent/parser.py`
  流式解析模型响应中的类 XML 标签，标签或属性被拆到多个网络块时仍可解析；
  完整标签转事件、普通文本转隐式发言事件，未完成部分留在缓冲区。不做网络
  I/O、不写记忆、不改人格状态。
- `src/core/agent/history.py`
  组装上下文前的幂等清理：修复未闭合的 `<say>`、移除副作用标签、合并连续同
  角色消息、按字符预算裁剪。只影响模型请求，不修改持久化历史。
- `src/core/agent/segmentation.py`
  把一条台词按语义切成多条气泡并计算打字停顿，二者共用同一套节奏参数；
  参数全部来自 `src/core/config/schema.py` 的 `TypingConfig`，不在此写死。
  停顿时长随出站载荷下发，平台适配器不重算节奏。

## 提示词组装

- `src/core/agent/prompt.py`
  组装主对话与主动搭话的系统提示词：人格、时间、关系、记忆、活动、日程、
  表达习惯、场景、黑话各渲染为独立块，交给 `src/core/prompts/registry.py` 的
  固定模板组合；另有动作协议、工具协议、回复者协议的渲染入口。只做文本
  构造，不调用模型。
- `src/core/agent/character.py`
  会话级语调抽取：按配置概率从候选语调中最多选一项，写入会话上下文；
  不生成或修改人格文本。
- `src/core/agent/relationship.py`
  把 0~100 的数值好感度映射为固定的关系深度标签，供提示词与观察面板使用。
- `src/core/agent/vocab.py`
  后端可识别的表情与动作固定词表，须与角色视图支持的标识一致。

## 表达与黑话学习

- `src/core/agent/expression.py`
  表达方式候选池：按会话从 `expressions` 表加权抽样并渲染注入文本；候选数量
  截断只发生在取池阶段。
- `src/core/agent/expression_select.py`
  按当前情境从候选池挑选表达样本：只向模型列出情境描述，模型返回编号；
  说法示例在选中之后才拼进注入文本——一并给会诱导模型偏向表述质量而非情境
  匹配。解析失败不生成新文本、不扩展候选集合。
- `src/core/agent/expression_learn.py`
  从已发生的对话学习「什么情境下怎么说话」并负责词表淘汰：回合后后台任务、
  独立游标、按阈值触发。语料只保留非助手发言——学习自身发言构成自举闭环，
  已实测把一句口癖的占比从 0 推到 7.3%。淘汰是确定性规则且只淘汰本模块学来
  的行，迁移存量清理由人工在管理面板进行。
- `src/core/agent/jargon.py`
  黑话查表与命中：扫描本轮上下文里他人消息中命中的已确认词条，纯子串匹配、
  会话高频词表加分压底误报；同一词一段对话只解释一次，Bot 名字与别名永不
  注入。不做自动学习、不做候选表、不做衰减。
- `src/core/agent/jargon_mine.py`
  黑话学习的纯逻辑层：提取候选、累积出现证据、按阶梯阈值三步推断（带上下文
  推断、只看词推断、比较两次结果，一致判普通词、有差异判黑话）。与服务层
  `src/core/services/maintenance/jargon_learn.py` 的分工是纯逻辑与调度游标，
  本层可独立单测。

## 回合后的模型子任务

- `src/core/agent/sub_agent.py`
  一次性模型子任务的统一执行器：独立提示词 + 流式收完 + 返回解析，集中处理
  消息校验、观测事件与渲染参数绑定；任务特有的解析与降级由调用方负责。
  表达选择、黑话挖掘、场景观察、摘要等小任务共用。
- `src/core/agent/fact_extract.py`
  把已发生的对话抽成结构化人物事实供长期记忆写入。独立于回复生成，是回合后
  的后台一级：自己的进度游标、按阈值触发、输出非法整批丢弃。同一次往返同时
  给出人物事实与知识候选，落库走 `MemoryStore.add_fact` 并写来源标记。
- `src/core/agent/summarize.py`
  把一段对话压缩为可检索的情节摘要：清理协议标签、渲染对话文本、请求结构化
  JSON；解析失败或对话过短返回 None，不写存储。
- `src/core/agent/observer.py`
  情景分析：从比工作记忆更宽的历史提炼「话题 + 气氛」两字段场景画像并缓存，
  供后续若干轮复用。气氛用封闭枚举（该字段直接进系统提示词），产物只作背景
  块——不进门控事实、不影响动作空间、观察失败则本轮无场景块。
- `src/core/agent/impression.py`
  会话印象：对一段对话的进程内概括，用作事实检索的第二检索词（短应答当检索
  词什么也捞不到）。与当前文本并集召回；状态不落库，重启后重新生成。
- `src/core/agent/profile.py`
  人物画像派生层：把事实与情节收敛成「对这个人的印象」。画像是派生缓存而非
  事实来源——整表删除不丢信息、可随时重建。信任分两档：确凿档由 facts 账本
  直接投影、逐条带可追溯 fact id、全程不经模型；印象档是唯一允许模型写入的
  一档。刷新先算证据指纹，指纹没变只推进时间戳、不再调用模型。

## 对外接口与调用方

- `action_protocol`：全包对外词汇（动作枚举、理由码、`DecisionFrame`、
  `ConversationDecision`、`GateInputFacts`、`ActionDecisionEvent`），被
  `src/core/services/chat/`（协议装配、出站、面板快照）与包内
  `conversation`、`tool_schema`、`prompt`、`conversation_gate` 消费。
- `ConversationAgent` 与 `decide_disposition`：由 `src/core/services/chat/`
  在 DELIBERATE / FORCE 候选与门控路径上调用；`available_actions` 产出的动作集
  由服务层按平台能力填充。
- `build_system_prompt` / `build_itemized_system_prompt`：聊天服务的上下文组装
  与主动搭话共用。
- `split_into_bubbles` / `typing_delay_seconds`：由
  `src/core/services/chat/helpers.py` 的出站收束逻辑调用。
- `run_extraction` / `run_learning` / `summarize` / `SceneObserver` /
  `refresh_profiles`：聊天服务在回合收尾与后台任务里调度。
- `mark_dirty`：`fact_extract` 写完事实后置画像脏位；`profiles_for_injection`
  供提示词组装读取。
- `run_sub_agent`：包内表达学习、表达选择、黑话挖掘、场景观察四个子任务共用，
  不出包。
- `lookup_jargon` 与 `mine_batch` 的调度对接在
  `src/core/services/maintenance/jargon_learn.py` 与 `jargon_stats.py`。

## 依赖方向

- 包外依赖：`src/core/memory/`（store、knowledge、association、decay、
  high_frequency 的读写与检索）、`src/core/prompts/registry.py`（模板取用与
  占位符校验）、`src/core/llm_models/`（流式协议、快照绑定、`LlmError`）、
  `src/core/platform_io/types.py`（StreamKind、消息类型）、`src/core/observe`
  （行动事件与学习事件落账）、`src/core/runtime/clock.py`、
  `src/core/config/schema.py`（节奏参数）、`src/core/tooling/`（工具注册与
  执行）。
- 被依赖：`src/core/services/chat/`（编排）、
  `src/core/services/maintenance/`（黑话学习与反馈纠错的调度）、
  `src/core/api/http.py`（面板读行动事件、复用门控判据、管理表达池与黑话
  开关）。
- 不允许反向：本包不 import `services`、不读运行配置文件；与聊天服务的全部
  协作通过函数参数与注入完成。纯逻辑模块（`action_protocol`、
  `conversation_gate`、`jargon_mine`、`segmentation`、`parser`）不调用模型、
  不碰数据库，可独立单测。
