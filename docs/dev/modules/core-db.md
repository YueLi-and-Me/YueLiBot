# core/db —— SQLite 连接、表结构与迁移链

数据库层真源：进程唯一的写连接、全部表结构的 DDL、以及把任意历史版本的库推进
到当前结构的迁移链。不引入 ORM，Python 侧直接以 sqlite3 执行 DDL；上层服务
（记忆存储、平台注册表、人格状态）拿到的是连接或自己实现的访问接口，本包不管
业务查询的语义。

## 职责边界

- 负责：连接生命周期、库文件备份、版本迁移、表结构与索引定义、启动期的结构
  报告。
- 不负责：业务数据的读写规则（在各业务模块）、事件账本的写入（账本
  `src/core/observe/store.py` 只从这里取 `EVENTS_DDL`，自己持有独立连接）。
- 单向约束：表结构只能在本包定义或经迁移链变更；`src/core/platform_io/registry.py`
  等直接读表的模块只按已迁移的结构读写，不允许自行建表。

## 目录内容

- `src/core/db/__init__.py`
  包说明。
- `src/core/db/connection.py`
  `open_db` / `get_db` / `close_db` 管理进程级单写连接，配 WAL 避免多写连接互等；
  `run_in_thread` 用 `asyncio.to_thread` 把阻塞查询移出事件循环。建表与迁移委托
  给 `migrations/manager.py`，保证已有库先备份、再判版本、后变更。测试可传
  `:memory:` 建隔离库。
- `src/core/db/schema.py`
  全部表结构的唯一真源：`DDL`（业务表与索引、contentless FTS5 全文索引、种子
  数据 `SEED`）与事件账本的 `EVENTS_DDL`。表覆盖平台归属（persons、streams、
  identities、group_memberships、messages）、记忆（facts、episodes、knowledge、
  jargon、expressions、memory_nodes 等）、人格（persona、persona_bond、
  persona_snapshots、person_profile）、表情包（emoji、emoji_banned）与反馈纠错、
  事实操作流水等。新增表按惯例直接写进 DDL 用 `CREATE TABLE IF NOT EXISTS`，
  存量库另配迁移补齐。
- `src/core/db/schema_report.py`
  启动时的结构对账报告。`CREATE TABLE IF NOT EXISTS` 有两个盲区：建表静默无
  痕迹、表已存在时加列被跳过且不报错。这里把期望结构与实际结构都物化成
  「表名 → 列名集合」做差集（期望结构由当前 DDL 在内存库跑一遍得到，不解析
  SQL 文本），差异非空就打印信息框并记日志。
- `src/core/db/migrations/__init__.py`
  迁移包说明。
- `src/core/db/migrations/registry.py`
  迁移注册表。每个迁移是 `(db: sqlite3.Connection) -> None` 的函数，用
  `register` 登记后在单事务内执行、失败自动回滚。
- `src/core/db/migrations/bootstrap.py`
  迁移前的版本对齐。历史 TypeScript 实现把结构版本写在 `meta.schema_version`
  （最终值为 3），现行迁移管理器按 SQLite 原生 `PRAGMA user_version` 找入口。
  三种情况：没有 `messages` 表的新库直接标记当前版本；有业务表且 user_version
  为 0 的旧库从 `meta.schema_version` 领取迁移入口；已被现行迁移器接管的库
  保持原值。
- `src/core/db/migrations/manager.py`
  `run_migrations`：读 user_version、沿注册表逐版本推进到 `CURRENT_VERSION`
  （30），每次迁移前自动备份库文件；收尾调用结构报告对账。`migration_chain_errors`
  在启动自检里校验迁移链没有断口。

## 迁移文件

命名即方向：`vN_to_vN+1.py` 只做一步，`FROM_VERSION` 对应迁移前的 user_version。
一步一事务，全部幂等可重放。

- `src/core/db/migrations/v3_to_v4.py`：全文索引从手写 bigram 预分词迁到 jieba
  分词，为 facts 加 `tokens_v2` 并重建 facts 与 episode_cues 的 contentless FTS
  索引。
- `src/core/db/migrations/v4_to_v5.py`：facts 加 `embedding` 列（原始 float32
  packed bytes），供向量混合召回；存量补算由启动期任务后台执行，不阻塞启动。
- `src/core/db/migrations/v5_to_v6.py`：为记忆和人格建立 stream / person 分区。
  纪律点：facts_fts 是 contentless FTS5，rowid 必须恒等于 facts.id，重建 facts
  时显式搬运 id，绝不能重建或重写 facts_fts，否则召回静默指向错误事实。
- `src/core/db/migrations/v6_to_v7.py`：关系状态收敛为按人物保存的单一好感度。
- `src/core/db/migrations/v7_to_v8.py`：群名片与账号昵称分离的关联表。
- `src/core/db/migrations/v8_to_v9.py`：按历史消息补齐缺失的群成员关系，修复
  「群聊发言者必存在成员关系」不变量对存量数据不成立导致快照接口 500 的问题。
