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

## 🌟 什么是月璃 · INTRO

月璃是一个运行在 Windows 桌面上的 AI 陪伴角色，也可以接入 QQ 私聊与群聊。

她的目标不是「大模型加立绘」的组合，而是**持续性**：有自己的日程与作息，
记得与你之间发生过的事，关系随相处慢慢变化。关掉程序、隔几天再打开，她仍然是同一个她——
这正是一般同类组合最欠缺、而本项目投入最多的部分。

## ✨ 特性 · FEATURES

**🧠 跨重启的记忆与人格**

- 三层记忆各司其职：工作记忆保存当前会话，情节摘要留住这段日子发生过的事，关于人的事实结构化归档；
  事实带遗忘曲线，随时间淡化，被提及时重新强化。
- 好感度按人隔离，随每一次互动漂移；精力与心情是全局状态，持续影响她的表达，也影响她想不想主动开口。
- 人格不是一段写死的提示词——冷落她，也是有代价的。

**🎭 会学习的表达**

- 表达方式：从群友的句式与口癖里学习「什么情境可以怎么说话」，学到的条目经你复核后才会被使用。
- 黑话：自动学习群内用语及其含义，仅供她自己理解，不会刻意卖弄；内置名字守卫，人名不会被学成黑话。

## 🚀 部署 · QUICK START

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
接入 QQ 需另外配置协议端（NapCat 或 SnowLuma），见[接入总览](docs/manual/adapters/index.md)。

- 首次安装：[从零跑起来](docs/manual/deployment/first-run.md)，共五个环节，每个环节写明了预期现象与排查入口。
- 服务器部署：[无头部署](docs/manual/deployment/headless.md)，仅运行 QQ 与面板，含 systemd 单元。
- 配置预览：首次运行生成的配置与 [`config.example/`](config.example/README.md) 一致，每个字段均带中文说明，可在克隆前查阅。

## 📚 文档 · DOCS

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

## 💡 设计理念 · IDEA

> **最像，而不是最好。**
>
> 完美的助手不需要人格——没有人在意计算器今天的心情。可一旦目标是陪伴，
> 「像人」便压倒一切：回答可以不够全面，情绪可以有起伏，被冷落后可以闹一点别扭；
> 唯独不能在你说了一句话之后，表现得像初次见面。

## 📌 注意事项与许可 · LICENSE

> [!IMPORTANT]
> 使用生图管线制作自有角色时，参考图来源需自行把关：参考他人的角色设计或成品画作时，
> 生成结果仍可能构成侵权，与是否由 AI 生成无关。仓库自带的立绘素材由本项目自行生成。

**License**：[AGPL-3.0](LICENSE)。可以自由使用、修改和分发本项目，但衍生作品必须以同一许可证开源；
通过网络提供服务同样构成分发——将修改后的月璃部署为在线服务时，需向使用者提供对应源码。

暂不接受外部贡献，问题与建议请开 issue。
