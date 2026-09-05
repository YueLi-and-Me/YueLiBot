# adapters —— 协议端适配器插件

每个子目录是一个独立的适配器插件，负责把一个具体的 QQ 协议端接进来。适配器
插件只做三件事：读自己那段配置、驱动协议实现收发、如实报告协议端能做什么。
协议实现本身不在这里——两个适配器都复用 `src/platforms/onebot11/` 的协议核心，
插件里不写协议代码。

当前有两个适配器：

- `adapters/yueli-napcat-adapter/`：连接 NapCat 协议端。
- `adapters/yueli-snowluma-adapter/`：连接 SnowLuma 提供的 OneBot 11 正向
  WebSocket 协议端。

NapCat 与 SnowLuma 是用户需要自行安装的外部组件，本仓库只提供对接它们的插件。

## 目录内容

- `adapters/yueli-napcat-adapter/_manifest.json`
  插件清单。声明 id `yueli.napcat-adapter`、协议 `onebot11`、配置段 `napcat`；
  能力分两栏：`static` 静态声明六项（发送、引用回复、回应、转发、群历史、
  成员信息），`probed` 声明 `poke` 一项待连接后实测。
- `adapters/yueli-napcat-adapter/plugin.py`
  `NapCatAdapterPlugin`。读配置、组装并驱动 `OneBot11Runner`、连接后实测
  `poke`。`poke` 必须探测而不能静态声明：它依赖 NapCat 私有 packet 后端对当前
  QQ 构建的匹配程度，QQ 自动更新后可能从可用变成恒定失败。探测判据是 NapCat
  私有 action `nc_get_packet_status`，任何失败形态（拒绝、超时、断连、未知
  action）一律按不可用处理，不重试、不兜底。
- `adapters/yueli-snowluma-adapter/_manifest.json`
  插件清单。id `yueli.snowluma-adapter`、配置段 `snowluma`；七项能力全部静态
  声明，`probed` 为空。
- `adapters/yueli-snowluma-adapter/plugin.py`
  `SnowlumaAdapterPlugin`。只做身份与配置装配，协议逻辑全部复用协议核心。
  SnowLuma 不依赖可选封包组件，不存在「能力随客户端构建失效」的动态来源，
  因此全部能力静态声明、无待探测项；投递失败由协议核心既有的失败回传链路落账，
  插件不自检。

## 插件与宿主的协议

**清单**：`_manifest.json` 由 `src/plugin_system/manifest.py` 解析并严格校验，
字段缺一不可、版本必须为 `manifest_version: 1`。字段含义：

| 字段 | 含义 |
| :--- | :--- |
| `id` / `name` / `version` / `description` | 插件标识与描述 |
| `plugin_type` | 固定 `adapter`，决定清单解析成适配器类型 |
| `protocol` | 适配器对接的协议标识 |
| `config_section` | 本插件读取的配置段名 |
| `capabilities.static` | 无需验证即可声明的能力 |
| `capabilities.probed` | 必须连接后实测的能力 |

**能力结算是刻意单向收窄的**：探测只能确认待探测能力可用，不能新增能力；
探测失败一律按不可用。反方向（失败当可用）会让主体把执行不了的动作放进
动作集，现场表现为动作发出去了、对方什么都没收到，且账本里查不出来。这段
契约实现在 `src/plugin_system/adapter.py` 的 `AdapterPlugin` 基类。

**生命周期**：宿主保证 `on_load` → `probe_capabilities` → `on_start` 的顺序，
停机或重连前调用 `on_stop`（必须幂等）。`on_start` 要阻塞到停机为止——宿主以
它的返回作为收发结束的信号，改成派生任务后立即返回会让适配器零错误退出。

**进程模型**：适配器跑在独立进程里，且互斥——一个账号只连一个协议端。进程
入口是 `src/platforms/onebot11/__main__.py`，按 `config/adapter.toml` 声明的
活动适配器用 `src/plugin_system/loader.py` 的 `load_adapter_plugin` 加载对应
目录，不走「扫目录全部加载」的发现路径。进程本身由
`src/core/services/host/adapter_host.py` 按 `src/core/config/adapter_selection.py`
读到的声明拉起并监护；没有声明文件或连接配置时告警跳过，主体照常运行。
适配器进程与主体之间以 HTTP 通信，消息类型就是
`src/core/platform_io/types.py` 的那套数据类。

## 依赖方向

- 依赖：`src/platforms/onebot11/`（协议核心）、`src/plugin_system/`
  （`AdapterPlugin` 基类、清单校验、能力枚举）、`src/core/runtime/backend_runtime.py`
  （定位运行时文件）、`src/core/logging/logger.py`。
- 被依赖：不被任何业务模块直接 import。宿主（插件加载器、适配器进程入口、
  适配器监护）只认基类与清单，不认具体插件。
- 约束：插件里不得复制协议代码，不得出现按协议端名字分支的判断——后端差异
  应当表达为清单里的能力差异，而不是插件代码分支。换协议端等于换一个插件
  目录加一段配置，宿主代码不动。

新增一个适配器因此有一份固定的最小清单：建一个 `adapters/` 下的插件目录，
写一份能通过 `src/plugin_system/manifest.py` 校验的 `_manifest.json`，实现一个
只做配置装配与能力声明的 `AdapterPlugin` 子类；协议收发、进程拉起、互斥选择
与能力结算全部由既有宿主承担，插件目录之外没有需要改动的地方。
