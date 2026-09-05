<div align="center">

  <h1>月璃 · YueLiBot</h1>

  <!-- Badges Row -->
  <p>
    <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
    <img src="https://img.shields.io/badge/Electron-TypeScript-47848F" alt="Electron">
    <img src="https://img.shields.io/badge/Backend-Windows%20%7C%20Linux-blue" alt="Backend: Windows | Linux">
    <img src="https://img.shields.io/badge/Live2D-not%20required-brightgreen" alt="No Live2D">
    <img src="https://img.shields.io/badge/License-AGPL--3.0-blue" alt="License: AGPL-3.0">
  </p>

</div>

<br>

<!-- Mascot on the Right (Float) -->
<img src="assets/character/yueli/face/normal.png" align="right" width="30%" alt="月璃">

## 简介 · INTRO

月璃是一个基于大语言模型的 AI 角色，具备跨会话持续的记忆与随交互演化的人格，当前主要通过 QQ（私聊与白名单群）和浏览器管理面板使用。

本项目的核心难点不在「LLM + 立绘」——该组合两小时即可搭建完成——而在持续性：状态须跨重启保留，关系随交互演化；关闭程序数日后再次启动，她仍然是同一个她。

- 🧠 **持久记忆**：三层记忆结构与遗忘曲线相结合，跨重启保留；记忆随时间衰减，而非定期清零。
- 🎭 **动态人格**：好感度按对话者隔离、随交互漂移，冷落会使之下降；全局精力状态影响其当前表达。人格由状态驱动，而非一段固定的提示词。
- 👀 **发言节制**：日程与打扰预算共同决定每一轮是否发言，避免无节制的插话。
- 🎛 **全程可观察**：管理面板支持模型配置、逐级查看每轮模型调用的输入与输出；修改提示词后，可基于历史事件重放验证。
- 🌙 **离线补偿**：离线期间她仍会做梦、补偿流逝的时间，所写日记可供查阅。
- 🖼 **不依赖 Live2D**：角色素材为 AI 生成的立绘差分，依据一张参考图与一句描述即可生成整套表情。
- 💬 **主要出口为 QQ**：私聊与白名单群共享同一份记忆与同一条关系轴；群聊场景下亦会先行判断当轮是否发言。
- 💻 **实验性桌宠**：默认关闭的 Windows 10/11 外壳，启用后可能遇到未知问题。

---

## 安装 · INSTALL

**环境要求**：Python 3.11+ · Node.js（Electron 43）· 桌宠为实验性功能，需要 Windows 10/11

进程入口为 Python。**若仅运行 QQ 与管理面板，Node 与图形环境均非必需**，Linux 服务器可直接部署。

```bash
uv sync         # 安装 Python 后端依赖
npm install     # 安装实验性桌宠外壳与管理面板前端；无头部署可跳过
uv run bot.py   # 一条命令启动全部组件
```

首次运行会在 `config/` 生成一份带完整中文注释的初始配置后停止，并在控制台列出缺失的配置项。
**通常仅剩一项：API Key**——厂商地址与六个模型条目已按阿里云百炼的 OpenAI 兼容端点预填，
各任务已按档位分配（对话使用质量档，决策与摘要使用快档，视觉与嵌入各有专用模型），
默认全部关闭思考。填写完成后再次启动即可。如需接入 QQ，另需填写两个账号，见
[QQ 与群聊接入](docs/manual/adapters/index.md)。
若希望在填写前了解配置结构，无需 clone 仓库：[`config.example/`](config.example/README.md)
即首次运行所生成的配置，每个字段均附中文说明。

初次安装建议从 [从零跑起来](docs/manual/deployment/first-run.md) 开始——共五步，每步说明预期结果与对应的排查位置。

---

## 📚 文档 · DOCS

| | |
| :--- | :--- |
| [从零跑起来](docs/manual/deployment/first-run.md) | **新手从这里开始**：五步走完，每步写清该看到什么 |
| [安装与配置](docs/manual/deployment/install.md) | 依赖、五份 TOML 的生成与填写、启动方式 |
| [QQ 与群聊接入](docs/manual/adapters/index.md) | 协议端选择、必改项、群聊白名单与回复触发 |
| [管理面板](docs/manual/webui/index.md) | 浏览器里的观察与配置入口 |
| [无头部署](docs/manual/deployment/headless.md) | 服务器上只跑 QQ 与面板 |
| [数据库迁移与恢复](docs/manual/deployment/upgrade.md) | 备份位置与回退旧库的正确步骤 |
| [生图管线](docs/manual/features/sprite.md) | 从一张参考图跑出整套角色素材 |
| [架构总览](docs/dev/architecture/overview.md) | 分层、目录形状、进程关系与硬性边界 |
| [常见问题](docs/manual/troubleshooting.md) | 按症状查：启动、面板、模型调用、QQ、桌宠（实验性）、数据 |
| [开发与验证](docs/dev/guide/testing.md) | 常用命令、四条状态门、启动探针 |

全部文档的索引在 [`docs/`](docs/README.md)。

---

## 架构一句话 · ARCHITECTURE

业务真源在 Python，Electron 仅承担平台层职责——窗口、托盘、屏幕采集、键鼠活动。
**进程治理同样位于 Python 一侧**：Python 为入口，负责拉起并监护 QQ 适配器与可选的实验性桌宠外壳；
Electron 仅连接后端，不启动、不终止任何进程。因此在服务器上部署 QQ 机器人无需安装图形环境。

三条硬性边界：Python 后端不得依赖任何 Electron API；API Key 仅存在于主进程与
Python 后端，绝不下发至渲染层；Python 仅监听 `127.0.0.1`，所有接口均经过 token 鉴权。

目录形状与依赖方向见[架构总览](docs/dev/architecture/overview.md)。

---

## 💡 设计理念 · IDEA

> **最像，而不是最好。**
>
> 一个完美的助手不需要人格——你不会在意计算器今天心情如何。而一旦目标是陪伴，
> 「像人」就压倒一切：她可以答得不够全，可以有起伏，可以在你冷落她之后闹一点别扭，
> 但不能在你说了一句话之后表现得像刚认识你。
>
> 这条原则有具体后果：凡是「机械定时」的地方都应该换成连续量；新加的行为优先挂到
> 已有的人格轴上，而不是新开一个开关；随机不等于像人——掷骰子只会显得神经质。

---

## 💬 反馈 · FEEDBACK

- 如有问题，欢迎提交 issue：请附症状描述、复现步骤与相关日志。
- 暂不接受外部贡献。

---

## 🌟 致谢 · THANKS

- **[NapCat](https://github.com/NapNeko/NapCatQQ)**：现代化的 NTQQ 协议实现。
- **[SnowLuma](https://github.com/SnowLuma/SnowLuma)**：OneBot 11 正向 WebSocket 协议端，与 NapCat 二选一。

---

## 📌 注意事项 & License

> [!IMPORTANT]
> 用生图管线做自己的角色时，参考图的来源要自己把关：若参考的是他人的角色设计或成品画作，
> 生成结果仍可能构成侵权——这与「是否 AI 生成」无关。仓库内自带的立绘是本项目自己生成的。

**License**：[AGPL-3.0](LICENSE)。你可以自由使用、修改和分发本项目，但衍生作品必须以同一许可证开源；
**通过网络提供服务也算分发**——把改过的月璃挂成在线服务时，需要向使用者提供对应源码。
