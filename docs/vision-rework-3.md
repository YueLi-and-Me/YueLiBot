# 第三轮：游戏感知分档 + 视觉睡眠态

前两轮（`vision-rework.md` / `vision-rework-2.md`）共 13 项**全部复审通过**，
逐条追过代码：`lookState.test.ts` 的五步时序断言、`withinLookCooldown` 的边界值、
`game-event` 不参与去重、首帧 `delta = 0`，都对得上。这轮不动那些。

本轮两件事，互相独立，可以分开做。

> **提前说明，避免白做工**：`src/core/schedule/daily.ts` 那张写死的 `SCHEDULE` 表
> 下一轮会被改成**模型生成的、每天不一样的日程**，入睡时机也会从「到点」改成
> 由状态推导（精力是参考输入之一，不是唯一决定项）。
> 所以本轮 P0-2 的重点是**把接口留对**，不要围着现在这张固定表做优化。

---

## P0-1 · 游戏感知拆成 `game-event` / `game-scene` 两档

### 问题

现在 gameplay 场景的提示词是：

```
只在死亡、胜利、结算、任务失败或其他明显事件时输出 GAME_EVENT；普通游戏过程输出 NONE。
```

问题不是词写窄了，是**触发条件和提问方式是同一个形状——都围着「事件」转**。
结果对两类游戏都不成立：

- **快节奏动作游戏**：画面一直在动，`delta` 几乎永远超过 `FRAME_CHANGE_THRESHOLD`
  → 每 90 秒触发一次 → 然后问「刚才是不是发生了事件」→ 绝大多数答 NONE
  → 白花一次调用、白传一张截图
- **慢节奏游戏**（模拟经营、建造、策略、视觉小说、休闲）：`delta` 基本到不了阈值
  → 只有 3 分钟的 `idle-glance` 能触发 → 但提示词仍然在问「事件」→ 模型答 NONE
  → **她对这一整类游戏永远一个字都不会说**

慢游戏是被双重排除的。这和第一轮抓到的 Steam 时长 bug 是同一个形状：
触发和提问都是照着「你死了」设计的。

### 修法：让触发方式决定问什么

两条触发路径本来就携带了正确的语义，现在却问同一个问题。把它们分开：

| 触发 `LookReason` | 提问 | 产出 kind |
|---|---|---|
| `frame-change`（画面突变） | 刚刚发生了什么事件 | `game-event` |
| `idle-glance`（停留够久） | **他在玩什么，画面上有什么值得随口提一句的** | `game-scene`（新增） |

`game-scene` 才是覆盖全品类的那一档。它和 Steam 时长是同一类东西——
**不是事件，是状态和反差**，而那正是用户最早举的例子。

想要的效果：「农场种满了南瓜」「城市路网已经铺得很密了」「BOSS 血条只剩一点」
「地图上铺开了很多城市」。

### 一个刻意保留的性质，不要「修」它

快游戏 `frame-change` 常年占先，会一直消耗 `lastCallAt`，导致 `idle-glance` 几乎不触发
→ 快游戏只会被问事件。慢游戏反过来，只会被问场景。

**这正是想要的：品类路由是自动发生的，不需要任何游戏名单。**
在 `notes.md` 里写下这条，否则下一个人会以为快游戏「漏了 scene」而去加特例。
同理，**不要**为此调整 `FRAME_CHANGE_THRESHOLD` 或加 per-context 阈值。

### 实现要点

**1. 传递 intent。** `VisionClient.describe(jpeg, context, intent)`，
`intent: 'event' | 'scene' | 'folder'`。由 `maybeLook` 从 `decision.reason` 映射：
`frame-change → 'event'`、`idle-glance → 'scene'`、`folder-switch → 'folder'`。
不要让 `VisionClient` 自己去猜。

**2. 场景校验分两层，别混。**

