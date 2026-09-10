# 让 AI 帮你装

不想一条条敲命令的话，把下面这段复制给任意能执行命令的 AI（Claude、ChatGPT
的桌面端、Cursor、终端里的 AI agent 都行），它会照着这份文档检查环境、装依赖、改配置。

## 第一步：复制这段给它

```text
帮我在本机装好 YueLiBot（月璃），全程你来执行，我在旁边确认。

安装手册：https://docs.yuelibot.org/manual/ai-install/
请按那份页面里的「AI 执行手册」逐条执行。

要求：
1. 动手前先问清楚我的系统、要不要桌宠、要不要接 QQ，不要自己假设
2. 用户协议必须由我本人阅读并确认，你不要代我同意
3. 每一步做完告诉我结果，缺什么、要我做什么，说清楚
4. 报错了先按手册里的排查项定位，不要盲目重试或改用别的办法绕过
```

装完之后如果还想接 QQ，把[接入总览](adapters/index.md)的链接也发给它。

---

## AI 执行手册

以下内容写给 AI。人也可以读，写的都是它接下来会做的事。

### 你是谁

你是 YueLiBot（月璃）的安装助手。**用户只负责回答问题，所有操作由你执行。**
你不是向导，是执行者；每一步做完要给用户一句明确的结果。

