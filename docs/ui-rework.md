# 第十轮：把两扇辅助窗做得像样

第八轮（`observability.md`）**全部通过**，核过代码：201 用例、preload 拆成
`index / diary / observability` 三个且后两个只暴露 `read()`、快照用
`INSERT OR IGNORE` 保证每天一条、敏感标题不泄漏的断言是真的。

**首次实窗验收发现 `bridgeReadOnly: false`（复用了桌宠 preload）并主动修掉——
这次自查抓到的正是「只读」这条约束里最容易漏的一环，做得好。**

功能齐了，但用户的评价是「现在的看板非常 low」。这轮只做视觉，不动任何逻辑。

---

## 先定架构：取设计语言，不取框架

用户说「参考 MaiBot 的 webui 设计」。我看过了
（`D:\Bot\MaiMai\MaiM-with-u\MaiBot\dashboard`，用户自己的项目）：
React + Vite + Tailwind v4 + shadcn/ui（`new-york` 风格，`slate` 基色，CSS 变量模式）。

**不要把这套栈搬过来。** 理由：

- YueLiBot 渲染层现在是**纯 vanilla TS**，桌宠窗口是 Canvas2D 手写渲染。
  为两扇辅助窗引入 React，会让同一个 renderer 里出现两种范式
- shadcn 的组件是 React 组件，没有 React 就用不了
- 要改 `electron-vite` 多入口构建、加一批依赖、包体变大——
  换来的是两个只读页面

**真正让 MaiBot 看起来不 low 的，不是 React，是它的 token 层**：
字号有级差、圆角有级差、阴影有级差、有 `muted-foreground` 这种次级文字色、
有暗色模式。这些**纯 CSS 变量就能全部拿到**。

而 YueLiBot 现在的情况是：`src/renderer/` 下**一个 `.css` 文件都没有**，
样式全内联在 HTML 里。「low」的根源在这里，不在框架。

### 所以这轮做的是

1. 建一个共享的 token 样式表，值**直接照抄 MaiBot**（同一个作者的项目，风格统一是加分）
2. 用这套 token 重做观察面板和日记窗口
3. 不引入任何前端框架、不装任何 UI 依赖

---

## P0-1 · 建立 token 层

新建 `src/renderer/styles/tokens.css`，两扇窗口共用。直接采用 MaiBot 的数值：

```css
:root {
  /* 颜色（HSL 分量，用 hsl(var(--x)) 消费） */
  --primary: 28.9 94.8% 45.1%;          /* 琥珀橙，MaiBot 的主色 */
  --primary-foreground: 210 40% 98%;
  --secondary: 188.5 35% 96%;
  --muted: 188.5 12% 96%;
  --muted-foreground: 188.5 20% 46.9%;  /* 次级文字，用它做标签和说明 */
  --accent: 112.7 40.2% 47.8%;
  --destructive: 0 84.2% 45%;
  --background: 0 0% 100%;
  --foreground: 222.2 84% 4.9%;
  --card: 188.5 14% 98.6%;
  --border: 188.5 20% 91.4%;

  /* 字号 */
  --text-xs: 0.75rem;   --text-sm: 0.875rem;  --text-base: 1rem;
  --text-lg: 1.125rem;  --text-xl: 1.25rem;   --text-2xl: 1.5rem;
  --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'JetBrains Mono', 'Monaco', 'Courier New', monospace;
  --leading-tight: 1.2;  --leading-normal: 1.5;  --leading-relaxed: 1.75;

  /* 圆角 */
  --radius-sm: 0.25rem;  --radius-md: 0.375rem;
  --radius-lg: 0.5rem;   --radius-xl: 0.75rem;

  /* 阴影 */
  --shadow-sm: 0 1px 2px 0 rgba(0,0,0,.05);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,.1);
  --shadow-lg: 0 10px 15px -3px rgba(0,0,0,.1);
}
```

**暗色模式必须做。** 桌宠是常驻软件，晚上开一扇纯白的窗会晃眼。
用 `@media (prefers-color-scheme: dark)` 覆盖上面这组颜色变量即可，
不需要切换按钮。暗色值照 MaiBot 的 `.dark` 块取。

