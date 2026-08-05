"""
YueLiBot Python 后端入口。

放在仓库根而不是包里，是为了让 `src/` 保持纯粹的包结构（照 MaiBot 的分法：
根目录一个 bot.py，业务全在 src/ 下）。真正的启动逻辑在 src/main.py，
这里只负责把它跑起来。

    python bot.py --data-dir <dir> --config-path <dir> --token <token>

Electron 侧的 PythonSupervisor 就是这么拉起后端的，工作目录为仓库根。
"""

from src.main import main

if __name__ == "__main__":
    main()
