# 第十一轮：后端换成 Python

渲染层保留 Electron（Canvas2D 桌宠、逐像素命中、托盘、DPI 定位这些
`docs/notes.md` 里记着的坑不重走一遍）。`src/core/` + `src/main/` 的业务逻辑
搬到 Python，Electron 主进程退化成**窗口宿主 + 平台调用 + Python 监护进程**。

已定的四件事：

| | 决定 |
|---|---|
| 传输 | FastAPI + WebSocket，`127.0.0.1` 随机端口 + 启动时生成的一次性 token |
| 数据 | 现有 `memory.db` 原地保留，加编号迁移链 |
| 借鉴 | jieba 分词、pydantic 配置 + structlog、向量混合召回、服务化分解 |
| 切换 | 按域逐个迁移，两栈并存到最后 |

---

## 硬约束（先写在最前面，后面每一阶段都受它约束）

1. **`src/shared/ipc.ts` 的事件形状一个字都不改。** `ChatStreamEvent` /
   `VoiceEvent` / `DiaryPayload` / `ObservabilityPayload` 保持原样，Python 侧的
   pydantic 模型按它反推。主进程只做 1:1 转发，不做形状变换 ——
   这样渲染层和第十轮的视觉改造完全不用动。
2. **API Key 只存在于 Python 进程。** 现在连 Electron 主进程都不该有。
   这比现状更严：主进程不再持有凭证。
3. **`memory.db` 迁移前自动备份。** 库里是她真实积累的记忆、人格漂移、日记，
   196KB + 272KB WAL。迁移脚本先复制到
   `data/backups/memory.db.v<n>.<date>`，再改 schema。
4. **每个阶段结束时 `npx vitest run` 与 `pytest` 都要绿。** 不允许出现
   「TS 侧测试删了、Python 侧还没写」的中间状态。
5. **日记无数值那条断言不许破**（第八轮红线，第十轮已复核过一次）。

---

## 目标结构

```
python/
  pyproject.toml
  yueli/
    __main__.py            uvicorn 启动 + --selftest CLI
    config/
      schema.py            pydantic 配置模型（替代散落的 process.env 读取）
      loader.py            四文件配置组合加载，字段与跨文件引用校验
      upgrade.py            配置版本升级钩子（借鉴 MaiBot config_upgrade_hooks）
    common/
      logger.py            structlog
      clock.py             ← core/time/clock.ts
      db/
        connection.py      单写连接 + asyncio.to_thread 包裹
        schema.py          ← core/memory/schema.ts 的 DDL 原样搬过来
        migrations/
          registry.py      版本 → 迁移函数
          manager.py       启动时读 user_version，按链推进，迁移前备份
          v3_to_v4.py      预分词列换 jieba / 加 embedding 列
    memory/
      store.py             ← core/memory/store.ts（556 行，最大一块）
      tokenize.py          ← jieba 替换 Intl.Segmenter，保留 bigram 兜底
      decay.py             ← core/memory/decay.ts 遗忘曲线
      similarity.py
      recall.py            ★ 新：BM25 + 稠密向量混合召回
    persona/state.py       ← 四轴人格 + describePersona
    schedule/
      plan.py              ← core/schedule/plan.ts（548 行）
      daily.py
    awareness/
      classify.py monitor.py budget.py sleep.py look.py look_state.py
    agent/
      parser.py            ★ 流式标签解析，最吃紧的一域
      prompt.py character.py vocab.py summarize.py reflect.py
    llm/
      openai.py            httpx，SSE 解析
    tts/ vision/
    services/
      lifecycle.py         启动/关停顺序编排
      chat.py              ← main/chat.ts ChatOrchestrator
      proactive.py         ← main/proactive.ts ProactiveGate
      reflection.py        ← main/reflect.ts
      observability.py     ← main/observability.ts
    api/
      ws.py                事件下推（chat / voice / vision / sleep）
      http.py              diary / observability / 平台回调
      auth.py              一次性 token 校验
  tests/                   ← 17 个 .test.ts 移植过来

src/
  main/
    index.ts               只剩：窗口、托盘、截图、前台、Python 监护
    python/
      supervisor.ts        ★ 新：拉起/健康检查/崩溃重启/退出清理
      client.ts            ★ 新：WS + HTTP 客户端，翻译成现有 IPC 事件
    platform/              基本不动
  renderer/                完全不动
  shared/ipc.ts            不动
```

---

## 平台调用怎么分

搬到 Python：

- `foreground.ts` → `pywin32` / `psutil`（`active-win` 的原生模块加载失败问题
  一并消失）
- 定时器、状态机、全部业务判断

留在 Electron（Python 侧没有等价物，或代价不划算）：

- 窗口创建、点击穿透、拖动、DPI 定位
- 托盘
- 音频播放（渲染层的 `audio/player.ts`）
- **截图**

截图这条要说明理由：现在用 `desktopCapturer` 按标题匹配**只截那一个窗口**。
Python 侧 `mss` 只能整屏截，`pywin32` 的 `PrintWindow` 能截单窗口但对
被遮挡窗口和硬件加速表面不稳。「只截前台那一个窗口」是隐私边界的一部分，
不拿它换实现方便。所以：Electron 截图 → HTTP POST 给 Python 做帧差与视觉调用。

---

## 借鉴 MaiBot 的四项，分别做到哪一档

