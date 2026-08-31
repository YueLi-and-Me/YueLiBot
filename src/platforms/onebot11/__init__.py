"""QQ 协议适配器包。

本包负责 QQ 配置读取、事件解析、消息段转换、WebSocket 传输和适配器运行循环；
`runner.py` 组合这些组件并向平台无关的 broker/driver 层提供统一事件。
"""
