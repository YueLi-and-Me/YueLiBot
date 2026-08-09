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

四份文件里的引用关系是单向的：`models.toml` 的每个模型引用 `providers.toml` 里的一个厂商名，任务再引用一串模型名作为候选。改名字的时候要顺着这条链一起改，否则启动时会直接报错退出——加载期会做完整的交叉校验，不会带着一个悬空引用继续跑。

> [!IMPORTANT]
> `providers.toml` 当前仍包含明文 Key。`config\` 和 `data\` 已被 Git 忽略，仍不要把 Key、相关截图或日志提交到版本库。

### QQ 与群聊接入

QQ 接入依赖独立安装的 NapCat。先在 NapCat WebUI 创建一条**正向 WebSocket 服务端**，端口和令牌要与 `config\napcat.toml` 的 `[napcat]` 保持一致，并把消息上报格式 `messagePostFormat` 改为 `array`；否则文本和 @ 段无法按当前协议解析。确认连接信息后再把 `enabled` 改为 `true`，并分别填写机器人登录号 `self_qq` 与用户本人的 `owner.qq`，这两个号码不能相同。

群聊只支持白名单：

```toml
# config/napcat.toml
[group]
mode = "whitelist"
list = [123456789]  # 允许接入的数字 QQ 群号
```

白名单按**群号**判断。即使用户本人正在名单外群里发言，该群也不会被豁免；消息会在适配器侧直接丢弃，不创建人物、不写记忆，也不请求模型。白名单群中的消息会进入主体保存上下文，但她不会随机插话：只有协议 @、名字或别名明确叫到她时，才会进入回复判定。

回复触发和群聊消耗在 `config\bot.toml` 配置：

```toml
[bot]
name = "月璃"
aliases = ["小璃"]

[group_chat]
# true 时，协议 @ 不受睡眠和回复窗口限制，必须回应
at_mention_must_reply = true
# 名字、别名或非必回 @ 命中后的回复概率，范围 0~1
name_mention_probability = 1.0
# 群聊人格增量统一按较低倍率折算
persona_weight = 0.05
# 普通群聊回复的时间窗口与窗口内上限
reply_window_minutes = 10
max_replies_in_window = 3
```

`bot.aliases` 是除主名字外可以叫到她的称呼；`at_mention_must_reply` 只决定协议 @ 是否必回；`name_mention_probability` 决定名字、别名和非必回 @ 命中后的回复概率。未提及她的群消息仍会落入该群自己的历史，但不会调用回复模型。

屏幕感知分成两个正交开关：`[vision] enabled` 决定是否截屏并交给视觉模型，`[perception] surfaces` 决定已经获得的前台程序、持续时间和屏幕描述允许出现在哪个出口。默认只有桌宠窗口：

```toml
# config/features.toml
[perception]
surfaces = ["desktop"]

# 如果愿意让自己的 QQ 私聊看见屏幕情境，可以显式加入 direct：
# surfaces = ["desktop", "direct"]
```

`direct` 即使开启也只对 `owner.qq` 对应的用户本人生效，`[private]` 名单里的其他联系人看不到。`group` 不是“默认关闭”的可选项，而是配置结构中不存在的出口：填入会在加载期直接报错。她在任何群里都看不到你的前台程序或屏幕内容，而且这一条无法通过配置打开。

### 数据库迁移与恢复

程序升级数据库前，会在 `data\backups\` 创建带旧版本号和日期的 SQLite 一致性备份。迁移是
单向的：特别是事实归属与人格关系拆分后的多人数据，不能可靠地自动降级。

若升级后需要恢复旧库，先完全退出程序，再用对应的 `memory.v5.<日期>.db` 备份覆盖当前
`memory.db`，随后在 SQLite 中执行 `PRAGMA user_version = 5`。不要尝试手工删除新列或运行
不存在的反向迁移；那样会留下表结构和数据内容不一致的库。

### 3. 跑起来

```bash
npm run dev
```

托盘里有全部入口：显示/隐藏、跟她说话、看她的日记、搬回原位、开机自启、退出。

- **点她**开合输入栏，**拖她**移动窗口（4px 位移阈值区分点与拖）
- 角色轮廓之外全部鼠标穿透，不挡桌面操作
- `Ctrl+Shift+Q` 退出

---

## ⚠️ 三个不报错的坑 · PITFALLS

改 Electron 那一侧之前先看这三条。它们的共同点是**没有任何报错，只表现为功能不工作**，不知道的话能查很久：

- **窗口 `focusable`**：置成 `false` 之后输入栏永远拿不到焦点，但窗口本身看起来一切正常，点击也有反应。
- **`setPosition` 在非整数 DPI 下漂移**：125% / 150% 缩放时传进去的坐标和实际落点差几像素，多次「搬回原位」会越积越偏。取整时机要对齐缩放因子。
- **ESM preload 与 sandbox**：sandbox 开着时 preload 走不了 ESM，`import` 会静默失败，渲染层拿到的是一个空的 IPC 桥——表现为「按钮点了没反应」，控制台干净。

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
  persona/          按人好感度 + 全局精力 → 自然语言行为指令
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
> 测试文件（`pytests/`、`tests/`、`*.test.ts`）和 `docs/` **都不进版本库**，检出后不会有这些目录，上面三条测试命令也就无从执行。它们只存在于开发机的工作区。
> 版本库里刻意只留代码和这份 README——所以 README 必须自己把话说完整，不指望读者能翻到别的文件。
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
