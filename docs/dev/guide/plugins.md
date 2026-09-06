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

目录里两份必需文件：`_manifest.json` 与 `plugin.py`；`config.toml` 可选，装开关与插件自己的配置项。

两个根都会被扫，内置排在前面。同一个 id 出现在两处时先扫到的生效，后者的入口
代码根本不会执行。

### 二、`_manifest.json`

```json
{
  "manifest_version": 1,
  "id": "example.hello-yueli",
  "plugin_type": "tool",
  "name": "Hello-YueLi",
  "version": "0.1.0",
  "description": "一句话说明这个插件是干什么的"
}
```

六个字段全部必填，少一个或者 `manifest_version` 不是 1 都会被拒绝加载。
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

## 生命周期

| 钩子 | 时机 | 约束 |
| :--- | :--- | :--- |
| `tools()` | 构造之后、`on_load` **之前** | 因此工具声明不得依赖 `on_load` 建立的状态 |
| `on_load()` | 启动期 | **不应发起网络 I/O**；抛异常只导致本插件被移出注册表 |
| `on_unload()` | 停机与重载 | **必须幂等**，重复调用不得抛异常 |

`tools()` 早于 `on_load` 是刻意的：宿主必须先把工具登记进注册表，才能把注册表交给
对话代理构造，而 `on_load` 允许做 I/O、只能在事件循环里执行。

## 启用与关闭

开关在插件**自己的目录**里，`config.toml`：

```toml
[plugin]
enabled = false
```

`false` 就不加载，`true` 或者整个文件不存在就加载。

放在插件自己身上而不是主体配置里，是因为开关随插件一起装、一起删：主配置里不会
留下指向已删插件的孤儿项，用户也不必为了开一个插件去改另一个文件。

**文件不存在视为启用**，这是向后兼容——先于本机制存在的插件都没有这份配置，要求
它们补一个文件才肯加载等于升级即失能，而能力消失只在模型「不会用某个工具」时才被
察觉，极难归因。

**读不懂的配置也按启用处理**（文件损坏、`enabled` 不是布尔值），只记一条 warning。
关闭是显式意图，坏文件表达不了意图；把「读不懂」当成「要关掉」，等于让一个笔误
悄悄拿掉一项能力。

跳过发生在**读完清单之后、加载之前**，所以一个入口代码写坏了的插件也能靠这个开关
彻底绕开，不必删目录。

反馈都在启动日志：加载成功打印「工具插件已加载」，被关闭打印
「工具插件已在自身配置中关闭」。

插件自己的其余配置项也写在这份 `config.toml` 里，但由插件在 `on_load` 里自行解析——
宿主只读 `[plugin] enabled` 这一个键，不替插件定型任何字段。

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
