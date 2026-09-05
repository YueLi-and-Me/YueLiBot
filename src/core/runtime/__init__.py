"""进程运行期的基础设施。

本包提供统一毫秒时钟（``clock``）、外部子进程监护（``child_process``）、后端连接
坐标的生成与读取（``backend_runtime``），以及只读的启动健康检查（``self_check``）。

依赖方向是单向的：本包依赖 ``src.core.logging`` 与 ``src.core.db``，反向不成立。
"""
