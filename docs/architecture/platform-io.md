# 平台抽象与适配器

本文说明平台接入层的形状：核心如何用同一套出口契约同时服务桌面的流式出口和 QQ 的整句出口，一条外部消息如何解析到具体的人，QQ 适配器为什么是独立进程，以及适配器插件协议长什么样、按什么顺序加载。进程治理的总体格局见[架构总览](overview.md)；门控、缓冲与回合内部的判定在[一次回合的完整链路](conversation-turn.md)，本文不重复，只展开出口与归属这两个边界。

## 1. 一套出口契约，两种投递形态

对话产出在核心内部只有一种：解析器吐出的事件流（`SayEvent` / `TextEvent` / `SayEndEvent` / `MoodEvent` 等）。两种出口的差别不在产出端，而在投递端消费这同一份产出的方式：

1. 桌面出口是流式的。`src/core/services/chat/outbound.py:131` 的 `_emit_parse_event` 把解析事件逐个转成 `chat.event` 载荷推给桌面客户端，且只在 `context.stream.platform == 'desktop'` 时生效（同文件 148-149 行）。桌面那边拿到的是情绪、语气词、逐段文本这类表现层增量，Electron 主进程以 `client=desktop` 订阅（`electron/main/python/client.ts:283`），转发给渲染层。
2. QQ 出口是整句的。`_dispatch_outbound`（`src/core/services/chat/outbound.py:195`）把已经按 `<say>` 边界切好的分句一次性打包成 `OutboundMessage`，交给 `PlatformBroker`（`src/core/platform_io/broker.py:21`）按 stream 单播到平台驱动。

能共用一套的原因是切分前置：分句在投递之前完成，平台出站、助手历史落库、控制台渲染读的是同一份 segments（`src/core/services/chat/outbound.py:1-8` 的模块说明）。如果让每个出口自己切，三个消费者看到的气泡就会各不一样。

`PlatformBroker`（`src/core/platform_io/broker.py:21`）是一张 stream id 到驱动的单播路由表：一个 stream 只能注册一个驱动，重复注册或向未注册的 stream 投递都显式报错（broker.py:32-45, 56-68）。desktop 不实现 `PlatformDriver`、不进这张表（broker.py:1-6 的模块说明）：桌面回复走解析事件链路，事件里只有 `TextEvent` / `SayEndEvent` 对应平台消息，`SayEvent` 与 `MoodEvent` 分别是表现层和观察层的副作用，硬塞进同一套驱动契约会让平台侧收到一堆无从投递的事件。broker 的注册是惰性的：主体启动时只建好一个 `QqWebSocketDriver` 实例（`src/main.py:651-652`），某个 QQ stream 第一次有入站消息时才把它绑进路由表（`src/core/api/http.py:553-554` 调 `src/main.py:654` 的 `_register_platform_stream`）。驱动不带任何 per-stream 状态、只是一个推送回调的封装，所以全部 QQ stream 共享同一实例，注册只是登记「这个 stream 的出口存在」。

`OutboundMessage`（`src/core/platform_io/types.py:110`）的字段安排体现了「角色行为归主体、协议执行归适配器」的分工：

| 字段 | 谁决定 | 为什么在这里 |
| :--- | :--- | :--- |
| `segments` | 主体按 `<say>` 边界切 | 三种消费者共用同一份气泡 |
| `batch_delays_ms` | 主体按人格配置算好 | 打字速度是角色行为参数，散到各适配器会各算一套（types.py:118-122） |
| `quote_external_message_id` | 投递层判定 | 群里目标之后已有人插话才挂引用；判定细节见[一次回合的完整链路](conversation-turn.md) |
| `turn_id` | 发起回合 | 投递失败回报时把失败归到发起它的那一轮（types.py:128-130） |

引用与否刻意不进模型的动作头：模型只选定目标消息，「要不要引用」是这个选择在平台上的呈现方式，由 `_quote_target`（`src/core/services/chat/outbound.py:168`）按「指认歧义是否存在」一条判据代码强制；给模型多一个可写的协议字段只会多一个写错的面。

出站动作按平台上的真实动作拆成三个类型而不是一个大结构：`OutboundMessage`（发消息）、`OutboundReaction`（给已有消息贴表情，types.py:143）、`OutboundPoke`（戳某个人，types.py:171）。合并成一个类型会让「segments 为空但 reaction 非空」这类半合法状态只能靠约定约束。对应地，`PlatformDriver` 基类（`src/core/platform_io/driver.py:24`）里 `react` / `poke` 的默认实现是直接抛 `DeliveryError`（driver.py:58-91）：核心只在能力判定为真时才会调用它们，真走到了说明能力判定与驱动实现已经不一致，必须当场报错，不能静默吞掉一个 Bot 已经决定要做的动作。

