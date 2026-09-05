# 从零跑起来

三条命令，填一个 API Key。第一次跑通大约十分钟，大半在等依赖安装。

**要准备的**：Python 3.11+（推荐用 [uv](https://docs.astral.sh/uv/) 管理）、一个百炼（阿里云 DashScope）的 API Key——配置已按百炼预填，用它只差这一个 Key；换其他厂商见[安装与配置](install.md)。桌宠另需 Node.js 和 Windows 10/11；只用 QQ 或管理面板不需要。

## 1. 装依赖

```bash
uv sync                    # 后端本体
npm install                # 桌宠与管理面板前端，都不要可跳过
```

两条命令跑完不报错即完成。

## 2. 首次启动，让它生成配置

```bash
uv run bot.py
```

**它生成 `config/` 后会主动停下来，这是正常流程**，不是报错。控制台用中文说明还差什么，看到这段提示即完成。

## 3. 填 API Key

打开 `config/providers.toml`，唯一要填的一项：

```toml
api_key = ""    # 填你的密钥
```

厂商地址、六个模型条目、各任务的分档都已预填，六个模型走同一条百炼连接；名字默认「月璃」，不用改。字段逐个的说明见 [`config.example/`](../../../config.example/README.md)。保存即完成。

## 4. 再跑一次

```bash
uv run bot.py
```

看到「WebUI 已就绪」信息框即完成。浏览器打开框里的地址进[管理面板](../webui/index.md)——**本机访问自动登录**，不用手输 token。

## 5. 说第一句话

**管理面板里没有对话框**，它是观察和配置入口。真正说话有三条路：

**命令行**（不装任何额外东西，最快验证）。后端保持运行，另开一个终端：

```bash
curl -X POST http://127.0.0.1:7999/chat/send \
  -H "Authorization: Bearer <信息框里的 token>" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好"}'
```

它只返回 `{"accepted":true}`——**回复不在 HTTP 响应里**，经 WebSocket 推送，同时打在后端终端的回合面板上，管理面板的「会话观察」页也能翻。看到她的回复即完成。

**桌宠**：`config/bot.toml` 里 `[desktop_pet] enabled = true`，重新启动即可。

**QQ**：先装一个协议端，见 [QQ 与群聊接入](../adapters/index.md)。

## 卡住了

按症状查[常见问题](../troubleshooting.md)。
