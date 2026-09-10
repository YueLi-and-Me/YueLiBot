<div align="center">

  <h1>月璃 · YueLiBot</h1>

  <p>
    <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
    <img src="https://img.shields.io/badge/%E8%AE%B8%E5%8F%AF%E8%AF%81-AGPL--3.0-orange" alt="许可证">
  </p>

  <p>
    官方群 <b>424949962</b> · 文档中心 <b><a href="https://docs.yuelibot.org/">docs.yuelibot.org</a></b>
  </p>

</div>

<br>

<img src="assets/character/yueli/face/normal.png" align="right" width="30%" alt="月璃">

## 🌟 什么是月璃

月璃是一个由 LLM 驱动的可交互智能体。

她不仅仅是一个机器人，她有记忆、有性格、有自己的日程和作息，是一个像真实人类一样陪伴着你的数字生命。

## ✨ 特性

- 💬**更拟人的回复**：或长或短，一条一条地来；不会甩给你一篇分点罗列的答卷。
- 👂 **她看气氛**：群里聊得热闹时不抢话，没人搭理时也不会一直刷存在感；该开口时开口，该安静时安静。
- 🧠 **关掉再打开，她还记得**：你随口提过的事她会记住很久，也会像人一样慢慢淡忘；
  哪天再提起，她又想得起来。
- ❤️ **她越来越了解你**：你的喜恶、习惯和说过的话，她都放在心里；关系随相处变化，冷落她也有代价。
- 🎭 **她会学你们说话**：群里流行的句式和口癖她会悄悄学，等你点头认可之后才会真的用。
- 💭 **黑话她听得懂，但不卖弄**：你们圈里才懂的词她明白意思，只为听懂你们聊天，不会挂在嘴边。

## 🚀 快速开始

```bash
uv sync         # 安装后端依赖
npm ci          # 构建桌宠与面板前端；无头部署可跳过
uv run bot.py   # 启动
```

**环境要求**：Python 3.11 以上，由 uv 自动准备，不用自己装；桌宠需要 Windows 10 / 11，
且项目目录**不能放在 C 盘**；管理面板的前端产物要用 Node.js 22 以上构建一次。

**首次启动不会直接跑起来**：终端打印用户协议，输入「同意」后生成一份带完整中文注释的
配置并主动退出（退出码 1，正常）。填上 `config/providers.toml` 里的 `api_key`，
再次启动就会看到「WebUI 已就绪」。协议正文在 [AGREEMENT.md](AGREEMENT.md)，
建议先读完再输入。

- [用户手册](docs/manual/index.md)：三条路线怎么选、每步该看到什么，写清了
- [把安装交给 AI](docs/manual/ai-install.md)：交给 AI 执行的规格，含硬约束、完成判据与出错判定表
- [环境要求](docs/manual/deployment/requirements.md)：逐项对照，不满足的地方写清了怎么补

## 📚 文档

用户手册与开发手册都在[文档中心](https://docs.yuelibot.org/)，完整索引见 [`docs/`](docs/README.md)。

| 我想…… | 看这篇 |
| :--- | :--- |
| 第一次装 | [用户手册](docs/manual/index.md) |
| 让 AI 代劳 | [把安装交给 AI](docs/manual/ai-install.md) |
| 接到 QQ | [QQ 接入总览](docs/manual/adapters/index.md) |
| 部署到服务器 | [无头部署](docs/manual/deployment/headless.md) |
| 调配置 | [配置总览](docs/manual/configuration/index.md) |
| 查功能默认值 | [功能总览](docs/manual/features/index.md) |
| 出问题了 | [常见问题](docs/manual/troubleshooting.md) |

## 🔌 适配器接入

接入需要两个东西：一个**独立安装并登录的协议端**，以及月璃随附的适配器。
月璃不会自动安装或登录协议端，两者也不能同时启用。

目前可用适配器有：

- [NapCat](https://github.com/NapNeko/NapCatQQ)
- [SnowLuma](https://github.com/SnowLuma/SnowLuma)

Windows 上推荐用 [NapCatQQ Desktop](https://github.com/NapNeko/NapCatQQ-Desktop)：
可以统一配置 NapCat 与 SnowLuma。
见 [QQ 接入总览](docs/manual/adapters/index.md)。

接入 QQ 有账号风险，**机器人号建议用小号**，协议正文见 [AGREEMENT.md](AGREEMENT.md)。

## 📊 匿名统计

默认开启，用于统计共有多少个安装、分别是什么版本。上报内容固定三项：应用版本、
系统类型、Python 版本。**不含聊天内容、不含任何身份信息，也不采集 IP。**

关闭：把 `config/features.toml` 的 `[telemetry] enabled` 改成 `false`。
首次启动时控制台也会把这几句念一遍。完整说明见[用户协议](AGREEMENT.md)第四节，
字段位置见 [features.toml 配置](docs/manual/configuration/features.md#匿名统计)。

## 📌 注意事项与许可

> [!IMPORTANT]
> 使用生图管线制作自己的角色时，参考图来源需自行把关：参考他人的角色设计或成品画作，
> 生成结果仍可能构成侵权，与是否由 AI 生成无关。仓库自带的立绘素材由本项目自行生成。

**许可证**：[AGPL-3.0](LICENSE)。可以自由使用、修改和分发本项目，但衍生作品必须以同一许可证开源；
通过网络提供服务同样构成分发——把改过的月璃部署为在线服务时，需向使用者提供对应源码。

## 🛠️ 开发

欢迎 issue 与 PR。提 PR 前请确认[四条门](docs/dev/guide/testing.md)全绿——
`uv run pytest`、`npm run typecheck`、`npm test`、`npm run build`；
CI 会在 PR 上把这四条连同配置对齐校验一起再跑一遍。

架构与模块说明见 [`docs/`](docs/README.md) 的开发手册部分。

## 🌟 致谢

| 项目 | 用途 |
| :--- | :--- |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | QQ 协议端 |
| [SnowLuma](https://github.com/SnowLuma/SnowLuma) | QQ 协议端 |
| [NapCatQQ Desktop](https://github.com/NapNeko/NapCatQQ-Desktop) | 协议端的桌面控制台 |