## 2. 归属解析：一条消息如何落到「哪个人在哪个会话」

`StreamRegistry`（`src/core/platform_io/registry.py:34`）是 persons / identities / streams / group_memberships 四张表的唯一读写入口，业务层只持有它返回的不可变引用（`PersonRef` / `StreamRef` / `IdentityRef`，types.py），不允许自行拼接数据库 ID。两个锚点由迁移固定下来：owner person 恒为 id=1，desktop stream 恒为 id=1（registry.py:27-31；种子写入在 `src/core/db/migrations/v5_to_v6.py:218` 的 `_write_owner_records`）。

桌面入站是这套体系的退化情形：`/chat/send`（`src/core/api/http.py:499`）直接用 `desktop_context()`（registry.py:263）拼出固定 stream 加固定 owner 的上下文，不走解析——桌面只有一个会话、一个对话方，没有归属问题。真正需要解析的是平台方向。

QQ 方向的入站归属走 `resolve_inbound`（registry.py:273），顺序固定：

1. `get_or_create_stream`（registry.py:665）按 (platform, kind, external_id) 读或建会话；
2. `find_person_by_identity`（registry.py:430）按 (platform, external_id) 查这个人是否已经见过；
3. 没见过就 `create_person('contact', ...)`——业务路径只能创建 contact，owner 不由消息创建（registry.py:404-428）；
4. `link_identity`（registry.py:563）绑定或刷新该身份的显示名；一个身份已绑到别人时直接报错，不做静默转移；
5. 群聊再 `set_group_card`（registry.py:496）更新群名片。空串的语义是「清除名片」，这条语义反过来约束了适配器：见下面适配器侧的两步查询。

owner 的 QQ 号不是猜出来的。适配器每次连上主体后调 `link_owner_identity`（`src/platforms/onebot11/backend.py:365`），主体侧 `/platform/identity/link`（`src/core/api/http.py:1025`）走 `set_sole_identity`（registry.py:616）：换号时同平台旧绑定必须解除，否则旧号会继续解析到 owner，造成归属错误和记忆越权。

另有一条只读入口 `resolve_existing_context`（registry.py:324）给状态通知用：这类事件通常只有外部标识、没有昵称名片，既不为瞬时通知创建 person/stream，也不拿空值覆盖已有身份；会话或身份尚未建立就返回 `None`。它的实际使用者是「对方正在输入」通知：适配器提交到 `/platform/typing`（`src/platforms/onebot11/backend.py:206`，主体侧 `src/core/api/http.py:874`），主体只据此决定是否追问一次，不为这条瞬时事实留下任何归属记录。

昵称与群名片在归属体系里是两个字段，不许合并。适配器侧为此保留两个查询：`_query_display_name`（`src/platforms/onebot11/runner.py:396`）做「名片优先、否则昵称」的合并，只用于渲染正文里的 `@`；`_query_member_identity`（runner.py:428）把昵称和名片分开返回，用于入站提交。合并后再拆会把「没有名片」写成「名片等于昵称」，或把有名片的人抹空（runner.py:433-438）。同一个约束解释了戳一戳为什么要先查成员信息再提交（runner.py:899-915）：通知本身不带昵称名片，拿空值提交等于清除发起者已存的名片，查不到就整条放弃。

适配器进程内还有一层显示名缓存 `_display_names`（runner.py:110-111），键是 (群号, QQ 号)——群名片按群独立，不能跨群复用；上限 512 条按插入序淘汰（runner.py:60-65）。

读取侧同样是两级：`display_name`（registry.py:451）只读平台账号昵称，`stream_display_name`（registry.py:473）在群聊里先取非空群名片、再回退到账号昵称。分开的原因是消费场景不同：写入画像与关系的是「这个人是谁」，展示在会话里的是「他在这个群叫什么」，合并成一个读法会让其中一侧读到错的名字。

## 3. 适配器为什么是独立进程

