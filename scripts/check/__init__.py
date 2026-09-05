"""只读校验脚本：报告是否合规，不写库、不改配置。

- ``self_check``：运行时健康检查的命令行入口，与启动路径共用 ``src.core.runtime.self_check``。
- ``config_parity``：校验 Electron 写入器与 Python schema 是否仍是超集关系，漂移时退出码 1。
- ``config_defaults.ts``：供 ``config_parity`` 调用，把写入器的全默认配置导出到临时目录。
"""
