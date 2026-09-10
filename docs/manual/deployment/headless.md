# 无头部署

服务器仅运行 QQ 与管理面板时，使用 Python 入口，无需桌宠外壳。
QQ 协议端独立运行；月璃负责启动适配器，见[接入总览](../adapters/index.md)。
下文以项目放在 `/opt/yueli`、专用运行用户为 `yueli` 为例。

## 安装与首次配置

在项目根目录运行：

```bash
uv sync
uv run bot.py
```

**首次启动要先接受用户协议。** 无头环境无法在终端询问，用 `--accept-agreement`
接受一次：

```bash
uv run bot.py --accept-agreement
```

协议正文在仓库根目录的 [`AGREEMENT.md`](https://github.com/YueLi-and-Me/YueLiBot/blob/main/AGREEMENT.md)，接受前请先读。
记录写在数据目录的 `consent.json`，之后照常 `uv run bot.py` 启动。
交给 systemd 之前必须先完成这一步，否则服务会在启动时退出。

接受之后首次生成 `config/` 会退出，先完成终端列出的待填项。
默认百炼连接通常仅需补充 `providers.toml` 的 `api_key`。
换厂商需同步修改模型引用，见[配置总览](../configuration/index.md)。
确认 `bot.toml` 中 `[desktop_pet] enabled = false`，再次启动。
确认「WebUI 已就绪」后，可将前台进程改为服务托管。
服务托管前须完成首次配置，避免因配置缺失导致反复重启。

## 部署面板构建产物

在另一台构建机检出与服务器相同版本的代码，在根目录执行：

```bash
npm ci
npm run build
```

将完整的 `out/webui` 目录复制到服务器项目的 `out/` 下，连同其下的资源目录一起。
拷完的结果应当是 `/opt/yueli/out/webui/index.html`——注意别多套一层，
拷成 `out/webui/webui/` 面板就打不开。
例如在有 SSH 工具的构建机运行：

```bash
ssh yueli@server 'mkdir -p /opt/yueli/out'
scp -r out/webui yueli@server:/opt/yueli/out/
```

`server` 替换成服务器主机名；目标目录须归运行用户可读。
面板静态资源可跨平台复制，Python 环境和桌宠依赖不在复制范围内。
构建脚本见 [package.json](https://github.com/YueLi-and-Me/YueLiBot/blob/main/package.json)，输出位置见[面板构建配置](https://github.com/YueLi-and-Me/YueLiBot/blob/main/webui/vite.config.ts)。
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

journal 包含标准输出、错误与启动入口框；对外提供日志前须移除其中的 token。
默认文件日志位于 `data/logs/app_*.log.jsonl`，受数量、大小和保留天数限制。
模型失败请求在 `data/logs/llm_request/`，调用记录在 `data/logs/prompt/`。
使用 `--data-dir` 时，以上日志均位于指定数据目录内。

后端只监听 `127.0.0.1`，从本机建立隧道：

```bash
ssh -L 7999:127.0.0.1:7999 yueli@server
```

保持 SSH 连接，用浏览器打开本机 `127.0.0.1:7999`。
若后端使用其他端口，同步修改转发目标。
停止服务使用 `sudo systemctl stop yueli`，`SIGTERM` 会触发完整收尾。
升级前先停止服务，按[升级与回退](upgrade.md)保存配置与数据。
