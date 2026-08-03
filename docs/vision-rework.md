# 屏幕感知第二轮改造说明

针对当前 `main` 上视觉链路的复审结论。**当前实现方向是对的，不要推倒重来** ——
`sanitizeVisionDescription` 的本地收口、`kind !== fallbackKind(context)` 的场景交叉校验、
`captureWindow` 匹配不上标题宁可返回 null，这三条是这套设计的地基，全部保留。

下面 7 项按优先级排列。P0 是「用户提的需求实际上没做出来」，P1 是正确性缺口，P2 是取舍与体验。

---

## P0-1 · 触发条件缺一条「闲时瞥一眼」，用户举的例子打不中

### 问题

用户的原话是「**偶尔**看到 steam 的某个游戏时长，会吐槽：时长这么高玩的一定很厉害吧」。

现在 `maybeLook()` 的唯一触发条件是画面变化 `delta >= FRAME_CHANGE_THRESHOLD`。走一遍实际路径：

1. 切进 Steam → `lastFrame` 为 null → `frameDelta` 返回 1 → 看一眼。但此刻多半停在商店首页，模型返回 NONE。
2. 点进某个游戏页面，盯着「已游玩 860 小时」那行字。**页面是静止的。**
3. 90 秒后再截一帧，与上一帧 `delta ≈ 0` → 跳过。再 90 秒，还是跳过。

**只要停在原地不动，她永远看不到。** 能不能撞上，取决于翻页动作是否恰好跨过某个 90 秒采样边界 —— 是抛硬币。

### 附带的第二个 bug：`lastFrame` 跨场景污染

`this.lastFrame` 是**全局一个 Buffer**，不分 context。Steam → 游戏 → Steam 来回切时，
每次都是拿这个应用的图跟**另一个应用**的图比，delta 必然接近 1 → 每次都过。

净效果是反的：**乱切窗口时她看得最勤，安静盯着一个页面时她瞎了。**

### 改法

**第一步：把「要不要看」抽成 core 里的纯函数。**

现在这段判断埋在 `src/main/proactive.ts` 里，而 `proactive.ts` 经 `capture.ts` 依赖 electron，
根本没法单测 —— 这也是为什么上面两个 bug 没被任何断言拦住。

新建 `src/core/awareness/look.ts`：

```ts
export interface LookInput {
  context: VisionContext
  now: number
  /** 上一次真的截图看过（任何场景），全局节流用 */
  lastLookAt: number
  /** 上一次真的调用视觉模型的时间，按场景分开 */
  lastCallAt: number
  /** 连续处于当前 context 的起始时间 */
  contextSince: number
  /** 与同场景上一帧的差异，0~1 */
  delta: number
  /** 本轮前台窗口是否变了 */
  windowChanged: boolean
}

export type LookReason = 'cooldown' | 'frame-change' | 'idle-glance' | 'folder-switch' | 'no-change'

export function shouldLook(input: LookInput): { look: boolean; reason: LookReason }
```

规则（常量都放这个文件里，导出以便测试引用）：

- `now - lastLookAt < LOOK_COOLDOWN_MS`（90s）→ `{ look: false, reason: 'cooldown' }`。全局硬地板，先判。
- `context === 'game-folder'`：只认 `windowChanged`，不做闲时瞥。命中 → `'folder-switch'`，否则 `'no-change'`。
- `delta >= FRAME_CHANGE_THRESHOLD`（0.18）→ `'frame-change'`。**保持现有行为。**
- **新增**：`now - contextSince >= IDLE_GLANCE_AFTER_MS`（3 分钟）
  且 `now - lastCallAt >= IDLE_GLANCE_INTERVAL_MS`（10 分钟）→ `'idle-glance'`。
  这条无视 delta —— 它存在的全部意义就是抓静止画面。
- 其余 → `'no-change'`。

**第二步：`proactive.ts` 只做状态维护和调用。**

- `lastFrame: Buffer | null` → `lastFrames: Map<VisionContext, Buffer>`
- 新增 `lastCallAt: Map<VisionContext, number>`，在**真的发出视觉请求**时写入（不是截图时）
- 新增 `visionContext: VisionContext | null` + `contextSince: number`，
  context 发生变化时重置 `contextSince = now`
- `visionStats` 加一项 `byReason: Record<LookReason, number>`，替换现在语义含糊的
  `skippedNoChange` / `skippedNoContext`（后者每 15 秒就 +1，数字被噪声淹没，没有诊断价值）

### 验收断言（`src/core/awareness/look.test.ts`）

