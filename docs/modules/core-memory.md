# core/memory —— 记忆系统

SQLite 长期记忆的读写、召回与维护：近期对话、情节摘要与结构化事实三层记忆，
加一层无衰减的知识库。写入、历史与召回由 `MemoryStore` 统一提供；衰减、相似度、
分词、向量与图扩散是独立组件，被存储与召回链路组合使用。

## 职责边界

- 负责：三层数据与知识的存取、检索打分与调参、事实的遗忘与人工管理、
  关联图扩散、向量生成与量化补算、外部资料导入。
- 不负责：什么时候写记忆（抽取在 `src/core/agent/fact_extract.py`）、召回结果
  怎么进提示词（在 `src/core/services/chat/context_build.py`）、数据库文件与
  连接生命周期（连接由装配注入，表结构真源在 `src/core/db/`）。

## 目录内容

- `src/core/memory/__init__.py`
  包说明与游标常量（如事实抽取的消费水位键）。
- `src/core/memory/store.py`
  `MemoryStore`（全包最大，约两千行）：`messages`、`episodes`、`facts` 三层的
  读写与事务更新，历史查询与相关内容召回。调用方注入已打开的 sqlite3 连接，
  本模块不决定库文件位置——生产由统一连接管理器控制迁移与生命周期，测试用
  `:memory:` 隔离。事实的显式取代（运行期抽取与反馈纠错）也经它落
  `fact_operations` 流水。
- `src/core/memory/decay.py`
  遗忘曲线：指数衰减加双阈值滞回（低于冻结阈值转非活跃但绝不删除，须回升过
  复活阈值才重新激活）；预计算 `due_at` 避免周期性全表扫描。另含召回排序权重
  与 BM25 相关度。被 `db/migrations/v18_to_v19.py` 引用常量，是全库衰减口径的
  真源。
- `src/core/memory/tokenize.py`
  检索分词：jieba 常规切词加 CJK bigram 补专名与新词召回；索引保留重复项提供
  词频，查询串构造避免 FTS5 语法注入。
- `src/core/memory/similarity.py`
  事实去重判据：字符 Jaccard 衡量词面重合、bigram Jaccard 约束局部顺序，两项
  同时达阈值才允许合并，降低语序相反的句子被错误去重的概率。
- `src/core/memory/scope.py`
  事实可见性：按「事实被听见的场合」（`origin_kind`）判定它在当前会话是否
  可见——人物事实人在场就不算泄露，私聊听来的话换个群复述一定出事。只含纯
  函数，配置开关由调用侧读好传入。
- `src/core/memory/embed.py`
  向量生成客户端：只在 `[vector].enabled` 时被服务层调用，复用对话模型的认证
  与地址；单批最多 20 条（服务端硬限制），失败由上层记录并保留 BM25 路径，
  向量故障不中断对话。
- `src/core/memory/quantize.py`
  float32 向量到对称 int8（SQ8）的量化与反量化，blob 自带维度与 scale、可独立
  解码；原始 float32 列保留便于校验回滚。含启动期后台回填入口。
- `src/core/memory/vector_health.py`
  只读汇总事实与知识向量列的覆盖与格式健康状态，供启动期告警与主动自检取数；
  只执行 SELECT，不触发补算或修复。
- `src/core/memory/knowledge.py`
  知识层的索引维护与检索。knowledge 不是「Bot 的记忆」而是「Bot 知道的事」：
  无衰减、不参与遗忘曲线。写侧管全文索引补齐（外部内容表 rowid 必须与
  `knowledge.id` 显式对齐）与向量重算（待办集合就是 `embedding IS NULL`，天然
  断点续跑）；检索侧提供全文搜索、关联概念与触达强化。
- `src/core/memory/association.py`
  三层记忆与知识之间的关联图：`memory_nodes` 把已有记录映射到统一节点，
  `memory_edges` 存无向关联边；召回时统一记忆图与知识概念图分别跑个性化
  PageRank，只在结果层融合。中间结果以结构化事件落账，可在观察面板回放。
- `src/core/memory/pagerank.py`
  与存储无关的个性化 PageRank 稳态计算，含超时保护。
