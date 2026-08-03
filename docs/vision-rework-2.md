# 屏幕感知第二轮复审 —— 后续修正

上一轮（`vision-rework.md`）七项**全部已实现且断言有效**，逐条追过代码，不是看报告：
`look.test.ts` 六条在改动前都会跑挂，`ContextFrameCache` 是真的按场景分桶，
`refusesWhenTitleUnknown` 红线还在，147 用例本地跑通。这一轮不推翻任何东西。

下面 6 项是复审新发现的。P0 是**线上会真出错的行为缺陷**，P1 是两个静默失效陷阱
（改坏了没有任何断言会红），P2/P3 是覆盖缺口与文字准确性。

---

## P0-1 · `game-event` 被 4 小时静音

### 问题

看你自己贴的真实模型输出：

```
gameplay  3730ms
TYPE: GAME_EVENT
SUBJECT: 无
DETAIL: 游戏角色死亡，提示按R键重生
```

`SUBJECT: 无` 是游戏画面的**常态**，不是意外 —— 一张 "YOU DIED" 黑屏本来就认不出游戏名。

而 `visionSightingKey` 在 subject 为空时退回用 kind 单独做 key：

```ts
return normalized ? `${kind}:${normalized}` : kind
```

于是**所有游戏事件塌缩成同一个 key `'game-event'`**，共吃一份 `SIGHTING_COOLDOWN_MS`（4 小时）。

净效果：**她嘲笑你死一次之后，接下来 4 小时里你怎么死、通关、翻车、任务失败，
她一个字都不会说。** 这不是节流，这是把功能关掉了。

### 为什么会写成这样

去重是上一份说明里**专为 `steam-playtime` 设计**的 —— 那一档的问题是时长在涨、
措辞在变，同一个游戏每次都生成新 key，所以需要按游戏名收敛。

游戏事件的性质正好相反：**它本来就该反复发生。** 节流已经由
`COOLDOWN_MS = 30 分钟` 的打扰预算负责了，不需要第二层。

`game-folder` 塌缩成单 key 是**对的**（description 本来就是常量），不要改它。
只有 `game-event` 错了。

### 改法

让 `game-event` 完全不参与 4 小时去重。两种写法任选，倾向前者：

- `visionSightingKey` 对 `game-event` 返回空串，`maybeLook` 见空串就跳过去重检查与
  `recordSighting`
- 或在 `maybeLook` 里显式 `if (seen.kind !== 'game-event')` 才做去重

无论哪种，**在代码注释里写清楚为什么这一档不去重**，否则下一个人会觉得是漏了。

### 验收断言

```
★ 同一段游戏里连续两次事件不会被四小时冷却拦下
   两次 game-event 观察（subject 均为空）→ 第二次仍可开口

同一个游戏的 steam-playtime 在四小时内只提一次
   ('Hollow Knight', 860 小时) 说过之后，('Hollow Knight', 880 小时) 被去重拦下

不同游戏的 steam-playtime 互不影响
   'Hollow Knight' 说过之后，'Hades' 仍可开口
```

第一条必须能在改动前跑挂。

---

## P1-2 · `shouldLook` 的冷却判断是踩着边界过的（静默失效陷阱）

`maybeLook` 第二次调用 `shouldLook` 时传的是：

```ts
lastLookAt: now - LOOK_COOLDOWN_MS,
```

而 `shouldLook` 内部判的是 `now - lastLookAt < LOOK_COOLDOWN_MS`。
代入后正好 `LOOK_COOLDOWN_MS < LOOK_COOLDOWN_MS`，为 false，**擦着零边距通过**。

任何人把这个 `<` 改成 `<=`（或把常量参与一次浮点/单位换算），第二段判断永远返回
`cooldown`，**整条视觉链路当场死掉，而 `look.test.ts` 全绿** —— 因为那些用例是直接调
`shouldLook`、传 `NOW - LOOK_COOLDOWN_MS - 1`，根本走不到这个边界。

