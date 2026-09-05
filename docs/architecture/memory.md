# 记忆系统

记忆的真源是 SQLite 里的几张表，算法全部在 `src/core/memory/` 的纯函数与
`MemoryStore` 方法里。本文说明三层记忆各存什么、一条数据从写入到召回走的
路径、召回怎么打分、遗忘曲线怎么生效、联想扩散在什么时候介入。层号口径以
`src/core/memory/store.py` 的小节注释与 `src/core/db/schema.py` 为准。
进程与分层边界见[架构总览](overview.md)，本文不重复。

## 一、各层存什么

三层之外还有一张并列的知识表，它刻意不是「记忆」：

| 层 | 表 | 内容 | 生命周期 |
| :--- | :--- | :--- | :--- |
| L1 工作记忆 | `messages`（schema.py:81） | 原始对话逐条，含角色、发送者、平台原生消息编号 | 不删除；被摘要归档后置 `episode_id`，退出工作记忆窗口 |
| L2 情节记忆 | `episodes` + `episode_cues` + `cues_fts`（schema.py:100-120） | 一批消息的模型摘要与召回线索 | 不衰减；只按 FTS 命中与「最近 N 条」进上下文 |
| L3 语义记忆 | `facts` + `facts_fts`（schema.py:123-157） | 关于某个人的结构化事实，带强度、半衰期、来源、槽位 | 走遗忘曲线；跌破阈值只冻结不删除 |
| 知识层 | `knowledge` + `knowledge_fts`（schema.py:164-205） | 与人无关的客观信息 | 明确不衰减（knowledge.py:3「不是 Bot 的记忆而是 Bot 知道的事」） |

为什么这样分：三层对应三种不同的失效语义。L1 的失效是「被归档」——数据还在，
只是不再算「最近」；L2 的失效是「检索不到」——摘要不衰减，旧情节靠 FTS 分数
自然沉底；L3 的失效是显式的——事实只会被取代（`superseded_by` 非空）或被遗忘
曲线冻结（`active = 0`），两者都留行。把三种语义塞进一张表，任何一条规则改动
都会误伤另外两种。

每张内容表都配一张 FTS5 外部内容表（`content=''`），`rowid` 必须显式对齐主键，
这是写入方的责任而不是触发器的（store.py:606-607、knowledge.py:61-62 的注释
都记录了这里踩过的坑：rowid 对不齐时检索结果指向错误的行，且行数完全正常）。

## 二、写入路径

一条入站消息从落库到变成长期记忆，走三条互相独立的后台队列：

```
入站消息
  └─ append_message → messages（L1，service.py:799）
        ├─ 摘要队列    _maybe_summarize   → episodes/cues_fts（L2）
        ├─ 事实抽取队列 _maybe_extract_facts → facts/facts_fts（L3）+ knowledge
        └─ （旁路）联想建边 link_together  → memory_nodes/memory_edges
```

1. **L1 写入是同步的。** 入站确认接收时立刻 `MemoryStore.append_message`
   （store.py:292，调用点 service.py:799），Bot 自己的回复在投递前也落库。
   图片描述等慢操作先用占位正文落库、后台补完后 `update_message_content`
   回写同一行（store.py:343），因此入库时间戳反映消息到达时刻而非内容就绪
   时刻。
2. **L2 由归档驱动。** 回合收尾后 `_maybe_summarize`
   （services/chat/background.py:78）检查待归档条数，达到阈值后取最早一批
   交摘要模型（agent/summarize.py 的 `summarize`），成功后 `add_episode`
   （store.py:577）在**同一事务**里写摘要、写线索 FTS、把这批消息的
   `episode_id` 回填——摘要已写而原消息仍显示「待处理」的中间态不存在。
   摘要模型确定性失败（如内容策略拒绝）时写一条 `UNSUMMARIZED_KIND` 占位
   情节占住归档位（background.py:138-186），否则同一批会被无限重投；
   占位情节不写线索、被 `recent_episodes` 显式排除，只在 WebUI 可见。
3. **L3 由独立游标驱动。** `_maybe_extract_facts`（background.py:303）在
   回合后调用 `run_extraction`（agent/fact_extract.py:564）。抽取不看
   `episode_id`，而是按存在 `meta` 表里的自有游标取消息
   （store.py:1563 的 `messages_after`）——摘要和抽取是同一批消息的两个
   独立消费者，共用一个「待处理」判据会互相消费对方的输入且不报错，
   store.py:1566 的注释把这一点写死了。抽取失败游标不动、下轮重跑；同一批
   连续失败到上限才把游标推过这一批（background.py:252 的
   `_skip_stuck_batch`），宁可丢掉这一段也不让它挡住其后全部对话。