- `src/core/memory/curate.py`
  事实的人工管理：标失效、恢复、永久保留、人工取代、冲突裁决与操作撤销。
  不改写召回判据——失效用自指哨兵编码、永久保留用半衰期编码，都不加新状态
  列；每次操作在 `fact_operations` 留流水，自动链路的改动同样可见、可撤销。
  SQL 全部在 `MemoryStore` 方法里，本模块只做状态判定、快照与事件。
- `src/core/memory/high_frequency.py`
  会话高频词表：统计该会话其他人真实在用的词，给黑话召回提供量级领先的加分。
  统计前剥离平台占位段，单条粘贴文本的出现次数封顶——词频反映多人使用而非
  一篇长文。
- `src/core/memory/import_center.py`
  导入中心：粘贴文本或上传文件切条后逐条走 `add_knowledge`，按来源批次登记、
  整批撤销；NULL 批次的存量行永不命中。进度是进程内状态，一次导一批、并发
  导入直接拒绝。
- `src/core/memory/tuning.py`
  检索调优中心：参数白名单（白名单外的覆盖在构造时即被拒绝）、命名 profile
  （落 `retrieval_profiles` 表，进程内覆盖表生效）、从事件账本与提示词转储抽
  弱监督样本做 nDCG 评估，以及基于生产留痕重放的第二轮评估。弱监督的已知
  局限（度量的是既定选择的稳定性而非绝对相关性）记录在模块 docstring。

## 对外接口与调用方

- `MemoryStore`：`src/core/services/chat/service.py`（聊天主服务持有）与
  `src/core/services/maintenance/memory_feedback.py`（反馈纠错写事实与情节
  标记）构造；全部 SQL 落在这一个类。
- `curate` 与 `tuning` 与 `import_center`：由 `src/core/api/http.py` 的管理面板
  端点调用（事实管理页、检索调优页、导入中心页）；`tuning` 的运行期读取点
  （`tuned_value`、`blend_score`）在 `src/core/services/chat/` 的召回与上下文
  组装里。
- `scope`：`MemoryStore` 的召回出口按它过滤；`origin_kind_for_stream` 由写入侧
  决定落库时的来源标记。
- `embed` / `quantize` / `vector_health`：`src/core/services/maintenance/vector.py`
  （向量服务的补算任务）与 `src/core/runtime/self_check.py`（启动自检）调用。
- `association`：`src/core/agent/fact_extract.py` 写入后建边、
  `src/core/agent/cognition.py` 召回后扩散。
- `high_frequency`：`src/core/agent/jargon.py`、`src/core/agent/jargon_mine.py`
  （黑话召回与挖掘）与 `src/core/services/maintenance/jargon_stats.py`。
- `decay`：除包内使用外，`src/core/db/migrations/v18_to_v19.py` 引它的类别
  常量与半衰期函数对齐迁移口径。

## 依赖方向

- 包内：`store` 是组合点，依赖 `decay`、`scope`、`similarity`、`tokenize`、
  `tuning`；`association` 依赖 `decay`、`pagerank`、`tuning`；`knowledge` 依赖
  `decay`、`similarity`、`tokenize`；`quantize`、`vector_health`、`curate`、
  `import_center` 相对独立，按需引用 `store` 与工具模块。
- 包外依赖：`src/core/db/schema.py`（`store` 取 DDL 与种子数据）、
  `src/core/observe`（建边、扩散、导入等事件落账）、
  `src/core/platform_io/types.py`（仅 `scope.py` 的 `StreamKind`）、
  `src/core/runtime/clock.py`（统一时钟）、`src/core/config/schema.py`（
  `embed.py` 的模型候选）、`src/core/llm_models/router.py`（向量客户端的模型
  路由）。
- 被依赖：`services`、`agent`、`api`、`runtime/self_check`、以及一条例外——
  `db/migrations/v18_to_v19.py`（方向是迁移引用本包常量，不是业务依赖迁移）。
- 约束：本包不决定连接与迁移；`MemoryStore` 拿到的库必须已经迁移到当前版本。
  contentless FTS 的 rowid 对齐纪律（facts、knowledge、episode_cues）是全库
  检索正确性的底线，见 [core/db](core-db.md) 的迁移说明。
