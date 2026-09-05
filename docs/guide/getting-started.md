# 从零跑起来

这一篇把「clone 下来」到「她回你第一句话」拆成五步，每步都写清楚**你应该看到什么**，
以及**没看到的话先查哪里**。照着走一遍大约十分钟，其中大部分时间在等依赖装完。

如果你只想先看看配置长什么样，不必 clone：[`config.example/`](../../config.example/README.md)
就是首次运行会生成的那一份，每个字段都带中文说明。

## 你需要先准备什么

| 必需 | 说明 |
| :--- | :--- |
| Python 3.11+ | 后端本体。推荐用 [uv](https://docs.astral.sh/uv/) 管理，下面的命令都基于它 |
| 一个能用的模型 API | 任何 OpenAI 兼容的服务都行。你需要它的 `base_url` 和 `api_key` |
| Node.js | **只有要桌宠或管理面板才需要**。只跑 QQ 的话可以完全不装 |
| Windows 10/11 | **只有桌宠需要**。后端本身在 Linux 服务器上跑得好好的 |

先想清楚你要哪条出口——这决定了后面要不要装 Node、要不要装协议端：

- **桌面上的她**：需要 Node 与 Windows。
- **QQ 上的她**：需要另外安装一个 QQ 协议端，见 [QQ 与群聊接入](qq-setup.md)。
- **只想先确认能跑**：两样都不用装，第五步有一条命令行的验证路径。

## 第一步：装依赖

```bash
uv sync
```

只跑 QQ 与管理面板的话，到这里就够了。要桌宠或管理面板界面，再来一条：

```bash
npm install
```

**你应该看到**：`uv sync` 结束时打印装了多少个包，仓库根出现 `.venv/`。

**没看到的话**：`uv` 不在 PATH 上，或 Python 版本低于 3.11。`uv python list` 看一眼
它找到了哪些解释器。

## 第二步：第一次启动，让它把配置生成出来

```bash
uv run bot.py
```

这一次**它会主动停下来**，这是正常的，不是报错。

**你应该看到**：`config/` 目录被创建，里面出现五个 `.toml`（`adapter` `bot` `features`
`models` `providers`），控制台用中文列出还差哪几项，然后进程退出。类似：

```
配置已生成，但还差这些才能启动：
  - bot.toml 的 [bot] name：她叫什么
  - models.toml 的 [[models]] model_identifier，以及 providers.toml 的
    [[api_providers]] base_url、api_key：对话任务至少要有一条填好的模型连接
```

**没看到的话**：

- 报权限错误——检查仓库目录可写。
- 如果你开着桌宠：外壳会拒绝把运行时根目录解析到 C 盘（避免运行数据写进系统盘），
  把仓库挪到别的盘，或用 `YUELI_PROJECT_ROOT` 指定其它位置。无头运行没有这道限制。

## 第三步：把那两处填上

用任何文本编辑器打开 `config/` 下的文件。**每个字段都带一行中文说明**，不用去翻文档。

`config/bot.toml`：

```toml
[bot]
name = "月璃"     # 改成你想让她叫的名字
```

`config/providers.toml`：

```toml
[[api_providers]]
name = "主力"                          # 这个名字下一步要引用，改了两处要一起改
base_url = "https://.../v1"            # 你的 API 端点
api_key = "填你的密钥"
```

`config/models.toml`：

```toml
[[models]]
name = "chat"
api_provider = "主力"                  # 必须与 providers.toml 里的 name 完全一致
model_identifier = "填模型 ID"          # 例如 deepseek-chat
```

> **这三份之间的引用是单向的**：模型条目通过 `api_provider` 指向厂商名，任务再引用
> 一串模型名作为候选。改名字要顺着这条链一起改，否则启动时会直接报错退出——加载期
> 做完整的交叉校验，不会带着一个悬空引用继续跑。

## 第四步：再启动一次

```bash
uv run bot.py
```

**你应该看到**：控制台先滚一批启动日志，然后出现一个信息框：

```
╭─ WebUI 已就绪 ─────────────────────────────────────╮
│ 地址：http://127.0.0.1:7999                        │
│ 登录 token：<一串 64 位十六进制>                    │
│ token 每次启动重新生成，也可从 ...backend.json 读取 │
╰────────────────────────────────────────────────────╯
```

看到这个框就说明后端起来了。浏览器打开那个地址，用框里的 token 登录，就能进
[管理面板](webui.md)。

**没看到的话**：

- 停在某个「服务正在启动 名称：xxx」不动——某个后台服务的启动钩子没返回。
  这是需要报 issue 的情况，请附上卡住的那一行。
- 报端口被占用——`bot.py` 支持 `--port` 改端口。
- 面板打开是「尚未构建」——前端产物没生成，跑一次 `npm run build`。API 和 QQ
  不受影响，只是没有界面。

## 第五步：跟她说第一句话

**管理面板里没有对话框**——它是观察和配置用的。真正说话有三条路：

### A. 命令行验证（不装任何额外东西）

后端在跑的情况下，另开一个终端：

```bash
curl -X POST http://127.0.0.1:7999/chat/send \
  -H "Authorization: Bearer <上面那个 token>" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好"}'
```

**你应该看到**：这条命令返回 `{"accepted":true}`，然后**回后端那个终端看**——
回复不走 HTTP 响应，它通过 WebSocket 推给桌面端，同时在控制台打出分层的回合面板：
这一轮调了哪几级模型、每一级的输入输出。她说了什么就在那里面。

管理面板的「会话观察」页也能看到同一轮的完整链路。

### B. 桌宠

确认 `config/bot.toml` 里 `[desktop_pet] enabled = true`，然后 `uv run bot.py`
会自动把 Electron 外壳拉起来。点她开合输入栏，直接打字。

### C. QQ

见 [QQ 与群聊接入](qq-setup.md)。需要先装一个协议端，步骤比前两条长。

## 接下来

- 想让她住在桌面上：`[desktop_pet] enabled = true`，见 [安装与配置](install.md)
- 想接 QQ 和群聊：[QQ 与群聊接入](qq-setup.md)
- 想放到服务器上跑：[无头部署](headless.md)
- 想调她的性格、记忆策略、回复频率：管理面板的「月璃设置」页，或直接编辑 `config/bot.toml`
- 出问题了：[常见问题](troubleshooting.md)
