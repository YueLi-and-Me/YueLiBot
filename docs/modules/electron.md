# electron —— 桌面外壳

Electron 端只做平台层：桌宠窗口、托盘、屏幕捕获、键鼠活动采集、设置与日记
窗口。业务真源（对话、记忆、人格、日程、模型调用）全在 Python 后端；本端
**只连接**已在运行的后端，不启动也不终止它——进程入口是 Python，按
`[desktop_pet] enabled` 决定是否拉起本外壳（见
`src/core/services/host/desktop_shell.py`）。

## main —— 主进程

- `electron/main/index.ts`
  主进程入口：设置运行时目录、读配置、注册 IPC、创建桌宠窗口，经
  `BackendLink` 连接后端。前台窗口轮询、屏幕捕获与输入活动在本端执行，
  渲染器仅经 preload bridge 访问主进程。
- `electron/main/config.ts`
  设置窗口使用的运行配置读写（全仓最大的 TypeScript 文件，约 3000 行）：
  兼容旧配置、解析 TOML、创建配置目录、生成可安全展示的快照。带自己的
  `CONFIG_VERSION` 与升级记录；Python 侧有对应 schema，两个写入器的字段级
  一致由 `scripts/check/config_parity.py` 守住。
- `electron/main/runtimePaths.ts`
  统一推导项目根、配置目录、运行数据目录与用户数据目录，避免各处按当前
  工作目录重复推导出不同位置。
- `electron/main/screenIntent.ts`
  本地关键词规则识别「这条输入需要屏幕上下文」，决定是否触发一次视觉截图；
  规则刻意保守，优先降低误判率。
- `electron/main/inputActivity.ts`
  全局键鼠活动聚合：只累计按键数、点击数、位移标量与最后输入时间，绝不记录
  键值、字符、窗口内容或坐标原文——隐私红线写在这里。

## main/platform —— 系统调用适配层

- `electron/main/platform/appProtocol.ts`
  注册 `app://` 自定义协议并把请求映射到渲染资源目录，含文件 URL 安全校验；
  定义四个页面 URL（主窗口、截图、日记、设置）。
- `electron/main/platform/petWindow.ts`
  桌宠窗口：透明、置顶、可点击穿透；只管窗口属性、素材 URL 与尺寸约束，
  角色渲染在 renderer。
- `electron/main/platform/settingsWindow.ts`
  设置窗口的生命周期与首次启动阻塞；配置解析写盘在 `main/config.ts`。
- `electron/main/platform/diaryWindow.ts`
  只读日记窗口的生命周期，导航、脚本与写入能力受限于窗口配置。
- `electron/main/platform/tray.ts`
  托盘图标、菜单与用户入口：打开各窗口、切换前台采集、退出；「重启后端」
  只是转发一次 `/system/restart` 请求，由后端自己完成。
- `electron/main/platform/foreground.ts`
  前台窗口采集：进程名、标题与全屏状态，生成稳定的活动快照供轮询使用。
- `electron/main/platform/capture.ts`
  桌面或前台窗口的 JPEG 截图，按配置限制尺寸与质量；原始图像不持久化。

## main/python —— 后端连接

- `electron/main/python/backendLink.ts`
  后端连接管理：读运行时凭据、健康探测后接管已在运行的后端、周期探测存活、
  失联重连、限时连不上则报告退出。不依赖 Electron API，可在 Node 测试中单独
  验证。
- `electron/main/python/client.ts`
  后端 HTTP + WebSocket 客户端：维持 WS 连接、把后端事件转换为 IPC 事件发给
  渲染层、提供聊天 / 日记 / 观察 / 前台活动 / 截图的 HTTP 方法；事件出口抽象
  为 `EventSink`，无头测试可验证通道映射。

## preload —— 最小 IPC 桥

- `electron/preload/index.ts`
  桌宠渲染层的桥：聊天、角色、前台采集与窗口控制通道，只做参数转发。
- `electron/preload/settings.ts`
  设置窗口的桥：配置读取、保存与后端重启通道。
- `electron/preload/diary.ts`
  日记窗口的最小只读桥：日记读取与窗口关闭，不暴露业务写入接口。

## renderer —— 画面

- `electron/renderer/main.ts`
  渲染层入口：组装 character、chat、ui 子模块；不直接调主进程 API，全部经
  preload bridge。
- `electron/renderer/index.html` / `electron/renderer/env.d.ts`
  桌宠主窗口结构与 Vite 环境变量类型（角色素材目录由构建时变量选择）。
