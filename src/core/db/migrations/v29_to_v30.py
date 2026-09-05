"""v29 -> v30：删除运行画像表，它的两个理由都在落地当天作废了。

``runtime_profile`` 于 v29 建立，当时的依据是「它恰好就是将来遥测上报的载荷本体」。
随后查实参考实现之后这条依据不成立：

- 上报只有**三个字段**（应用版本、系统类型、Python 版本），由客户端主动推送；
  启动次数、累计运行时长、库规模一个都不上传。「多少在线」是服务端按最后心跳
  时间数活跃安装，客户端不需要计算在线时长。
- 身份改为**服务端下发 UUID**，客户端不自己生成安装 ID；本表里那个随机值
  因此也没有消费方。

唯一读它的是 ``/stat`` 开发者命令，而那条命令的字段一半与 ``/version`` 重复、
另一半没有任何决策挂钩，已一并删除。表留着就是没有消费方的代码，
所以整表删掉而不是留一列空转。

重放幂等：表不存在时整体跳过。删表不影响任何功能——它本就是可丢的派生数据。
"""

from __future__ import annotations

import sqlite3

from .registry import register

FROM_VERSION = 29


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    """判断给定表是否存在。

    :param db: 目标连接。
    :param name: 表名。
    :return: 存在返回 ``True``。
    副作用：只读 sqlite_master。
    """
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """删除 ``runtime_profile`` 表。

    :param db: 迁移事务内的连接。
    :return: ``None``。
    :raises sqlite3.Error: 删表失败。
    副作用：删除 ``runtime_profile`` 及其数据；不触碰任何其它表。
    """
    if not _table_exists(db, 'runtime_profile'):
        return
    db.execute('DROP TABLE runtime_profile')
    if _table_exists(db, 'runtime_profile'):
        raise sqlite3.OperationalError('迁移 v29 -> v30 之后 runtime_profile 仍然存在')
