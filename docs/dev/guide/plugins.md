# 写一个工具插件

工具插件给月璃增加她能主动调用的能力。宿主在启动期扫目录、读清单、导入入口文件、
找出其中唯一的 `ToolPlugin` 子类，接线就完成了——**不需要改动任何主体代码**。

照着 `src/plugins/built_in/hello-yueli/` 抄一份是最快的路径，那个目录就是为此存在的。

## 必要的四件事

### 一、目录放在被扫描的根下

```
src/plugins/built_in/<插件名>/     内置插件，随程序发布
plugins/<插件名>/                  第三方插件，用户自行放置
```

目录里两份必需文件：`_manifest.json` 与 `plugin.py`。`config.toml` **不要手写**，它由宿主按插件声明的配置模型生成。

两个根都会被扫，内置排在前面。同一个 id 出现在两处时先扫到的生效，后者的入口
代码根本不会执行。

### 二、`_manifest.json`

```json
{
  "manifest_version": 2,
  "id": "example.hello-yueli",
  "plugin_type": "tool",
  "name": "Hello-YueLi",
  "version": "0.1.0",
  "description": "一句话说明这个插件的功能",
  "host_application": {
    "min_version": "0.2.0",
    "max_version": "0.2.5"
  },
  "author": { "name": "示例作者", "url": "https://example.org" },
  "license": "MIT",
  "urls": { "repository": "https://example.org/plugin" },
  "python_dependencies": ["feedparser>=6.0"]
}
```

| 字段 | 要求 |
| :--- | :--- |
| `manifest_version` | 必填，固定为整数 `2`；新增依赖声明不改变清单版本 |
| `id`、`plugin_type`、`name`、`version`、`description` | 必填；工具插件的 `plugin_type` 为 `tool` |
| `host_application` | 第三方插件必填；`min_version` 与 `max_version` 两端均须提供，详见下文版本区间 |
| `author` | 加载时可选，市场收录时必填；字符串或含 `name`、可选 `url` 的对象 |
| `license` | 加载时可选，市场收录时必填；填写已确认的许可证标识 |
| `urls` | 可选，键值均为非空字符串，例如仓库地址 |
| `python_dependencies` | 可选，PEP 508 字符串数组，缺省为空；不接受 URL 依赖 |

`id` 是全局唯一标识，两个根目录里撞名时靠它去重，启动日志也打印它。

### 三、`plugin.py` 里恰好一个 `ToolPlugin` 子类

```python
from src.plugin_system import PluginManifest, ToolPlugin, tool


class MyPlugin(ToolPlugin):
    def __init__(self, manifest: PluginManifest) -> None:
        super().__init__(manifest)
```

**必须只有一个。** 加载器用「模块内唯一实现」定位入口类，出现第二个直接报错——
多入口意味着加载顺序决定行为，那是最难查的一类问题。

构造函数只接受清单一个参数。需要可调参数时在 `on_load` 里读配置，不能加到构造签名上。

### 四、用 `@tool` 声明工具

```python
    @tool(
        name='hello_yueli',
        description='当用户明确要求演示插件工具时调用，返回一句问候。',
        parameters={
            'type': 'object',
            'properties': {'name': {'type': 'string', 'description': '称呼'}},
            'required': [],
            'additionalProperties': False,
        },
        side_effect='readonly',
    )
    async def hello_yueli(self, invocation, context):
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation='……',
        )
```

声明和实现写在同一个方法上：装饰器产出声明，方法本身就是执行体。几条硬规矩：

- **执行体必须是 `async`**，同步方法在导入期就报错。
- **`description` 的口径是「什么时候用它」**，不是复述工具名——模型只凭这一句决定要不要调。
- **失败必须给 `error_message`**，`ToolExecutionResult` 会拒绝构造没有原因的失败。
- **`side_effect` 三档**：`readonly` 默认可用，`reversible` 需显式启用，
  `irreversible` 一律被注册表拒绝登记。
- **参数来自模型输出，长度和类型都要自己兜住**。`observation` 会原样回灌进下一轮
  提示词，不设上限等于把提示词预算交给模型决定。

