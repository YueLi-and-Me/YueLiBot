# core/platform_io —— 平台接入层

平台中立的会话流与人物基础设施：stream、person、身份绑定、出站消息类型与投递契约。
这里只承载**已经完成归属解析**的数据——一条消息从协议字节变成 `InboundMessage`、
归属到某个 stream 与 person 之后，在这套类型里流转。归属怎么判定、回复说什么，
都不归本包管。

## 职责边界

- 负责：引用与消息类型的定义、stream/person/identity 的唯一读写入口、
  出站驱动的生命周期契约与单播路由、合并转发消息树的中立表示与边界序列化。
- 不负责：协议解析与连接管理（在 `src/platforms/onebot11/`）、回复内容与时机
  （在 `src/core/agent/` 与 `src/core/services/chat/`）、桌面平台的逐句流式出口
  （`src/core/services/chat/outbound.py` 的解析事件链路，不经本包驱动）。

## 目录内容

- `src/core/platform_io/__init__.py`
  包说明。声明本包定位：平台无关的会话流与人物基础设施，具体平台驱动在 `drivers` 子包。
- `src/core/platform_io/types.py`
  十个不可变数据类：`PersonRef`、`IdentityRef`、`GroupMembershipRef`、`StreamRef`、
  `ConversationContext`、`InboundMessage`、`OutboundMessage`、`OutboundReaction`、
  `OutboundPoke`、`DeliveryReceipt`。只做数据承载，不含读写与发送逻辑；是全包被
  引用最多的文件。
- `src/core/platform_io/registry.py`
  `StreamRegistry`，stream / person / identity 的唯一读写入口，直接以 sqlite3 读写
  `persons`、`identities`、`streams`、`group_memberships` 等归属表，并提供按
  stream 查历史消息的查询。业务层只能持有这里发放的引用，不得自行拼接数据库 ID；
  平台适配器不得编造 stream ID。
- `src/core/platform_io/driver.py`
  `PlatformDriver` 抽象基类与 `DeliveryError`。定义非桌面平台出站驱动的生命周期
  与投递契约：驱动接收已按句切分的消息，返回可追踪的 `DeliveryReceipt`；
  连接管理与平台错误由具体实现负责。
- `src/core/platform_io/broker.py`
  `PlatformBroker`，非桌面平台的单播出站路由。desktop 走解析事件链路，不实现
  驱动接口；解析事件里只有文本与结束事件对应平台消息，表现层与观察层副作用
  不路由，因此这里只为 direct、group 等外部平台做单播驱动路由。
- `src/core/platform_io/drivers/__init__.py`
  驱动实现包说明。驱动由注册方按平台配置实例化，业务服务不直接依赖具体协议客户端。
- `src/core/platform_io/drivers/qq_ws.py`
  `QqWebSocketDriver`，QQ 出站驱动。主体进程不持有 QQ 协议连接，而是调用注入的
  `push` 回调把消息交给适配器进程，适配器返回的订阅数量用于判断消息是否确实有
  接收端，没有接收端按投递失败处理。
- `src/core/platform_io/forward.py`
  合并转发消息树：`ForwardMessagePart`、`ForwardNode`、`ForwardMessageTree` 三个
  数据类，加 `forward_tree_to_payload` / `forward_tree_from_payload` 一对边界序列化
  函数。适配器把各自协议的转发结构还原成这棵树，主体只保存解析完的不可变树；
  模型侧看不到整棵树，由只读工具按路径逐层展开。节点内文本与嵌套转发保留原始
  顺序，不做扁平化。

## 对外接口与调用方

- 类型层（`types.py`、`forward.py`）是本包对全库的公共词汇。调用方覆盖
  `src/core/services/chat/` 几乎所有文件、`src/core/agent/action_protocol.py` 与
  `src/core/agent/conversation_gate.py`、`src/core/observe/source.py`、
  `src/core/memory/scope.py`、`src/core/persona/state.py`、`src/core/tooling/spec.py`、
  `src/core/api/state.py`、`src/plugin_system/tools.py`，以及协议侧的
  `src/platforms/onebot11/`（backend、events、forward、runner）和内置插件
  `src/plugins/built_in/forward-message/plugin.py`。
- `StreamRegistry` 在三处构造：`src/core/services/chat/service.py`（聊天主服务持有）、
  `src/core/persona/state.py`（人格状态按 stream 取画像）、`src/main.py`
  （装配时挂到应用状态，供 `src/core/api/` 的路由读取）。
- `PlatformBroker` 与 `QqWebSocketDriver` 只在 `src/main.py` 装配：按当前适配器
  能力构造驱动、注册进 broker，再把 broker 交给聊天服务的出站路径。
- 合并转发树的载荷序列化用在主体与适配器进程之间的 HTTP 边界上，
  协议侧的还原实现在 `src/platforms/onebot11/forward.py`。

## 依赖方向

本包对项目内的依赖只有两处：`registry.py` 取 `src/core/logging/logger.py` 的
logger，`types.py` 引用包内的 `forward.py`。此外只依赖标准库（含 sqlite3）。

- 向下：`registry.py` 直接读写数据库表，但表结构真源在 `src/core/db/schema.py`，
  本包不 import 迁移代码，前提是启动装配时迁移已经跑完。表结构改动必须走
  `src/core/db/` 的迁移链，这里只按已迁移的结构读写。
- 向上：`services`、`agent`、`persona`、`observe`、`plugin_system`、`api` 都依赖
  本包；本包不 import 它们中的任何一个，反向依赖不允许。
- 平台协议在 `src/platforms/onebot11/`，方向是**协议层依赖接入层**（用类型、
  还原转发树），接入层不知道任何具体协议。新接一个平台只应在
  `drivers/` 下新增驱动实现并注册，而不是让本包出现按平台名字分支的代码。

整体流程与适配器插件协议的跨模块叙述见《架构总览》
（[architecture/overview.md](../architecture/overview.md)）与
[adapters](adapters.md)。
