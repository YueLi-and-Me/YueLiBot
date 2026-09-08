"""声明应用版本号的唯一来源。

pyproject.toml 与 package.json 的 ``version`` 字段都必须与本模块的
:data:`APP_VERSION` 相等，该约束由机检（pytests/core/test_app_version.py）
钉住：任何一处单独改动都会在测试中失败。改版本号时先改本模块，再同步那
两处声明文件。运行时代码一律从这里导入 APP_VERSION，不重复写字面量。

本模块保持零依赖：版本号是全项目最低层的事实，任何模块都要能导入它。
"""

from __future__ import annotations

APP_VERSION = '0.1.1'
