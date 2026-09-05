# 月璃 · 文档

按「怎么用 / 怎么搭的 / 怎么改」三类组织。想跑起来看[使用](#使用)，想看内部结构看
[架构](#架构)，准备动代码看[模块](#模块)与[开发](#开发)。

## 使用

| 文档 | 内容 |
| :--- | :--- |
| [安装与配置](guide/install.md) | 环境要求、依赖安装、五份 TOML 的生成与填写 |
| [QQ 与群聊接入](guide/qq-setup.md) | 协议端选择、正向 WebSocket 必改项、群聊白名单与回复触发 |
| [管理面板](guide/webui.md) | 浏览器里的观察与配置入口，各页面用途 |
| [无头部署](guide/headless.md) | 服务器上只跑 QQ 与面板，含 systemd 单元 |
| [数据库迁移与恢复](guide/database.md) | 自动备份位置、迁移的单向性、回退旧库的正确步骤 |
| [生图管线](guide/sprite.md) | 从一张参考图跑出整套角色素材 |

## 架构

| 文档 | 内容 |
| :--- | :--- |
| [架构总览](architecture/overview.md) | 分层、目录形状、进程关系与三条硬性边界 |
| [一次回合的完整链路](architecture/conversation-turn.md) | 从入站消息到出站气泡，中间经过哪些判定 |
| [记忆系统](architecture/memory.md) | 三层记忆、召回、遗忘曲线与联想扩散 |
| [平台抽象与适配器](architecture/platform-io.md) | 出口契约、适配器插件协议、消息归属解析 |
| [可观测性](architecture/observability.md) | 日志分层、事件账本、回合追踪与重放 |
| [配置体系](architecture/configuration.md) | 五份 TOML 的职责划分、交叉校验与版本升级 |

## 模块

按目录逐个说明职责、对外接口与依赖关系。

| 文档 | 覆盖 |
| :--- | :--- |
| [core/agent](modules/core-agent.md) | 人设、动作协议、认知动作、表达与黑话学习 |
| [core/memory](modules/core-memory.md) | 记忆存储、检索调优、量化与联想 |
| [core/services](modules/core-services.md) | 对话编排与按职责分组的各类服务 |
| [core/db](modules/core-db.md) | 连接管理、表结构与迁移链 |
| [core/config](modules/core-config.md) | schema、加载器、写入器与版本升级 |
| [core/platform_io](modules/core-platform-io.md) | 出口契约、驱动与消息转发 |
| [core/logging 与 observe](modules/core-observability.md) | 日志实现层与事件账本 |
| [electron](modules/electron.md) | 主进程、preload、渲染层与共用契约 |
| [webui](modules/webui.md) | 管理面板前端的页面、hooks 与数据层 |
| [adapters](modules/adapters.md) | QQ 适配器插件的结构与协议 |
| [scripts](modules/scripts.md) | 校验、数据维护、跑数评估、迁移与生图 |

## 开发

| 文档 | 内容 |
| :--- | :--- |
| [开发与验证](development/testing.md) | 常用命令、四条状态门、启动探针 |
| [Electron 侧的三个坑](development/electron-pitfalls.md) | 不报错但功能不工作的三类问题 |
