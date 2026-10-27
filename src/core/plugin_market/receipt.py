"""读写市场安装收据并核对插件文件指纹，保护手动改动不被升级覆盖。

operations 写入收据，管理接口和升级检查读取；配置与解释器缓存不参与指纹。
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import hashlib
import json
import os

# 市场所有权凭证文件名，不列入自身文件指纹。
RECEIPT_FILENAME = '.yueli-install.json'


def _files(directory: Path) -> Dict[str, str]:
    """计算受管理文件的 SHA-256；排除配置、收据和任意层级的解释器缓存。"""
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob('*')) if path.is_file()
            and path.name not in ('config.toml', RECEIPT_FILENAME)
            and '__pycache__' not in path.relative_to(directory).parts}


def read_receipt(directory: Path) -> Optional[Dict[str, Any]]:
    """读取收据；不存在返回 None，损坏则抛错，不把损坏凭证当成手动安装。"""
    path = directory / RECEIPT_FILENAME
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or not all(isinstance(raw.get(key), str) for key in ('source','index_url','id','version','commit','sha256','installed_at')):
        raise ValueError(f'插件安装收据字段非法：{path}')
    if not isinstance(raw.get('files'), dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in raw['files'].items()):
        raise ValueError(f'插件安装收据 files 非法：{path}')
    return raw


def write_receipt(directory: Path, *, source: str, index_url: str, plugin_id: str, version: str, commit: str, sha256: str) -> None:
    """原子写入来源与文件指纹，供升级、卸载和管理页识别市场安装。

    :param directory: 已解压或已经就位的插件目录。
    :param source: 索引源名称。
    :param index_url: 该版本所属索引地址。
    :param plugin_id: 索引与清单共同确认的 id。
    :param version: 安装版本。
    :param commit: 四十位固定提交摘要。
    :param sha256: 下载归档的六十四位摘要。
    :raises OSError: 扫描或写入失败，调用方负责目录事务恢复。
    """
    payload = dict(source=source, index_url=index_url, id=plugin_id, version=version, commit=commit, sha256=sha256,
                   installed_at=datetime.now(timezone.utc).isoformat(), files=_files(directory))
    temporary = directory / (RECEIPT_FILENAME + '.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, directory / RECEIPT_FILENAME)
    finally:
        temporary.unlink(missing_ok=True)


def modified_files(directory: Path) -> List[str]:
    """返回新增、修改和删除文件的相对路径；无收据时拒绝执行指纹比较。"""
    receipt = read_receipt(directory)
    if receipt is None:
        raise ValueError('手动安装插件没有收据，无法比较代码改动')
    actual = _files(directory)
    expected = receipt['files']
    return sorted(name for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name))
