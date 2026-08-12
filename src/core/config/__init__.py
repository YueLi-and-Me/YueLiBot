"""配置模式、文件加载和全局配置访问入口。

``schema`` 定义并校验配置对象，``loader`` 负责读取分层配置和模型路由；本包导出
稳定的配置加载函数供进程入口和测试使用。
"""

from .schema import Config
from .loader import get_config, load_config, reset_config

__all__ = ["Config", "load_config", "get_config", "reset_config"]