- `electron/renderer/chat.ts` / `electron/renderer/chatState.ts` /
  `electron/renderer/chatState.test.ts`
  聊天接线：输入栏、流式事件、气泡与语音播放的连接；`chatState.ts` 是回合
  结束后表情状态回落的纯函数（按主进程同步的睡眠状态，不读浏览器时钟），带
  对应单测。
- `electron/renderer/turnGate.ts`
  按轮次开始事件更新当前轮并拒绝迟到的旧轮事件，防止旧回合的流式事件漏进
  新回合的 UI。
- `electron/renderer/audio/player.ts`
  语音播放：音频来自主进程转发的事件，播放期读 `AnalyserNode` 的 RMS 值驱动
  口型，不按文本长度猜测。
- `electron/renderer/character/types.ts`
  角色渲染抽象层 `CharacterView`：上层逻辑只认这个接口，底层是 Live2D 还是
  立绘差分对它不可见；换渲染方案只改实现类。
- `electron/renderer/character/sprite/SpriteCharacterView.ts`
  立绘差分实现：先绘基础立绘，再按素材清单的矩形区域覆盖眼睛与嘴部图层。
- `electron/renderer/ui/bubble.ts`
  流式文本气泡：增量文本按固定时间片逐步写入，生成快于展示时动态加大单次
  揭示量，回合结束后按文本长度自动隐藏。
- `electron/renderer/ui/pointer.ts`
  指针交互状态机：Canvas 透明像素命中检测决定窗口穿透与可交互切换，左键
  按下按位移阈值区分点击与拖动。
- `electron/renderer/ui/layout.ts`
  按角色 Canvas 实际渲染框同步输入栏与气泡的几何位置，随窗口与素材尺寸
  变化同步。
- `electron/renderer/diary.html` / `electron/renderer/diary.ts` /
  `electron/renderer/diaryState.ts` / `electron/renderer/diaryState.test.ts`
  只读日记页：经 preload bridge 读后端日记数据；日期标签与事实拆分是纯函数
  （当前时间由主进程注入，避免渲染层时钟差异改变「今天 / 昨天」），带单测。
- `electron/renderer/settings.html` / `electron/renderer/settings.ts`
  设置页：首次启动引导与后续编辑共用的表单控制器，负责字段映射、校验与
  快照组装；持久化经 preload 到 `main/config.ts`。
- `electron/renderer/capture.html`
  截图采集页：只承载隐藏的媒体捕获 DOM，不展示界面、不保存截图。
- `electron/renderer/styles/tokens.css` / `electron/renderer/styles/settings.css` /
  `electron/renderer/styles/diary.css`
  样式分层：tokens 声明共享的颜色、间距、圆角与字体变量，settings 与 diary
  只用这些变量控制各自的表单与卡片布局。

## shared —— 两端契约

- `electron/shared/ipc.ts`
  主进程、preload 与各渲染窗口共享的 IPC 通道与数据结构：聊天事件、日程、
  观察快照、人物摘要、配置模型（`YueliConfig` 等）统一声明，避免两端各写
  一套字符串字段。
- `electron/shared/character-vocab.ts`
  角色表现词表（表情、动作、装扮）：提示词构造、模型输出解析与渲染层共用
  同一份类型与别名表；id 清单与 `scripts/sprite/config.ts` 的表情清单严格
  一致。
- `electron/shared/sprite-manifest.ts`
  立绘素材清单与局部图层矩形的共享类型：生图管线写、渲染层读，放 shared
  就是为了两边共用一份定义。

## 依赖方向

- `main/` 依赖 `shared/ipc.ts` 的类型；`preload/` 只转发通道；`renderer/`
  只经 bridge 通信，不直接调用 Electron 主进程 API——「不可信内容的运行
  环境，无凭证」的边界由 CSP 与 preload 最小化共同维持。
- `main/python/` 刻意不依赖 Electron API（路径由调用方注入），因此可在
  Node 测试环境单独验证。
- 与 Python 侧的契约有三份共享文件：`shared/ipc.ts` 对应后端推送的事件
  形态、`character-vocab.ts` 对应 `src/core/agent/vocab.py` 的动作表情词表、
  `sprite-manifest.ts` 对应 `scripts/sprite/` 的产物格式——三处任一侧改动
  都必须两侧同步。
- Python 后端不依赖任何 Electron API；反向的进程关系是
  `src/core/services/host/` 拉起本外壳、本外壳只连接后端。