主体不持有任何 QQ 协议连接。主体侧的 QQ 出站驱动 `QqWebSocketDriver`（`src/core/platform_io/drivers/qq_ws.py:20`）只是一个回调封装：构造时注入主体的 WS 推送函数（装配在 `src/main.py:651-652`），`send` 把载荷交给这个回调就结束。推送按 stream 分区：固定桌面 stream 进 `desktop` 分区，其余全进 `platform` 分区（`src/core/api/ws.py:104`），每条推送是 `{'stream_id', 'channel', 'payload'}` 的 JSON 信封（ws.py:113-117），QQ 的三个通道 `qq.send` / `qq.react` / `qq.poke` 各占一个 channel，不共用——协议端那边是三个不同的 action，共用一个通道会让适配器靠字段有无猜意图（qq_ws.py:103-107）。分区里没有订阅者时推送返回 0，驱动据此抛 `DeliveryError`（qq_ws.py:91-95）——不能在没有接收端时回成功回执，否则上层会以为消息已送达。

适配器进程由主体拉起并监护：`build_adapter_process`（`src/core/services/host/adapter_host.py:43`）按 `config/adapter.toml` 的声明组装 `python -m src.platforms.onebot11 --adapter <插件目录>` 命令行，解释器用 `sys.executable` 而不是 PATH 里的 python（adapter_host.py:75-82），避免虚拟环境分叉。启动时机绑在监听建立之后（`src/main.py:413-416` 的说明）：适配器一上来就要连后端；停止顺序相反，先收走适配器，保证不会再有新入站消息进来。适配器与桌面外壳都是可选子进程，缺声明只告警不阻断（`_build_children`，`src/main.py:352`）——无头部署形态见[无头部署](../guide/headless.md)。

独立进程买来的东西：

1. 故障隔离。协议端断连、鉴权失败、账号不匹配都在适配器进程内消化：`run()`（runner.py:124）按 `_is_retryable`（runner.py:1449）区分可重试与不可重试错误，前者指数退避重连，后者直接终止。这些都不波及主体的对话循环。
2. 生命周期解耦。协议端能力随连接变化（见下节能力探测），每次重连都是一次全新的「这是哪个协议端」。
3. 部署解耦。没有图形环境的服务器只需要主体加适配器两个 Python 进程。

两条 WS 通道共用一个鉴权端点（`src/core/api/ws.py:151`）：桌面端把 token 放在 `yueli-<token>` 子协议里（`electron/main/python/client.ts:283`），适配器走标准 Authorization 头（backend.py:129-136）；`client` 查询参数不在枚举内或鉴权失败都以 1008 关闭，accept 之前就拒绝（ws.py:164-176）。token 本身不落在任何配置里：主体启动时生成运行时凭据文件，适配器经 `--runtime-path` 读它（`src/platforms/onebot11/__main__.py:52-57`），所以重启主体即轮换凭据，旧适配器连接自然失效。

代价是跨进程协议的每一跳都要严格校验。入站方向 `PlatformInboundBody` 用 `extra='forbid'`（`src/core/api/http.py:75-78`），合并转发树在进业务前逐棵严格还原、拒绝未知字段（`src/core/platform_io/forward.py:97` 与 `_reject_unknown_keys`，forward.py:154）；出站方向适配器对 `qq.send` / `qq.react` / `qq.poke` 报文逐字段校验（`src/platforms/onebot11/backend.py:481` 起的 `_parse_outbound` 等）。`turnId` 存在但类型不对也当场报错而不是按 0 放过（backend.py:461-478）：静默放过会让投递失败回传丢掉回合归属。

投递的诚实性由两个方向合起来保证。主体的 `DeliveryReceipt` 只证明报文到了适配器；协议端真正执行失败时，适配器通过 `/platform/delivery/failed` 回报（backend.py:268，runner.py:1112），主体只落账不补偿重发（`src/core/api/http.py:966` 的说明：失败原因基本在平台侧，自动重发会把一次可见失败变成反复骚扰）。回报本身失败只记日志——一次局部失败不应升级成整条出站通道停摆（runner.py:1125-1131）。

一条 QQ 消息的完整路径：

```
协议端(NapCat/SnowLuma)
  │  正向 WS 事件
  ▼
适配器进程 src/platforms/onebot11
  classify_event → parse_inbound_event(events.py:307/392)
  → 补 @显示名 / 引用摘要 / 图片来源 / 合并转发树(runner.py:999-1022)
  │  HTTP POST /platform/inbound
  ▼
主体 src/core/api/http.py:517
  StreamRegistry.resolve_inbound 归属解析
  → 注册 stream→driver(main.py:654) → 门控 → ChatService
  │  WS qq.send / qq.react / qq.poke(经 PlatformBroker → QqWebSocketDriver)
  ▼
适配器 _consume_backend_outbound(runner.py:1043)
  → 分批停顿 → send_private_msg / send_group_msg / set_msg_emoji_like / group_poke
  │  失败 → HTTP POST /platform/delivery/failed
  ▼
协议端
```

