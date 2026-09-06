# 月璃 · 文档

分两套：

- **[用户手册](#用户手册)** —— 装、配、用。读者是使用者，不看代码。
- **[开发手册](#开发手册)** —— 架构与模块。读者是要改代码的人。

---

## 用户手册

### 部署

| 文档 | 内容 |
| :--- | :--- |
| [从零跑起来](manual/deployment/first-run.md) | **新手从这里开始**：五步，每步写清该看到什么 |
| [安装与配置](manual/deployment/install.md) | 环境要求、依赖安装、配置文件的生成与填写 |
| [Windows 上带桌宠](manual/deployment/windows.md) | 前端依赖与构建、托盘与窗口行为、开机自启 |
| [无头部署](manual/deployment/headless.md) | 服务器上只跑 QQ 与面板，含 systemd 单元 |
| [升级与回退](manual/deployment/upgrade.md) | 配置版本升级、数据库迁移与备份、回退旧库 |

### 配置

| 文档 | 内容 |
| :--- | :--- |
| [配置总览](manual/configuration/index.md) | 五份 TOML 的分工、单向引用链、配置模板在哪 |
| [bot.toml](manual/configuration/bot.md) | 名字与别名、用户关系、人格提示词、会话记忆、群聊触发、桌宠开关 |
| [models.toml](manual/configuration/models.md) | 模型目录、任务分档、挑选策略、超时、思考开关 |
| [providers.toml](manual/configuration/providers.md) | 厂商条目、预设标识与地址、鉴权、超时与重试 |
| [features.toml](manual/configuration/features.md) | 语音、视觉与感知出口、向量召回、反馈纠错、表情包库 |
| [适配器配置](manual/configuration/adapter.md) | `adapter.toml` 与插件目录下的 `config.toml` |

### 功能

| 文档 | 内容 |
| :--- | :--- |
| [功能总览](manual/features/index.md) | 哪些默认开、哪些要显式打开、彼此的依赖 |
| [记忆](manual/features/memory.md) | 她记住什么、怎么召回、会忘掉什么、你怎么查和改 |
| [表达方式与黑话](manual/features/expression-jargon.md) | 学什么、从哪学、怎么审核与撤销 |
| [表情包](manual/features/emoji.md) | 收录、语义标签、发送配额与内容审核 |
| [主动搭话](manual/features/proactive.md) | 什么时候会主动开口、怎么调频率、怎么关 |
| [人格与状态](manual/features/persona.md) | 好感度按人漂移，精力与心情如何影响表达 |
| [日程与睡眠](manual/features/schedule.md) | 一天的节奏怎么生成、睡着时什么能叫醒她 |
| [屏幕感知](manual/features/perception.md) | 两个正交开关、各出口的开放范围 |
| [生图管线](manual/features/sprite.md) | 从一张参考图跑出整套角色素材 |

### 接入 QQ

| 文档 | 内容 |
| :--- | :--- |
| [接入总览](manual/adapters/index.md) | 两个适配器怎么选、群聊白名单与回复触发 |
| [NapCat](manual/adapters/napcat.md) | 装、建正向 WebSocket、必改项、连不上的常见原因 |
| [SnowLuma](manual/adapters/snowluma.md) | 同上 |

### 管理面板

| 文档 | 内容 |
| :--- | :--- |
| [面板总览](manual/webui/index.md) | 怎么进、各页做什么、监听范围与鉴权 |
| [会话观察](manual/webui/observe.md) | 阶段板、事件账本、提示词工作台、实时日志 |
| [模型与厂商](manual/webui/models.md) | 厂商与模型目录、任务候选与采样参数 |
| [人物与关系](manual/webui/persons.md) | 她记住的人、关系状态与事实 |
| [黑话词表](manual/webui/jargon.md) | 学到的群内说法与命中情况 |
| [表达方式](manual/webui/expressions.md) | 学到的说话方式与被选用次数 |
| [记忆联想网络](manual/webui/memory-graph.md) | 记忆之间的边与扩散路径 |
| [检索调优](manual/webui/retrieval-tuning.md) | 召回参数扫描与效果对照 |
| [导入中心](manual/webui/import-center.md) | 资料入知识层、按批预览与撤销 |
| [记忆管理](manual/webui/memory-manage.md) | 按人看事实、失效与取代、冲突裁决 |
| [表情包库](manual/webui/emojis.md) | 目录与语义标签 |
| [月璃设置](manual/webui/settings.md) | 五份 TOML 的读改存 |
| [开发者命令](manual/webui/developer.md) | 聊天里可用的命令与通道开关 |

### 出问题了

[常见问题](manual/troubleshooting.md) —— 按症状查：启动、面板、模型调用、QQ、桌宠、数据。

---

## 开发手册

| 文档 | 内容 |
| :--- | :--- |
| [架构总览](dev/architecture/overview.md) | 分层、目录形状、进程关系与三条硬性边界 |
| [一次回合的完整链路](dev/architecture/conversation-turn.md) | 从入站消息到出站气泡，中间经过哪些判定 |
| [记忆系统](dev/architecture/memory.md) | 三层记忆、召回、遗忘曲线与联想扩散 |
| [平台抽象与适配器](dev/architecture/platform-io.md) | 出口契约、适配器插件协议、消息归属解析 |
| [可观测性](dev/architecture/observability.md) | 日志分层、事件账本、回合追踪与重放 |
| [配置体系](dev/architecture/configuration.md) | 五份 TOML 的职责、交叉校验与版本升级 |

按目录逐个说明职责、对外接口与依赖关系：
[core/agent](dev/modules/core-agent.md) ·
[core/memory](dev/modules/core-memory.md) ·
[core/services](dev/modules/core-services.md) ·
[core/db](dev/modules/core-db.md) ·
[core/config](dev/modules/core-config.md) ·
[core/platform_io](dev/modules/core-platform-io.md) ·
[core/logging 与 observe](dev/modules/core-observability.md) ·
[electron](dev/modules/electron.md) ·
[webui](dev/modules/webui.md) ·
[adapters](dev/modules/adapters.md) ·
[scripts](dev/modules/scripts.md)

开发流程：[开发与验证](dev/guide/testing.md) ·
[写一个工具插件](dev/guide/plugins.md) ·
[Electron 侧的三个坑](dev/guide/electron-pitfalls.md)
