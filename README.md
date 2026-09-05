<div align="center">

  <h1>月璃 · YueLiBot</h1>

  <p>
    <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
    <img src="https://img.shields.io/badge/Electron-TypeScript-47848F" alt="Electron">
    <img src="https://img.shields.io/badge/Platform-Windows-0078D6" alt="Platform">
    <img src="https://img.shields.io/badge/%E8%AE%B8%E5%8F%AF%E8%AF%81-AGPL--3.0-orange" alt="许可证">
  </p>

</div>

<br>

<img src="assets/character/yueli/face/normal.png" align="right" width="30%" alt="月璃">

## 🌟 什么是月璃

月璃是一个由 LLM 驱动的可交互智能体。

她不仅仅是一个机器人，也不仅仅是一个帮你完成任务的「有帮助的助手」——
她有记忆、有性格、有自己的日程和作息，是一个以真实人类的方式与你相处的数字生命。
不追求完美，不追求高效，追求的是亲切和真实：关掉程序、隔几天再打开，她仍然是同一个她。

## ✨ 特性

- 💬 **她说话像真人打字**：或长或短，一条一条地来；不会甩给你一篇分点罗列的答卷。
- 👂 **她看气氛**：群里聊得热闹时不抢话，没人搭理时也不会一直刷存在感；该开口时开口，该安静时安静。
- 🧠 **关掉再打开，她还记得**：你随口提过的事她会记住很久，也会像人一样慢慢淡忘；
  哪天再提起，她又想得起来。
- ❤️ **她越来越了解你**：你的喜恶、习惯和说过的话，她都放在心里；关系随相处变化，冷落她也有代价。
- 🎭 **她会学你们说话**：群里流行的句式和口癖她会悄悄学，等你点头认可之后才会真的用。
- 💭 **黑话她听得懂，但不卖弄**：你们圈里才懂的词她明白意思，只为听懂你们聊天，不会挂在嘴边。

## 🚀 部署

**环境要求**：Python 3.11+；桌宠外壳需要 Windows 10/11 和 Node.js；
只要 QQ 和管理面板的话，一台 Linux 服务器就够了。

```bash
uv sync         # 安装后端依赖
npm install     # 桌宠外壳与面板前端，无头部署可跳过
uv run bot.py   # 一条命令起全套
```

首次运行会生成一份带完整中文注释的配置，然后停下，告诉你还差什么——通常只差一个 API Key，
模型厂商与默认值都已预填好。填好后再次启动即可。接入 QQ 需另外配置协议端（NapCat 或 SnowLuma），
见[接入总览](docs/manual/adapters/index.md)。

- 首次安装：[从零跑起来](docs/manual/deployment/first-run.md)，五个环节，每步都写明了该看到什么、没看到先查哪里。
- 服务器部署：[无头部署](docs/manual/deployment/headless.md)，只跑 QQ 和面板。
- 配置预览：[`config.example/`](config.example/README.md) 就是首次运行会生成的那份配置，每个字段都有中文说明，可以先看看再决定。

## 📚 文档

| | |
| :--- | :--- |
| [从零跑起来](docs/manual/deployment/first-run.md) | 新手入口：五个环节，每步写清预期现象 |
| [安装与配置](docs/manual/deployment/install.md) | 依赖、配置文件的生成与填写、启动方式 |
| [功能总览](docs/manual/features/index.md) | 各项功能的默认开关、前提与依赖 |
| [QQ 与群聊接入](docs/manual/adapters/index.md) | 协议端选择、必改项、群聊白名单与回复触发 |
| [管理面板](docs/manual/webui/index.md) | 浏览器内的观察与配置入口 |
| [无头部署](docs/manual/deployment/headless.md) | 服务器上仅运行 QQ 与面板 |
| [升级与回退](docs/manual/deployment/upgrade.md) | 配置升级、数据库迁移与备份回退 |
| [常见问题](docs/manual/troubleshooting.md) | 按症状排查：启动、面板、模型调用、QQ、桌宠、数据 |

完整索引见 [`docs/`](docs/README.md)（含面向开发者的架构与模块手册）。

## 💡 设计理念

> **最像，而不是最好。**
>
> 完美的助手不需要人格——没有人在意计算器今天的心情。可一旦目标是陪伴，
> 「像人」便压倒一切：回答可以不够全面，情绪可以有起伏，被冷落后可以闹一点别扭；
> 唯独不能在你说了一句话之后，表现得像初次见面。

## 📌 注意事项与许可

> [!IMPORTANT]
> 使用生图管线制作自己的角色时，参考图来源需自行把关：参考他人的角色设计或成品画作，
> 生成结果仍可能构成侵权，与是否由 AI 生成无关。仓库自带的立绘素材由本项目自行生成。

**许可证**：[AGPL-3.0](LICENSE)。可以自由使用、修改和分发本项目，但衍生作品必须以同一许可证开源；
通过网络提供服务同样构成分发——把改过的月璃部署为在线服务时，需向使用者提供对应源码。

暂不接受外部贡献，问题与建议请开 issue。