两个细节值得注意：

1. 出站的分批是「每条文字一批、每张表情包一批」（`src/platforms/onebot11/segments.py:392` 的 `outbound_message_batches`），QQ 为每批生成独立气泡，这正是分句在聊天窗口里表现为多条消息的原因；引用段只插在第一批，逐条都挂会把窗口刷满（segments.py:404-405）。停顿发生在出站消费循环内、按批阻塞等待而不是并发发（runner.py:1084-1088），为了保住同一 stream 内的气泡顺序。
2. 入站的附加解析（`@` 显示名、引用摘要、图片本地路径、合并转发树）全部在提交主体前补齐，且每条都是「失败退回占位形态，不阻断正文入站」（runner.py:1-9）。引用还原尤其不能省：`reply` 段只有消息 ID，没有正文，模型对着「[引用消息]？」只能硬猜，而引用又是触发必回的强信号（runner.py:514-524）。
3. 失败隔离的粒度是「条」。入站提交超时或被主体拒收只丢这一条，不拆连接（runner.py:1025-1041）；适配器与协议端之间的 action 用一个 reader 协程统一接收、按 echo 字段分发给挂起的调用（`src/platforms/onebot11/transport.py:1-7`），连接断开时所有挂起调用收到明确异常而不是永远挂住。

## 4. 插件协议的形状与加载顺序

适配器插件的磁盘形状只有两个固定文件：`_manifest.json`（自述）与 `plugin.py`（实现），约定在 `src/plugin_system/loader.py:31-32`。目录名允许含连字符、不是合法 Python 包名，所以加载器按文件路径加载而不是按包导入（loader.py:42 的 `_load_module`）；模块名用插件标识派生，因为两个适配器的入口文件同名，按文件名注册会互相覆盖（loader.py:117-118）。入口模块里必须恰好定义一个 `AdapterPlugin` 子类：零个是忘了写，多个是入口有歧义，都当场报错（`_single_plugin_class`，loader.py:61）。

清单（`src/plugin_system/manifest.py`）的要点：

| 字段 | 约束 | 出处 |
| :--- | :--- | :--- |
| `manifest_version` | 必须逐字等于 1，不接受「大于等于」；格式变更即语义变更，静默接受旧版会让插件按旧语义运行而无人察觉 | manifest.py:23, 148-152 |
| `plugin_type` | `adapter` 或 `tool`；决定基类与发现路径 | manifest.py:33 |
| `protocol` | 协议族标识，当前只有 `onebot11`；同族适配器共用 `src/platforms/onebot11` 的协议实现 | manifest.py:59 |
| `config_section` | 该适配器读哪个配置段；两个适配器不得相同 | manifest.py:60-61 |
| `capabilities.static` / `capabilities.probed` | 两集合必须不相交——同一能力既静态又待探测，探测等于白做 | manifest.py:127-133 |

能力取值是封闭枚举（`src/plugin_system/capabilities.py:29`），拼错的名字在加载期就抛出——放过去的话现场只会表现为「那个动作永远不生效」，分不清是拼写问题还是协议端不支持。清单全部校验都在加载期一次做完是同一个取向（manifest.py:1-8 的模块说明）：清单是插件对主体的全部自述，残缺或矛盾拖到运行期，症状与协议端故障无法区分。

生命周期固定为 `on_load` → `probe_capabilities` → `on_start`，停机调 `on_stop`（`src/plugin_system/adapter.py:29-36`）。三条硬约定：

1. `on_load` 不做网络 I/O（`src/plugin_system/plugin.py:37-44`）：加载失败应当纯粹是配置问题，混入网络故障会让「配置写错」与「对端没起来」无法区分。
2. `on_start` 必须阻塞到停机（adapter.py:56-71）：宿主用它的返回判断「收发已结束」，改成起个任务就返回会让宿主立刻进入收尾路径，进程零错误退出、日志里什么都没有。
3. `on_stop` 必须幂等：停机与重连两条路径都会调它。

