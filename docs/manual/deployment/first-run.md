# 第一次启动与配置

第一次启动分两趟：第一趟同意协议、生成配置文件；填上 API Key 之后第二趟才真正跑起来。

## 第一趟：同意协议

在项目目录里执行：

```bash
uv run bot.py
```

终端会打印用户协议。读完在终端输入**「同意」**两个字，回车。
只需要做这一次，之后不再询问。协议正文在
[AGREEMENT.md](https://github.com/YueLi-and-Me/YueLiBot/blob/main/AGREEMENT.md)。

输入之后，程序在 `config/` 下生成五份配置文件，然后退出。
**退出码是 1，这是正常的**，不是报错。

## 找到配置文件

配置文件在**项目目录下的 `config/` 文件夹**里。
项目装在 `D:\YueLiBot`，那它们就在 `D:\YueLiBot\config\`：

| 文件 | 管什么 |
| :--- | :--- |
| `providers.toml` | 厂商地址与 API Key |
| `models.toml` | 用哪些模型、任务怎么分档 |
| `bot.toml` | 名字、人设、群聊规则、桌宠开关 |
| `features.toml` | 语音、视觉、表情包等功能开关 |
| `adapter.toml` | 用哪个 QQ 适配器 |

这些是纯文本文件，**用记事本就能改**：右键文件 → 打开方式 → 记事本。

## 填 API Key

现在只需要改一个地方。用记事本打开 `providers.toml`，找到这一行：

```toml
api_key = ""
```

把 Key 填进引号里：

```toml
api_key = "sk-你的Key"
```

`Ctrl+S` 保存，关掉记事本。**其他行不用动** —— 厂商地址和六个模型都已按阿里云百炼填好。

还没有 Key 就到[百炼控制台](<https://bailian.console.aliyun.com/>)开通并创建一个，
形如 `sk-xxxxxxxx`。想换别的厂商见 [providers.toml](../configuration/providers.md)。

## 第二趟：启动

再执行一次：

```bash
uv run bot.py
```

终端最后出现这个框，就成了：

```text
WebUI 已就绪
```

**这个窗口是月璃本体，不要关掉**，关了她就下线了。

## 打开管理面板

浏览器打开终端里打印的地址，默认是：

```text
http://127.0.0.1:7999
```

本机访问自动登录，不用输 token。面板里能看会话、改配置、管记忆，
但**没有对话输入框** —— 要跟她说话用下面三种方式之一。

## 说第一句话

=== "命令行"

    另开一个终端（Windows 用 Git Bash），把 token 换成终端里打印的那串：

    ```bash
    curl -X POST http://127.0.0.1:7999/chat/send \\
      -H "Authorization: Bearer <终端打印的 token>" \\
      -H "Content-Type: application/json" \\
      -d '{"text":"你好"}'
    ```

    回复不在这个响应里，显示在终端和面板的「会话观察」页。

=== "QQ"

    先装协议端并配好适配器，见[适配器接入](../adapters/index.md)，
    然后用你自己的 QQ 私聊机器人号。

=== "桌面"

    打开桌宠，她出现在屏幕角落，见[桌面桌宠](windows.md)。

## 下一步

接 QQ：[适配器接入](../adapters/index.md)。开桌宠：[桌面桌宠](windows.md)。
跑不动了：[常见问题](../troubleshooting.md)。
