# core/config —— 配置体系实现层

配置的类型定义、读取、校验、写入与升级。持久化层是四份版本化 TOML
（providers.toml、models.toml、bot.toml、features.toml），加载器把它们组合成
一个运行时视图 `Config` 交给业务服务——业务代码不需要知道磁盘布局。

## 职责边界

- 负责：Pydantic 配置模型与校验、TOML 读取与版本校验、启动期升级对账、
  首装生成、管理面板的配置读写端点、当前启用的 QQ 适配器声明。
- 不负责：QQ 适配器自身的连接协议字段（模型在 `src/platforms/onebot11/config.py`，
  本包只负责定位文件与段名）、配置里的密钥安全边界（明文落盘到本机，
  WebUI 读取时密钥不回传明文）。

## 目录内容

- `src/core/config/__init__.py`
  包出口：`Config` 与 `load_config` / `get_config` / `reset_config`。
- `src/core/config/schema.py`
  全部 Pydantic 配置模型（约 35 个类）：四份 TOML 各自的文档模型
  （`BotDocument`、`FeatureDocument`、`ProviderCatalog`、`ModelCatalog`）、
  按域拆分的子配置（群聊策略、输入指示器、日程、人格、表情包、TTS、视觉、
  向量、反馈纠错、日志、开发者等），以及组合视图 `Config`。启动时一次性校验，
  字段缺失或类型错误在任何业务逻辑开始前就报错。
- `src/core/config/loader.py`
  `load_config` 读四份 TOML 组合成 `Config`：先校验厂商、模型与任务的引用
  完整性，再解析任务候选与功能开关；解析模型路由（一个任务一串候选模型加
  轮询策略）。提供全局 `get_config`、`reload_config` 与重载监听注册。
- `src/core/config/toml_io.py`
  `read_versioned_toml`：读取主体与适配器共用的版本化 TOML，版本不匹配时在
  解析业务字段之前直接失败，并给出面向用户的修复提示。
- `src/core/config/upgrade.py`
  `upgrade_config_directory`，由 `src.main` 在解析之前调用。解决版本升级的两个
  沉默问题：新增字段用户不可见、废弃字段留在文件里看似生效。口径是新增字段只
  追加不改写（已有行、注释、顺序不动），废弃字段就地删除并展示实际删除的路径；
  字段对账成功后再把 `[inner].version` 改写成当前 `CONFIG_VERSION`（版本号最后
  写，避免留下版本已新、字段还旧的配置）；写任何字节之前先整目录备份到
  `data/backups/config/<时间戳>/`。
- `src/core/config/bootstrap.py`
  首装生成。`bootstrap_config_directory` 在配置目录缺失时按 schema 默认值生成
  整份可编辑配置——`config/` 不入版本库，无头形态下没有它根本起不来。默认值
  取自 `schema.py` 的 Pydantic 默认（不是第二份默认值表），并预填一条模型厂商
  连接与六个分档模型。产出是故意不完整的：api_key 只能由人填，生成的
  providers.toml 连自身校验都过不了，调用方应当停下来提示用户。
  `render_example_configs` 用同一套写入器渲染随代码分发的 `config.example/`，
  `pytests/core/test_config_example.py` 守着它不漂移——那个目录不要手改。
  `missing_startup_requirements` 报告还缺哪些人工必填项。
- `src/core/config/adapter_selection.py`
  `read_active_adapter` 及配套：把「当前启用哪个适配器」收敛成
  `config/adapter.toml` 一处声明（两个协议端互斥），Electron 与主体各自读同一份
  事实。提供插件目录名、连接配置路径与清单声明的连接段名；清单解析复用
  `src/plugin_system` 的加载器，不重复解析 `_manifest.json`。
- `src/core/config/settings_schema.json`
  声明式 schema（版本 1）：每个字段的 key、类型、中文标签、说明与文件布局，
  是 WebUI 设置页的映射真源。字段说明的最终出处在这里，手改 TOML 注释会在
  下次保存时被覆盖。
- `src/core/config/settings_webui.py`
  管理面板设置页的读写层。读取时用 Pydantic 模型补齐默认值并隐藏密钥；保存
  先写临时文件、通过完整启动校验后再原子替换。QQ 适配器的连接配置属于适配器
  自身（`adapters/<当前适配器>/config.toml`），由 `adapter_selection` 定位、按
  清单声明的段名读写；在 schema 里以稳定逻辑名 `adapter.toml` 出现。
- `src/core/config/model_webui.py`
  管理面板模型工作台的读写层：providers.toml / models.toml 与表单 JSON 快照的
  双向转换，保存时先落临时文件、复用既有密钥与引用校验再原子替换；
  `test_connection` / `list_models` 只读上游接口探测连通性与模型列表，不改配置。

## 对外接口与调用方

- `Config` 与 `load_config` / `get_config`：全库最广的依赖，业务服务、agent、
  记忆、平台层都在装配时拿配置对象；进程入口 `src/main.py` 调 `load_config`
  并持有全局实例。
- 启动序：`src/main.py` 先 `bootstrap_config_directory`（缺失才建），报告
  `missing_startup_requirements`，再走 `load_config`（内部先
  `upgrade_config_directory` 对账）。
- `settings_webui` / `model_webui`：被 `src/core/api/settings_config.py` 与
  `src/core/api/model_config.py` 的 HTTP 端点调用，是管理面板配置页的后端。
- `adapter_selection`：除包内使用外，被 `src/core/services/host/adapter_host.py`
  （按声明拉起适配器进程）与 `src/main.py` 读取。
- 写入端有两个：管理面板（本包，schema 驱动）与 Electron 设置窗口
  （`electron/main/config.ts`，自带写入器与版本升级记录，见
  [electron](electron.md)）。两个写入器必须字段级一致，
  `scripts/check/config_parity.py` 守这条：任一方向出现差集即失败。

## 依赖方向

- 依赖：`schema` / `loader` 依赖 `toml_io`；`settings_webui`、`model_webui`、
  `bootstrap` 依赖 `adapter_selection` 与 `loader`；全包依赖
  `src/core/logging`。指向包外的引用有三处——`adapter_selection` 引
  `src/plugin_system`（清单解析）、`bootstrap` 与 `settings_webui` 引
  `src/platforms/onebot11/config.py`（适配器文档模型与版本常量）、`loader` 引
  `src/core/llm_models/openai.py` 的 `resolve_base_url`（厂商预设地址归一化）。
- 被依赖：几乎全部业务包。反向不允许：业务模块读 `get_config()`，
  不允许在业务模块里直接解析 TOML 或绕过 schema 写配置。
- 平台适配器的配置模型放在协议侧而不是本包，本包只借它的文档模型拼装
  面板表单——协议字段随协议版本演进，不进主体 schema。
