# 配置模板

这一份是**首次运行时会自动生成的配置的副本**，放在这里是为了让人在 clone 之前就能看清
要准备什么。你不需要手动拷贝它：直接 `uv run bot.py`，程序会在 `config/` 生成同样一份，
然后停下来告诉你还差哪几项。

| 文件 | 落到哪里 | 内容 |
| :--- | :--- | :--- |
| `adapter.toml` | `config/adapter.toml` | 当前启用哪个 QQ 适配器（写 `adapters/` 下的插件目录名） |
| `bot.toml` | `config/bot.toml` | 她叫什么、用户关系、人格提示词、会话记忆策略、群聊触发 |
| `features.toml` | `config/features.toml` | 语音、视觉、向量召回、感知出口、调试开关 |
| `models.toml` | `config/models.toml` | 模型条目与各任务的候选清单、温度和输出上限 |
| `providers.toml` | `config/providers.toml` | API 厂商、地址、密钥、超时与重试 |
| `adapters/<插件名>.toml` | `adapters/<插件名>/config.toml` | 协议端连接参数、owner 身份、私聊与群聊名单 |

注意最后一行的落点：适配器的连接配置和插件源码同目录，**不在 `config/` 下**。模板里改成
按插件名平铺，是为了让两种协议端各要填什么一眼可见；真实安装只会生成当前启用的那一个。

**最少要填的只有一处**：`providers.toml` 的 `api_key`。

厂商地址、六个模型条目与各任务的分档都已预填：对话与回复生成走质量档，决策、摘要与
情景分析走快档，表达选择用最便宜的一档，记忆与日程要长上下文，视觉与嵌入各有专用
模型；语音合成留空（它需要专门的语音厂商）。所有模型的 `extra_body` 都写了
`enable_thinking = false`，默认关闭思考。

六个模型全部走同一条连接——DeepSeek 系列也由百炼托管，不需要单独开账号。换别的厂商
要改 `providers.toml` 的 `kind` 与 `base_url`，再把各模型条目的 `model_identifier`
换成那家接受的真实 ID。

接 QQ 还要填适配器那份，见[QQ 与群聊接入](../docs/manual/adapters/index.md)。

> 本目录由 `src.core.config.bootstrap.render_example_configs` 从 Pydantic schema 渲染，
> 与首次运行走同一套代码和同一个写入器。**不要手改这里的文件**——改了会被
> `pytests/core/test_config_example.py` 判红。字段变了就重新生成：
>
> ```bash
> uv run python -c "from pathlib import Path; from src.core.config.bootstrap import render_example_configs; render_example_configs(Path('config.example'))"
> ```
