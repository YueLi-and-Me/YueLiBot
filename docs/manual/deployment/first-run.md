# 从零跑起来

完成依赖安装和 API Key 配置后，验证基础对话功能。

**环境与凭据**：Python 3.11+、uv、百炼 API Key；更换厂商见[安装与配置](install.md)。本机构建面板需 Node.js，桌宠另需 Windows 10/11；服务器可部署已构建的面板。

## 安装依赖

```bash
uv sync                    # 后端本体
npm ci                    # 安装前端依赖；只用命令行或 QQ 可跳过
npm run build             # 构建管理面板与桌宠
```

## 首次启动并生成配置

```bash
uv run bot.py
```

**第一次会先要求你同意用户协议**：控制台打印协议要点与正文位置，逐字输入「同意」
才继续。协议讲的是接入 QQ 的账号风险、程序会替你存群成员的哪些信息、以及对话内容
会发给谁——正文在仓库根目录的 [`AGREEMENT.md`](../../../AGREEMENT.md)。
同意一次即可，记录写在数据目录的 `consent.json`。

同意之后会生成 `config/` 并退出，属于正常初始化流程。控制台列出待填写的配置项；
出现该提示表示配置文件已生成。

## 配置 API Key

打开 `config/providers.toml`，填写唯一必需的凭据字段：

```toml
api_key = ""    # 填写实际密钥
```

厂商地址、六个模型条目和任务分档均已预填，六个模型共用百炼连接；默认名称为「月璃」。字段说明见 [`config.example/`](../../../config.example/README.md)。保存即完成。

## 重新启动

```bash
uv run bot.py
```

出现「WebUI 已就绪」信息框表示后端监听已建立。通过浏览器打开所示地址进入[管理面板](../webui/index.md)；本机访问自动登录，无需手动输入 token。

## 发送首条消息

管理面板提供观察与配置功能，不包含对话输入框。可通过以下三种方式发送消息：

**命令行**：后端保持运行，另开 Bash 终端（Windows 可用 Git Bash），替换 token 后执行：

```bash
curl -X POST http://127.0.0.1:7999/chat/send \
  -H "Authorization: Bearer <信息框里的 token>" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好"}'
```

请求接收成功后返回 `{"accepted":true}`，HTTP 响应不包含回复正文。回复通过 WebSocket 推送，并显示在后端终端及管理面板的「会话观察」页；确认正文后即完成基础验证。

**桌宠**：依赖就绪后，在 `config/bot.toml` 里设 `[desktop_pet] enabled = true` 并重启，详见 [Windows 上带桌宠](windows.md)。

**QQ**：须独立安装协议端，具体要求见 [QQ 与群聊接入](../adapters/index.md)。

## 故障排查

按故障现象查阅[常见问题](../troubleshooting.md)。
