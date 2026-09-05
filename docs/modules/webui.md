# webui —— 管理面板前端

React + Vite + Tailwind v4 的单页应用，构建产物出到 `out/webui`，由 Python 后端
托管（`src/core/webui/`）。全部数据来自后端只读与管理接口，凭据只保存在
HttpOnly Cookie，前端不持有 token；严格 CSP 禁止内联脚本与外部资源，图标为
内联 SVG，字体自托管。

## 入口与布局

- `webui/vite.config.ts`
  Vite 开发与构建配置：只处理前端资源打包，不启动 Python 服务；开发模式下
  由钩子移除 index.html 的 CSP meta 以放行 react-refresh。
- `webui/index.html` / `webui/favicon.svg`
  入口文档（只声明元信息与 `#root` 挂载容器）与站点图标。
- `webui/src/main.tsx`
  挂载入口：React 渲染前先写主题属性避免首帧闪白，性能探针先于 `createRoot`
  初始化以接管 WebSocket / fetch 统计。
- `webui/src/app/App.tsx`
  根组件：路由容器、认证上下文与登录门禁。业务页面全部走 `React.lazy` 动态
  导入，登录页静态引入保证首屏直出。
- `webui/src/components/layout/AppShell.tsx`
  布局壳：桌面「浮动侧栏 + 主区」，窄屏退化为顶部导航条；路由切换播放入场
  动画避免硬切。
- `webui/src/components/layout/Sidebar.tsx`
  侧栏：品牌区、主导航、会话分区锚点、主题开关与登出；激活态用共享元素
  转场在导航项间滑动。
- `webui/src/components/layout/BrandMark.tsx`
  品牌标识组件，侧栏、窄屏顶栏与登录页共用。
- `webui/src/components/layout/PageHeader.tsx`
  页面头部：eyebrow 标识、标题、副标题与右侧工具区，业务页面统一使用。
- `webui/src/index.css`
  设计令牌与全局样式的唯一入口：语义色 → Tailwind 主题映射 → 组件语义类名；
  主题切换由 `<html data-theme>` 驱动。

## components/ui —— 基础组件

- `webui/src/components/ui/index.ts`
  桶文件，业务组件只从这里导入基础组件，不感知内部文件划分。
- `webui/src/components/ui/cn.ts`
  Tailwind 类名组合工具，注册了本项目的自定义语义令牌参与冲突合并。
- `webui/src/components/ui/button.tsx` / `webui/src/components/ui/card.tsx` /
  `webui/src/components/ui/data.tsx`
  按钮形态全集、卡片与分区标题、指标行 / 进度条 / 标签 / 翻页条与空态、
  加载、错误状态组件。
- `webui/src/components/ui/field.tsx` / `webui/src/components/ui/checkbox.tsx` /
  `webui/src/components/ui/toggle.tsx`
  表单控件（字段包装器、输入框、文本域、原生下拉）、多选勾选框与即时开关。
- `webui/src/components/ui/dialog.tsx` / `webui/src/components/ui/confirm-dialog.tsx`
  弹窗基座（Portal、焦点陷阱、ESC 与遮罩关闭）与替代 `window.confirm` 的
  确认弹窗封装。
- `webui/src/components/ui/tabs.tsx`
  分段选项卡：激活指示块用共享元素转场滑动；点击已激活项同样触发回调，
  排序控件据此用两个按钮表达四种口径。
- `webui/src/components/ui/batch-bar.tsx`
  批量操作工具条，表情包、黑话、表达方式三个词表页共用，选择态表述一致。
- `webui/src/components/ui/toast.tsx` / `webui/src/components/ui/theme-switch.tsx`
  模块级发布订阅的全局通知，以及日月昼夜动画主题开关。

## features —— 业务页面

- `webui/src/features/auth/LoginScreen.tsx`
  登录页：token 校验前的唯一可见界面，凭据只进 HttpOnly Cookie。
- `webui/src/features/observe/ObservePage.tsx`
  会话观察页（默认主页）：编排快照分区、阶段看板、事件账本、调用记录、
  提示词工作台与实时日志；本组件只管会话选中态与自动刷新，不直接发请求。
  - `webui/src/features/observe/StatusStrip.tsx`：顶部四张关键状态卡。
  - `webui/src/features/observe/StageBoard.tsx`：各会话当前停在哪一步、停了
    多久，秒级轮询。
  - `webui/src/features/observe/SnapshotSections.tsx`：七个快照业务分区的
    网格展示映射。
  - `webui/src/features/observe/TracePanel.tsx`：事件账本——轮次卡片、后台
    事件、历史检索与隔离重放对照。
  - `webui/src/features/observe/PromptRecordPanel.tsx`：分阶段调用记录，按
    任务筛选后逐份回看「这一级收到什么、答了什么」。
  - `webui/src/features/observe/PromptWorkbench.tsx`：提示词模板的元数据、
    版本历史与在线编辑（本机可编辑模板才可写）。
  - `webui/src/features/observe/LogPanel.tsx`：恒定深色终端风格的实时日志，
    行组件 memo 化配合 use-logs 的合批，日志风暴不全量重绘。
- `webui/src/features/models/ModelConfigPage.tsx`
  模型与厂商工作台：厂商列表过滤、模型表维护、按任务优先级指定候选模型；
  保存后经重启生效。
- `webui/src/features/settings/SettingsConfigPage.tsx`
  月璃设置页：按后端返回的 settings_schema.json 动态渲染配置表单，页面不
  写死具体配置项；保存整包提交，后端启动级校验后原子写回。
- `webui/src/features/persons/PersonsPage.tsx` /
  `webui/src/features/persons/PersonDetailPage.tsx` /
  `webui/src/features/persons/IdentityChips.tsx`
  人物与关系列表、单人画像详情（关系、身份会话、事实记忆）与两端共用的
  身份标签组渲染。
