"""校验插件归档边界并解到全新随机目录，拒绝路径逃逸和链接条目。

operations 在完整性下载校验之后调用，清单校验复用 plugin_system.manifest。
"""

from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import shutil
import stat

from src.plugin_system.manifest import load_manifest

# 单包最多文件与目录条目数，正整数；限制大量空文件耗尽文件系统资源。
MAX_ARCHIVE_ENTRIES = 5000
# 解压总量上限，单位字节；压缩包大小不代表展开后的磁盘占用。
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024


def extract_archive(path: Path, staging: Path, *, plugin_id: str, version: str) -> Path:
    """验证全部条目后解压，检查顶层清单与索引 id、版本逐字相等。

    :param path: 已校验摘要的 zip 路径。
    :param staging: 市场暂存根目录，解压只在新建 UUID 子目录内进行。
    :param plugin_id: 索引登记的插件 id。
    :param version: 索引登记的插件版本。
    :return: 解压出的唯一顶层插件目录。
    :raises ValueError: 归档、路径、链接、大小或清单不符合契约。
    :raises OSError: 文件系统失败；本次创建的暂存目录会清除。
    """
    staging.mkdir(parents=True, exist_ok=True)
    destination = staging / str(uuid4())
    destination.mkdir()
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError('插件包条目超过上限')
            if sum(entry.file_size for entry in entries) > MAX_EXTRACTED_BYTES:
                raise ValueError('插件包解压总量超过上限')
            tops = set()
            names = set()
            for entry in entries:
                name = entry.filename
                relative = PurePosixPath(name)
                if not name or not relative.parts or '\\' in name or ':' in name or relative.is_absolute() or PureWindowsPath(name).drive or '..' in relative.parts:
                    raise ValueError(f'插件包含非法路径：{name}')
                if any(part.endswith((' ', '.')) or part.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *{f'COM{i}' for i in range(1,10)}, *{f'LPT{i}' for i in range(1,10)}} for part in relative.parts):
                    raise ValueError(f'插件包含系统保留路径：{name}')
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.external_attr & 0x400:
                    raise ValueError(f'插件包含符号链接或重解析条目：{name}')
                if len(relative.parts) == 1 and not entry.is_dir():
                    raise ValueError('插件包顶层必须恰好是一个目录')
                normalized = str(relative).casefold()
                if normalized in names:
                    raise ValueError(f'插件包路径重复：{name}')
                names.add(normalized)
                tops.add(relative.parts[0])
            if len(tops) != 1:
                raise ValueError('插件包顶层必须恰好是一个目录')
            archive.extractall(destination)
        directory = destination / next(iter(tops))
        manifest = load_manifest(directory / '_manifest.json')
        if manifest.plugin_id != plugin_id or manifest.version != version:
            raise ValueError(f'插件包清单与索引不符：{manifest.plugin_id}@{manifest.version}，预期 {plugin_id}@{version}')
        return directory
    except Exception as exc:
        shutil.rmtree(destination)
        if isinstance(exc, BadZipFile):
            raise ValueError(f'插件包不是有效 zip：{exc}') from exc
        raise
