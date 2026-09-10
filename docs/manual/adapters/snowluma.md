# SnowLuma 接入

SnowLuma 是面向 QQ 客户端的新一代互操作运行时，把 QQ 原生会话转成 OneBot 接口。
它在 Windows 上体验最完整；Linux 也能跑，但 QQ 扫码登录需要一块能看见的远程桌面。

**前置**：月璃后端已经能跑（[第一次启动与配置](../deployment/first-run.md)），
准备好两个不同的 QQ 号——机器人号用**小号**。

## 第一步：装 SnowLuma

=== "Windows：桌面控制台或发行包"

    两条都可以：

    **走桌面控制台**——到
    [NapCatQQ Desktop 的 Releases](https://github.com/NapNeko/NapCatQQ-Desktop/releases)
    下载 `NapCatQQ-Desktop-<版本>-x64.msi` 安装。这个控制台同时管 NapCat 和
    SnowLuma，能一键装组件、起停、看日志。

    **走官方发行包**——到
    [SnowLuma 的 Releases](https://github.com/SnowLuma/SnowLuma/releases)
    下载 Windows 完整发行包，解压到**独立目录**，运行 `launcher.bat`。

    > Lite 版不带运行时，需要自己装 Node.js 22.13 以上（23 系要 23.4 以上）。
    > 不确定就下完整版。

=== "Linux"

    下载对应平台的完整发行包解压，然后：

    ```bash
    chmod +x launcher.sh
    ./launcher.sh
    ```

    QQ 的扫码登录需要图形界面；无头服务器要先准备可见的远程桌面。
    **月璃不提供代登录入口**，这一步必须由你在协议端完成。

## 第二步：登录机器人 QQ

打开桌面 QQ 并用机器人小号登录，然后启动 SnowLuma。
两者应当用**同一个 Windows 用户、相同的权限等级**运行，否则注入会失败。

启动日志里会打印 WebUI 地址，默认是：

```text
http://localhost:5099
```

用日志里给的**初始密码**登录（全新数据目录时由启动日志提供）。
这个密码是 SnowLuma 自己的管理面板密码，**不是 OneBot 访问令牌**。

进面板先看 QQ 的连接／注入状态：状态不对时先解决协议端自己的问题，
这时候去连月璃只会白费功夫。

## 第三步：建一个正向 WebSocket 服务端

在 SnowLuma 的面板里进入 OneBot 网络配置，配置一个 **`wsServers`** 条目：

- `wsClients` 是反向客户端，**不适用**于本接入方式
- 服务端要**同时提供事件与动作**：当前配置形态里 `role` 填 `Universal`，
  只接事件或只收 API 都不行
- 根路径为 `/`
- 记下访问令牌（`accessToken`），月璃那边要填同一个值

同机部署填 `127.0.0.1` 加一个端口（例如 `8095`），保存后确认**服务真的开始监听**。

!!! danger "消息上报必须是数组格式"

    SnowLuma 不同版本的配置键名不一样：有的界面叫 `messagePostFormat`，
    当前的配置文档用的是 `messageFormat`。**哪个存在就改哪个，都设成 `array`。**

    只识别新键名的版本里，写旧键名不会报错但也不生效——
    最终判据是**事件里的 `message` 是数组而不是字符串**。

## 第四步：在月璃这边选插件、填连接

编辑 `config/adapter.toml`：

```toml
plugin = "yueli-snowluma-adapter"
```

从[连接模板](https://github.com/YueLi-and-Me/YueLiBot/blob/main/config.example/adapters/yueli-snowluma-adapter.toml)
复制一份到 `adapters/yueli-snowluma-adapter/config.toml`，改这几个值：

```toml
[snowluma]
enabled = true            # 启用前先在协议端建好 wsServers 条目
self_qq = "123456789"     # 机器人小号，与协议端登录的号一致
host = "127.0.0.1"        # 只填地址，不带 ws://、端口和路径
port = 8095               # 与协议端实际监听端口一致
token = ""                # 填成协议端的 accessToken；没有就留空

[owner]
qq = "987654321"          # 你自己的 QQ 号，与上面必须是两个不同的号
```

段名必须是 `[snowluma]`，不能沿用别家插件的段名。

## 地址怎么填才对

**`host` 只填主机名或 IP。** 月璃当前固定使用普通 `ws://主机:端口` 连接，
**不支持自定义路径**——协议端必须在根路径 `/` 上提供服务。

**服务器上写 `0.0.0.0` 是监听范围，不是连接地址。** 月璃这边要填**能连到的实际地址**。

**容器里的 `127.0.0.1` 指容器自己。** 月璃跑在宿主机就填映射到宿主机的端口；
月璃也在容器里，就填容器网络里能到达的地址。

**别把端口搞混**：管理面板端口、OneBot HTTP 端口和 WebSocket 端口是三回事，
只有 WS 端口能填进 `port`。

## 第五步：启动并确认

保持 QQ 与 SnowLuma 在线，在月璃项目根目录运行：

```bash
uv run bot.py
```

看后端终端里的适配器连接状态与识别到的登录号，然后：

1. 用**你自己的 QQ** 私聊机器人号，确认 QQ 里收到回复
2. 把测试群写进 `[group]` 名单，重启月璃
3. 在群里用 **@** 提及机器人，确认群里有回复

## 连不上的常见原因

| 现象 | 先查这里 |
| :--- | :--- |
| 面板能打开，但 QQ 里没反应 | QQ 登录了吗、注入成功了吗、版本对得上吗 |
| WS 连接失败 | `wsServers` 条目启用了吗、端口与容器映射一致吗 |
| 401 / 403 | 填的是 `accessToken` 吗，是不是把管理员密码填了进去 |
| 收得到事件、发不出消息 | 服务端的 `role` 是不是 `Universal`，看动作报错 |
| 消息格式校验失败 | 实际生效的那个格式键（`messageFormat` 或 `messagePostFormat`）设成 `array` 了吗 |
| 插件配置校验失败 | 段名是不是 `[snowluma]`，两个 QQ 号是不是填成了同一个 |
| 私聊正常、群里不回 | 群号进白名单了吗、@ 到了吗、她是不是在睡眠 |

**`reconnect_interval_sec` 管断线重连，`action_timeout_sec` 管动作等待。**
端口、鉴权或账号错了，把等待时间调长解决不了问题。

配置能读通、连接能建立、QQ 真收到回复——这三件事要分别确认。
只读自检见[适配器配置](../configuration/adapter.md)。
