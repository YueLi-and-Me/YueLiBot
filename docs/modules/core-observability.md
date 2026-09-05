# core/logging 与 core/observe —— 可观测性实现层

两个包各管一段：`src/core/logging/` 是日志的实现层（structlog 封装、控制台排版、
JSONL 落盘），`src/core/observe/` 是管线事件账本与实时广播。两者都在为
「发生了什么、什么时候、在哪一步」提供记录，但用途不同：日志面向诊断与留存，
事件账本面向回合追踪、面板呈现与重放，不要混用。

## 职责边界

- `src/core/logging/` 负责：给全库模块发 logger、控制台与 WebUI 的中文着色渲染、
  JSONL 文件落盘与滚动清理、跨终端统一的信息框排版。英文机器标识（事件名、
  字段名、枚举值）是落盘协议的一部分，只有展示支路翻译成中文。
- `src/core/observe/` 负责：管线事件的登记与上下文绑定（stream / turn / 阶段）、
  事件的 SQLite 账本与游标分页读取、阶段稳定 ID 与中文标签表、消息来源标签。
- 两包都不负责：业务事件的语义（谁在什么条件下发什么事件，由各业务模块调用
  `emit` 决定）、WebUI 的页面呈现（在 `webui/src/features/observe/`）、
  面板与账本之间的 HTTP/WS 通道（在 `src/core/api/`）。

## src/core/logging 目录内容

- `src/core/logging/__init__.py`
  包说明，并解释一个容易起疑的点：包名与标准库 `logging` 同名不会遮蔽——
  Python 3 是绝对导入，包内 `import logging` 取到的仍是标准库，全项目的
  `sys.path` 插入都指向仓库根。
- `src/core/logging/logger.py`
  structlog 封装，全库 67 个模块通过 `get_logger(__name__)` 取 logger，不直接
  调 print 或标准库 logging。含 `initialize_logging`（装配合成管线：控制台渲染器、
  WebUI 处理器、JSONL sink）、`ModuleColoredConsoleRenderer`（时间戳、中文模块名、
  中文事件与字段分层着色）、`emit_console_trace`（管线追踪的控制台出口）。
- `src/core/logging/logger_colors.py`
  模块颜色表、中文别名表与 ANSI 转义工具。登记当前实际使用的 logger 并映射为
  高对比颜色；支持 truecolor 与 256 色降级，含 Windows 控制台 ANSI 开启。
- `src/core/logging/log_display.py`
  机器标识到中文显示文本的翻译层（事件名、字段名、枚举值）。未知值原样显示，
  新事件在补翻译之前不丢诊断信息。
- `src/core/logging/log_sink.py`
  `JsonlFileSink` 与 `render_json_line`。按单文件字节数切换输出文件，按保留
  天数与最大文件数滚动清理；落盘结构稳定，只含英文机器标识。
- `src/core/logging/console_layout.py`
  `display_width` / `render_box` / `print_box`。用纯 Unicode 文本框保证启动公告
  与管线追踪在 Electron 转发、PowerShell 直跑、WebUI 日志面板三种环境下结构
  一致，不依赖终端宽度与光标重绘。

## src/core/observe 目录内容

- `src/core/observe/__init__.py`
  包说明，转出 `bind_origin` / `emit` / `enter_stage` 三个统一入口。
- `src/core/observe/events.py`
  事件登记、持久化与广播。`bind_origin` 用 contextvars 把当前 stream、turn 绑进
  上下文，`enter_stage` 标记管线阶段，`emit` 写账本并向订阅者广播；高频实时
  事件只广播不落账。另含 `EventBroadcaster` / `EventSubscriber` 与测试用的
  `reset_for_tests`。
- `src/core/observe/store.py`
  `EventStore`：SQLite 事件账本，独立连接写入，按数量与时间周期清理旧记录，
  支持游标分页（`since`）与条件检索（`search_events`）。读取方先用游标回放
  再订阅实时流。
- `src/core/observe/stages.py`
  `Stage` 与 `label_for`：管线阶段的稳定 ID 与中文标签查找表。业务层只传
  稳定 ID，不在多个调用点重复维护显示文本。
- `src/core/observe/source.py`
  `source_label`：把 `StreamRef` 转成各观测端共用的消息来源标签。

## 对外接口与调用方

- `get_logger`：全库入口，所有带日志的模块都依赖它。`initialize_logging` 只由
  `src/main.py` 在装配时调用一次（传日志配置与日志目录）。
- `emit` / `bind_origin` / `enter_stage`：业务模块发事件的标准路径，调用方遍布
  `src/core/agent/`、`src/core/memory/`、`src/core/services/chat/`、
  `src/core/llm_models/router.py` 等；`src/core/api/http.py` 与 `src/core/api/ws.py`
  是账本的读取端，把事件喂给管理面板。
- `EventStore`：读取方为 `src/core/api/`（面板查询）、
  `src/core/services/dev/replay.py`（事件重放）与 `src/main.py`（装配置）。
- `emit_console_trace`：由 `src/core/observe/events.py` 调用，把回合内关键事件
  渲染成控制台追踪行。
- `render_box` / `print_box`：调用方为 `src/core/db/schema_report.py`、
  `src/core/config/upgrade.py` 与 `src/main.py` 的启动公告。
- `source_label`：调用方为 `src/core/api/http.py`、`src/core/services/chat/`
  （service 与 group_observe）和 `src/core/services/console/trace_console.py`。

## 依赖方向

- `logging` 包内方向单一：`logger` 依赖其余四个模块，`console_layout` 依赖
  `logger_colors`；包对外的项目内依赖只有 `src/core/webui/logs.py` 的
  `webui_logs`——日志行在进程内缓存并广播给 WebUI 的 WebSocket 订阅者。
  不依赖任何其他 `src/core` 包。
- `observe` 依赖 `logging`（`emit_console_trace`）、`src/core/db/schema.py`
  （账本表的 DDL 常量 `EVENTS_DDL`）与 `src/core/runtime/clock.py`（统一时钟）；
  依赖 `src/core/platform_io/types.py` 的只有 `source.py` 一处。
- 不允许反向：业务模块调用这两个包，这两个包不 import 业务模块。事件账本的
  表结构属于 `src/core/db/`，账本自己不做迁移。
- 中文翻译只存在于展示支路：JSONL 与账本里的英文标识是检索、重放与跨版本
  兼容的根基，翻译层的改动不允许反向写回协议。
