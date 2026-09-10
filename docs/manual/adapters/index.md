# QQ 接入总览

想让月璃在 QQ 里说话，需要**两个独立的东西**：

1. **协议端**——登录 QQ、收发消息的程序。它是一个独立项目，不随月璃一起安装。
2. **适配器**——月璃随附的插件，负责把协议端的消息转给月璃，再把回复发回去。

月璃和协议端之间走**正向 WebSocket**：协议端开一个服务端口等着，
月璃主动连上去。两者可以在同一台机器，也可以分开。

## 选哪个协议端

按你的系统和对稳定性的偏好选，两者都能收发文字、图片、引用、@、合并转发。

**NapCat** —— 支持的平台最多，社区最大，Windows、Linux、Docker 都能装。
出自 [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ)。
**不确定选哪个就选它。**

**SnowLuma** —— 面向 QQ 客户端的新一代互操作运行时，功能特性更新得快。
出自 [SnowLuma/SnowLuma](https://github.com/SnowLuma/SnowLuma)。
Windows 上体验最完整；Linux 需要可见的远程桌面来完成扫码登录。

**两个不能同时启用。** 先选定一个走通，想换另一个时改
`config/adapter.toml` 里的插件名与连接配置即可。

## 先去哪儿下载

Windows 用户有一个省事的办法：用同一批维护者做的桌面控制台，把 QQ、协议端、
依赖组件都装在一个窗口里管。

<div class="grid cards" markdown>

-   :octicons-device-desktop-24:{ .lg .middle } **NapCatQQ Desktop**

    ---

    Windows 桌面控制台，同时管 NapCat 与 SnowLuma：本机与远端部署、起停、看日志、
    一键装组件。**新手推荐从这里开始。**

    [:octicons-arrow-right-24: 下载安装包](https://github.com/NapNeko/NapCatQQ-Desktop/releases)

-   :octicons-server-24:{ .lg .middle } **NapCatQQ**

    ---

    NapCat 协议端本体，提供 Shell 包与 Docker 镜像。Linux 服务器走这里。

    [:octicons-arrow-right-24: 打开仓库](https://github.com/NapNeko/NapCatQQ)

-   :octicons-rocket-24:{ .lg .middle } **SnowLuma**

    ---

    SnowLuma 协议端本体，按平台下载完整发行包解压即用。

    [:octicons-arrow-right-24: 打开仓库](https://github.com/SnowLuma/SnowLuma)

</div>

详细的安装与配置见对应篇章：[NapCat 接入](napcat.md)、[SnowLuma 接入](snowluma.md)。

## 两个 QQ 号的职责

**机器人号**（`self_qq`）——协议端登录的那个号，也就是「月璃的号」。
建议用小号：接入 QQ 有账号风险，协议正文见
[AGREEMENT.md](https://github.com/YueLi-and-Me/YueLiBot/blob/main/AGREEMENT.md)。

**你的号**（`owner.qq`）——你自己的聊天账号，用来私聊她、在群里点名。
这个号在私聊名单里**自动放行**，不用再写进名单。

两个号必须是不同的数字 QQ 号；配置里写错、或和协议端实际登录的不一致，
连接后会直接报身份不匹配。

## 谁能跟她说话

适配器在消息进门时就按名单挡掉，**名单外的消息不会到达月璃**：
不会创建人物、不会存进会话、也不会消耗模型额度。

**私聊**默认白名单——只回名单里的人。想把范围放开，改成黑名单模式。
**群聊**固定白名单——只回名单里的群，而且即使你本人在名单外的群里说话也不豁免。

配置片段（改的是适配器插件目录下的 `config.toml`，段名要与所选插件一致）：

```toml
[private]
mode = "whitelist"    # whitelist 只处理名单内，blacklist 名单内不处理
list = [10001]        # 每行一个数字 QQ 号；owner.qq 自动放行，不用写

[group]
mode = "whitelist"    # 群聊固定白名单，没有别的模式
list = [123456789]    # 每行一个数字群号；空列表表示不接入任何群
```

测试阶段只想让自己那个群能用，就**只往 `list` 里写那一个群号**。
名单外群的消息在适配器侧被挡下，日志里能看到，不会走到模型。

## 什么时候她会开口

群里不是每句话都回。三个层次的信号：

**协议 @** —— QQ 原生的 @ 提醒，是最明确的信号。默认配置下被 @ 必回，
优先级高于睡眠与回复次数窗口。

**名字提及** —— 正文里出现 `bot.toml` 中 `[bot]` 的名字或别名，
按包含匹配、不区分大小写。注意正文里的 `@` 字符不构成协议提及。

**自然接话** —— 没人点名时，她会综合发言频率、话题延续性和当前气氛判断要不要插话。
这是「有机会」，不是「一定回」，而且受回复次数窗口限制。

```toml
[bot]
name = "月璃"
aliases = ["小璃"]

[group_chat]
at_mention_must_reply = true      # 被 @ 必回
name_mention_probability = 1.0    # 名字被提及时的基础概率，实际还要看发言占比衰减
reply_window_minutes = 10         # 回复次数窗口长度
max_replies_in_window = 3         # 窗口内最多回几条；必须是正数
```

这段改的是 `bot.toml` 里已有的字段，**保留模板中的其他群聊选项**。
减少她主动插话，应该调窗口与行动模式，而不是清掉会话上下文。

## 验证与排错

**接入成功的标准**：用你的 QQ 私聊机器人号，QQ 里真的收到了回复；
再把测试群加进白名单，用 @ 提及她，群里也有回复。

注意**面板上显示回复 ≠ QQ 里收到**。前者只说明模型出了结果，
后者才证明出站动作被协议端接受。

**私聊正常、群聊没反应** —— 群号没进白名单，或群里没 @ 到她，或被睡眠状态拦住。
三项分别检查。

**她连着好几个群都接了** —— 白名单里写了不止一个群。测试阶段只留一个。

**回复了不该回复的人** —— 私聊模式被改成了黑名单，且名单没写全。
默认白名单更安全：配错的表现是「朋友没收到回复」，而不是「陌生人消耗你的额度」。

**具体连不上的排查** —— 见 [NapCat 接入](napcat.md#连不上的常见原因) 或
[SnowLuma 接入](snowluma.md#连不上的常见原因)，那里按现象列了核对项。

**实现层面的边界**（出口契约、插件协议、消息归属解析）见
[开发手册：平台与适配器](../../dev/architecture/platform-io.md)。
