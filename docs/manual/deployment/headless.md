# 无头部署

服务器上只跑 QQ 与管理面板、不要桌宠时的部署方式，含 systemd 单元与两个必须知道的前提。

服务器上只跑 QQ 与管理面板，不要桌宠。进程入口本来就是 Python，所以这条路上没有 Electron：

```bash
uv sync
uv run bot.py
```

第一次运行会生成一份初始配置然后退出，控制台列出还差哪几项——**通常只有一项：
`providers.toml` 的 `api_key`**。厂商地址与六个模型条目已按阿里云百炼的 OpenAI 兼容
端点预填并分好档，换厂商才需要连着改。生成的 TOML 每个字段都带中文说明，直接编辑
就行。填好后再启动一次即可。
确认 `bot.toml` 的 `[desktop_pet] enabled = false`（默认就是），入口便不会去找 Electron；
QQ 适配器仍由它拉起和监护。终端里 `Ctrl+C` 走完整收尾，`SIGTERM` 同理，适合交给 systemd：

```ini
[Service]
WorkingDirectory=/opt/yueli
ExecStart=/opt/yueli/.venv/bin/python bot.py
Restart=on-failure
KillSignal=SIGTERM
TimeoutStopSec=60
```

两件事要知道：

- **管理面板需要构建一次**。它是前端产物，`npm run build` 会写到 `out/webui`；没有这一步时
  面板页面会明说「尚未构建」，API 与 QQ 不受影响。不想在服务器上装 Node，就在别处构建后把
  `out/webui` 拷过去，或者干脆只编辑 TOML。
- **面板只监听 `127.0.0.1`，且不打算改**。远程访问请走 SSH 端口转发
  （`ssh -L 7999:127.0.0.1:7999 <主机>`），不要把它直接暴露到公网。