必须包含这几条，**每条都要能在改动前跑挂**：

```
★ 静止的 Steam 页面待够 3 分钟后仍会被瞥一眼
   contextSince = now - 4min, lastCallAt = 0, delta = 0
   → { look: true, reason: 'idle-glance' }

★ 刚切进来的静止画面不会立刻触发闲时瞥
   contextSince = now - 30s, delta = 0 → look: false

闲时瞥有自己的 10 分钟间隔，不会每 90 秒重复
   contextSince = now - 30min, lastCallAt = now - 2min, delta = 0 → look: false

90 秒全局冷却压过一切，包括闲时瞥
   lastLookAt = now - 10s, contextSince = now - 1h, lastCallAt = 0 → reason: 'cooldown'

画面大变仍然照旧触发
   delta = 0.5, contextSince = now - 10s → reason: 'frame-change'

game-folder 不做闲时瞥，只认窗口切换
   context = 'game-folder', windowChanged = false, contextSince = now - 1h → look: false
   context = 'game-folder', windowChanged = true → reason: 'folder-switch'
```

再加一条 `proactive` 侧的：**同一场景连续两帧相同不会重复调用视觉模型，
但切到另一场景再切回来时，比较的是该场景自己的上一帧**（覆盖 `lastFrames` 分桶）。
如果 `ProactiveGate` 难以直接单测，把帧缓存也抽成 core 里的一个小类再测。

---

## P1-2 · 去重的 key 是整句描述，必然失效

`sightingKey = ${seen.kind}:${seen.description}`。模型两次说
「《空洞骑士》累计游玩 860 小时」和「空洞骑士 860 小时」，字符串不同 → 算两条线索，
4 小时冷却形同虚设。而且时长本身还在涨，同一个游戏每次都是新 key。

（`game-folder` 的 description 是固定串，那一档的去重是有效的 —— 只有 `steam-playtime` 坏了。）

### 改法：把去重主体从模型那里单独要过来

协议扩成三行：

```
TYPE: STEAM_PLAYTIME | GAME_FOLDER | GAME_EVENT | NONE
SUBJECT: 游戏名；没有或不确定就写"无"
DETAIL: 不超过 30 个汉字的安全概述；TYPE 为 NONE 时写"无"
```

- `SUBJECT` 走**同一个** `sanitizeVisionDescription`，脱敏失败则整条观察作废（不是降级放行）
- 去重 key 改成 `${kind}:${normalize(subject)}`，`normalize` = 去空格标点 + 转小写
- `SUBJECT` 为「无」时，退回用 `kind` 单独做 key，冷却照旧

顺带把 `max_tokens` 从 100 提到 160 —— 三行协议加上模型可能的开场白，100 有截断风险。

### 持久化

`recentSightings` 现在只在内存里，重启清零 → 重开一次 app 她就能把同一句话再说一遍。
改成经 `store.readJson/writeJson('vision_sightings', ...)` 落库，和 `proactive_state` 同一套路。

写盘的是游戏名，不是截图 —— 而且她基于这条线索说的话本来就已经进了 `messages` 表，
所以这里没有引入新的隐私面。**把这句话写进代码注释**，否则下一个人看到
「已提过的安全线索只在内存中去重，不写入磁盘」那条注释会以为是故意的。

---

## P1-3 · 删掉 `legacyYes` 兼容分支

```ts
const legacyYes = /^YES\b/i.test(lines.at(-1) ?? '')
const kind = explicitKind ?? (legacyYes ? fallbackKind(context) : null)
```

这条路径把「模型没按协议输出、但最后一行碰巧是 YES」当成有效观察，
并且把**前面所有散行拼起来当 description**（`detailLines.join(' ')`）。
这是对整套结构化协议的一个后门 —— 一个爱写开场白、末尾附和一句的模型就能绕过 TYPE 校验。

旧协议从未发布过，没有任何需要平滑升级的存量。删掉，让不合协议的输出一律作废。
`detailLines` 兜底也一起删，只认 `DETAIL:` 显式行。

---

## P1-4 · TYPE/DETAIL 协议从来没有见过真模型

这是本轮唯一**必须真跑**的项。之前验证过的 2264ms 那次用的是**旧提示词**，不作数。

扩展 `src/main/index.ts` 里的 `probeVision`，在现有死亡画面之外再加一张合成的
**Steam 游戏库截图**（SVG 画一个库页面：左侧列表、右侧大图、一行「已游玩 863 小时」），
用真 Key 打一次，断言：

