"""
YueLiBot Python 后端入口。

放在仓库根而不是包里，是为了让 `src/` 保持纯粹的包结构：根目录一个 bot.py，
业务全在 src/ 下。真正的启动逻辑在 src/main.py，这里只负责把它跑起来。

    python bot.py --data-dir <dir> --config-path <dir>

Python 会生成运行时 token；Electron 侧只解析启动公告，工作目录为仓库根。
"""

from src.main import main

if __name__ == "__main__":
    main()
