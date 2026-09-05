# scripts —— 校验、维护、评估、迁移与生图

五个分组的独立脚本。与 `src/` 的关系是单向的：脚本 import 业务包复用同一套
实现，业务包从不 import 脚本。会不会写库是分组的第一分界：`check` 与 `eval`
只读，`maintain` 会写库（执行前先备份），`migrate` 只读源库、写目标库，
`sprite` 不碰运行库。

## check —— 只读校验

- `scripts/check/__init__.py`
  包说明：报告是否合规，不写库、不改配置。
- `scripts/check/self_check.py`
  运行时健康检查的命令行入口，与启动路径共用
  `src/core/runtime/self_check.py` 的实现。
- `scripts/check/config_parity.py`
  配置写入器对齐校验：用 Electron 写入器把全默认配置写进临时目录，与
  `src/core/config/schema.py` 各文档模型做字段级差集，任一方向差集非空即
  失败。这条防线源于一次真实漂移——schema 加了字段而模板没跟上，整份重写
  静默毁掉约 30 个字段。
- `scripts/check/config_defaults.ts`
  供上一条调用的辅助：把 Electron 写入器的全默认配置导出到指定目录。

## eval —— 只读跑数

- `scripts/eval/__init__.py`
  包说明：输出可与历史基线对照的数字，可在 Bot 运行期间随时执行；个别脚本
  会调用模型，单独注明。
- `scripts/eval/memory_effect_report.py`
  量化记忆线在真机上的实际效果：库状态与事件账本之外，重点从规划器提示词
  转储里数「本回合真正进了提示词的事实条数」——比「recall 动作被选了几次」
  更接近召回改造的成败。支持按时刻分段对照。
- `scripts/eval/reference_watch.py`
  群聊「回复指向」的效果观察：引用还原、提及显示名、出站引用、目标合规
  四项指标，全部只读自库与事件账本。
- `scripts/eval/shadow_watch.py`
  持续采集 Conversation Agent 的行动决策事件，打印紧凑事件行并生成滚动
  统计报告（解析失败率、非法动作率、reply/silent 分布、延迟分位等）；
  观察当前版本建议固定 `--hash`，避免旧提示词样本混入。
- `scripts/eval/system_perf_watch.py`
  系统级性能监测：按间隔采样整机与关键进程的 CPU / 内存 / GPU 追加写 CSV，
  整机卡死重启后最后几行就是死机前的现场。依赖 psutil，GPU 列依赖
  nvidia-smi。
- `scripts/eval/benchmark_m2_platform_io.py`
  平台入站并发基线：内存 SQLite 加模拟模型与嵌入客户端，测固定注入速率下
  多会话并发的首个文本事件延迟，隔离网络与磁盘因素。
- `scripts/eval/profile_trust_acceptance.py`
  人物画像信任分级的真机副本验收：备份式复制真机库、迁移、全量置脏后跑两轮
  刷新（第二轮应全部走证据指纹短路、不调模型），并对确凿档做 id 级对账。
  第一轮会真实调用 `memory` 模型槽。
- `scripts/eval/retrieval_eval_round2.py`
  真机库副本上的检索评估：先复现旧口径对照历史基线，再按「有留痕 / 无留痕」
  两个样本池跑留痕重放评估，向量融合同生产口径；明细落 JSON。会调用向量
  客户端。

## maintain —— 数据维护（写库，先备份）

- `scripts/maintain/__init__.py`
  包说明：会写 `data/memory.db`，执行前先备份。
- `scripts/maintain/knowledge_reindex.py`
  把迁入的知识变成可检索的：先补全文索引（BM25 路径不受向量开关影响），
  再在开关开启时用当前向量模型重算 embedding——旧向量与新模型不同源，一个
  数值都不许搬。待办集合就是空值行，天然断点续跑。
- `scripts/maintain/vector_quantize.py`
  为存量事实与知识向量补算 SQ8 量化列：只处理已有原向量、缺量化列的行，
  不改写原列、不推进迁移。
- `scripts/maintain/emoji_orphan_cleanup.py`
  清理目录里有、库里无记录的孤儿表情文件，补上启动校验只查「库 → 文件」
  单方向的另一半。默认 `--dry-run` 先看清单，只删文件不写库，幂等。
- `scripts/maintain/fix_expression_style_prefix.py`
  一次性清洗表达词表：历史学习提示词的示例以「使用」开头，模型把前缀学了
  回去，真机库 42% 的条目是指令句式。只改文本不动历史值，撞唯一键的保持
  原样并报告。
- `scripts/maintain/jargon_guard_names.py`
  黑话词表的人名兜底：与身份显示名精确撞名的词条自动降级待定（降级不删除），
  释义里出现人名的只出清单交人工——判断错了会误伤真黑话。与运行时侧的
  受保护名单（`src/core/agent/jargon.py`）互不替代。

## migrate —— 离线历史资产迁移