能力结算的方向是刻意单向收窄的（adapter.py:80-121 的 `resolve_capabilities`）：最终能力是「静态能力」并上「探测确认可用的待探测能力」，探测抛异常时待探测部分整体按不可用处理；探测返回清单没声明的能力则直接抛装配错误。反向（失败当可用）会让主体把执行不了的动作放进动作集，现场是「她做了动作但对方什么都没收到」，账本里也查不出来。

加载与启动的完整顺序：

```
主体 bot.py
  1. 读 config/adapter.toml 选定插件目录(adapter_selection.py:37)
  2. 监听建立后拉起子进程: python -m src.platforms.onebot11 --adapter <目录>
适配器进程(宿主 src/platforms/onebot11/__main__.py)
  3. load_adapter_plugin: 校验清单 → 按路径加载 plugin.py → 取唯一子类 → 构造
  4. on_load: 读插件目录下 config.toml + 主体运行时凭据，构造运行器；无网络 I/O
  5. on_start → OneBot11Runner.run(runner.py:124):
     连协议端 → 校验 self_qq 与配置一致(runner.py:144)
     → 连主体 → link_owner_identity
     → 能力探测并上报(每次重连都重做一次，runner.py:149-170)
     → 回填白名单群最近历史(只观察不回复，runner.py:252)
     → 进入入站/出站双消费者循环
  6. 停机: 宿主先取消 on_start 任务再调 on_stop(__main__.py:101-111)；
     顺序反过来，运行器会把断连当成可重试故障，在停机过程中重连
```

能力上报到主体后是整体替换而不是并入：`set_platform_capabilities`（`src/core/services/chat/capabilities.py:106`）每次连接都换掉旧结论。动作可用性的最终判据是「配置开关 与 协议端能力」的与（`_react_available` / `_poke_available`，capabilities.py:67-104）；从未收到过上报时一律按不可用（`_backend_supports`，capabilities.py:124-137）。戳一戳的配置默认就是关（`src/core/config/schema.py:122`），还有一层理由写在 `_poke_available` 里：表情回应无推送，戳一戳会给对方推送提醒，扰动量级不同，须由使用者主动打开。

现有两个适配器演示了协议的两种用法：

1. `adapters/yueli-napcat-adapter/`：`poke` 声明为待探测，因为它依赖 NapCat 的私有 packet 后端与当前 QQ 构建的匹配程度，QQ 自动更新后可能从可用变成恒定失败；探测动作是 `nc_get_packet_status`，任何失败形态一律按不可用处理（`adapters/yueli-napcat-adapter/plugin.py:93-116`）。
2. `adapters/yueli-snowluma-adapter/`：SnowLuma 不依赖封包组件，全部能力静态声明，`probe_capabilities` 返回空集、即使被调也不发网络请求（`adapters/yueli-snowluma-adapter/plugin.py:81-88`）。

两者共享 `src/platforms/onebot11` 的全部协议代码，插件里不允许出现按协议端名字分支的判断——后端差异必须表达为清单里的能力差异（`src/plugin_system/adapter.py:1-12` 的模块说明）。适配器之间互斥（一个账号只连一个协议端），所以发现路径是「按名字选一个」而不是「扫目录全部加载」；扫目录那条路属于工具插件（`src/plugin_system/registry.py:1-18`），与适配器共用清单格式但互不合并。

## 5. 边界速查

| 边界 | 形状 | 位置 |
| :--- | :--- | :--- |
| 核心 ↔ 平台驱动 | `PlatformDriver` 抽象 + `PlatformBroker` 单播路由；desktop 不实现驱动 | src/core/platform_io/driver.py, broker.py |
| 主体 → 适配器 | WS `client=platform` 分区，通道 `qq.send` / `qq.react` / `qq.poke` | src/core/api/ws.py, src/platforms/onebot11/backend.py |
| 适配器 → 主体 | HTTP `/platform/inbound`、`/platform/capabilities`、`/platform/delivery/failed`、`/platform/typing`、`/platform/identity/link`、群名称与回填接口 | src/core/api/http.py |
| 归属 | `StreamRegistry` 唯一读写入口，owner=person 1、desktop=stream 1 由迁移固定 | src/core/platform_io/registry.py |
| 插件 | `_manifest.json` + `plugin.py`，清单版本逐字相等，能力封闭枚举 | src/plugin_system/ |
| 协议实现 | OneBot 11 传输、事件分类、消息段、合并转发，两个适配器共用 | src/platforms/onebot11/ |
