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
- 🎭 **她会因为你而变**：四条连续人格轴随交互漂移，冷落她也有代价。人格不是一段写死的提示词。
- 👀 **她知道什么时候不该说话**：日程、屏幕感知和打扰预算共同决定她开不开口。一个随时插话的桌宠只会被关掉。
- 🌙 **你不看她的时候她也在**：会做梦，会补偿离线期间的时间流逝，写的日记你可以翻。
- 🖼 **不依赖 Live2D**：角色是 AI 生成的立绘差分，一张参考图加一句描述就能出一整套表情。

---

## 安装 · INSTALL

**环境要求**：Windows 10/11 · Python 3.11+ · Node.js（Electron 43）

### 1. 依赖

```bash
uv sync         # Python 后端依赖
npm install     # Electron 端依赖
```

可选 extra：`--extra vector` 装向量召回用的 faiss 与 numpy，`--extra dev` 装 pytest。

Electron 用 PATH 上的 `python` 拉起后端。用 uv 建的虚拟环境要么先激活，要么用环境变量指过去：

```bash
$env:YUELI_PYTHON_EXE = ".venv\Scripts\python.exe"
```

抠图需要 rembg，只在跑生图管线时用到，日常运行不需要：

```bash
uv pip install "rembg[cli]" onnxruntime
```

### 2. 配置

第一次运行会打开设置窗口。保存后，运行时配置固定写入项目根目录的 `config\`，按职责拆成四份：

| 文件 | 内容 |
| :--- | :--- |
| `providers.toml` | API 厂商、地址、密钥、超时与安全重试 |
| `models.toml` | 具体模型、任务引用，各任务的温度和输出上限 |
| `bot.toml` | Bot 名字、用户关系、人格提示词与会话记忆策略 |
| `features.toml` | 语音、视觉、向量召回和调试开关 |

数据库、日志和 Electron 缓存统一放在项目根目录的 `data\`。程序启动时会拒绝把运行时根目录解析到 C 盘；特殊启动方式可用 `YUELI_PROJECT_ROOT` 明确指定其它盘。项目根目录若存在旧版 `config.toml`，会自动迁移为四文件结构并保留原文件。

字段说明、引用关系和手工编辑示例见 [配置指南](docs/configuration.md)。

> [!IMPORTANT]
> `providers.toml` 当前仍包含明文 Key。`config\` 和 `data\` 已被 Git 忽略，仍不要把 Key、相关截图或日志提交到版本库。

### 3. 跑起来

```bash
npm run dev
```

托盘里有全部入口：显示/隐藏、跟她说话、看她的日记、搬回原位、开机自启、退出。

- **点她**开合输入栏，**拖她**移动窗口（4px 位移阈值区分点与拖）
- 角色轮廓之外全部鼠标穿透，不挡桌面操作
- `Ctrl+Shift+Q` 退出

---

## 📚 文档 · DOC

| 文档 | 内容 |
| :--- | :--- |
| [`docs/notes.md`](docs/notes.md) | 设计决策与踩坑汇总。**动手前强烈建议扫一遍** |
| [`docs/configuration.md`](docs/configuration.md) | 配置字段说明与手工编辑示例 |
| [`docs/observability.md`](docs/observability.md) | 把后台黑盒打开：人格、记忆、日程的可观测方案 |
| [`docs/design-review-roadmap.md`](docs/design-review-roadmap.md) | 2026-08-03 快照的系统性复盘（其中路线图是建议，不代表已完成） |

`docs/` 下另有若干 `*-rework*.md`，是各轮重构的过程记录，按需查阅。

`notes.md` 里有几条是**没有任何报错、只表现为功能不工作**的坑（Electron 的 `focusable`、`setPosition` 在非整数 DPI 下的漂移、ESM preload 与 sandbox），不知道的话能查很久。

> [!NOTE]
> 推进计划、接入方案、规范约定这类**过程文档不入库**，和测试一样只存在于开发机的工作区（`docs/roadmap.md`、`docs/napcat-plan.md`、`docs/webui-plan.md`、`CLAUDE.md`、`Agent.md` 等）。README 是**唯一**随代码分发的文档，所以它必须自己把话说完整，不要指望读者能翻到别的文件。

---

## 🧱 架构 · ARCHITECTURE

业务域已经从 TypeScript 迁到 Python：Electron 只做平台层和进程治理，对话、记忆、人格、日程、主动行为全在 Python 后端。

目录形状：`src/` 是 Python 包根，一个模块一个文件夹，入口在仓库根的 `bot.py`；Electron 那一侧整体收在 `electron/`。

```
bot.py              后端入口

