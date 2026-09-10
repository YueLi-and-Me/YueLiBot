# 下载与安装

这一步只做一件事：把月璃的代码放到你的机器上，并把依赖装好。装完还不会启动她，
下一步才是[第一次启动与配置](first-run.md)。

整段过程大约十分钟，其中大部分时间在下载依赖。

## 先做一件事：把目录放在非 C 盘

**想要桌宠的话，项目目录不要放在 C 盘。** 桌面外壳会拒绝把运行时文件写进系统盘，
启动时会直接报错：

```text
拒绝把 YueLiBot 运行时文件写入 C 盘：C:\\YueLiBot。请把项目放到其它盘，或设置 YUELI_PROJECT_ROOT。
```

放在 `D:\\YueLiBot`、`E:\\apps\\YueLiBot` 这类位置都可以。
只跑后端（服务器部署）不受这条限制，C 盘也能用。

## 安装 uv

uv 是 Python 的包与环境管理器。月璃用它装依赖、管虚拟环境、按需下载 Python，
所以**你不需要自己装 Python**。

=== "Windows"

    打开 PowerShell，执行：

    ```powershell
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```

=== "Linux / macOS"

    打开终端，执行：

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

装完**关掉再重新打开终端**（让 PATH 生效），然后确认：

```bash
uv --version
```

看到版本号就对了。提示「找不到命令」说明 PATH 没刷新，重开终端即可。

## 拿到代码

=== "用 Git"

    已经装了 Git 的话，克隆仓库最省事，以后升级只要 `git pull`：

    ```bash
    git clone https://github.com/YueLi-and-Me/YueLiBot.git
    cd YueLiBot
    ```

    没装 Git 又想用这种方式，先到 [git-scm.com](https://git-scm.com/downloads) 下载安装。

=== "下载压缩包"

    不想装 Git 就到
    [Releases 页面](https://github.com/YueLi-and-Me/YueLiBot/releases)
    下载源码包，解压到刚才选好的目录：

    ```text
    D:\\YueLiBot\\
    ```

    解压后目录里应该能看到 `pyproject.toml`、`bot.py`、`src`、`docs` 这些条目。
    注意不要多套一层目录（`D:\\YueLiBot\\YueLiBot\\pyproject.toml` 是错的），
    多套一层会让后面的命令找不到项目。

以下所有命令都在**项目根目录**执行，也就是能看到 `pyproject.toml` 的那一层。

## 安装后端依赖

```bash
uv sync
```

首次运行会做三件事：下载一个合适的 Python、创建 `.venv` 虚拟环境、按
`pyproject.toml` 装齐后端依赖。大约 1 GB，视网络情况需要几分钟。

装完可以确认一下：

```bash
uv run python -c "import src; print('ok')"
```

打印 `ok` 说明依赖可用。

## 构建前端（桌宠需要，服务器可跳过）

```bash
npm ci
npm run build
```

这两条命令装前端依赖并构建两块界面：桌宠外壳与管理面板。
构建产物落在 `out/`，管理面板的入口是 `out/webui/index.html`。

需要 **Node.js 22 或更新版本**。没装的话：

- Windows：到 [nodejs.org](https://nodejs.org/zh-cn) 下载 LTS 版安装，或执行 `winget install OpenJS.NodeJS.LTS`
- Linux：用系统包管理器，或参考 Node 官方文档

**只跑后端与 QQ、不打算用桌宠，且打算在别的机器上构建面板的话**，这一段可以跳过。
跳过时管理面板会提示「尚未构建」，QQ 与 API 照常工作，见[无头部署](headless.md)。

## 验证与排错

装完之后，项目目录应该是这个样子：

```text
YueLiBot/
├── pyproject.toml     依赖声明
├── bot.py             启动入口
├── .venv/             uv sync 创建的虚拟环境
├── src/               后端源码
├── out/               npm run build 的产物（跳过了就没有）
└── config/            第一次启动后才会出现
```

**`uv: command not found`／「不是内部或外部命令」** —— 装完 uv 没有重开终端。
关掉这个窗口重新打开一次。

**`uv sync` 卡在下载不动** —— 网络访问 PyPI 或 GitHub 不畅。换一个网络环境重试；
已经下载的部分会保留，重跑不会从零开始。

**`npm ci` 报错 `EBADENGINE` 或提示 Node 版本过低** —— Node.js 低于 22。
用 `node --version` 确认，升级后再试。

**`npm ci` 报 `EACCES`／权限错误** —— 项目目录在系统保护目录下（例如
`C:\\Program Files`）。换到用户目录或其它盘。

**下一步** —— [第一次启动与配置](first-run.md)。