这是 notes.md 里用 ⚠ 标的那一类坑：能跑、能测、但测不到真正会坏的地方。

### 改法

把冷却从策略函数里拆出来，别用哨兵值绕过自己的检查：

```ts
export function withinLookCooldown(now: number, lastLookAt: number): boolean
```

- `maybeLook` 在**截图前**调一次；命中就 `byReason.cooldown++` 并返回
- `LookInput` 删掉 `lastLookAt` 字段，`shouldLook` 不再关心冷却
- `LookReason` 里的 `'cooldown'` 保留（计数还要用），但由调用方给出

### 验收断言

`shouldLook` 现有六条用例相应调整后仍需全绿；另加：

```
withinLookCooldown 在恰好等于 90 秒时放行，89.999 秒时拦下
```

边界值单独钉死，这样以后改比较符号会有用例红。

---

## P1-3 · `npm run vision:selftest` 在功能没开时报 `ok: true`

```ts
let visionProtocolOk = !enabled
```

`VISION_ENABLED=0` 时它什么都没测，最终 `ok: true`。

`probeVision` 作为大自检的一环这样写是合理的（视觉是可选功能，没开不该让整轮自检变红）。
但 `vision:selftest` 这个命令**存在的唯一目的就是测视觉** —— 配置没开就是没跑成，
必须判失败。**报一个假的通过比不报更糟。**

### 改法

`VISION_SELFTEST` 为真且 `createVisionClient()` 返回 null 时，
输出 `ok: false` 且 `reason: 'VISION_ENABLED=0，本命令要求视觉已启用'`，进程退出码非 0。
大自检（`YUELI_SELFTEST=1`）路径下的现有宽容行为**保持不变**。

---

## P2-4 · 首帧不该自动触发一次云端调用

`frameDelta(null, x)` 返回 1，所以**每次切进 Steam 或游戏都必定判定 `frame-change`
并调用一次视觉模型**。而那一刻画面多半是刚落地的首页，返回 NONE 的概率最高 ——
这批调用几乎全是浪费，而且是一次白白上传的截图。

它还有个副作用：切进来那次把 `lastCallAt` 写成当前时刻，导致闲时瞥要等
`IDLE_GLANCE_INTERVAL_MS = 10 分钟` 才可能发生。**`IDLE_GLANCE_AFTER_MS = 3 分钟`
这个常量在正常路径上其实是死的**，只在切进来那次被预算拦下时才生效。

（顺带说明：你的完成报告里「静止 Steam 页面停留 3 分钟后会低频识别」这句不准确，
实际是 10 分钟。这是上一份说明的规则本身造成的，不是实现错误，但描述要改对。）

### 改法

首次进入某场景（`ContextFrameCache` 里没有该场景的上一帧）时，把 delta 当 **0** 处理
而不是 1 —— 只记帧、不调模型。让「进来 3 分钟后瞥一眼」成为该场景的第一次识别。

这样两件事同时解决：砍掉一批必然浪费的调用，`IDLE_GLANCE_AFTER_MS` 恢复语义。

代价：alt-tab 进游戏后的头 3 分钟内如果立刻死了，她不会立刻发现 —— 可以接受，
游戏中的死亡画面本来就会产生大 delta，进入 3 分钟后照常触发。

### 验收断言

```
★ 首次进入某场景只记帧、不调模型
   该场景无历史帧 → look: false（不是 frame-change）

★ 进入三分钟后的静止画面会被瞥一眼
   同一场景，lastCallAt 仍为 0，contextSince = now - 4min → reason: 'idle-glance'
```

---

## P2-5 · 纯函数测得很好，但「接线」一条断言都没有

上一轮我抓到的两个 bug（`lastFrame` 全局共用、缺闲时瞥）**都在 `proactive.ts` 的接线里**，
不在纯函数里。现在 `shouldLook` 和 `ContextFrameCache` 各自有测试，但没有任何断言覆盖
它们**怎么被接起来**：