- `webui/src/features/emojis/EmojisPage.tsx`
  表情包库管理：浏览、封禁 / 解封 / 删除（封禁按内容哈希独立于记录存在，
  删除不解封），逐张与批量两条路径，顶部给出库容量、目录占用与孤儿文件
  总览。
- `webui/src/features/expressions/ExpressionsPage.tsx`
  表达方式人工复核：确认（永不自动淘汰）、驳回（退出候选池但保留行）、
  删除（不可逆，可被重新学到）；「在用」与「在学」分开表述，自动淘汰只碰
  本机学来的行。
- `webui/src/features/jargon/JargonPage.tsx`
  黑话词表复核：范围徽标区分全局与会话词条；确认 / 驳回都会锁定推断阶梯，
  防止证据继续增长时自动判定覆盖人工结论。
- `webui/src/features/memory/MemoryManagePage.tsx`
  记忆人工管理：事实检视与手工修正、同槽冲突裁决、操作流水与撤销；后端对
  不允许的状态转移返回 409，页面原样展示。
- `webui/src/features/memory/RetrievalTuningPage.tsx`
  检索调优中心：白名单参数表、命名 profile 的保存 / 应用 / 回滚 / 导出、
  弱监督评估报告。
- `webui/src/features/memory/MemoryGraphPage.tsx` /
  `webui/src/features/memory/AssociationGraph.tsx` /
  `webui/src/features/memory/force-layout.ts`
  记忆联想网络：页面编排、SVG 力导向图渲染，以及与 React 完全解耦的数值
  布局迭代（三种力：斥力、弹簧、向心力）。扩散预览调用运行时同一份实现。
- `webui/src/features/memory/ImportCenterPage.tsx`
  导入中心：粘贴或上传文本成批写入知识层，按来源批次整批撤销；导入是同步
  请求，并发导入被 409 拒绝。
- `webui/src/features/developer/DeveloperCommandsPage.tsx`
  开发者命令通道只读页：展示开关、鉴权边界与已注册命令，不提供执行入口。

## hooks —— 数据通道

- `webui/src/hooks/use-auth.tsx`
  认证上下文：会话检查、登录登出，全库 401 统一汇聚到这里切登录页。
- `webui/src/hooks/use-observability.ts` / `webui/src/hooks/use-traces.ts` /
  `webui/src/hooks/use-logs.ts` / `webui/src/hooks/use-prompt-records.ts` /
  `webui/src/hooks/use-prompts.ts`
  会话观察页的五个通道：快照轮询（15 秒可选）、事件账本（WebSocket 增量 +
  历史检索按 seq 去重合并、断线指数退避）、实时日志（100ms 合批、单行截断、
  500 行上限）、调用记录（摘要与详情分两次请求）、提示词工作台状态机。
- `webui/src/hooks/use-model-config.ts` / `webui/src/hooks/use-settings-config.ts` /
  `webui/src/hooks/use-restart.ts`
  两个配置页的状态机与共用的重启发令逻辑（确认弹窗由调用方持有）。
- `webui/src/hooks/use-persons.ts`
  人物列表与详情的一次性读取（404 转成 missing 状态）。
- `webui/src/hooks/use-emojis.ts` / `webui/src/hooks/use-jargon.ts` /
  `webui/src/hooks/use-expressions.ts`
  三个词表页的数据加载与人工复核写操作（逐条 + 批量）。
- `webui/src/hooks/use-selection.ts`
  三个词表页共用的批量选择集：跨页累积、筛选变化清空、全选只作用当前页。
- `webui/src/hooks/use-memory-manage.ts` / `webui/src/hooks/use-memory-graph.ts` /
  `webui/src/hooks/use-import-center.ts` / `webui/src/hooks/use-retrieval-tuning.ts`
  记忆四页的通道：事实 / 冲突 / 流水三组接口、整图与扩散预览、批次导入与
  撤销、调优参数与评估。
- `webui/src/hooks/use-developer-commands.ts` / `webui/src/hooks/use-theme.ts`
  开发者命令目录只读数据与主题切换 hook。

## lib —— 纯工具

- `webui/src/lib/api.ts`
  HTTP 封装：同源凭据、401 抛 `UnauthorizedError`、404 抛 `NotFoundError`，
  其余错误带后端 detail。
- `webui/src/lib/format.ts`
  弱类型 JSON 的收窄函数与中文展示格式化（时长、时间戳、发送者标签等），
  被 features 全部业务组件依赖。
- `webui/src/lib/ansi.ts`
  日志行 ANSI 转义解析，支持基础色、粗体、24 位 RGB 与 256 色。
- `webui/src/lib/list-ops.ts`
  三个词表页共用的排序口径与批量分块写入口径。
- `webui/src/lib/theme.ts`
  主题的解析、应用与持久化；`initTheme` 在 React 挂载前同步执行。
- `webui/src/lib/perf-probe.ts`
  前端性能探针：`?perf=1` 启用，采集 FPS、长任务、堆内存、DOM 节点、
  WebSocket 与 fetch 速率，支持卡死后从 localStorage 读回存档。

## 依赖方向

- 页面只经 hooks 取数，hooks 只经 `lib/api.ts` 发请求；基础组件只从
  `components/ui/index.ts` 桶文件导入——三层单向，页面间互不引用。
- 与后端的接口清单分布在各 hook 的 docstring 里（`/api/...` 路径），后端
  实现在 `src/core/api/`；`use-selection` / `list-ops` / `batch-bar` 的成对
  出现是刻意的：选择行为、排序口径、工具条表述三处一致，三个词表页才像
  同一个产品。
- 构建产物由 `src/core/webui/` 托管，本目录不包含也不修改后端配置。