4. **抽取提示词自带去重清单。** `render_known_facts`（fact_extract.py:150）
   把在场者每人最多 6 条、总共最多 30 条既有事实连同 `#事实ID` 渲染进
   提示词。模型据此声明 `supersedes`；`parse_extraction`（fact_extract.py:259）
   校验取代目标必须在清单内，指向清单外 ID 的声明直接丢弃并发观测事件——
   模型可能编造 ID，越界的取代绝不能落到无关行上。这份清单读事实时传旁路值
   `stream_kind='all'`（store.py:1822 的 `top_facts`）：它的用途是去重，
   必须看到全部事实，否则私聊来源的事实会因可见性挡下而在清单缺席，随后被
   当成新事实重复写入。
5. **知识层搭同一次模型往返。** 一次抽取同时产出人物事实与知识候选
   （fact_extract.py:226 的 `Extraction`），理由写在注释里：同一段对话读两遍
   开销翻倍，且两次读的结果可能互相矛盾。知识由 `add_knowledge`
   （knowledge.py:71）落库，`content_key` 唯一约束去重，FTS 当场建——
   留给离线补的话，没有 FTS 行的知识不是排名靠后而是整行检索不到。
6. **向量是旁路。** 正文先落库，`embed_fact` / `embed_knowledge` 回调
   （services/maintenance/vector.py:172、198）随后生成向量并写回
   `embedding` 与 SQ8 列（quantize.py）。向量服务失败只记日志，事实正文
   不回滚；启动时 `VectorService.startup`（vector.py:64）按
   `embedding IS NULL` 冻结快照补算存量。批量请求每批最多 20 条是服务端
   硬限制（embed.py:27），不是调优值。
7. **同批写入即建边。** `run_extraction` 末尾把本批写出的事实与知识两两
   `link_together`（fact_extract.py:646-650），这是联想层两种建边时机之一
   （另一种在召回侧，见第六节）。

## 三、读取路径：一个回合怎么拼出记忆

回合上下文组装在 `ContextBuildMixin._prepare_turn_context`
（services/chat/context_build.py:296），全部只读、不调用模型、不改写记忆：

1. **工作记忆**：`working_memory`（store.py:372）取该 stream 未归档的最近
   若干条；可传用户消息水位，把本批之后才落库的用户消息隔离在本轮视野外，
   保证「回合固定快照」。
2. **事实候选池**：`_recall_turn_facts`（context_build.py:224）用两个检索词
   各跑一次 `recall_facts_in_scope`——当前用户文本，加上会话印象（对一段
   对话的概括，短应答捞不到东西时靠它撑覆盖面）。两批候选按 ID 去重、按
   分数稳定排序，当前文本命中的同分时在前。检索范围是「在场者」：当前说话人
   加 `recent_speakers`（store.py:1594）给出的近期发言者，逐人查会把一次
   检索放大成十几次 FTS 查询，放开到全库又越过会话隐私边界。候选池不在
   这里截断，保留约 `limit * 3` 的量级供确认回复后的向量重排。
3. **情节**：`recall_episodes`（store.py:676，按线索 FTS 命中）与
   `recent_episodes`（store.py:621，按结束时间倒序）取并集去重
   （context_build.py:330-348）。
4. **渲染时的两道工序**（`_render_prepared_context`，context_build.py:430）：
   进提示词的条数由渲染层按 `fact_recall_limit` 截取；`_facts_for_prompt`
   （services/chat/helpers.py:30）把同槽冲突组的**全体成员**（包括本轮没被
   召回的）补齐进提示词——冲突事实只注入一半等于没注入，模型只看到一边
   就会把那边当定论。
5. **确认要回复之后才做写回**（`_enrich_prepared_context`，
   context_build.py:556）：为当前文本取向量，`rank_recalled_facts`
   （store.py:1699）在既有候选池上纯内存重排（不重新查库），然后
   `reinforce_recalled_facts`（store.py:1732）只对最终实际进提示词的事实
   回补强度。决策期的检索不回补——`recall_facts_in_scope` 整体是只读的，
   否则「被反复检索」本身会改写遗忘曲线，一条没人用的事实会越查越牢。