- 如果 `this.lastFrames.replace()` 传的是个写死的 key，147 个用例照样全绿
- 如果 `lastCallAt.set()` 挪到预算检查之前，没有用例会红
- 如果 `updateVisionContext` 忘了在场景切换时重置 `contextSince`，也没有用例会红

### 改法

把这组状态收进 core 里一个纯类（`src/core/awareness/lookState.ts`）：

```ts
export class VisionLookState {
  /** 场景变化时重置 contextSince。 */
  enter(context: VisionContext | null, now: number): void
  /** 记录本场景的新帧，返回与上一帧的比较基准（首帧返回 null）。 */
  swapFrame<T>(context: VisionContext, frame: T): T | null
  evaluate(context: VisionContext, now: number, delta: number, windowChanged: boolean): { look: boolean; reason: LookReason }
  noteLook(now: number): void
  noteCall(context: VisionContext, now: number): void
}
```

`ProactiveGate` 只持有它 + 调 `captureWindow` / `vision.describe`，自己不再存这些字段。
`frameDelta` 由调用方算好传进来（它依赖 Buffer，不进 core）。

然后写一条**序列**断言，这是现在完全缺失的形态：

```
★ 完整时序：
   t=0     进 steam-library，首帧      → 不调模型
   t=90s   同一张静止画面              → 不调模型
   t=3min  同一张静止画面              → idle-glance，调模型
   t=4min  切到 gameplay，首帧          → 不调模型
   t=5min  切回 steam-library，静止画面  → 比较基准是 t=3min 的 Steam 帧，
                                          不是 t=4min 的游戏帧
```

最后一步是关键：它是唯一能证明分场景帧缓存**真的被正确接上**的断言。

---

## P3-6 · 文字与文档

- `look.test.ts` import 了 `IDLE_GLANCE_INTERVAL_MS` 却没用；「十分钟间隔」那条硬编码
  `NOW - 2 * 60_000`。常量改小它仍然绿。改成 `lastCallAt: NOW - IDLE_GLANCE_INTERVAL_MS + 1`。
- 「分场景帧缓存」那个 test 末尾多调了一次 `shouldLook`，与缓存无关，是前面用例的重复。删掉。
- `.env.example` 里「2.2 秒能读懂一张游戏死亡画面」是旧协议的实测值。新协议实测
  1.8~3.7 秒，改成区间。
- `docs/notes.md` 目前**一个字都没提视觉链路**。补一节，至少记下三条：
  - 为什么 `game-event` 不参与四小时去重（P0-1 的结论）
  - ⚠ `captureWindow` 匹配不上标题必须返回 null，不得退回全屏截
  - ⚠ 提示词不是安全边界，`sanitizeVisionDescription` 是最后一道；放宽它等于放弃收口

---

## 不要做的事

沿用上一份说明的全部禁止项，另加：

- 不要为了让 `game-event` 不刷屏而去加新的节流层。`COOLDOWN_MS = 30 分钟` 已经在管这件事，
  再叠一层就是这次问题的成因。
- 不要在拆 `withinLookCooldown` 时顺手改 `LOOK_COOLDOWN_MS` / `FRAME_CHANGE_THRESHOLD`
  的数值。这轮只改结构，数值不动。
- 不要为了让 P2-5 的序列断言好写而把 `captureWindow` 或 `frameDelta` 搬进 `src/core/`。
  core 不碰 Electron、不碰 Buffer 实现细节，这条约束不让步。

## 交付要求

- `npx vitest run` 全绿并报出新的用例总数
- P0-1 和 P2-4 各自的 ★ 断言，**说明它在改动前是怎么失败的**（贴失败信息）
- `npm run vision:selftest` 在 `VISION_ENABLED=1` 下重跑一次，确认 P1-2 拆分后视觉链路
  没有被改断；贴出结果
- 同时确认 `VISION_ENABLED=0` 时该命令现在报失败
- 有不同意的项写明理由，不要默默跳过
