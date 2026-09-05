# 无头部署

服务器只运行 QQ 与管理面板时，Python 就是入口，不需要桌宠外壳。
QQ 协议端独立运行；月璃负责拉起适配器，见[接入总览](../adapters/index.md)。
下文以项目放在 `/opt/yueli`、专用运行用户为 `yueli` 为例。

## 安装与首次配置

在项目根目录运行：

```bash
uv sync
uv run bot.py
```

首次生成 `config/` 后会退出，先完成终端列出的待填项。
默认百炼连接通常只差 `providers.toml` 的 `api_key`。
换厂商需同步修改模型引用，见[配置总览](../configuration/index.md)。
确认 `bot.toml` 中 `[desktop_pet] enabled = false`，再次启动。
看到「WebUI 已就绪」后，再把前台运行改为服务托管。
不要让首次缺配置的退出一直被服务管理器重启。

## 把面板构建产物带到服务器

在另一台构建机检出与服务器相同版本的代码，在根目录执行：

```bash
npm ci
npm run build
```

把整个 `out/webui` 目录复制到服务器项目的 `out/` 下。
目标应为 `/opt/yueli/out/webui/index.html`，其下资源目录也必须完整。
不能只复制 `index.html`，也不要多套一层 `webui/webui`。
例如在有 SSH 工具的构建机运行：

```bash
ssh yueli@server 'mkdir -p /opt/yueli/out'
scp -r out/webui yueli@server:/opt/yueli/out/
```

`server` 替换成服务器主机名；目标目录须归运行用户可读。
面板静态资源可跨平台复制，Python 环境和桌宠依赖不在复制范围内。
构建脚本见 [package.json](../../../package.json)，输出位置见[面板构建配置](../../../webui/vite.config.ts)。
访问页面并切换一个页面，确认脚本和样式都加载成功。
未放入产物时页面会提示「尚未构建」，API 与 QQ 仍能运行。

## 使用 systemd 托管

先创建运行用户，并授予项目、配置、数据目录相应读写权限。
将下面内容保存到 `/etc/systemd/system/yueli.service`，按实际路径改写：

```ini
[Unit]
Description=YueLiBot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=yueli
WorkingDirectory=/opt/yueli
ExecStart=/opt/yueli/.venv/bin/python /opt/yueli/bot.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

首次 `uv sync` 已创建项目解释器；服务直接使用它，不依赖服务账户的 PATH。
自定义配置或数据目录时，把对应启动参数追加到 `ExecStart`。
运行用户也要能读取插件目录里的连接配置。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yueli
systemctl status yueli --no-pager
```

## 日志与远程访问

```bash
journalctl -u yueli -n 100 --no-pager
journalctl -u yueli -f
```

journal 包含标准输出、错误与启动入口框；分享前去掉其中的 token。
默认文件日志位于 `data/logs/app_*.log.jsonl`，受数量、大小和保留天数限制。
模型失败请求在 `data/logs/llm_request/`，调用记录在 `data/logs/prompt/`。
改过 `--data-dir` 时，以上日志都跟随新数据目录。

后端只监听 `127.0.0.1`，从本机建立隧道：

```bash
ssh -L 7999:127.0.0.1:7999 yueli@server
```

保持 SSH 连接，用浏览器打开本机 `127.0.0.1:7999`。
若后端使用其他端口，同步修改转发目标。
停止服务使用 `sudo systemctl stop yueli`，`SIGTERM` 会触发完整收尾。
升级前先停止服务，按[升级与回退](upgrade.md)保存配置与数据。