6. **主动检索走认知动作**（agent/cognition.py）：ReAct 回环里模型可调
   `recall`（事实+情节，cognition.py:128）、`inspect`（水位之前的聊天原文，
   走 store.py:1639 的 `search_messages`）、`consult`（知识层，
   knowledge.py:173 的 `search_knowledge`）。`search_messages` 有意不建 FTS
   表，用「分词 → LIKE 预筛（硬上界 200 行）→ 内存按命中词数打分」代替，
   代价可控且行为可单测（store.py:1648 的注释）。

## 四、召回打分

打分链路的所有数值都在 `decay.py` / `tuning.py`，索引文本的构造在
`tokenize.py`：

1. **分词**：`index_tokens`（tokenize.py:90）= jieba 精确模式 token + CJK
   bigram，重复 token 保留，因为 BM25 用词频；bigram 是专名与新词的召回
   补充（jieba 词典外的词切不出来，但相邻二字组合总能切出来）。查询侧
   `match_query`（tokenize.py:107）去重后用双引号转义、OR 拼接，挡住
   FTS5 的 `*` / `NEAR` / `-` 等查询语法注入。
2. **词面相关度**：FTS5 的 `bm25()` 值越小越相关，
   `relevance_from_bm25`（decay.py:117）取负后做 `r/(1+r)` 归一到 `[0,1)`。
3. **留存度权重**：`retention_weight`（decay.py:130）把留存度线性映射到
   `[0.35, 1.0]`——下限不是 0，因此一条快被遗忘但词面高度相关的事实仍然
   能被召回，只是排不到前面。
4. **融合形态**：`blend_score`（tuning.py:150）= `relevance^bm25_weight ×
   (floor + (1-floor) × retention)`。两个权重取默认值时与旧的
   `decay.score` 逐字节等价；调优只改系数，不改乘法形态。
5. **向量融合**：查询向量与事实向量都在时，余弦 `[-1,1]` 映射到 `[0,1]`
   后按 0.4/0.6 与词面相关度加权（store.py:1413-1421）。任一侧缺失或单条
   坏向量只放弃该条的语义融合，退回 BM25。词面权重要保留的原因写在
   store.py:1408-1412 的注释里：BM25 对专名、数字、代码关键字的精确匹配
   不可由语义相似度完全替代；留存度也必须在融合**之后**统一施加，否则
   向量分支会绕过遗忘曲线。
6. **知识层复用同一套公式**，唯一差别是留存度恒取 1
   （knowledge.py:173-226）。
7. **调优中心**：`tuning.py` 的白名单（tuning.py:74）只允许调八个参数
   （`bm25_weight`、`retention_weight_floor`、`ppr_alpha`、`ppr_hops`、
   `pool_score_percentile` 与三个条数上限）；冻结阈值、半衰期表、0.4/0.6
   混合比不在调优范围内。profile 落 `retrieval_profiles` 表，进程内覆盖表
   生效（tuning.py:113-147）；评估用弱监督样本重放检索算 nDCG@k，另有
   「留痕重放」把重放候选池与生产 `memory_retrieval_trace` 事件逐位对账
   （tuning.py:707 起），对不上的回合单独计数——那说明重放与生产仍有
   差异，其读数比 nDCG 本身重要。
8. **可见性在打分之前**：`fact_visible_in_stream`（scope.py:39）按事实
   「被听见的场合」过滤候选——私聊来源的事实在群聊默认不可见，
   `legacy` 存量放行，被挡下时发 `memory_fact_scope_blocked` 事件，
   让「她怎么突然不记得了」与「本来就没有」可区分。来源只会单向升格：
   私聊里说过的事实在群聊被重说，`add_fact` 强化命中时把 `direct` 改写成
   `group`（store.py:790-801），反向永不发生。

## 五、遗忘曲线

实现集中在 `decay.py`，一句话：`retention = strength × 2^(-已过小时/半衰期)`，
加双阈值滞回。

1. **按类分半衰期**（decay.py:21-29）：身份、日期一年；偏好、关系 90 天；
   事件 21 天；状态 12 小时。类别未知回落到「事件」。
2. **滞回**：活跃事实跌破 `FREEZE = 0.1` 才冻结；已冻结的必须回升过
   `REVIVE = 0.15` 才复活（decay.py:201）。单阈值会让临界事实在活跃与非
   活跃之间抖动。
3. **不扫表**：每次强度更新时预计算「下次跌破阈值的时刻」存进 `due_at`
   （`freeze_due_at`，decay.py:85），`sweep`（store.py:1798）只按索引查
   `active = 1 AND due_at <= now`。sweep 挂在回合路径上
   （services/chat/service.py:1057），不是定时任务——没有对话就没有新召回，
   也就没有需要立刻冻结的东西。