- `src/core/db/migrations/v9_to_v10.py`：可校验、可发送的表情包库。
- `src/core/db/migrations/v10_to_v11.py`：表情包记录补 OneBot 图片子类型。
- `src/core/db/migrations/v11_to_v12.py`：消息记录补平台原生消息编号。
- `src/core/db/migrations/v12_to_v13.py`：自身状态与历史快照增加心情轴。
- `src/core/db/migrations/v13_to_v14.py`：连续的生活活动时间线。
- `src/core/db/migrations/v14_to_v15.py`：表情包使用记录与按内容哈希的封禁表。
  使用次数（Bot 自己发过几次）与见到的次数分开记；封禁以内容哈希为主键，行被
  删后封禁仍然生效。
- `src/core/db/migrations/v15_to_v16.py`：表达方式表补人工确认标记与最近使用
  时间，不触碰历史使用计数。
- `src/core/db/migrations/v16_to_v17.py`：会话高频词表。
- `src/core/db/migrations/v17_to_v18.py`：黑话学习的三个证据列（出现次数、证据
  消息数组、上次推断水位）。jargon 表历来由链尾的幂等建表 DDL 创建，迁移先保证
  表存在再补列。
- `src/core/db/migrations/v18_to_v19.py`：把抽取模型自由生成的三十种事实类别
  收拢到衰减模型已有的七类，并按新类别重算半衰期与到期时间。
- `src/core/db/migrations/v19_to_v20.py`：为事实与知识向量增加并存的 SQ8 量化
  列。只补列不量化，存量补算交给启动期任务，保持 DDL 事务短小可审计。
- `src/core/db/migrations/v20_to_v21.py`：事实的来源场合标记列。存量行一律
  `legacy`，不按推断回填——用猜测给已有数据编造出处，错了无处可发现。
- `src/core/db/migrations/v21_to_v22.py`：事实账本两列（单值槽位 `slot`、显式
  取代链 `superseded_by`）与冲突检测索引。同槽异值两条都保留，注入时并排呈现，
  不静默覆盖。
- `src/core/db/migrations/v22_to_v23.py`：按正文内容为四类存量事实回填单值槽位，
  只有正文明确表达单值维度时才填，其余保持空槽。
- `src/core/db/migrations/v23_to_v24.py`：反馈纠错的两张表（待观察锚点、纠错
  结果）与情节待重建列。
- `src/core/db/migrations/v24_to_v25.py`：导入中心的来源批次表与知识批次列。
  存量行保持 NULL（迁移进来的，无批次），按批次撤销永不命中 NULL 行。
- `src/core/db/migrations/v25_to_v26.py`：人物画像信任分级——确凿档（由事实
  账本投影、带可追溯 fact id）与证据指纹（指纹没变就不再调用模型）。存量摘要
  语义收窄为「印象」档，确凿档留空等自然刷新，不编造出处。
- `src/core/db/migrations/v26_to_v27.py`：会话流补充可空的可读展示名（群名称）。
- `src/core/db/migrations/v27_to_v28.py`：事实操作流水表 `fact_operations`，
  供人工管理与自动取代留痕、审计与撤销。
- `src/core/db/migrations/v28_to_v29.py`：建运行画像表。它已在下一步删除，保留
  在链条里只为让停在 v28 的真机走完两步——真机已越过 v29，把这两步合并会让它
  跳过删表。
- `src/core/db/migrations/v29_to_v30.py`：删除运行画像表。它的两个立项依据
  （遥测载荷本体、自生成安装 ID）在落地当天即被查证作废，唯一读它的开发者命令
  一并删除；表留着就是没有消费方的代码。

## 对外接口与调用方

- `open_db` / `close_db`：`src/main.py` 装配时开连接、关闭时收连接；
  `src/core/api/http.py` 的请求处理与 `src/selftest.py` 也直接取连接。
- `run_migrations`、`write_user_version`：调用方是 `src/main.py`、
  `src/core/runtime/self_check.py`（启动自检校验迁移链）。
- `DDL` / `SEED` / `EVENTS_DDL`：`migrations/manager.py` 建库用；`EVENTS_DDL`
  另被 `src/core/observe/store.py` 取用。`snapshot_shape` /
  `describe_schema_changes` 被迁移管理器调用。
- 各业务存储（`src/core/memory/store.py` 的 `MemoryStore`、
  `src/core/platform_io/registry.py` 的 `StreamRegistry` 等）在装配时接收连接，
  本包不知道它们的存在。

## 依赖方向

本包只依赖标准库与 `src/core/logging`。唯一指向业务的引用是
`src/core/db/migrations/v18_to_v19.py` 引 `src/core/memory/decay.py` 的类别
常量与半衰期函数——保证迁移口径与运行期衰减模型一致，方向是迁移依赖业务
常量，业务模块一律不依赖迁移代码。
