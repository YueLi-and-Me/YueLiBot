<div align="center">

  <h1>月璃 · YueLiBot</h1>

  <p>
    <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
    <img src="https://img.shields.io/badge/Electron-TypeScript-47848F" alt="Electron">
    <img src="https://img.shields.io/badge/Platform-Windows-0078D6" alt="Platform">
    <img src="https://img.shields.io/badge/License-AGPL--3.0-orange" alt="License">
  </p>

</div>

<br>

<img src="assets/character/yueli/face/normal.png" align="right" width="30%" alt="月璃">

## 简介

月璃是一个运行在 Windows 桌面上的 AI 陪伴角色，同时可接入 QQ 私聊与群聊。
项目的目标不是「大模型加立绘」的组合，而是**持续性**：关闭程序、隔几天再次打开之后，
她仍然是同一个她——记得之前的事，关系与状态都有延续。

- **跨重启的记忆**：三层记忆（工作记忆、情节、事实）配合遗忘曲线，事实随时间淡化、被提及时强化。
- **随互动漂移的人格**：好感度按人隔离并随互动变化；全局精力与心情持续影响表达与主动开口的意愿。
- **开口有节制**：日程、屏幕感知与打扰预算共同决定是否开口，而非随时插话。
- **离线时间照常流逝**：日程持续推进，精力与心情按时间结算；再次打开时，她的状态与当前时刻一致。
- **桌面与 QQ 是同一个人**：QQ 私聊与白名单群聊共用同一份记忆与人格，仅出口不同；
  群聊中同样先判断这一轮是否该回应。
- **内部状态可观察**：浏览器管理面板可配置模型与全部 TOML、查看每轮每一级模型调用的输入输出、
  管理记忆与表情包库。
- **角色素材自给**：内置生图管线，一张参考图加一句角色描述即可生成整套立绘差分（表情、闭眼、嘴型），
  无需手工美术资产。

## 快速开始

**环境要求**：Python 3.11+；Node.js（Electron 43）；桌宠外壳需要 Windows 10/11。

进程入口是 Python。仅运行 QQ 与管理面板时，Node 与图形环境均非必需，可在 Linux 服务器上做无头部署。

```bash
uv sync         # Python 后端依赖
npm install     # 桌宠外壳与管理面板前端，无头部署可跳过
uv run bot.py   # 一条命令起全套
```

首次运行会在 `config/` 生成一份带完整中文注释的初始配置后退出，并在控制台列出缺失项。
通常唯一缺失的是 API Key：厂商地址与六个模型条目已按阿里云百炼的 OpenAI 兼容端点预填，
各任务已完成分档（对话走质量档、决策与摘要走快档、视觉与嵌入使用专用模型）。填写后再次启动即可。
接入 QQ 需另外填写两个账号，见[接入总览](docs/manual/adapters/index.md)。
首次运行生成的配置与 [`config.example/`](config.example/README.md) 内容一致，每个字段均带中文说明，
可在克隆前先行查阅。

首次安装建议从[从零跑起来](docs/manual/deployment/first-run.md)开始：共五个环节，每个环节写明了预期现象与排查入口。

## 文档

| | |
| :--- | :--- |
| [从零跑起来](docs/manual/deployment/first-run.md) | 新手入口：五个环节，每步写清预期现象 |
| [安装与配置](docs/manual/deployment/install.md) | 依赖、五份 TOML 的生成与填写、启动方式 |
| [功能总览](docs/manual/features/index.md) | 各项功能的默认开关、前提与依赖 |
| [QQ 与群聊接入](docs/manual/adapters/index.md) | 协议端选择、必改项、群聊白名单与回复触发 |
| [管理面板](docs/manual/webui/index.md) | 浏览器内的观察与配置入口 |
| [无头部署](docs/manual/deployment/headless.md) | 服务器上仅运行 QQ 与面板 |
| [升级与回退](docs/manual/deployment/upgrade.md) | 配置升级、数据库迁移与备份回退 |
| [常见问题](docs/manual/troubleshooting.md) | 按症状排查：启动、面板、模型调用、QQ、桌宠、数据 |
| [架构总览](docs/dev/architecture/overview.md) | 分层、目录形状、进程关系与硬性边界 |
| [开发与验证](docs/dev/guide/testing.md) | 常用命令、状态门与启动探针 |

完整索引见 [`docs/`](docs/README.md)。

## 架构

业务真源在 Python，Electron 仅承担平台层职责：窗口、托盘、屏幕采集、键鼠活动。
进程治理同样在 Python 一侧：它是入口，负责拉起并监护 QQ 适配器与可选的桌宠外壳；
Electron 只连接后端，不启动也不终止任何进程。因此在服务器上运行 QQ 机器人不再需要图形环境。

三条硬性边界：Python 后端不依赖任何 Electron API；API Key 只存在于主进程与 Python 后端，
不下发到渲染层；Python 仅监听 `127.0.0.1`，所有接口均经 token 鉴权。

目录形状与依赖方向见[架构总览](docs/dev/architecture/overview.md)。

## 设计理念

> **最像，而不是最好。**
>
> 完美的助手不需要人格——没有人在意计算器今天心情如何。而目标是陪伴时，
> 「像人」压倒一切：回答可以不够全面，状态可以有起伏，被冷落后可以闹一点别扭，
> 但不能在用户说了一句话之后表现得像初次见面。
>
> 这条原则有具体后果：凡是机械定时的地方都换成连续量；新增行为优先挂到
> 既有的人格轴上，而不是新开一个开关；随机不等于像人。

## 注意事项与许可

> [!IMPORTANT]
> 使用生图管线制作自有角色时，参考图来源需自行把关：参考他人的角色设计或成品画作时，
> 生成结果仍可能构成侵权，与是否由 AI 生成无关。仓库自带的立绘素材由本项目自行生成。

**License**：[AGPL-3.0](LICENSE)。可以自由使用、修改和分发本项目，但衍生作品必须以同一许可证开源；
通过网络提供服务同样构成分发——将修改后的月璃部署为在线服务时，需向使用者提供对应源码。

暂不接受外部贡献，问题与建议请开 issue。
