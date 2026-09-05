# 从零跑起来

三条命令，填一个 API Key。十分钟，大半时间在等依赖装完。

**要准备的**：Python 3.11+（推荐用 [uv](https://docs.astral.sh/uv/) 管理）、一个 OpenAI 兼容的
模型 API（要它的 `api_key`）。桌宠另需 Node.js 与 Windows 10/11；只跑 QQ 或管理面板都不需要。

## 1. 装依赖

```bash
uv sync                    # 后端本体
npm install                # 桌宠与管理面板前端，都不要就跳过
```

## 2. 跑一次，让它生成配置

```bash
uv run bot.py
```

**它会生成 `config/` 然后停下来，这是正常流程**，不是报错。控制台会用中文告诉你还差什么。

## 3. 填 API Key

打开 `config/providers.toml`，只有这一项要填：

```toml
api_key = ""    # 填你自己的密钥
```

厂商地址、六个模型条目、各任务的分档、她的名字都已预填。想在填之前看清全貌，
不必翻代码：[`config.example/`](../../config.example/README.md) 就是这一份，每个字段都带说明。

换别的厂商、调整任务用哪个模型，见[安装与配置](install.md)。

## 4. 再跑一次

```bash
uv run bot.py
```

看到「WebUI 已就绪」的信息框就成了。浏览器打开框里的地址进[管理面板](webui.md)——
**本机访问自动登录**，不用手输 token。

## 5. 跟她说第一句话

**管理面板里没有对话框**，它是观察和配置用的。三条路：

**最快验证**（不装任何额外东西）。后端跑着，另开一个终端：

```bash
curl -X POST http://127.0.0.1:7999/chat/send \
  -H "Authorization: Bearer <框里那个 token>" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好"}'
```

这条命令只返回 `{"accepted":true}`。**她说的话不在 HTTP 响应里**——回复通过 WebSocket
推送，同时打在后端终端的回合面板上，管理面板的「会话观察」页也能翻。

**桌宠**：`config/bot.toml` 里 `[desktop_pet] enabled = true`，再 `uv run bot.py` 即可。

**QQ**：见 [QQ 与群聊接入](qq-setup.md)，需要先装一个协议端。

## 卡住了

见[常见问题](troubleshooting.md)，按症状查。