- `kind === 'steam-playtime'`（不是 null，不是 game-event）
- `description` 非空且包含「小时」
- `subject` 能提取出游戏名
- 用同一张图跑 `gameplay` context，断言被交叉校验**拒绝**（kind 为 null）

另外补一条纯本地的负向断言：把一张写着「验证码 8823」「D:\\Work\\客户名单.xlsx」的合成图
喂给 `game-folder` context，断言最终 description 要么是那个固定串、要么为空，
**绝不含路径或数字**。这条不需要真 Key 也该有 —— 现在的 `vision.test.ts` 只测了
`parseVisionOutput` 的输入输出，没测「模型真的看到敏感画面时会怎样」。

跑完把实测延迟和模型原始输出贴进报告。如果模型不遵守三行协议，**先报告再改**，
不要自己悄悄加宽解析。

---

## P2-5 · `game-folder` 降级成二级开关，默认关

性价比是负的：上传的是**资源管理器**截图 —— 全项目熵最高的隐私面（路径、文档名、
客户资料、下载记录），换回来的是一个恒定字符串「发现一个看起来很神秘的小游戏」，
零信息量。风险最高，收益最低。

不删代码（逻辑本身是对的），改成显式二级开关：

- `automaticVisionContexts(env)` 接收 env，默认返回 `{steam-library, gameplay}`
- 仅当 `VISION_FOLDER_ENABLED=1` 时才加入 `game-folder`
- `.env.example` 里单独一段说明，写清楚它截的是什么、为什么默认关

现有那条 `automaticVisionContexts()` 返回三个场景的断言要相应改成两条：
默认两个场景、开关打开后三个场景。

---

## P2-6 · 全屏游戏等于关掉了用户最初那个需求

`classify()` 里 `silent: !!info.fullscreen`，而 `visionContextFor` 见 `silent` 就返回 null。
测试自己钉死了这个行为（`fullscreenGame → null`）。

而 `isFullscreen()` 是拿窗口 bounds 跟显示器比 —— **无边框窗口化**的游戏同样命中。
这是最常见的游戏设置，此时她其实是 `alwaysOnTop` 可见的，却被静音了。
结果就是用户最早提的「打游戏死掉了嘲笑我」在实际场景里基本不会发生。

真全屏时她被游戏盖住，静音是对的；无边框时不对。从 bounds 分不出这两者。

### 改法：给用户一个开关，不要替他猜

`AWARENESS_FULLSCREEN_SILENT`，默认 `1`（保持现状）。设为 `0` 时全屏不再强制静默。

实现上不要去改 `classify()` 的签名污染纯函数 —— 在 `readForeground()` 里读 env，
`AWARENESS_FULLSCREEN_SILENT=0` 时直接不上报 `fullscreen: true` 即可，
`classify` 一行不动。`.env.example` 里说明：**打开后她会在你全屏游戏时开口，
如果你在直播或录屏，声音会进去。**

---

## P2-7 · 补一个「她正在看屏幕」的可见提示

触发已经全自动了，用户比之前更不知道什么时候有一张图上了云。这是最后一道知情权。

- 视觉请求发出时经 IPC 通知渲染层，收到响应（或失败）时关闭
- 渲染层做一个**克制**的提示：角色身上一个小光点、或她短暂朝屏幕方向看一眼
  —— 不要弹窗、不要 toast、不要文字
- 提示的显示时长下限 800ms，否则 2 秒的请求会闪一下就没，等于没有

`IPC` 常量加一项，走现有那套 `contextBridge` 通道，不要新开机制。

---

## 不要做的事

- 不要放宽 `sanitizeVisionDescription` 的正则去迁就模型输出。它拦错了就是模型该改，不是它该改。
- 不要让 `captureWindow` 在标题匹配不上时退回全屏截。这条是硬红线，自检里有断言。
- 不要把截图写盘做缓存 —— 包括为了调试的临时目录。
- 不要把窗口标题原文带进任何返回值、日志或状态对象。`ForegroundProcessMonitor` 现在存指纹不存原文，保持这个做法。
- 不要为了让测试通过而放宽断言。**报一个假的通过比不报更糟。**

## 交付要求

- `npx vitest run` 全绿，并报出新的用例总数
- P1-4 的真实 API 调用必须实跑，贴出模型原始输出和延迟；跑不通就明说跑不通
- 每一项改动在报告里说清楚：改了什么、哪条断言能证明它生效、有没有跑过
- 如果某项你认为不该做，写明理由，不要默默跳过
