# NapCat 接入

本篇把 NapCat 的正向 WebSocket 接到月璃。
先完成[后端安装](../deployment/install.md)，准备机器人 QQ 和用户本人 QQ 两个不同账号。
群聊准入与回复规则见[接入总览](index.md)。

## 安装并登录协议端

Windows 可使用 NapCat 的 Shell 发行包。
安装与该发行版兼容的桌面 QQ，从 NapCat 发布页面取得 `NapCat.Shell.zip` 并解压。
协议端应解压到独立目录，不应放入月璃的适配器插件目录。
Windows 11 运行包内 `launcher.bat`，Windows 10 使用 `launcher-win10.bat`。
按启动提示完成机器人账号扫码登录，保持协议端进程运行。

使用其他安装形态时，以协议端自身的安装提示完成 QQ 依赖和登录。
月璃的 `uv sync` 不会安装协议端，`npm ci` 也不会替协议端登录。
通过启动日志提供的 NapCat WebUI 地址进入协议端管理界面；该界面与月璃面板相互独立。
登录后按界面要求更新管理密码，确认页面显示的 QQ 号与机器人账号一致。
管理页面可访问不代表 OneBot WebSocket 已启用，仍需配置网络服务。

## 创建正向 WebSocket 服务端

在 NapCat WebUI 打开网络配置，点「新建」，选择「WebSocket 服务端」。
它是正向连接：NapCat 等待月璃适配器来连。
WebSocket 客户端或仅启用 HTTP 服务端均不满足本接入方式的要求。

同机部署可使用下面这一组值：

| 协议端设置 | 示例值 | 月璃中对应内容 |
| :--- | :--- | :--- |
| 启用 | 打开 | 插件连接段也设 `enabled = true` |
| 监听主机 | `127.0.0.1` | `host = "127.0.0.1"` |
| 监听端口 | `8095` | `port = 8095` |
| 访问令牌 | 自定义令牌 | 将相同值填入 `token` |
| 消息上报格式 | `array` | 由适配器按消息段解析 |

**必须把 `messagePostFormat` 设为 `array`。**
字符串消息格式会使消息段解析失败，文本和 @ 不能按当前契约处理。
保存并启用服务；只保存草稿而未启用不会开始监听。
不同机器或容器部署时，监听地址与端口映射要让后端能访问。
适配器 `host` 填连接目标，不能填服务端的通配监听地址 `0.0.0.0`。

## 选择插件并填写连接

在月璃的 `config/adapter.toml` 设置：

```toml
plugin = "yueli-napcat-adapter"
```

使用[连接模板](https://github.com/YueLi-and-Me/YueLiBot/blob/main/config.example/adapters/yueli-napcat-adapter.toml)，
真实文件放在 `adapters/yueli-napcat-adapter/config.toml`。
修改其中已有段；以下号码仅示范两个不同身份：

```toml
[napcat]
enabled = true
self_qq = "123456789"
host = "127.0.0.1"
port = 8095
token = ""

[owner]
qq = "987654321"
```

若协议端设置了令牌，把空 `token` 改成该值。
这里的令牌是 WebSocket 服务端访问令牌，不是 NapCat 管理面板密码，
也不是月璃启动时打印的后端 token。
`self_qq` 必须与协议端实际登录一致，`owner.qq` 填写用户本人的聊天账号。
保留模板的版本、重连与访问名单配置；启用前核对实际 QQ 号。

## 启动与确认

保持 NapCat 在线，在月璃项目根运行：

```bash
uv run bot.py
```

检查后端终端中的适配器启动、协议连接及登录身份信息。
由本人 QQ 私聊发一句短消息，确认有入站记录、模型结果和 QQ 实际收到的回复。
将测试群加入插件群白名单后，使用 QQ 的 @ 功能提及机器人。
只有后端面板显示回复而 QQ 没收到时，继续检查出站动作错误。

## 连不上的常见原因

| 现象 | 核对项 |
| :--- | :--- |
| 连接被拒绝或超时 | NapCat 是否运行、正向 WS 是否启用、8095 是否与实际监听一致 |
| 握手返回 401／403 | 核对 WS 访问令牌是否被误填为管理页面密码 |
| 握手失败或方法不可用 | 是否把 WebUI／HTTP 端口填成了 WS 端口 |
| 登录号不匹配 | `self_qq` 与当前连接账号不一致，或连到了另一个实例 |
| 连接正常但解析报错 | `messagePostFormat` 是否为 `array` |
| 本人私聊正常，其他私聊或群聊无回复 | 私聊访问名单、群白名单与回复触发分别检查 |
| 找不到对应配置段 | 文件必须在所选插件内，连接段必须是 `[napcat]` |

断线重连间隔和动作等待时间在插件配置内；延长它们不会修复令牌或端口错误。
戳一戳需要能力探测通过且群聊动作开关开启；普通文字成功不能证明戳一戳可用。
只读配置检查见[适配器配置](../configuration/adapter.md)，它不替代网络与真实投递验收。
