<div align="center">

  <h1>月璃 · YueLiBot</h1>

  <!-- Badges Row -->
  <p>
    <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
    <img src="https://img.shields.io/badge/Electron-TypeScript-47848F" alt="Electron">
    <img src="https://img.shields.io/badge/Platform-Windows-0078D6" alt="Platform">
    <img src="https://img.shields.io/badge/Live2D-not%20required-brightgreen" alt="No Live2D">
  </p>

</div>

<br>

<!-- Mascot on the Right (Float) -->
<img src="assets/character/yueli/face/normal.png" align="right" width="30%" alt="月璃">

## 简介 · INTRO

月璃是一个住在 Windows 桌面上的 AI 陪伴角色。对标《妹居物语》。

她的核心不是「LLM + 立绘」——那个组合两小时就能搭出来，而且用两天就会腻。真正难的是**持续性**：让她在你关掉程序、隔了三天再打开之后，仍然是同一个她。

- 🧠 **关掉再打开，她还记得**：三层记忆加遗忘曲线，跨重启保留。不是每次对话都从空白开始。
- 🎭 **她会因为你而变**：按人隔离的好感度会随交互漂移，冷落她也有代价；全局精力持续影响她的表达。人格不是一段写死的提示词。
- 👀 **她知道什么时候不该说话**：日程、屏幕感知和打扰预算共同决定她开不开口。一个随时插话的桌宠只会被关掉。
- 🌙 **你不看她的时候她也在**：会做梦，会补偿离线期间的时间流逝，写的日记你可以翻。
- 💬 **不只在桌面**：QQ 私聊和白名单群里是同一个她——同一份记忆、同一条关系轴，只是出口不同。群里她也会先判断这一轮该不该开口。
- 🎛 **内部是可以看见的**：浏览器里配模型、翻她这一轮每一级模型调用的输入输出、改提示词再拿历史事件重放。
- 🖼 **不依赖 Live2D**：角色是 AI 生成的立绘差分，一张参考图加一句描述就能出一整套表情。

---

## 快速开始 · QUICK START

**环境要求**：Python 3.11+ · Node.js（Electron 43）· 桌宠需要 Windows 10/11

进程入口是 Python。**只跑 QQ 与管理面板的话，Node 与图形环境都不是必需的**，
Linux 服务器可以直接部署。

```bash
uv sync         # Python 后端依赖
npm install     # 桌宠外壳与管理面板前端，无头部署可跳过
uv run bot.py   # 一条命令起全套
```

第一次运行会在 `config\` 生成一份带完整中文注释的初始配置然后停下，控制台列出还差哪几项——
最少只需要两处：`bot.toml` 的 `[bot] name`，以及 `models.toml` 的 `model_identifier`
加 `providers.toml` 的 `base_url`、`api_key`。填好后再启动一次即可。

完整步骤见[安装与配置](docs/guide/install.md)。

---

## 文档 · DOCS

| | |
| :--- | :--- |
| [安装与配置](docs/guide/install.md) | 依赖、五份 TOML 的生成与填写、启动方式 |
| [QQ 与群聊接入](docs/guide/qq-setup.md) | 协议端选择、必改项、群聊白名单与回复触发 |
| [管理面板](docs/guide/webui.md) | 浏览器里的观察与配置入口 |
| [无头部署](docs/guide/headless.md) | 服务器上只跑 QQ 与面板 |
| [数据库迁移与恢复](docs/guide/database.md) | 备份位置与回退旧库的正确步骤 |
| [生图管线](docs/guide/sprite.md) | 从一张参考图跑出整套角色素材 |
| [架构总览](docs/architecture/overview.md) | 分层、目录形状、进程关系与硬性边界 |
| [开发与验证](docs/development/testing.md) | 常用命令、四条状态门、启动探针 |

全部文档的索引在 [`docs/`](docs/README.md)。

---

## 架构一句话 · ARCHITECTURE

业务真源在 Python，Electron 只做平台层——窗口、托盘、屏幕采集、键鼠活动。
**进程治理也在 Python 一侧**：它是入口，拉起并监护 QQ 适配器与可选的桌宠外壳，
Electron 只连接后端，不启动也不终止任何进程。所以服务器上不再需要为了跑一个
QQ 机器人先装图形环境。

三条硬性边界：Python 后端不得依赖任何 Electron API；API Key 只存在于主进程和
Python 后端，绝不下发到渲染层；Python 只监听 `127.0.0.1`，所有接口都过 token 鉴权。

目录形状与依赖方向见[架构总览](docs/architecture/overview.md)。

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

## 📌 注意事项 & License

> [!IMPORTANT]
> 用生图管线做自己的角色时，参考图的来源要自己把关：若参考的是他人的角色设计或成品画作，
> 生成结果仍可能构成侵权——这与「是否 AI 生成」无关。仓库内自带的立绘是本项目自己生成的。

**License**：[AGPL-3.0](LICENSE)。你可以自由使用、修改和分发本项目，但衍生作品必须以同一许可证开源；
**通过网络提供服务也算分发**——把改过的月璃挂成在线服务时，需要向使用者提供对应源码。

暂不接受外部贡献。有问题欢迎开 issue。