4. **冻结不是删除**：`sweep` 只把 `active` 置 0，事实行、FTS 行、取代链
   都还在；再次被写入或召回命中时 `reinforce` 会把它拉回活跃。
5. **回补是饱和的**：`reinforce(current) = current + 0.35 × (1 - current)`
   （decay.py:104），越牢的事实单次回补越少，强度渐近 1 而永远到不了 1。
   写入侧命中相似事实与召回侧确认采用，都走这同一个函数。
6. **失效只有两个入口**：显式取代（`add_fact` 在同一事务里回填旧行的
   `superseded_by`，store.py:856-870；守卫要求同人物、未被取代、非自身）
   与人工管理（curate.py，标失效用自指哨兵编码，与指向他行的取代链区分）。
   每次失效写一条 `fact_operations` 流水，`prev` 记操作前的值，可撤销。
   同槽冲突不解决：同一 `(person_id, slot)` 下两条活跃事实都保留，由提示词
   并排呈现（store.py:884 的 `slot_conflicts`），写入侧不按时间取新、不按
   分数取高。
7. **「永久保留」是编码不是状态**：pin 就是把半衰期改成 87.6 万小时
   （`PIN_HALF_LIFE_HOURS`，decay.py:38），衰减在人的时间尺度上不再可见，
   facts 表无需为此加列；被召回强化命中时天然安全（`reinforce(1.0)=1.0`）。
8. **量化是存储优化不是语义**：SQ8（quantize.py）把 float32 向量压成带
   自解释头（版本、维度、单向量 scale）的 int8 blob，原 float32 列保留，
   补算是 `embedding_q8 IS NULL` 驱动的幂等扫描。召回打分目前读原向量列。

## 六、联想扩散

联想层（association.py）把三层已有记录映射成统一节点，召回命中后沿边扩散
出「顺带想起」的内容。

1. **节点与边**：`memory_nodes` 是 `(ref_kind, ref_id)` 指向 fact / episode /
   knowledge 的指针表；`memory_edges` 是无向共现边（按较小 ID 归一化存储，
   每对只留一行），强度复用 `reinforce` 的饱和口径，半衰期 720 小时
   （association.py:36）。
2. **建边只发生在「一起被点亮」**：同批写入（fact_extract.py:646），或同一
   次召回里真正进了观察文本（cognition.py:239-243）。被检索到不等于被用到，
   这个区分是边质量的全部来源；读路径绝不顺手建边。
3. **两张图不合图**：`memory_edges` 与 `knowledge_edges` 的节点空间和边权
   语义不同（后者是概念共现计数，可以是几百的整数），`spread`
   （association.py:600）在两图上各自跑个性化 PageRank，概念图的结果按
   「概念出现在哪些记忆正文里」均分回映到记忆节点，两边都得到概率分布后
   才相加。PPR 实现（pagerank.py）带 0.1 秒墙钟超时，超时整次回退到改造前
   的固定跳数扩散并发 `memory_ppr_timeout` 事件。
4. **扩散结果仍受生命周期约束**：邻居与结果都按自身留存度过滤
   （低于 `REVIVE` 的节点不参与），事实取衰减留存度，情节与知识恒按 1.0
   （association.py:227-252）；最后乘进程内短期激活系数
   （`ShortTermActivation`，association.py:166，10 分钟时间常数，不持久化）。
   扩散命中与种子在观察文本里分段呈现（cognition.py:223-237）——混在一起时
   模型会把联想当作确凿记忆复述。
5. **边的衰减走独立低频任务**：边没有 `due_at` 列，全表扫一次的判据以月为
   单位（新边约 54 天不被强化才够得着冻结阈值），为它加一列是过度设计；
   `EdgeDecayService`（services/maintenance/edge_decay.py）启动时先扫一次
   （停机期间边照常衰减），之后每 6 小时轮询。与事实的 sweep 不共用调度点
   的原因写在 edge_decay.py:4-10。

## 七、不在本文范围

- 事实的人工管理界面与撤销链：`src/core/memory/curate.py` 与
  `fact_operations` 流水，本文只在失效入口处提及。
- 反馈纠错链路（`memory_feedback_*` 表、`needs_rebuild` 屏蔽）与黑话、表达
  学习：各自消费 messages 但有自己的文档归属。
- 高频词表 `high_frequency.py` 服务于黑话打分，与召回主链路无关。