**jieba 分词**（阶段 1）。原注释里「不用编译型分词扩展，因为要按平台分发
`.dll`」的理由在 Python 侧不成立，jieba 是纯 Python。保留 bigram 兜底 ——
自造名字「月璃」靠 bigram 召回这条仍然有效。换分词器意味着
`facts_fts` / `cues_fts` 的预分词列内容变了，**必须整表重建索引**，
这正是 `v3_to_v4` 要做的事。

**pydantic + structlog**（阶段 0）。现在配置是散落的 `process.env.X` 读取，
填错只在运行到那一行时才报错。pydantic 在启动时一次性校验完。

**向量混合召回**（阶段 5.5，排在功能齐全之后）。现在 `recallFacts` 是纯
FTS5 关键词匹配，问「我上次说的那个项目」召不回「玩家在写一个桌宠」。
BM25 + 稠密向量混合能解决。但它引入 embedding provider 依赖和额外延迟，
所以**挂在配置开关后面，默认关**，FTS5 路径必须始终可用。
不做 A_memorix 那套 PageRank / 图关系召回 —— 那是为群聊多人场景设计的，
这里只有一个用户。

**服务化分解**（贯穿）。取 `lifecycle` 编排和服务边界，不取 MaiBot 的目录粒度。
它 `src/A_memorix/core/runtime/services/` 下有 26 个 service 文件，
这个项目的体量放 4 个就够。

---

## 阶段划分

每阶段独立可跑、独立可验。

**阶段 0 · 骨架**
pyproject、pydantic 配置、structlog、DB 连接与迁移框架、WS/HTTP 骨架 +
token 鉴权、`supervisor.ts` 拉起进程并健康检查。
验收：`npm run dev` 能起 Electron 且 Python 进程活着，WS 连上，桌宠照常显示。

**阶段 1 · memory + tokenize**
移植 `store.ts` / `tokenize.ts` / `decay.ts` / `similarity.ts`；
`v3_to_v4` 迁移重建 FTS 索引。
验收：`store.test.ts` / `tokenize.test.ts` / `pending.test.ts` 三份移植到
pytest 全绿；对着**真实 memory.db 的副本**跑一遍迁移，facts 条数与
persona 值前后一致。

**阶段 2 · persona + schedule**
验收：`plan.test.ts` / `daily.test.ts` 移植全绿。

**阶段 3 · awareness**
验收：`sleep.test.ts` / `sleepArchitecture.test.ts` / `budget.test.ts` /
`look.test.ts` / `lookState.test.ts` / `classify.test.ts` / `monitor.test.ts`
七份移植全绿。前台检测换 pywin32 后，敏感标题不外泄那条断言要重新写一遍。

**阶段 4 · agent + llm**（最吃紧）
`parser.py` 逐条对着 `parser.test.ts` 移植：半截标签、隐式 `<say>`、
漏写 `</say>` 的 flush 兜底、`<think>` 丢弃。这些行为除了测试之外没有别的
规格说明，漏一条就是线上 bug。
验收：`parser.test.ts` / `summarize.test.ts` / `reflect.test.ts` 全绿 +
`chat:test` 等价的 Python 冒烟测试真实跑通一轮。

**阶段 5 · tts + vision**
验收：`vision.test.ts` 移植全绿；语音链路真实出声。

**阶段 5.5 · 向量混合召回**（配置开关后面，默认关）

**阶段 6 · 拆掉 TS 后端**
删 `src/core/`、`src/main/chat.ts` / `proactive.ts` / `reflect.ts` /
`observability.ts` / `voice.ts`。selftest 拆两半：
`SELFTEST-WINDOW` / `HIT` / `TRAY` / `DIARY` 留在 `electron . --selftest`，
`CHAT` / `REFLECT` / `AWARE` 的逻辑部分进 Python CLI 自检。
两套都要绿。

---

## 测试迁移账

现在 201 个 vitest 用例，20 个测试文件：

- 17 个（`core/` + `main/observability`）→ pytest
- 2 个（`renderer/chatState`、`renderer/diaryState`）→ 留在 vitest，不动
- 新增：迁移链测试（拿真实库副本跑）、supervisor 崩溃重启测试、token 鉴权测试

---

## 已知风险，先记下来

1. **Python 分发。** 用户装的是桌面应用，不该要求先装 Python。
   最终要 PyInstaller 打包成 exe 由 Electron 拉起。**阶段 0 就要把
   Python 可执行路径做成可配置**，否则最后返工。
2. **jieba 首次加载词典约 1 秒。** 放启动预热，不要放第一次检索。
3. **`sqlite3` 是阻塞的。** 单写连接 + `asyncio.to_thread` 包裹非平凡查询，
   否则流式对话期间事件循环会被卡住。不上 SQLAlchemy —— 这个 schema 有
   contentless FTS5 虚表和手工维护的索引，ORM 只会挡路。
4. **两栈并存期数据库同时被两边打开。** WAL 能扛并发读写，但阶段 1–5 期间
   应当只有一侧写。每阶段明确「这一域的写入方是谁」。

---

## 不做的事

- 不动渲染层，不动第十轮的视觉成果
- 不引入前端框架（第十轮已经定过）
- 不重新设计 schema，不清库重建
- 不照搬 A_memorix 的目录粒度与图召回
- 不把截图挪到 Python（理由见上）
