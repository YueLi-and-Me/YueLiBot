# 下载与安装

**先确认**：Windows 10 / 11 或 Linux；磁盘留 3 GB；一个模型厂商的 API Key
（见[环境要求](requirements.md)）。

**项目目录不要放在 C 盘** —— 桌面外壳拒绝把运行时文件写进系统盘，放 C 盘以后开桌宠会直接报错。

## 安装 uv

uv 负责装依赖、准备 Python，**不用自己装 Python**。

=== "Windows"

    ```powershell
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```

=== "Linux / macOS"

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

装完**重开一次终端**，`uv --version` 有输出就行。

## 安装 Node.js

管理面板与桌宠要用 Node.js 22 以上构建一次。
到 [nodejs.org](https://nodejs.org/zh-cn) 下载 LTS 版，或执行 `winget install OpenJS.NodeJS.LTS`。

## 克隆仓库

```bash
git clone https://github.com/YueLi-and-Me/YueLiBot.git
cd YueLiBot
```

没装 Git 就到 [Releases](https://github.com/YueLi-and-Me/YueLiBot/releases) 下源码包解压，
目录里要能直接看到 `pyproject.toml`（多套一层，后面所有命令都会找不到项目）。

## 安装依赖

```bash
uv sync         # 后端依赖
npm ci          # 前端依赖
npm run build   # 构建管理面板与桌宠，产物在 out/
```

## 第一次启动

```bash
uv run bot.py
```

第一次会打印用户协议，输入「同意」后生成配置文件并退出（退出码是 1，正常）。
配置在**项目目录下的 `config/` 文件夹**里，用记事本打开 `providers.toml`，
把 `api_key` 填上，再启动一次，看到「WebUI 已就绪」就成了。

具体每一步该看到什么、卡住了查哪里，见[第一次启动与配置](first-run.md)
与[常见问题](../troubleshooting.md)。
