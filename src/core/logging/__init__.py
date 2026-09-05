"""控制台与文件日志的实现层。

本包提供 structlog 封装（``logger``）、控制台配色与中文别名表（``logger_colors``）、
机器标识到中文展示文本的翻译（``log_display``）、JSONL 落盘与滚动清理（``log_sink``），
以及启动公告和管线追踪共用的信息框排版（``console_layout``）。

调用方通过 ``get_logger(__name__)`` 取得绑定模块名的 logger；``src.core.observe``
负责的是事件账本与回合追踪，与本包各管一段，不要混用。

包名与标准库 ``logging`` 同名不会造成遮蔽：Python 3 的导入是绝对导入，包内
``import logging`` 取到的仍是标准库；全项目的 ``sys.path`` 插入都指向仓库根，
没有任何位置把 ``src/core`` 本身放进搜索路径。
"""
