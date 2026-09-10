# 配置体系

配置在系统里有两份存在形态：磁盘上的一组 TOML 文件，和内存里单一的 `Config`
运行时视图（src/core/config/schema.py:1022）。前者有三个写入方（Electron 设置
窗口、WebUI 保存接口、首次启动生成器），后者只有一个组装方
（src/core/config/loader.py）。所有形态的定义收敛在
src/core/config/schema.py 一份 Pydantic schema 上——类型、范围、字段关系、
废弃字段、版本号都在这里，其余模块只消费结论。本文说明文件为什么这么分、
一条配置从磁盘走到业务代码经过哪些关卡、版本升级如何不要求用户手改，
以及为什么需要一条专门的校验门把两个运行时的写入器钉在一起。

用户向的字段说明不在本文范围内，见[安装与配置](../../manual/deployment/install.md)与
[config.example/README.md](https://github.com/YueLi-and-Me/YueLiBot/blob/main/config.example/README.md)。

## 1. 五份 TOML 的职责划分

| 文件 | 落盘位置 | 对应模型 | 管什么 |
| :--- | :--- | :--- | :--- |
| `providers.toml` | `config/` | `ProviderCatalog`（schema.py:757） | API 厂商连接：地址、密钥、鉴权方式、超时重试 |
| `models.toml` | `config/` | `ModelCatalog`（schema.py:964） | 模型条目、十二类任务的候选清单与轮询策略、按任务的采样参数 |
| `bot.toml` | `config/` | `BotDocument`（schema.py:973） | Bot 身份与称呼、人格文本、群聊回复策略、会话与记忆节奏、打字节奏 |
| `features.toml` | `config/` | `FeatureDocument`（schema.py:1008） | 功能开关（语音、视觉、向量、感知出口、反馈纠错）、日志、代理等横切参数 |
| `adapter.toml` | `config/` | 无模型，`read_active_adapter` 直读（adapter_selection.py:37） | 一个字段：`plugin` 指向 `adapters/` 下哪个插件目录 |

另有一份不在 `config/` 下：当前适配器的连接配置固定与插件同目录
（`adapters/<插件名>/config.toml`），由 `AdapterDocument`
（src/platforms/onebot11/config.py）按独立的版本号 `NAPCAT_CONFIG_VERSION`
（同文件 20 行）解析，不随主体版本演进。QQ 是可选组件，它的连接参数跟着
插件走，换协议端就是换一个文件夹，配置不产生跨目录的搬家问题。

划分的理由，按边界逐个说：

1. **连接与路由分离**（providers 对 models）：一条厂商连接下挂多个模型条目，
   一个模型的候选可被多个任务引用。合并成一份文件会让「换一个 key」和
   「调整任务分档」动同一份文件；分开后密钥集中在 providers 一份里，
   models.toml 不含任何机密。熔断也按厂商记（schema.py:892 的注释），
   连接是天然的故障域边界。
2. **内容与开关分离**（bot 对 features）：bot.toml 里是用户会反复润色的
   人格与行为参数，features.toml 是「开不开、日志落哪」这类运维开关。
   两者的修改频率和审视方式不同，混在一份里会让改人设的人每次都滚过
   一整屏运维参数。
3. **adapter.toml 刻意没有 schema 和版本壳**：它只有一个字段，不随配置结构
   演进；给它套 `[inner].version` 只会多一处需要同步升级的地方
   （adapter_selection.py:50-52）。读取时缺少非空 `plugin` 直接报错、不回退
   默认适配器——猜错的那个适配器读的是另一份配置，表现为「设置页改了参数
   却不生效」，比报错难查得多（adapter_selection.py:44-47）。

## 2. 单向引用链与运行时视图

引用只有一个方向，从任务指向连接，永不反向：

```
model_tasks.<任务>.model_list ──> models[].name ──> api_provider ──> api_providers[].name
       （models.toml 内部）         （models.toml）                    （providers.toml）

features.toml 的开关 ──校验方向──> models.toml 对应任务的 model_list 必须非空

adapter.toml ──> adapters/<插件>/_manifest.json（段名）+ config.toml（连接参数）
```

- 任务候选是一个列表而不是一个模型名：失败按策略换人（sequential 顺序试、
  random 摊流量、balance 逐轮轮询，schema.py:799-814）。除 chat 外的对话系
  任务留空即继承 chat 的全部候选（loader.py:42-45、159-169），因此拆分
  决策/表达这些槽位不要求用户先配模型——不配就是旧行为。
- loader 把「模型定义 + 它引用的厂商连接」拍平成 `ModelCandidate`
  （schema.py:884），再按任务组成 `RoutingConfig`。业务服务只拿
  `Config.routing.<任务>.candidates` 用，不知道、也不需要知道磁盘上是几份
  文件、引用是怎么解析的（schema.py:1-9 的模块说明）。磁盘布局要变，只动
  loader 一处。

## 3. 加载期交叉校验拦什么

加载分三层，任何一层不过都到不了业务逻辑：版本闸 → Pydantic 字段校验 →
loader 的跨文件校验（loader.py:237-335）。逐条列：

1. **版本闸**（toml_io.py:14-35）：先比 `[inner].version` 再解释任何字段。
   同一个 `model_tasks` 在旧版本里是单个模型名、在新版本里是候选列表，
   版本对不上就继续解析只会产出误导性的类型错误。
2. **段名拼写**：`ModelTaskConfig` 设了 `extra='forbid'`（schema.py:852）。
   留空继承 chat 是合法语义，段名打错不是——没有这一条，
   `[model_tasks.summry]` 会静默变成「跟 chat 一样」。
3. **引用完整性**：厂商名、模型名各自要求非空且不重复，报错带第几个条目
   （loader.py:73-116）；模型引用了不存在的厂商、任务引用了不存在的模型
   都在加载期拦（loader.py:119-136、171-178）。注意 loader.py:262-265：
   不只校验被任务选中的模型——目录里未被引用的坏条目同样是配置错误，
   不能等轮询切到它、用户正在等回复时才暴露。
4. **协议错配**：volcengine 私有协议只能承载 tts（loader.py:190-194）；
   vision 任务的候选必须显式标了 `visual = true`，防止纯文本模型被手改进
   视觉路由（loader.py:182-186）；openai 协议的模型必须有真实模型 ID
   （loader.py:196-200）；`base_url` 在加载期就解析一次，留到首次请求才
   发现「这个 kind 没有内置地址」，表现为整轮候选试完才失败、根因埋在最
   后一条报错里（loader.py:203-204）。
5. **开关与候选的一致性**：tts / vision / 聊天图片 / 向量召回开了但对应
   `model_list` 为空，或 tts 开了没填音色，都拦在加载期
   （loader.py:293-305）——否则配置表面有效、运行时该功能根本执行不了。
   向量任务的全部候选还必须 `embedding_dim` 一致，维度不一的向量算相似度
   全错，且错得无声（loader.py:308-313）。
6. **字段间关系**：摘要批次必须小于触发阈值、触发后余量不能超出工作记忆
   窗口（schema.py:358-378）；慢响应阈值必须早于首字超时（schema.py:831-841）；
   鉴权类型与 api_key/auth_name 的组合（schema.py:734-754）；
   `group_chat.at_mention_must_reply` 必须显式写出、不接受缺省
   （schema.py:987-1005）——@ 必回是行为承诺，默认值替用户做决定不合适。
7. **退休字段**：见下节。

这套校验链同时是热重载的校验链：`reload_config` 先完整构建新 `Config`，
任何一项不过都在替换之前抛出，全局配置保持原状，不存在「失败就用旧配置
继续跑」的静默兜底（loader.py:509-545）。字段按生效方式分三类登记在两张
前缀表里：启动期装配的（模型客户端、日志管道等）标注「需要重启」，被拷进
实例属性的标注「本次重载不生效」，其余即改即用（loader.py:417-442）。

## 4. 版本升级与字段退休

版本号只有一个定义处：`CONFIG_VERSION`（schema.py:23），`InnerConfig`
用 `Literal` 把它钉死（schema.py:48）。Electron 侧另有一份同名常量
（electron/main/config.ts:45-48），两侧必须同步，schema.py:44-47 的注释
把这列为改字段时的硬性纪律。升级由两条机制分工：

1. **版本驱动的整份重写（Electron 侧，跨版本）**。
   `readConfigDirectory` 读目录时发现任一文件版本落后，就用解析出的旧值
   按当前模板整份重写四份文件（config.ts:1846-1865、1833-1835、
   2672-2686）。判定只比版本号、不比字段（config.ts:22-28），所以删除或
   重命名字段必须 bump 版本——不 bump 就不会重写，废弃字段永远留在用户
   文件里。旧版单文件配置的迁移也在同一个入口（config.ts:1848-1853）。
2. **字段级对账（Python 侧，同版本内的漂移）**。
   `upgrade_config_directory` 每次启动都在解析之前跑
   （src/main.py:552-563，upgrade.py:363-395），解决两个沉默问题
   （upgrade.py:1-18）：代码新增的配置项在用户的 TOML 里没有那一行，用户
   无从得知它可配、默认值是什么；废弃字段留在文件里看似生效，实际已经
   没有代码读它。处置口径：
   - 新增字段按 schema 默认值追加进对应的表，行扫描插入而不是解析后整份
     重写——重写会丢掉用户自己写的注释和排版（upgrade.py:226-271）；
   - 废弃字段就地删除，删完重新解析一遍 TOML，解析失败整份还原当作没删
     （upgrade.py:274-333）；
   - 写任何字节之前先把整个 `config/` 备份到 `data/backups/config/`，
     因为它含明文密钥且不入版本库，改坏没有第二份（upgrade.py:206-223）；
   - 差异汇总成一次性的控制台信息框，没变化不打印（upgrade.py:336-360）。
   - 两条刻意的保守：`api_providers` / `models` 这类列表字段当叶子跳过，
     它们是用户数据不是设置项，自动补默认值会造出不存在的厂商条目
     （upgrade.py:142-144）；嵌套段整段缺失时不逐字段补，整段是一整块
     新功能的配置，自动铺默认值会让用户以为自己配过（upgrade.py:164-165）。

字段退休因此不需要用户手改，三条路径各管一段：旧版本文件被 Electron
读取时退休字段就地剪除、随重写消失；已经声明当前版本却仍带退休字段
（升级后又手改回来）则报错，静默剪掉会让这次改动无声消失
（config.ts:1126-1135）。Python schema 里的 `before` 校验器是同一套名单
的最后兜底，报错信息直接写明「挪到哪里去」（schema.py:274-298 等三处）。

版本号什么时候**不** bump 同样有约束。`[developer]` 段整段可选、缺失即
关闭，且 bootstrap 生成初始配置时显式剔除它（bootstrap.py:230-238），用户
文件里永远不会出现这一段，因此新增它不 bump：升级器对「整段缺失」有意不
自动补，「只新增一个段」的版本跳升拿不到任何升级路径，存量配置会在版本闸
直接失败、Bot 起不来（schema.py:38-42 的完整推理）。

一个边界要说清：Python 只解析当前版本（toml_io.py:30-34），`upgrade.py`
对账不碰 `[inner]`（upgrade.py:40-41），版本跳升的整份重写只发生在
Electron 读取时。无头部署没有 Electron，撞上版本不匹配时会收到「对照模板
补齐」的提示（loader.py:41）——跨版本升级在无头形态下是手工动作，这是
当前形态的既定边界，不是漏实现。

## 5. 三个写入方与对齐门

同一份配置有三个写入方，各自的纪律不同：

1. **Electron 设置窗口与升级重写**（electron/main/config.ts）：手维护的
   TOML 模板，设置页保存与版本升级共用 `writeConfigDirectory` 整份重写
   （config.ts:2672-2686）。首次启动缺模型或密钥时先弹设置窗口，填完再
   继续（electron/main/index.ts:194-199）。
2. **WebUI 保存**（Python 侧）：`settings_webui.save` 先用 Pydantic 校验
   表单，再写临时目录、用与启动完全相同的 `_load_split_config` 验证整份
   新配置，通过后才 `os.replace` 原子换入，任何一步失败用保存前的字节
   恢复（settings_webui.py:444-548）。模型工作台 `model_webui.save` 是
   先落盘再在临时副本上整链验证、失败回滚（model_webui.py:162-217），
   校验口径与启动一致，但写入瞬间磁盘上短暂停留过未验证的内容。
3. **首次启动生成器**（bootstrap.py）：不是第二份手维护的默认值表——除
   少数必填且无合理默认的字段外，全部取自 Pydantic 默认值
   （bootstrap.py:148-159）；新增了必填字段却没在种子表给初值会直接
   KeyError，由测试提前拦（bootstrap.py:142-145）。产出**故意不完整**：
   api_key 为空、连自己的 schema 校验都过不了，因此生成后必须停下来等
   用户填（bootstrap.py:12-16；src/main.py:283-309）。`[developer]` 段
   在首次生成时被剔除（bootstrap.py:230-238）。

三个写入方里有两个分属不同语言，对同一份文件的所有权是分裂的：
Electron 负责写、Python 负责校验。schema 加了字段而 TS 模板没跟上时，
整份重写会静默毁掉用户配置——这类漂移实际发生过，一次约 30 个字段
（config_parity.py:1-19）。单靠「改字段时记得同步两边」的纪律拦不住，
所以需要一条独立的门：scripts/check/config_parity.py 用 Electron 写入器
把全默认值配置写进临时目录（scripts/check/config_defaults.ts），再用
tomllib 抽出每个表的字段集、每个字典表的键集，与 Python schema 做双向
差集（config_parity.py:170-217）。字典表连键集一起比——`model_tasks`
少写一个任务槽，等同于把用户已配好的那段从模板里抹掉（planner 等槽位
正是这样丢过的，config_parity.py:73-76 与 92-97）。这条校验挂在
pytests/core/test_config_parity.py 里随正常测试跑，是四条状态门之外的
跨语言补充门（见[开发与验证](../guide/testing.md)）。注意它只比
字段集与键集、不比默认值：TS 的 `DEFAULT_CONFIG` 与 bootstrap 种子表
的取值对齐目前靠两处相互引用的注释约定（config.ts:93-96），不在门内。

WebUI 侧另有一份声明式清单 `settings_schema.json`：设置页的字段、类型与
中文说明以它为唯一来源，保存也只写它列出的键。它在加载时先过两道自检：
每个配置段声明的字段必须与对应 Pydantic 模型完全一致
（settings_webui.py:95-143），任务条目清单必须等于 `ModelTaskConfig` /
`GenerationConfig` 的字段集（settings_webui.py:146-180）。缺这两条时，
清单漏一个字段，保存一次那个字段就从文件里消失、回落到默认值——默认
为 false 的字段丢了尤其看不出来。配置文件里逐字段的中文注释也来自这份
清单，由 `_write_documented_toml` 写出（settings_webui.py:379-442），
bootstrap 复用同一个写入器（bootstrap.py:47-52）。

随代码分发的 `config.example/` 也由同一台机器生产：
`render_example_configs` 用同一套 schema 与写入器渲染（bootstrap.py:375-431），
与首次运行创建真实配置走同一条代码路径，模板因此不可能悄悄漂移；
pytests/core/test_config_example.py 重新渲染一遍并要求与入库副本逐字一致
（换行符归一后比对），字段增删忘了重新生成时那条用例会红。模板与真实
安装的唯一差别是落点：适配器连接配置在模板里按插件名平铺，真实安装只
生成当前启用的那一个（bootstrap.py:412-414）。

## 6. 首次启动与「还差什么」

首次启动的完整顺序（src/main.py:544-571）：

1. `bootstrap_config_directory` 补齐缺失文件，已有的不动——它每次启动都
   跑，覆盖会冲掉用户填好的密钥（bootstrap.py:328-372）。只补出了适配器
   文件不算首次安装，QQ 是可选组件；创建了主体四份才停下（src/main.py:297-309）。
2. `upgrade_config_directory` 对账（上节）。
3. `load_config` 走第 3 节的整条校验链；失败时把诊断打到 stderr 并退出
   （loader.py:357-384），main 再补一句「哪些东西还没填」的人话
   （src/main.py:325-344）——「格式都对、就是还没填」从 Pydantic 的字段
   路径里看不出来。这句人话由 `missing_startup_requirements` 生成
   （bootstrap.py:434-508）：它只查能否启动一轮对话，并且把**全部**未填
   密钥的厂商一次报全——schema 对目录里每个厂商都要求非空 key，不区分
   是否被任务引用，只报对话任务用到的那一条会让用户填一次撞一次
   （bootstrap.py:488-501）。

`adapter.toml` 在两个进程里都可能首次创建，两侧持有同名同值的默认插件名
常量（adapter_selection.py:30 与 config.ts:2692），创建是一次显式初始化、
用户随后可改；读取路径永不回退——创建与读取的回退策略故意不对称，
理由见 adapter_selection.py:27-30 与 bootstrap.py:287-305 的注释。

相关阅读：[架构总览](overview.md)（进程与目录边界）、
[安装与配置](../../manual/deployment/install.md)（用户向的填写说明）、
[QQ 与群聊接入](../../manual/adapters/index.md)（适配器那份配置怎么填）。
