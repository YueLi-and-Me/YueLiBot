"""保存服务工具清单指纹并逐项展示变化，不保存连接参数或凭证。

说明变化可能改变模型收到的指令；开放且未覆盖说明时提升为告警，仍由部署者停用。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, Set, Tuple
import hashlib
import json

from src.core.logging.logger import get_logger

from .compat.types import McpServerInfo, McpTool

logger = get_logger(__name__)


@dataclass(frozen=True)
class McpSnapshotChange:
    """一项工具变化；说明变化保留前 100 字，供控制台与管理页展示。"""

    kind: str
    tool: str
    old_head: str = ''
    new_head: str = ''


def save_snapshot(data_dir: Path, name: str, info: McpServerInfo, tools: Tuple[McpTool, ...],
                  *, exposed: Set[str], overrides: Set[str]) -> Tuple[McpSnapshotChange, ...]:
    """比较前次快照并原子保存新指纹；第一次连接只写不报告变化。

    :param data_dir: 宿主数据根目录，快照写入其 mcp 子目录。
    :param name: 已校验服务名。
    :param info: 服务器自述与协商协议版本。
    :param tools: 全部发现工具，包含未开放工具。
    :param exposed: 实际开放的原始工具名。
    :param overrides: 配置覆盖说明的原始工具名。
    :return: 本次变化列表；不会因变化自动撤销工具。
    :raises OSError: 快照读写失败，不掩盖本机文件故障。
    """
    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    path = data_dir / 'mcp' / f'{name}.json'
    current: Dict[str, Any] = {t.name: {'description_sha256': digest(t.description),
        'schema_sha256': digest(t.input_schema), 'description_head': (t.description or '')[:100]} for t in tools}
    changes = []
    if path.is_file():
        previous = json.loads(path.read_text(encoding='utf-8'))['tools']
        for tool in sorted(current.keys() - previous.keys()):
            changes.append(McpSnapshotChange('新增', tool))
        for tool in sorted(previous.keys() - current.keys()):
            changes.append(McpSnapshotChange('移除', tool))
        for tool in sorted(current.keys() & previous.keys()):
            old, new = previous[tool], current[tool]
            if old['description_sha256'] != new['description_sha256']:
                changes.append(McpSnapshotChange('说明变化', tool, old['description_head'], new['description_head']))
            if old['schema_sha256'] != new['schema_sha256']:
                changes.append(McpSnapshotChange('参数变化', tool))
        for change in changes:
            log = logger.warning if change.kind == '说明变化' and change.tool in exposed - overrides else logger.info
            log(f'MCP 服务 {name} 工具 {change.tool} {change.kind}' +
                (f'：旧说明「{change.old_head}」，新说明「{change.new_head}」' if change.kind == '说明变化' else ''))
    document = {'server_name': info.name, 'server_version': info.version, 'protocol_version': info.protocol_version,
                'tools': current, 'saved_at': datetime.now().astimezone().isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return tuple(changes)