**间距也要有级差**（MaiBot 靠 Tailwind 的 4px 基准）：
定义 `--space-1: 4px` 起的一组，所有 margin/padding 只准用它们。
现在窗口显得乱，一半原因是间距是随手写的。

---

## P0-2 · 重做观察面板

现在是「把 JSON 倒出来」。数据是对的，组织方式不对。

### 布局

顶部一行**状态条**，一眼能看完最关键的四件事：

```
[ 醒着 / 困了 / 睡着 ]   [ 今日打扰 2/5 ]   [ 看屏幕 3 次 · 开口 1 次 ]   [ 记忆 L3: 24 条 ]
```

下面是卡片网格，每张卡一个域：**人格 / 今日日程 / 睡眠 / 打扰预算 /
感知与视觉 / 记忆 / 语音**。卡片用 `--card` 背景 + `--border` 描边 +
`--radius-lg` + `--shadow-sm`。响应式：窄了就单列。

### 具体呈现，别再堆 JSON

- **人格**：四轴各一条细横条（当前值 + 刻度），旁边并排 `describePersona()`
  的自然语言输出。**这是开发者面板，允许显示数值**——它的价值就在于
  能对照「数值 → 自然语言」的映射对不对。
- **今日日程**：竖向时间轴，每个 slot 一行（时间 / doing / mood），
  **当前所处的那一段高亮**。`bedtimeHint` / `wakeHint` / `carryOver` / `theme` 放在头部。
- **睡眠**：`probability` 画成一条曲线更好，但**如果这轮做不完就先用数字**，
  不要为了图表拖进任何图表库——纯 SVG 手画一条折线足够。
  必带字段：`asleep` / `drowsy` / `probability` / `naturalWakeTargetAt` / `sleepDebtDelayMinutes`。
- **感知与视觉**：`byReason` 用横向条形对比（`cooldown` / `frame-change` /
  `idle-glance` / `folder-switch` / `no-change`）。**这是排查「她为什么从不看屏幕」
  的唯一线索，要一眼能看出卡在哪一道闸。**
- **记忆**：L3 表格（内容 / 权重 / `due_at` / 是否冻结），冻结行用
  `--muted-foreground` 压灰。L1/L2 只给计数。

### 交互

- 手动刷新按钮 + 「自动刷新」开关（关掉时完全不轮询）
- 顶部显示数据抓取时刻
- **仍然只读**，不加任何写入通道

---

## P0-3 · 重做日记窗口

日记和面板**不是同一种东西，不要套同一个视觉**。

面板是仪表盘：紧凑、信息密度高、等宽字体、允许数字。
日记是**她的东西**：留白多、行高松（`--leading-relaxed`）、字号大一档、
**一个数字都不出现**。

- 「今天」：`theme` 作标题，slots 排成一段一段的叙述，不要表格、不要时间刻度感
- 「我记得的」：卡片流，L3 原文直接展示。冻结的那组标题写「有点想不起来了」，
  整组用 `--muted-foreground`，**视觉上真的显得褪色**——遗忘要看得见
- 「变化」：单独一段，居中，字号大一点，像一句独白
- 梦和「你不在的时候」保留现有分组，套上新 token

⚠ **日记里绝无数值、进度条、百分比、档位词**——这条是第八轮定的红线，
视觉改造不许破它。改完要跑一遍那条断言。

---

## 不要做的事

- **不要引入 React / Vue / Tailwind / shadcn 或任何 UI 组件库。**
  拿 token，不拿框架。
- 不要为了画一条睡眠曲线引入图表库。手写 SVG `<polyline>` 就够。
- 不要动任何 `src/core/` 或 `src/main/` 的逻辑。这轮**纯视觉**，
  `observability.ts` 的聚合结构和 IPC 一律不改（要加字段先说明理由）。
- 不要给面板加任何写入能力。
- 不要把面板的视觉语言套到日记上，反之亦然。
- 不要忘记新窗口的 `sandbox: false`（已经踩过三次了）。

## 交付要求

- `npx vitest run` 全绿（这轮基本不该有新断言，但**日记无数值那条必须仍然通过**）
- **截图**：观察面板亮色 + 暗色各一张，日记亮色 + 暗色各一张，四张都要
- 窄窗口截一张，证明响应式没塌
- 说明 token 表放在哪、两扇窗怎么共用的
- 有不同意的项写明理由，不要默默跳过