- **上下文层（隐私边界，保持严格）**：`gameplay` 只接受 `GAME_EVENT | GAME_SCENE`，
  `steam-library` 只接受 `STEAM_PLAYTIME`，`game-folder` 只接受 `GAME_FOLDER`。
  这一层挡的是「模型把 Steam 画面说成文件夹内容」，是第一轮定下的红线，**不许放宽**。
- **意图层（只挡语义不通，不是安全边界）**：`intent === 'event'` 时拒绝 `GAME_SCENE`
  ——她是因为画面突变才看的，回一句「他在种田」是答非所问。
  反过来 `intent === 'scene'` **接受** `GAME_EVENT`：闲时瞥正好撞上结算画面，值得说。

**3. 提示词。** gameplay 需要两套 `CONTEXT_GUIDANCE`。scene 那套要：

- 明确覆盖各品类，不要只举动作游戏的例子
- 说清楚要找的是**状态、规模、反差、时间点**，不是「发生了什么」
- **禁止复述画面上的文字对白**（视觉小说、剧情游戏满屏是文本，逐字复述既没意思也是隐私面）
- `SUBJECT` 写游戏名，认不出写「无」

**4. 去重按 kind 分开。** 把 `SIGHTING_COOLDOWN_MS` 换成 `sightingCooldownFor(kind)`：

| kind | 冷却 | 理由 |
|---|---|---|
| `game-event` | 不去重 | 事件本来就该反复发生，交给 30 分钟打扰预算（第二轮的结论，别退回去） |
| `game-scene` | **1 小时** | 场景是持续状态，同一片农场每 10 分钟念一次会很烦；但 4 小时太长 |
| `steam-playtime` | 4 小时 | 不变 |
| `game-folder` | 4 小时 | 不变 |

`game-scene` 的 key 用 `game-scene:<归一化 subject>`；subject 为「无」时退回 `game-scene` 单键。

**5. `describeVisionObservation` 加一档**：
`game-scene → 你瞥见他正在玩的游戏画面：${description}`。
措辞要比 event 那句更随意——她是闲着没事看了一眼，不是被什么惊到。

### 验收断言

```
★ 闲时瞥产出场景观察，而不是被当成"没有事件"丢弃
   parseVisionOutput('TYPE: GAME_SCENE\nSUBJECT: 星露谷物语\nDETAIL: 农场种满了南瓜',
                     'gameplay', 'scene')
   → kind: 'game-scene', noteworthy: true

★ 画面突变时不接受场景描述（答非所问）
   同样的输入，intent 'event' → kind: null

闲时瞥可以接受事件（正好撞上结算画面）
   'TYPE: GAME_EVENT ...', 'gameplay', 'scene' → kind: 'game-event'

上下文红线不因新增档位而松动
   'TYPE: GAME_SCENE ...', 'steam-library', 任意 intent → kind: null

game-scene 一小时内不重复，game-event 不去重
   sightingCooldownFor('game-scene') === 1h
   sightingCooldownFor('game-event') → 不参与（visionSightingKey 返回 ''）

场景描述同样过本地脱敏
   'TYPE: GAME_SCENE\nSUBJECT: 无\nDETAIL: 对话框写着"我的密码是..."' → kind: null
```

前两条必须能在改动前跑挂。

### 自检扩展

`vision:selftest` 加一张**慢节奏游戏**的合成图（农场/城市俯视，无任何「事件」元素），
用真 Key 打两次：

- `intent: 'scene'` → 断言 `kind === 'game-scene'` 且 `description` 非空
- `intent: 'event'` → 断言 `kind === null`（这张图上没有事件）

贴出模型原始输出和延迟。**如果模型在 scene 意图下仍然硬凑事件，先报告再改提示词，
不要自己放宽解析。**

---

## P0-2 · 补上视觉睡眠态

### 问题

计划 E 阶段写的是「睡眠态：切 sleepy 表情，戳她触发起床气」。
**起床气那半做了**——`describeSchedule()` 在睡眠时段会注入
「你是被他叫醒的，反应要符合刚被叫醒的状态」。

