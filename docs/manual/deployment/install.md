# 安装与配置

本文是依赖、配置和启动方式的完整参考。
基础对话验证流程见[从零跑起来](first-run.md)。
以下命令均在解压或检出后的项目根目录执行。

## 环境与依赖

后端需要 Python 3.11 或更新版本，依赖由 uv 按项目声明安装。
桌宠需要 Windows 10/11 与 Node.js；服务器只运行后端时不需要图形环境。
管理面板需要前端构建产物，运行静态文件不需要 Node.js。

```bash
uv sync
```

这会安装后端依赖。QQ 适配器复用后端解释器，不必单独建环境。
`--extra dev` 是开发验证所需，`--extra chart` 只有开发者命令 `/inst` 画图用得上，
日常使用两者都不必安装。
依赖声明见 [pyproject.toml](https://github.com/YueLi-and-Me/YueLiBot/blob/main/pyproject.toml)。

需要桌宠或在本机构建管理面板时，再执行：

```bash
npm ci
npm run build
```

`npm ci` 使用仓库锁文件。构建成功后面板入口位于 `out/webui/index.html`。
外壳与面板由同一条命令构建，要求见 [Windows 上带桌宠](windows.md)。
服务器可从相同代码版本的构建机复制面板，见[无头部署](headless.md)。
抠图所需的 rembg 与 onnxruntime 不属于日常依赖，见[生图管线](../features/sprite.md)。

## 生成与填写配置

首次从 Python 入口启动：

```bash
uv run bot.py
```

缺少主体配置时，程序在 `config/` 创建带中文注释的 TOML，然后退出。
首次生成后的退出码为 1；根据终端提示补齐必需配置后重新启动。
已有文件不会被首装生成器覆盖。
单独运行桌面外壳也有设置窗口，但外壳本身不会启动 Python 后端。

| 文件 | 用途 |
| :--- | :--- |
| `providers.toml` | 连接地址、密钥、鉴权、超时和重试 |
| `models.toml` | 模型目录、任务候选与生成参数 |
| `bot.toml` | 名字、关系、人设、群聊和记忆策略 |
| `features.toml` | 语音、视觉、向量、纠错与日志 |
| `adapter.toml` | 当前 QQ 插件目录名 |

首装预填百炼连接和六个模型，只需在 `providers.toml` 填写实际 `api_key`。
配置完整性校验不包含远端权限验证；密钥权限和模型可用性需通过实际调用确认。
换厂商时，先建连接，再修改模型的 `api_provider` 与真实模型 ID。
地址变更时，应同步核对模型 ID 和请求参数是否受目标厂商支持。
引用关系与编辑方式见[配置总览](../configuration/index.md)。

QQ 连接参数另存于所选插件目录下的 `config.toml`。
首次安装默认停用 QQ；启用前须完成协议端连接配置，并填写两个不同的 QQ 号。
协议端须独立安装，见[接入总览](../adapters/index.md)。

## 数据目录与启动参数

配置和数据默认取项目根目录的 `config/`、`data/`，不随终端当前目录漂移。
桌面外壳拒绝将运行时根目录放在 C 盘；Windows 安装时请选择其他盘。
特殊外壳启动方式可用 `YUELI_PROJECT_ROOT` 明确项目根目录。
无头 Python 入口不限制使用 C 盘。

```bash
uv run bot.py --config-path /srv/yueli-config --data-dir /srv/yueli-data
```

`--config-path` 接收包含主体 TOML 的目录，不是某一份文件。
插件连接配置仍位于当前代码目录下，不随该参数迁移。
`--port` 可更改后端端口，默认 7999，以终端实际打印的地址为准。
旧配置升级与备份边界见[升级与回退](upgrade.md)。

## 确认服务已运行

完成配置后重新运行 `uv run bot.py`。
出现「WebUI 已就绪」表示监听已建立；通过浏览器打开所示地址。
本机访问自动登录；页面提示尚未构建时，需部署面板构建产物。
面板用于观察与配置，第一句对话可按[最短路径](first-run.md)发送。

Python 在后端就绪后启动所选适配器，再按桌宠开关启动外壳。
开关关闭时完全不启动 Electron；QQ 和面板仍可运行。
外壳不可用时会输出具体原因，后端继续运行；桌宠启动状态须单独确认。
需要手动单开外壳时，后端加 `--no-shell`，另一个终端执行 `npm run dev`。

终端按 `Ctrl+C` 会先收尾子进程，再停止服务并关闭数据库。
关闭期间应等待收尾完成；重复按键不会中断收尾流程。
托盘退出的两种情形见 [Windows 上带桌宠](windows.md)。
运行配置及其备份包含明文密钥，配置、数据和含凭据的截图不得提交到版本控制。
