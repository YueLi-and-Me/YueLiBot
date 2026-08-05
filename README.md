# 月璃 · YueLiBot

一个住在 Windows 桌面上的 AI 陪伴角色。对标《妹居物语》，核心不是「LLM + 立绘」，而是**持续性**：

| | 实现 |
|---|---|
| 她记得 | 三层记忆 + 遗忘曲线，跨重启保留 |
| 她会变 | 四条连续人格轴，随交互与冷落漂移 |
| 她主动 | 日程 / 屏幕感知 / 打扰预算 |
| 她有内心 | 做梦、离线补偿、可翻阅的日记 |

技术栈：Electron + TypeScript + Canvas2D + SQLite。角色是 **AI 生成的立绘差分**，不依赖 Live2D。

---

## 上手

### 1. 依赖

```bash
npm install
```

抠图需要 Python 侧的 rembg（只在跑生图管线时用到，日常运行不需要）：

```bash
pip install "rembg[cli]" onnxruntime
```

### 2. 配置

第一次运行会打开设置窗口。保存后，运行时配置固定写入项目根目录的
`config\`，按职责拆成四份：

- `providers.toml`：API 厂商、地址、密钥、超时与安全重试；
- `models.toml`：具体模型、任务引用，以及各任务的温度和输出上限；
- `bot.toml`：Bot 名字、用户关系、完整人格提示词与会话记忆策略；
- `features.toml`：语音、视觉、向量召回和调试开关。

数据库、日志和 Electron 缓存统一放在项目根目录的 `data\`。程序启动时会拒绝
把运行时根目录解析到 C 盘；特殊启动方式可用 `YUELI_PROJECT_ROOT` 明确指定其它盘。
项目根目录若存在旧版 `config.toml`，会自动迁移为四文件结构并保留原文件。
字段说明、引用关系和手工编辑示例见[配置指南](docs/configuration.md)。

> `providers.toml` 当前仍包含明文 Key。`config\` 和 `data\` 已被 Git 忽略，仍不要把 Key、相关截图或日志提交到版本库。

先验通后端链路：

```bash
cd python && python -m pytest tests/ -q
```

### 3. 跑起来

```bash
npm run dev
```

托盘里有全部入口：显示/隐藏、跟她说话、看她的日记、搬回原位、开机自启、退出。

- **点她**开合输入栏，**拖她**移动窗口（4px 位移阈值区分点与拖）
- 角色轮廓之外全部鼠标穿透，不挡桌面操作
- `Ctrl+Shift+Q` 退出

---

## 生图管线

从参考图跑出一整套角色素材。**你只需做两件事：定稿底图、看图挑好坏**，不接触提示词工程。

详见 [`scripts/sprite/README.md`](scripts/sprite/README.md)。

```bash
npm run sprite:test                                        # 冒烟测试
npx tsx scripts/sprite/base.ts --ref <参考图> --desc "<角色描述>"
npx tsx scripts/sprite/base.ts --pick 2                    # 定稿底图
npm run sprite:gen                                         # 16 表情 + 4 闭眼 + 3 嘴型
npm run sprite:preview                                     # 逐张 diff 挑图
npm run sprite:process                                     # 抠图 + 对齐 + 烘焙 + manifest
```

> Windows 下 `npm run xxx -- --flag` 会被 npm 吞掉参数，带参数一律用 `npx tsx` 直调。

---

## 架构

业务域已经从 TypeScript 迁到 Python：Electron 只做平台层和进程治理，
对话、记忆、人格、日程、主动行为全在 Python 后端。

目录按 MaiBot 的形状组织：`src/` 是 Python 包根，一个模块一个文件夹，
入口在仓库根的 `bot.py`；Electron 那一侧整体收在 `electron/`。

```
bot.py              后端入口（等价 MaiBot 的 bot.py）

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

pytests/            Python 测试
tests/              TypeScript 测试
scripts/sprite/     生图管线
```

三条硬性边界：

- Python 后端**不得**依赖任何 Electron API
- API Key 只存在于主进程和 Python 后端。渲染层要显示模型生成的内容，给它凭证等于把 Key 放进不可信环境
- Python 只监听 `127.0.0.1`，且所有接口都要过 token 鉴权

---

## 命令

| 命令 | 作用 |
|---|---|
| `npm run dev` | 开发模式 |
| `npm run dev:renderer` | 只起渲染层（浏览器里调画面，比重启 Electron 快得多） |
| `npm run build` | 生产构建 |
| `npm run selftest` | **无头自检**，8 段断言 |
| `npm test` | Electron 端单元测试（4 个） |
| `npm run typecheck` | 类型检查 |
| `cd python && python -m pytest tests/ -q` | Python 后端测试（155 个），业务逻辑主要在这边 |
| `npm run test:integration` | 真实拉起 Python 后端的集成测试（需要可用的 Python 环境） |
| `npm run sprite:*` | 生图管线，见上 |

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

## 设计决策与踩坑

散落在各文件注释里的「为什么这么写」，汇总见 [`docs/notes.md`](docs/notes.md)。

当前架构、算法、提示词、安全、测试与发布体系的系统性复盘和分阶段改进建议，见 [`docs/design-review-roadmap.md`](docs/design-review-roadmap.md)。文档基于 2026-08-03 的工作区快照，其中路线图是待实施建议，不代表已经完成。

强烈建议动手前先扫一遍——里面几条（Electron 的 `focusable`、`setPosition` 在非整数 DPI 下的漂移、ESM preload 与 sandbox）都是**没有任何报错、只表现为功能不工作**的坑，不知道的话能查很久。

---

## 授权

- 角色立绘由 AI 生成。参考图若为他人角色设计或成品画作，生成结果仍可能构成侵权——这与「是否 AI 生成」无关，发布前需确认来源
- `星月水母` Live2D 模型仅曾作本地占位，**不进入任何分发包**（且其贴图已损坏，该路线已放弃）