**红线：用户协议必须由用户本人确认。** 协议讲的是接入 QQ 的账号风险、
程序会保存群成员的哪些信息、对话内容会发给谁。把
[AGREEMENT.md](https://github.com/YueLi-and-Me/YueLiBot/blob/main/AGREEMENT.md)
的链接交给用户，让他自己读完、自己在终端里输入「同意」。
**不要用 `--accept-agreement` 替他跳过这一步**（那个参数只用于无头服务器等确实无法交互的场景，
且必须由用户明确要求）。

### Step 0：先问清楚，不要假设

- 操作系统是 Windows 还是 Linux？版本是多少？
- 要不要桌宠（立绘站在桌面上）？要的话必须是 Windows 10/11。
- 要不要接 QQ？要接的话用 NapCat 还是 SnowLuma？（不确定就推荐 NapCat）
- 项目装在哪个盘？**要桌宠的话不能装 C 盘**（桌面外壳会拒绝把运行时文件写进系统盘）。
- 有没有模型厂商的 API Key？默认用阿里云百炼，形如 `sk-xxxxxxxx`。
- 有 QQ 小号吗？机器人号建议用小号，且必须与用户本人的号不同。

### Step 1：检查系统要求

- 磁盘剩余 ≥ 3 GB（长期跑建议 10 GB 以上）
- 内存 ≥ 4 GB
- 能访问 PyPI、GitHub 与模型厂商 API
- 要构建面板或桌宠则需要 Node.js 22+；纯后端部署可跳过

不满足就明确告诉用户哪里不够、怎么补，不要硬装。

### Step 2：安装 uv

先执行 `uv --version` 看是否已装。没有的话：

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

装完**必须重开终端**让 PATH 生效，否则后面所有命令都会找不到 uv。
uv 会自己下载合适的 Python（3.11+），**不要额外装 Python**。

### Step 3：拿到代码

```bash
git clone https://github.com/YueLi-and-Me/YueLiBot.git
cd YueLiBot
```

没有 Git 就引导用户到
[Releases](https://github.com/YueLi-and-Me/YueLiBot/releases) 下载源码包解压。
解压后确认目录里直接能看到 `pyproject.toml`，**不要多套一层目录**。

### Step 4：安装依赖

在项目根目录（能看到 `pyproject.toml` 的那一层）执行：

```bash
uv sync
```

需要桌宠或要在本机构建面板时，再执行：

```bash
npm ci
npm run build
```

缺少 Node.js 时，Windows 可用 `winget install OpenJS.NodeJS.LTS`，
其他系统用各自的包管理器，或引导用户从 nodejs.org 下载。

### Step 5：第一次启动与配置

```bash
uv run bot.py
```

1. 终端打印用户协议并要求输入「同意」——**停下来，把协议链接交给用户，让他自己读、自己输入**。
2. 同意后程序在 `config/` 生成五份带中文注释的 TOML，**然后主动退出，退出码是 1**。
   这是正常流程，不要当成报错去修。
3. 打开 `config/providers.toml`，把 `api_key` 改成用户提供的 Key。
   其余字段已按百炼预填，**不要改模型 ID 和厂商地址**，除非用户明确要换厂商。
4. 再次执行 `uv run bot.py`，直到终端打印「WebUI 已就绪」。

WebUI 默认在 `http://127.0.0.1:7999`，本机访问自动登录，不需要手动填 token。
端口被占用时用 `uv run bot.py --port 8100` 换一个。

### Step 6：接入 QQ（可跳过）

先让用户确认用的是哪个协议端，再按对应篇章操作：

- NapCat：<https://docs.yuelibot.org/manual/adapters/napcat/>
  协议端仓库 <https://github.com/NapNeko/NapCatQQ>，
  Windows 推荐用桌面控制台 <https://github.com/NapNeko/NapCatQQ-Desktop/releases>
- SnowLuma：<https://docs.yuelibot.org/manual/adapters/snowluma/>
  协议端仓库 <https://github.com/SnowLuma/SnowLuma>

要点（细节以上面两篇为准）：

1. 协议端是**独立程序**，不要把它装进月璃的目录里。
2. 协议端里建的是 **WebSocket 服务端**（月璃主动去连），不是客户端、也不是 HTTP 服务。
3. **消息上报格式必须是 `array`**，填成字符串会导致「连上了但没有正常内容」。
4. 月璃侧要改两处：`config/adapter.toml` 里的 `plugin`，
   以及插件目录下的 `config.toml`（`self_qq`、`host`、`port`、`token`、`owner.qq`）。
5. 三个「令牌」别填混：协议端 WS 访问令牌、协议端管理面板密码、月璃面板 token 是三回事。
6. `self_qq`（机器人号）与 `owner.qq`（用户本人）必须是**两个不同的号**。
7. 群聊是**白名单**：用户想用的群号必须显式写进 `[group].list`，空列表等于不接任何群。

### Step 7：验证

逐项确认，不要跳步：

1. 后端终端出现「WebUI 已就绪」
2. 浏览器能打开 `http://127.0.0.1:7999`
3. 发一条测试消息，在面板「会话观察」页里能看到回复正文：

   ```bash
   curl -X POST http://127.0.0.1:7999/chat/send \\
     -H "Authorization: Bearer <终端打印的 token>" \\
     -H "Content-Type: application/json" \\
     -d '{"text":"你好"}'
   ```

4. 接 QQ 的话：用用户本人的 QQ 私聊机器人号，**在 QQ 里真的收到回复**；
   再把测试群加进白名单、用 @ 提及机器人，确认群里有回复

### 常见问题

**`uv: command not found`** —— 装完 uv 没重开终端。关掉重新打开。

**`uv sync` 卡住** —— 网络访问 PyPI/GitHub 不畅。换网络环境后重跑，已下载的部分会保留。

**第一次启动就退出、退出码 1** —— 正常。配置已生成，去填 `api_key` 再启动。

**启动后报 `api_key` 为空 / 调用返回 401、403** —— Key 没填、填错，或该 Key 没开通对应模型。
不要靠改模型 ID 或厂商地址来「试试看」。

**桌面外壳报「拒绝把运行时文件写入 C 盘」** —— 项目在 C 盘。挪到其它盘，
或用 `YUELI_PROJECT_ROOT` 指定一个非 C 盘目录。

**面板空白、提示尚未构建** —— 跳过了 `npm run build`。

**QQ 连不上** —— 按[接入总览](adapters/index.md#验证与排错)的三层分别确认：
配置能读通、连接能建立、QQ 真收到回复。**不要靠加大超时或重连间隔来「解决」**，
端口、令牌、账号错了，等多久都连不上。

**用户想把机器人接到大号上** —— 提醒风险：接入 QQ 可能导致账号被风控或限制，
协议正文见 [AGREEMENT.md](https://github.com/YueLi-and-Me/YueLiBot/blob/main/AGREEMENT.md)。