- `scripts/migrate/__init__.py`
  包说明：源库只读（`mode=ro` 打开），目标库必须已存在且具备当前表结构，
  脚本不替运行时建库或升级库。
- `scripts/migrate/common.py`
  共用设施：数据库打开与安全校验、会话 / 身份映射装载、字段转换、跳过
  计数（已存在、字段为空、会话未映射等）与命令行骨架。
- `scripts/migrate/episodes.py`
  把历史聊天概括迁入情节表与线索索引。
- `scripts/migrate/expressions.py`
  把已审核的历史表达方式迁入表达词表。
- `scripts/migrate/jargon.py`
  把已确认且有释义的历史黑话迁入黑话表。
- `scripts/migrate/m4_knowledge.py`
  把旧库的知识正文与图谱结构迁入当前库，三个子步骤必须按顺序：正文（向量
  留空待重算）、节点按概念去重、边按概念名解析成新 id。幂等靠内容键不靠
  游标，支持 dry-run 与断点续跑。
- `scripts/migrate/profile_seeds.py`
  把历史人物印象作为待刷新的画像种子迁入，刷新交给运行时的画像派生链路。

## sprite —— 生图管线

从参考图跑出整套角色立绘差分素材的独立工具链，不碰运行库；完整流程、配置
与命令传参约定见 `scripts/sprite/README.md`。素材产物的运行时消费方是
`electron/renderer/character/`，共享类型在 `electron/shared/sprite-manifest.ts`。

- `scripts/sprite/config.ts`
  表情清单与生成指令模板：表情 id、中文展示名、生成描述、Live2D 映射与
  别名集中于此；生成脚本与渲染层共享同一组标识，保证同一表情在不同渲染
  实现中语义一致。
- `scripts/sprite/paths.ts`
  统一解析工作目录、输入文件与输出清单路径，避免各脚本把中间产物写散。
- `scripts/sprite/base.ts`
  底图生成与定稿：支持多参考图生成候选、单独选择定稿；底图构图决定角色
  一致性上限。
- `scripts/sprite/gen.ts`
  批量生成表情差分：并发受限、429 重试、state.json 断点续跑，预览复核
  清单里的问题会作为强化指令再次提交。
- `scripts/sprite/process.ts`
  后处理：抠图去背景、按主体包围盒统一缩放与锚点、像素差异定位眼睛与嘴部、
  表情脸部合成到底图并产出 manifest。对齐是关键——不校正生成偏移，运行时
  切表情会跳动。
- `scripts/sprite/consistency.ts`
  一致性检查：量化两张图之间的位移、缩放与局部像素差异，分区域报告，让
  「自动对齐可修复的偏差」不被误判为素材质量问题。
- `scripts/sprite/preview.ts`
  本机预览服务：展示素材、比较差异、维护审核清单（重生成项的来源）。
- `scripts/sprite/manifest.ts`
  素材清单的读写：登记底图、表情与动作文件，渲染层用同一份 JSON 定位资源。
- `scripts/sprite/diagnose.ts`
  生图接口诊断：代理、Key、模型可用性三步分层报告，区分网络、认证与模型
  问题。
- `scripts/sprite/fixture.ts`
  生成本地合成素材，不调外部 API 即可验证后处理链路。
- `scripts/sprite/test.ts`
  冒烟测试：Key、网络、模型、出图四件事，失败给人话诊断。
- `scripts/sprite/providers/types.ts` / `scripts/sprite/providers/http.ts` /
  `scripts/sprite/providers/image.ts` / `scripts/sprite/providers/proxy.ts`
  供应商层的公共件：统一输入输出与错误分类接口、HTTP 状态分类与有限重试、
  图像格式归一化（统一 PNG）、全局代理安装。
- `scripts/sprite/providers/gemini.ts` / `scripts/sprite/providers/seedream.ts` /
  `scripts/sprite/providers/index.ts`
  两个图像供应商适配（各自负责请求形态与响应解析，不自动重试）与按环境
  变量组装具体供应商的工厂函数。

## 依赖方向

- 五个分组都单向依赖 `src/`：`check` 用 `config` 与 `runtime`，`eval` 用
  `memory`、`observe`、`services` 与 `llm_models`，`maintain` 用 `memory`
  与 `services/media`，`migrate` 用 `memory.store`，`sprite` 只依赖
  `electron/shared` 的类型而不进 Python 侧。反向依赖不存在。
- `maintain` 与 `migrate` 的写库纪律不同：前者写运行库且先备份，后者只写
  指定的目标库、源库以只读 URI 打开。除 `profile_trust_acceptance` 与
  `retrieval_eval_round2`（各自注明）外，`eval` 全组不调模型。
- 两个脚本的文件名里带有历史阶段的编号痕迹
  （`scripts/eval/benchmark_m2_platform_io.py`、`scripts/migrate/m4_knowledge.py`），
  属于既有路径，改名不在文档任务范围内。
