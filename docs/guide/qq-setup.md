# QQ 与群聊接入

接入 QQ 需要独立安装的协议端。本文覆盖两个适配器的选择、正向 WebSocket 的必改项、群聊白名单与回复触发口径，以及屏幕感知在各出口的开放范围。

QQ 接入依赖独立安装的协议端，目前支持两个，**互斥，同时只能开一个**：

| 适配器目录 | 协议端 | 配置段 |
| :--- | :--- | :--- |
| `adapters\yueli-napcat-adapter\` | NapCat | `[napcat]` |
| `adapters\yueli-snowluma-adapter\` | SnowLuma | `[snowluma]` |

用哪个由 `config\adapter.toml` 的 `plugin` 声明（写插件目录名）；连接参数写在该插件目录下的
`config.toml`，段名与适配器一一对应——**目录名和段名对不上，适配器起来就连不上任何协议端**。
这一项 `scripts\self_check.py` 会检查。

先在协议端的 WebUI 创建一条**正向 WebSocket 服务端**，端口和令牌要与那份 `config.toml`
的连接段保持一致，并把消息上报格式 `messagePostFormat` 改为 `array`；否则文本和 @ 段无法按
当前协议解析。确认连接信息后再把 `enabled` 改为 `true`，并分别填写机器人登录号 `self_qq`
与用户本人的 `owner.qq`，这两个号码不能相同。

群聊只支持白名单，写在同一份适配器配置里：

```toml
# adapters/<当前插件目录>/config.toml
[group]
mode = "whitelist"
list = [123456789]  # 允许接入的数字 QQ 群号
```

白名单按**群号**判断。即使用户本人正在名单外群里发言，该群也不会被豁免；消息会在适配器侧直接丢弃，不创建人物、不写记忆，也不请求模型。白名单群中的消息会进入主体保存上下文，但她不会随机插话：只有协议 @、名字或别名明确叫到她时，才会进入回复判定。

回复触发和群聊消耗在 `config\bot.toml` 配置：

```toml
[bot]
name = "月璃"
aliases = ["小璃"]

[group_chat]
# true 时，协议 @ 不受睡眠和回复窗口限制，必须回应
at_mention_must_reply = true
# 名字、别名或非必回 @ 命中后的回复概率，范围 0~1
name_mention_probability = 1.0
# 群聊人格增量统一按较低倍率折算
persona_weight = 0.05
# 普通群聊回复的时间窗口与窗口内上限
reply_window_minutes = 10
max_replies_in_window = 3
```

`bot.aliases` 是除主名字外可以叫到她的称呼；`at_mention_must_reply` 只决定协议 @ 是否必回；`name_mention_probability` 决定名字、别名和非必回 @ 命中后的回复概率。未提及她的群消息仍会落入该群自己的历史，但不会调用回复模型。

屏幕感知分成两个正交开关：`[vision] enabled` 决定是否截屏并交给视觉模型，`[perception] surfaces` 决定已经获得的前台程序、持续时间和屏幕描述允许出现在哪个出口。默认只有桌宠窗口：

```toml
# config/features.toml
[perception]
surfaces = ["desktop"]

# 如果愿意让自己的 QQ 私聊看见屏幕情境，可以显式加入 direct：
# surfaces = ["desktop", "direct"]
```

`direct` 即使开启也只对 `owner.qq` 对应的用户本人生效，`[private]` 名单里的其他联系人看不到。`group` 不是“默认关闭”的可选项，而是配置结构中不存在的出口：填入会在加载期直接报错。她在群里不会知道你此刻开着什么、屏幕上是什么；但她在私下里记住的事，在群里仍然记得。