**睡着那半完全没做。** `sleepy` 目前只是 `character-vocab.ts` 里一个模型可以输出的表情词；
`isAsleep()` 只被 `proactive.ts`（打扰预算）和 `reflect.ts`（做梦时机）用到，
**渲染层根本不知道她在睡觉**。半夜打开桌宠，她精神奕奕地站着。

### ⚠ 最重要的约束：只能有一个「她是不是睡着」的来源

下一轮日程会改成模型生成、入睡由状态推导。所以：

- **渲染层绝对不许出现任何时钟判断**，不许 import `isAsleep`，不许写 0–7 点
- 主进程算出睡眠状态，经 IPC 推给渲染层（照 `setVisionWatching` 那条现成通路做）
- 主进程内部也要收敛：现在 `isAsleep(new Date(now))` 散在两处，
  统一走一个访问器，**将来换成生成式日程时只改那一个函数**

这条不做到，下一轮改日程就要满项目找调用点。

### 实现要点

**1. 主进程侧的睡眠状态。** 一个 `SleepState { asleep: boolean }`，
在现有的定时 tick 里计算并在变化时推送（不要新开定时器）。

**2. 被叫醒后要保持醒着。** 加 `wokenUntil` 时间戳：用户点她或发消息后
**N 分钟内**（建议 10 分钟）不回睡眠态。否则你发完一句她立刻躺回去，
下一句又把她叫醒一次，很蠢。用户主动交互 → `wokenUntil = now + N`。

**3. ⚠ 和表情 settle 的冲突（不处理会静默出错）。**
`src/renderer/chat.ts` 里说完话 6 秒后会 settle 回 `normal`。
睡眠态下 settle 的目标必须是 `sleepy` 而不是 `normal`，否则她说完一句就"醒"了、
再也不会睡回去。**这是本项最容易漏的一处，必须有断言。**

**4. 睡眠态的视觉表现要克制。** 切 `sleepy` 立绘即可。不要加呼吸动画、Z 字气泡、
变暗滤镜之类——那是另一件事，也不在这轮范围。

### 验收断言

```
★ 睡眠态下说完话，settle 回到 sleepy 而不是 normal
   这条挂了就意味着她说一句话就再也睡不回去

★ 被叫醒后十分钟内保持醒着，之后回到睡眠态
   交互 → asleep: false；+9min 仍为 false；+11min 回到 true

渲染层不含任何时钟判断
   src/renderer/ 下 grep 不到 isAsleep / getHours / SCHEDULE

主进程只有一个睡眠判定入口
   isAsleep 的直接调用点收敛到一处
```

最后两条用文件级断言或 lint 规则实现都行，重点是**下一轮改日程时它们会红**。

---

## 不要做的事

沿用前两轮全部禁止项，另加：

- **不要动 `src/core/schedule/daily.ts` 的日程表结构。** 它下一轮整个要换掉，
  现在优化等于白做。本轮只允许在它外面加一个统一的睡眠状态访问器。
- 不要为了 `game-scene` 放宽上下文层的场景校验。那是隐私边界，
  意图层的宽松（scene 接受 event）不能蔓延到上下文层。
- 不要因为快游戏「拿不到 scene」就去加游戏名单或调阈值。品类路由是自动的，见上文。
- 不要在睡眠态里加动画、滤镜、气泡。范围就是切表情。

## 交付要求

- `npx vitest run` 全绿并报出新的用例总数
- 每条 ★ 断言**贴出改动前的失败信息**
- `VISION_ENABLED=1 npm run vision:selftest` 重跑，贴出慢节奏游戏图在
  scene / event 两种意图下的模型原始输出与延迟
- 视觉睡眠态需要**真的起一次应用**看到效果（把系统时间调到凌晨，或临时注入睡眠状态），
  说明你是怎么验的；没真看到就明说没看到
- 有不同意的项写明理由，不要默默跳过
