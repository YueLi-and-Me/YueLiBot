# 从零跑起来

装好依赖，填一个 API Key，先验证一轮对话。

**要准备的**：Python 3.11+、uv、百炼 API Key；换厂商见[安装与配置](install.md)。本机构建面板需 Node.js，桌宠另需 Windows 10/11；服务器可接收已构建的面板。

## 装依赖

```bash
uv sync                    # 后端本体
npm ci                    # 安装前端依赖；只用命令行或 QQ 可跳过
npm run build             # 构建管理面板与桌宠
```

## 首次启动，让它生成配置

```bash
uv run bot.py
```

**它生成 `config/` 后会主动停下来，这是正常流程**，不是报错。控制台用中文说明还差什么，看到这段提示即完成。

## 填 API Key

打开 `config/providers.toml`，唯一要填的一项：

```toml
api_key = ""    # 填你的密钥
```

厂商地址、六个模型条目、各任务的分档都已预填，六个模型走同一条百炼连接；名字默认「月璃」，不用改。字段逐个的说明见 [`config.example/`](../../../config.example/README.md)。保存即完成。

## 再跑一次

```bash
uv run bot.py
```

看到「WebUI 已就绪」信息框即完成。浏览器打开框里的地址进[管理面板](../webui/index.md)——**本机访问自动登录**，不用手输 token。

## 说第一句话

**管理面板里没有对话框**，它是观察和配置入口。真正说话有三条路：

**命令行**：后端保持运行，另开 Bash 终端（Windows 可用 Git Bash），替换 token 后执行：

```bash
curl -X POST http://127.0.0.1:7999/chat/send \
  -H "Authorization: Bearer <信息框里的 token>" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好"}'
```

它只返回 `{"accepted":true}`——**回复不在 HTTP 响应里**，经 WebSocket 推送，同时打在后端终端的回合面板上，管理面板的「会话观察」页也能翻。看到她的回复即完成。

**桌宠**：依赖就绪后，在 `config/bot.toml` 里设 `[desktop_pet] enabled = true` 并重启，详见 [Windows 上带桌宠](windows.md)。

**QQ**：先装一个协议端，见 [QQ 与群聊接入](../adapters/index.md)。

## 卡住了

按症状查[常见问题](../troubleshooting.md)。
