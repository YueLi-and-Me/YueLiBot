# 适配器配置

QQ 接入分两层：主体选择一个插件，插件再连接独立运行的协议端。
安装协议端不会自动把月璃接进去。
先读[接入总览](../adapters/index.md)决定使用哪一个，再填写连接参数。

## 主体选择文件

默认真实文件是 `config/adapter.toml`，内容只需声明插件目录名：

```toml
plugin = "yueli-napcat-adapter"
```

选择 SnowLuma 时改为 `yueli-snowluma-adapter`。
这里填目录名，不是显示名、文件路径、QQ 号或连接地址。
两个插件互斥，不能用列表一次启用两个。
模板见 [adapter.toml](../../../config.example/adapter.toml)。

读取时不会因拼错名字而自动尝试另一个插件。
缺选择文件、插件目录、清单或连接配置时，查看后端启动诊断；
后端和面板还能运行，不意味着 QQ 已连接成功。
首装会显式创建默认选择，之后应由你维护。

## 插件目录与段名必须一致

| `plugin` | 连接配置落点 | 连接段 |
| :--- | :--- | :--- |
| `yueli-napcat-adapter` | `adapters/yueli-napcat-adapter/config.toml` | `[napcat]` |
| `yueli-snowluma-adapter` | `adapters/yueli-snowluma-adapter/config.toml` | `[snowluma]` |

不要把两个插件的连接文件直接互相覆盖后保留旧段名。
除连接段外，两者都有 `[owner]`、`[private]`、`[group]`。
段名、插件清单与目录对不上时，没有可用协议连接，主体不会猜测你想选谁。
换用自定义主体配置目录，也不会改变插件连接文件的落点。

## 从模板准备连接

首装会补当前所选插件缺失的连接文件，默认 `enabled = false`。
两个 QQ 号都填了占位数字，启用前必须改成实际的不同账号。
需要手动创建时，复制对应模板到上表中的真实落点：

- [NapCat 连接模板](../../../config.example/adapters/yueli-napcat-adapter.toml)。
- [SnowLuma 连接模板](../../../config.example/adapters/yueli-snowluma-adapter.toml)。

保留模板自己的 `[inner]` 版本，不要改成主体配置版本。
把 `host`、`port`、`token` 对齐协议端的正向 WebSocket 服务端。
`host` 只填主机名或 IP，不加 `ws://`、端口或路径。
协议端按根路径提供服务；当前连接配置没有自定义路径或 TLS 开关。

`self_qq` 填协议端实际登录的机器人号，`owner.qq` 填用户本人的号。
适配器连上后还会检查实际登录号，错号不能靠修改显示名解决。
群聊白名单按群号，私聊访问名单按联系人号，见[接入总览](../adapters/index.md)。

## 面板与手动编辑

「月璃设置」中的适配器页编辑当前插件的连接参数和名单。
它并不通过这张表单切换 `plugin`；切换仍编辑主体选择文件。
保存后重启后端，让旧适配器退出并按新选择重新启动。

手动改连接时，先保持 `enabled = false`，完成协议端设置，
确认两个 QQ 号和访问名单后再启用。
只改端口却没有在协议端启用监听，不会产生连接。
只停用连接会停止 QQ 接入，不影响桌面或管理面板。

## 自检与连接验收

在项目根运行：

```bash
uv run python scripts/check/self_check.py
```

命令见 [scripts/check/self_check.py](../../../scripts/check/self_check.py)。
它检查选择、目录、清单、段名和版本；配置可以在后端运行时只读检查。
若使用自定义位置，可用 `--config-dir`、`--adapters-dir` 指定对应目录。
自检还会检查两个随附插件，不只检查当前选中的一个。
未使用插件缺连接文件的报告，要与当前插件连接故障分开判断。

自检通过不是网络握手成功，也不是 QQ 消息投递成功。
启动后看实际连接日志，再由本人 QQ 私聊机器人发一句短消息验证收发。
收发失败按所选协议端篇排查：[NapCat](../adapters/napcat.md)、[SnowLuma](../adapters/snowluma.md)。
实现边界见[开发手册：平台与适配器](../../dev/architecture/platform-io.md)。
