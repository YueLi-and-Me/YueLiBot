"""
YueLiBot Python 后端入口。

放在仓库根而不是包里，是为了让 `src/` 保持纯粹的包结构：根目录一个 bot.py，
业务全在 src/ 下。真正的启动逻辑在 src/main.py，这里只负责把它跑起来。

    python bot.py --data-dir <dir> --config-path <dir>

入口反转后本进程就是整套应用的入口：监听建立后依次拉起 QQ 适配器与 Electron
桌面外壳（后者受 bot.toml 的 [desktop_pet] enabled 控制），退出时按相反顺序收走。
Python 生成运行时 token 并写入 data/runtime/backend.json，外壳只读它来连接。
"""

from src.main import main

if __name__ == "__main__":
    main()
