# 架构总览

业务真源在 Python，Electron 只做平台层。本文说明这条分界怎么落到目录上、进程之间谁拉起谁，以及三条不能越过的硬性边界。

业务域已经从 TypeScript 迁到 Python：Electron 只做平台层——窗口、托盘、屏幕采集、键鼠活动，对话、记忆、人格、日程、主动行为全在 Python 后端。**进程治理也在 Python 一侧**：它是入口，拉起并监护 QQ 适配器与可选的桌宠外壳，Electron 只连接后端，不启动也不终止任何进程。这样服务器上不再需要为了跑一个 QQ 机器人先装图形环境。

目录形状：`src/` 是 Python 包根，按「内核 / 桌宠 / 平台适配器」分成三个包，入口在仓库根的 `bot.py`；Electron 那一侧整体收在 `electron/`，管理面板的前端在 `webui/`。

```
bot.py              进程入口

src/                业务真源（Python 包根）
  main.py           启动装配：读配置、建服务、拉 uvicorn
  core/             内核：与出口无关的业务真源
    agent/          人设、动作协议与解析、认知动作、观察 Agent、表达习惯、反思
    llm_models/     OpenAI 兼容的流式对话、多厂商路由与每次调用的落盘记录
    memory/         三层记忆、分词、遗忘曲线
    persona/        按人好感度 + 全局精力 → 自然语言行为指令
    awareness/      前台归类、键鼠强度、兴趣值、意图队列、睡眠状态
    schedule/       24h 生成式日程
    services/       按职责分组的业务服务
      chat/         对话编排：ChatService 主体与按职责拆开的十个 mixin
      media/        图片理解、表情包、TTS
      maintenance/  联想边衰减、黑话学习与统计、向量索引、反馈纠错
      console/      回合追踪与分层面板的控制台呈现
      host/         QQ 适配器与桌宠外壳的子进程监护、生命周期编排
      dev/          开发者命令、事件重放、提示词记录
      proactive.py  主动搭话
    tooling/        统一工具协议：内置动作、插件与 MCP 共用一张表
    commands/       开发者命令注册表
    observe/        管线事件账本、冻结的阶段 ID、事件广播
    platform_io/    出口契约：桌面的流式出口与 QQ 的整句出口共用同一套
    prompts/        外置提示词模板、占位符校验与版本归档
    api/            FastAPI 路由与 WebSocket
    webui/          管理面板的静态资源托管与日志接口
    config/         配置 schema 与多文件 TOML 加载
    db/             SQLite 连接、表结构与版本迁移链
    logging/        结构化日志、控制台配色与中文别名、JSONL 落盘
    runtime/        统一时钟、子进程监护、后端连接坐标、启动自检
  desktop/          桌宠专属：前台归类、屏幕视觉、传感器
  platforms/onebot11/ QQ 适配器宿主，由入口拉成独立进程

webui/              管理面板前端（React + Vite），构建产物出到 out/webui

electron/           Electron 端（TypeScript）
  main/             主进程（持有 API Key）
    platform/       ★ 系统调用适配层，迁移 Tauri 只需重写这里
    python/         Python 后端的连接管理与 HTTP/WS 客户端（只连不拉）
  preload/          最小化 IPC 桥
  renderer/         画面（不可信内容的运行环境，无凭证）
    character/      CharacterView 接口 + 立绘差分实现
  shared/           两端共用的类型与 IPC 契约

scripts/            check 校验 / maintain 数据维护 / eval 跑数评估
                    / migrate 数据迁移 / sprite 生图管线
```

三条硬性边界：

- Python 后端**不得**依赖任何 Electron API
- API Key 只存在于主进程和 Python 后端。渲染层要显示模型生成的内容，给它凭证等于把 Key 放进不可信环境
- Python 只监听 `127.0.0.1`，且所有接口都要过 token 鉴权