## 声明 Python 依赖

在清单 `python_dependencies` 中声明额外的 Python 库，例如：

```json
"python_dependencies": ["feedparser>=6.0", "some-package[extra]>=1.0; python_version >= '3.12'"]
```

每项必须符合 PEP 508，包名不能重复；大小写以及 `_`、`-` 的差异不构成不同包名。
不接受 Git、文件或 HTTP URL 形式的依赖。月璃已经提供的库（例如 `httpx`）无需重复声明。
带环境标记的项只有在标记满足时才参与检查，已安装版本满足要求时不会再次安装。

月璃默认在启动、导入第三方插件之前检查依赖，并通过 uv 安装到当前月璃的 Python
环境。安装时用当前环境全部已安装库的固定版本生成约束，禁止为了插件升级、降级或
移除现有库。同一进程中的同一个库只能有一个版本；要求冲突时插件会显示「依赖缺失」，
月璃与其他可加载插件继续运行。

`uv sync` 可能清除不属于月璃依赖清单的额外库。升级月璃后再次启动时，会按插件清单
自动补装；不要为了保留插件依赖修改月璃的 `pyproject.toml`、锁文件或 requirements。
停用插件与点开头的目录不参与自动安装。

若部署者希望先检查库再安装，可在 `config/plugin_market.toml` 设置：

```toml
[dependencies]
auto_install = false
```

关闭后仍检查版本，但不会创建安装子进程。启动日志和「工具与 MCP」页会给出完整的
手动命令，其中包含当前解释器和版本约束文件；复制该命令执行后重启。uv 不可用、下载失败
或依赖冲突时，同样会给出原因与命令。不要直接删除约束以绕过冲突：改变月璃已有库的版本
可能导致月璃启动失败。

## 发布到插件市场

插件仓库中保留 `_manifest.json`、`plugin.py` 和使用说明，运行时的 `config.toml`
不得提交。只通过 `src.plugin_system` 门面导入宿主类型，不导入 `src.core` 等内部路径。
收录时要求清单填写作者与许可证，版本支持区间两端齐全。

创建 `vX.Y.Z` 或 `X.Y.Z` 形式的 Git Tag，清单 `version` 必须与去掉 `v` 后的 Tag
完全一致。无需自行制作 Release 附件；索引流水线按固定 commit 打包并记录 SHA-256。
已收录 Tag 不应改指向其他提交；需要修正时发布新版本。

