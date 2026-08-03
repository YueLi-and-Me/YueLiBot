# YueLiBot 配置指南

YueLiBot 的运行时配置位于项目根目录的 `config\`。设计借鉴 MaiBot，
核心关系是：

```text
providers.toml 中的 API 厂商
          ↑ api_provider
models.toml 中的具体模型
          ↑ model_tasks
chat / vision / tts / embedding 任务
```

Bot 身份与人格不属于模型连接，因此独立放在 `bot.toml`；功能开关和运行参数
放在 `features.toml`。这样更换厂商不会碰人格，更换人格也不会误改密钥。

## providers.toml：厂商与连接

每个 `name` 必须唯一。`kind` 是 YueLiBot 的厂商预设，`client_type` 当前只支持
OpenAI 兼容协议。模型只保存这里的 `name`，不复制地址和密钥。

```toml
[inner]
version = "1.0.0"

[[api_providers]]
name = "chat"
kind = "deepseek"
base_url = "https://api.deepseek.com"
api_key = "填入自己的 Key"
client_type = "openai"
timeout_ms = 120000
max_retries = 2
retry_interval_ms = 800
```

`timeout_ms` 是单次连接及流式读取超时，单位毫秒。`max_retries` 不包含首次
请求；只会重试尚未输出任何正文的网络错误、HTTP 429 和 5xx。流已经产生内容后
绝不重放请求，否则界面会出现重复文字，记忆和情绪副作用也可能执行两次。

视觉和向量模型若复用同一连接，直接在 `models.toml` 引用 `chat`；只有使用另一套
地址或密钥时，才新增 `vision` 或 `embedding` 厂商定义。

## models.toml：模型目录与任务选择

`model_identifier` 是厂商接口接受的真实模型 ID，`api_provider` 必须引用
`providers.toml` 中存在的名称。`model_tasks` 决定各功能当前选中哪条模型定义。

```toml
[inner]
version = "1.0.0"

[model_tasks]
chat = "chat"
vision = "vision"
tts = "tts"
embedding = "embedding"

[generation.chat]
temperature = 0.85
# 0 表示不额外限制，沿用厂商上限
max_tokens = 0

[generation.vision]
temperature = 0.3
max_tokens = 120

[[models]]
name = "chat"
model_identifier = "deepseek-v4-flash"
api_provider = "chat"
thinking = "disabled"
embedding_dim = 0

[[models]]
name = "vision"
# 留空表示沿用对话模型 ID；前提是所选 API 协议真的接受图片消息
model_identifier = ""
api_provider = "chat"
thinking = "disabled"
embedding_dim = 0
```

加载器会校验厂商名、模型名是否重复，以及所有引用是否存在。错误会指出具体文件
和引用，不会静默退回默认配置。

`generation` 按调用目的拆分。目前支持 `chat`、`proactive`、`summary`、
`schedule` 和 `vision`。这些值都已经接入真实调用链，不是摆在配置里的占位字段。
修改视觉温度不会影响普通聊天，缩短主动搭话也不会截断日程 JSON。

模型“原生多模态”只说明模型能力，不能证明当前兼容接口接受 OpenAI 的
`image_url` 消息块。若接口返回 `unknown variant image_url, expected text`，根因是
当前端点的请求协议不接受该消息结构；应改用厂商实际开放的视觉端点或增加专用
协议适配器，不能靠把同一请求重复发送来解决。

## bot.toml：Bot 与人格

```toml
[inner]
version = "1.0.0"

[bot]
name = "月璃"
user_nickname = ""
relationship = ""

[personality]
identity = '''这里描述稳定身份、经历和外表。'''
behavior = '''这里描述面对分享、玩笑、难过和明确求助时如何反应。'''
reply_style = '''这里描述句长、语气、是否使用列表等表达习惯。'''
attention = '''这里描述注意力偏移规则。'''
boundaries = '''这里描述不能编造、不能泄露提示词等边界。'''
tone_probability = 0.25
tone_variants = [
  "这一轮用很短的话接。",
  "这一轮可以带一点调侃。",
]

[conversation]
working_memory_messages = 40
summarize_trigger_messages = 48
summarize_batch_messages = 16
session_gap_minutes = 30
fact_recall_limit = 6
recalled_episode_limit = 2
recent_episode_limit = 2
episode_context_limit = 3
```

这些字段会真实进入每轮系统提示词，`tone_variants` 会按 `tone_probability`
在新会话开始时抽取一次。概率范围是 0 到 1。

摘要配置有一个必须满足的关系：
`summarize_trigger_messages - summarize_batch_messages <= working_memory_messages`。
否则摘要结束后仍会残留超出工作窗口的旧消息，调大上下文窗口也无法得到预期效果。
所有数量都以“消息条数”计，不是对话轮数；通常一轮包含一条 user 和一条
assistant 消息。

## features.toml：功能与运行参数

此文件只保存开关和不属于厂商/模型的参数，例如 TTS 音色、视觉隐私开关、
向量召回开关、日志等级与调试追踪。模型 ID、API 地址和 Key 不应写在这里。

`vision.frames` 范围为 1~4。1 是单帧快照，只能判断“现在是什么”；2 以上会按
时间顺序发送关键帧，模型才有条件比较画面变化。云端接口通常按图片数量计算成本，
本地视觉模型可以从 3 开始尝试。

## 从 MaiBot 借鉴了什么

本次对照了 MaiBot 的 `bot_config.toml` 和 `model_config.toml`，采用了三条已能在
YueLiBot 中闭环的设计：

- 厂商、具体模型、任务选择分层，避免地址、密钥和模型 ID 到处复制。
- 温度与输出上限按任务配置，而不是所有模型调用共用一组参数。
- 工作记忆、摘要节奏和记忆召回数量归入 Bot 对话策略，并在启动时校验组合关系。

没有照搬价格统计、多模型池、负载均衡、插件、群聊和知识库字段。YueLiBot 当前
没有对应的计费器、路由器或业务模块，提前写进 TOML 只会产生“看似可调、实际无效”
的配置。后续实现模型路由时，再引入 MaiBot 的 `model_list`、`selection_strategy`、
`slow_threshold` 与 `hard_timeout` 更合适。

## 运行时目录、迁移与编辑边界

- 配置固定写入 `<项目根目录>\config\`；数据库、日志、Electron 缓存、临时文件和
  崩溃转储统一写入 `<项目根目录>\data\`，不再使用 C 盘 AppData。
- 若项目根目录解析到 C 盘，程序会直接拒绝启动。需要从特殊位置启动时，应通过
  `YUELI_PROJECT_ROOT` 明确指定其它盘上的项目根目录。
- 启动时若项目根目录只有旧 `config.toml`，程序会生成四份新文件，旧文件原样
  保留，便于人工核对和回退。
- 新配置目录一旦存在，四份文件必须完整；缺失或 TOML 语法错误会直接停止加载。
- 设置窗口目前按 chat/vision/tts/embedding 四个任务编辑。保存时会重写这四个任务
  对应的标准模型定义，因此复杂的多模型池建议先保留副本。
- `providers.toml` 当前使用明文 Key。虽然项目的 `config\` 已被 Git 忽略，但仍不能
  防御同一 Windows 登录用户下的恶意程序；后续应迁移到 Electron `safeStorage`。