src/                业务真源（Python 包根）
  main.py           启动装配：读配置、建服务、拉 uvicorn
  agent/            人设、提示词、表达习惯、增量标签解析、历史修复、反思
  llm_models/       OpenAI 兼容的流式对话与多厂商路由
  memory/           三层记忆、分词、遗忘曲线
  persona/          四条人格轴 → 自然语言行为指令
  awareness/        前台归类、键鼠强度、兴趣值、意图队列、睡眠状态
  schedule/         24h 生成式日程
  services/         对话编排、主动感知、视觉、TTS、追踪
  api/              FastAPI 路由与 WebSocket
  config/           配置 schema 与多文件 TOML 加载
  common/           时钟、日志、SQLite 连接与迁移

electron/           Electron 端（TypeScript）
  main/             主进程（持有 API Key）
    platform/       ★ 系统调用适配层，迁移 Tauri 只需重写这里
    python/         Python 后端的 supervisor 与 HTTP/WS 客户端
  preload/          最小化 IPC 桥
  renderer/         画面（不可信内容的运行环境，无凭证）
    character/      CharacterView 接口 + 立绘差分实现
  shared/           两端共用的类型与 IPC 契约

scripts/sprite/     生图管线
```

三条硬性边界：

- Python 后端**不得**依赖任何 Electron API
- API Key 只存在于主进程和 Python 后端。渲染层要显示模型生成的内容，给它凭证等于把 Key 放进不可信环境
- Python 只监听 `127.0.0.1`，且所有接口都要过 token 鉴权

---

## 🎨 生图管线 · SPRITE

从参考图跑出一整套角色素材。**你只需做两件事：定稿底图、看图挑好坏**，不接触提示词工程。

```bash
npm run sprite:test                                        # 冒烟测试
npx tsx scripts/sprite/base.ts --ref <参考图> --desc "<角色描述>"
npx tsx scripts/sprite/base.ts --pick 2                    # 定稿底图
npm run sprite:gen                                         # 16 表情 + 4 闭眼 + 3 嘴型
npm run sprite:preview                                     # 逐张 diff 挑图
npm run sprite:process                                     # 抠图 + 对齐 + 烘焙 + manifest
```

详见 [`scripts/sprite/README.md`](scripts/sprite/README.md)。

> Windows 下 `npm run xxx -- --flag` 会被 npm 吞掉参数，带参数一律用 `npx tsx` 直调。

---

## 🛠 开发 · DEV

| 命令 | 作用 |
| :--- | :--- |
| `npm run dev` | 开发模式 |
| `npm run dev:renderer` | 只起渲染层（浏览器里调画面，比重启 Electron 快得多） |
| `npm run build` | 生产构建 |
| `npm run selftest` | **无头自检**，8 段断言 |
| `npm run typecheck` | 类型检查 |
| `npm test` | Electron 端单元测试 |
| `uv run pytest pytests/ -q` | Python 后端测试，业务逻辑主要在这边 |
| `npm run test:integration` | 真实拉起 Python 后端的集成测试。**手动验收项**，不在任何默认门里 |
| `npm run sprite:*` | 生图管线，见上 |

> [!NOTE]
> 测试文件（`pytests/`、`tests/`、`*.test.ts`）**不进版本库**，clone 下来不会有这些目录，上面三条测试命令也就无从执行。它们只存在于开发机的工作区。
>
> 因此**本项目不做 CI**：检出的仓库里没有测试可跑。验证只在开发机进行，标准是三条绿状态门（`pytest` / `tsc --noEmit` / `vitest run`）。
> 这是自觉取舍，代价是没有人能替你复核——所以地基级改动（数据库迁移、记忆分区这类）的额外验收项一条都不能省。

### 自检

「进程还活着」不等于「功能正常」——白窗口、preload 静默失败、素材 404 全都表现为进程正常运行。所以有一套无头自检直接问程序要证据：

```bash
npm run build && npm run selftest
```

```
SELFTEST           画布真的画出了东西
SELFTEST-WINDOW    可聚焦、可移动、置顶、拖动不撑大窗口
SELFTEST-HIT       输入栏隐藏时不挡点击、展开时点得到
SELFTEST-TRAY      显隐、搬回原位、图标加载
SELFTEST-CHAT      真实生成一轮，验记忆落库与人格推进
SELFTEST-REFLECT   做梦 → 排队 → 到点讲出来
SELFTEST-AWARE     感知可用，且原始窗口标题没有外泄
SELFTEST-DIARY     日记窗口渲染，兼验生产多入口
```

中文结果会写进 `YUELI_SELFTEST_OUT` 指定的 UTF-8 文件——Windows 控制台按本地代码页解码，中文在管道里就已经坏掉，事后无法还原。

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
> - 角色立绘由 AI 生成。参考图若为他人角色设计或成品画作，生成结果仍可能构成侵权——这与「是否 AI 生成」无关，发布前需确认来源
> - `星月水母` Live2D 模型仅曾作本地占位，**不进入任何分发包**（且其贴图已损坏，该路线已放弃）

**License**：尚未声明开源许可证，默认保留所有权利。
