"""数据维护脚本：会写 ``data/memory.db``，执行前先备份。

- ``knowledge_reindex`` / ``vector_quantize``：为存量数据补算索引与量化列。
- ``emoji_orphan_cleanup``：清理库里无记录的表情文件。
- ``fix_expression_style_prefix`` / ``jargon_guard_names``：对存量表达与黑话词条的一次性清洗。
"""