使用[登记 Issue 表单](https://github.com/YueLi-and-Me/plugin-index/issues/new?template=register.yml)
提交插件 id、仓库地址、分类与说明。首次登记先进行确定性检查和机器人审核，再由维护者
使用 `/approve` 批准一次。以后新版本通过确定性检查、机器人确认已审阅且无风险时自动收录；
依赖新增或改动、包含二进制文件、结论不明确的版本转人工处理。确定性检查失败的版本不能
靠人工批准绕过。

审核不会执行插件源码，也不能保证插件或其依赖安全。插件与月璃同进程运行，拥有读取
配置、模型密钥与数据库的权限。使用说明应明确外部服务、数据访问范围及必要配置。

## 生命周期

| 钩子 | 时机 | 约束 |
| :--- | :--- | :--- |
| `tools()` | `bind_config` 之后、`on_load` **之前** | 因此工具声明不得依赖 `on_load` 建立的状态；可以按 `self.config` 调整声明 |
| `on_load()` | 启动期 | **不应发起网络 I/O**；抛异常只导致本插件被移出注册表 |
| `on_unload()` | 停机与重载 | **必须幂等**，重复调用不得抛异常 |

`tools()` 早于 `on_load` 是刻意的：宿主必须先把工具登记进注册表，才能把注册表交给
对话代理构造，而 `on_load` 允许做 I/O、只能在事件循环里执行。

## 配置与开关

配置**声明在代码里**，`config.toml` 由宿主生成——声明只有模型这一份，手写一份初始
TOML 就是第二份声明，两份必然随演进漂移。

```python
from pydantic import Field

from src.plugin_system import PluginConfig, ToolPlugin


class MyConfig(PluginConfig):
    enabled: bool = Field(default=True, description='是否启用本插件')
    greeting: str = Field(default='你好', description='问候语，会出现在回复开头')


class MyPlugin(ToolPlugin):
    config_model = MyConfig
```

首次发现时，宿主在插件目录里生成：

```toml
# 本文件由宿主按插件声明的配置模型自动生成，可以直接编辑。
# 删掉它会在下次启动时重新生成一份默认配置。

[plugin]
# 是否启用本插件
enabled = true
# 问候语，会出现在回复开头
greeting = "你好"
```

每个字段的 `description` 成为它上方的注释——那是用户唯一能看到的字段解释，要写成
给人看的话。`PluginConfig` 基类自带 `enabled`，不声明 `config_model` 的插件也有开关，
默认启用。

读到的配置由宿主在 `on_load` **之前**注入 `self.config`，类型就是 `config_model`：

```python
    async def on_load(self) -> None:
        self._greeting = self.config.greeting
```

不走构造函数是因为构造签名是插件契约的一部分：加载器统一以 `plugin_class(manifest)`
构造，加一个位置参数会让所有既有插件失效。

### 开关的判定分两阶段

| 阶段 | 做什么 | 为什么 |
| :--- | :--- | :--- |
| 一 | 只读 `config.toml` 里的 `[plugin] enabled`，**不导入插件代码** | 已生成过配置的插件在这里就能挡下，入口写坏了也能靠开关绕开，不必删目录 |
| 二 | 文件不存在时按模型生成，然后完整校验 | 声明写在代码里，不导入就读不到；首次安装走这条路 |

代价是**首次发现必然执行一次插件的入口模块顶层代码**。没有别的办法。第二次启动之后
就走不到这条路了。

第一阶段是保守的快路径：只在看到明确的 `false` 时才敢短路，其余一律交给第二阶段。
所以 `enabled = "no"` 这类可强制转换的写法仍然会被正确判成关闭，只是多付一次导入。
两边不写同一套判据是刻意的——在快路径里抄一份 Pydantic 的布尔强转表，就是第二份真相源。

**读不懂的配置按启用处理**（文件损坏、值无法校验），只记一条 warning。关闭是显式
意图，坏文件表达不了意图；把「读不懂」当成「要关掉」，等于让一个笔误悄悄拿掉一项能力。

**写不进去也不影响运行**（只读安装、权限不足），记 warning 后以模型默认值运行。

反馈都在启动日志：生成配置打印「已生成插件配置」，加载成功打印「工具插件已加载」，
被关闭打印「工具插件已在自身配置中关闭」。

生成出的 `config.toml` 是运行时产物，已被 `.gitignore` 排除——默认值的真相源是代码里的
`config_model`，不是那份文件。

## 两个进阶挂载点

`ToolPlugin` 还有两个有默认实现的方法，多数插件用不上：

- **`observe_inbound(stream_id, message_id, inbound)`** —— 观察每条已入库的入站消息，
  供需要会话缓存的工具建索引。不得阻塞入站路径，也不得抛异常。
- **`stream_capabilities(stream_id)`** —— 按会话贡献平台能力，决定工具在该会话是否
  进入声明。用来表达「这个工具在这个会话里有没有东西可读」——没有可读内容时让工具
  声明出现，只会诱导模型调用后必败。

两者的真实用法见 `src/plugins/built_in/forward-message/plugin.py`。

## 故障隔离

发现、加载、入站观察、能力查询任何一步的单插件失败都只影响该插件自身：记 error、
从注册表移除，其余插件与主体照常运行。第三方插件目录里混着一个坏插件时整个 Bot
起不来，是不可接受的。

## 适配器插件不走这条路

`plugin_type` 还有 `adapter`，但适配器互斥、跑在独立进程，由进程入口按名字加载，
刻意不并入扫目录这条路径。把适配器清单放进插件根目录只会得到一条 warning。
适配器的写法见[适配器模块](../modules/adapters.md)。
## 主程序版本区间

第三方工具插件必须在清单 `host_application` 中同时声明 `min_version` 与
`max_version`，均为 `x.y.z` 三段非负整数，闭区间且下界不得高于上界。
内置插件与程序同时发布，可以省略区间。清单解析只检查格式，宿主在执行入口前
比较当前版本：低于下界、主次版本超过上界时跳过，原因与清单格式错误分别展示。
只有补丁号超过上界且主次版本相同时按兼容模式加载，每个进程只提示一次。

0.x 期间，插件门面、清单与上下文的不兼容变化只在次版本更新时发生，补丁版本
保持兼容。`features.toml [advanced] force_compatibility` 默认关闭；应急打开时
可放行版本不符或缺少区间的第三方插件，各插件均会告警，不影响清单格式校验。
该开关也放行 MCP 协议版本区间限制，但不放行 SDK 包版本限制。

## 调用 MCP 工具

插件只从 `src.plugin_system` 导入 `McpToolResult`、`McpContentItem`、
`McpUnavailableError` 与 `PluginMcpAccess`，通过
`await self.ctx.mcp.call_tool(服务名, 工具原名, 参数字典)` 调用。
工具必须列在宿主的 MCP 配置中、已确认只读、服务已连接且尚有今日额度。
`expose = false` 只限制她的可选项，不限制插件；两种调用共用同一计数。
不可用时抛出带中文原因的 `McpUnavailableError`；工具内部或服务器请求失败时
返回 `is_error=True`。结果 `items` 保留内容块，`text` 按顺序拼接全部文本，
`structured` 提供原有结构化内容，插件自行选择是否按 JSON 解析。

## 识别聊天图片与视频

`self.ctx.media`（`PluginMediaAccess`）把宿主识别入站图片、视频的同一套服务开放给
插件：`await describe_image(来源)` 与 `await describe_video(直链, 内容文件名)` 返回与
聊天正文同格式的替换文本（`[图片：…]`、`[视频：…]` 或视频超出时长上限的写法），
下载、解码、模型或读时长失败时返回 `None`，原因由宿主服务记日志。缓存、在途去重、
视频时长上限与模型路由都由宿主处理，插件拿不到服务对象本身。
`images_available()` / `videos_available()` 反映主配置开关与模型是否可用，调用前
应先检查；宿主没有装配对应服务时直接调用会抛 `RuntimeError`。

`describe_video` 不套用入站时的视频观看范围。要与顶层视频同口径，用
`videos_watched_on_arrival(入站消息)` 判断这条消息里的视频是否属于收到即看的范围
（范围为 `all`，或私聊、@ 她、叫她名字、回复她）；是否识别、何时识别由插件自己
决定并承担费用。

每一项的超时由宿主服务自己管：`max_wait_s(images=…, videos=…)` 报告所选类型里
按来源识别一项最长要等多久（图片是下载截止加识别截止，视频是识别截止）。截止在
服务构造时定下、配置热重载不重算，工具要等识别时可以在 `tools()` 里据此声明
`timeout_ms`，不必另写一个需要手工保持「大于宿主截止」的常量。

宿主的在途去重让同一内容的后来者直接等先发起者的任务，等待方被取消会把这个共享
任务一起取消。工具执行可能被取消（回复途中被打断会取消整个回合），所以要在工具里
等识别时，把 `describe_*` 放进插件自己创建的任务，再用 `asyncio.shield` 或
`asyncio.wait` 等它。真实用法见合并转发插件：收到转发时在后台为所有层发起识别，
读取时只等结果。

## 密钥字段与配置保存

密钥必须在配置模型中显式标注，例如
`api_key: str = Field('', json_schema_extra={'secret': True})`。
管理页读取时置空并显示是否已设置；保存时空串沿用磁盘原值。
普通字符串字段不凭名称猜测为密钥。`secret_fields(model)` 返回声明的密钥字段集合。
宿主 `save_plugin_config(directory, model, values)` 按实际模型整份校验，任一非法字段
拒绝整次保存并返回字段与原因；通过后按与配置生成相同的渲染器原子替换。
