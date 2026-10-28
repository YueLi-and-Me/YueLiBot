"""在插件入口执行前检查 Python 依赖，并用完整版本约束安装缺失项。

启动组合根与插件市场复用本模块；清单读取依赖 manifest，开关读取依赖 config。
只通过 uv 安装索引包，不修改下载源；INSTALL_LOCK 串行化安装与市场文件操作。
"""

from dataclasses import dataclass
from importlib import metadata
from packaging.requirements import Requirement
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import os
import re
import shutil
import subprocess
import sys
import threading

from src.core.logging.logger import get_logger

from .config import read_enabled_flag
from .manifest import load_manifest

logger = get_logger(__name__)
# 同一进程的依赖安装与市场目录操作共用互斥锁，禁止嵌套获取。
INSTALL_LOCK = threading.Lock()
# 安装子进程等待上限，单位秒，取正整数。
INSTALL_TIMEOUT_SECONDS = 300
# 安装错误摘要的字符数上限，保留末尾以展示解析器最终原因。
STDERR_LIMIT = 2000
# 只允许完整版本约束；本地项目与可编辑路径不能作为 uv 的约束项。
PIN_PATTERN = re.compile(r'^[A-Za-z0-9._-]+==\S+$')


@dataclass(frozen=True)
class DependencyProblem:
    """保存清单依赖原文与当前版本；installed 为 None 表示尚未安装。"""

    requirement: str
    installed: Optional[str]


@dataclass(frozen=True)
class InstallResult:
    """保存安装结果、退出码和错误尾部；未正常退出时 returncode 为 None。"""

    success: bool
    returncode: Optional[int]
    stderr: str


def unsatisfied(requirements: Sequence[str]) -> List[DependencyProblem]:
    """检查已通过清单校验的声明，不导入包或执行子进程。

    :param requirements: PEP 508 声明；环境标记为假时跳过。
    :return: 缺失或版本不符合的声明，保持输入顺序，允许满足区间的预发布版本。
    :raises ValueError: 声明或已安装版本元数据非法；不掩盖环境损坏。
    """
    problems: List[DependencyProblem] = []
    for original in requirements:
        requirement = Requirement(original)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            installed = None
        if installed is None or not requirement.specifier.contains(installed, prereleases=True):
            problems.append(DependencyProblem(original, installed))
    return problems


def _installer_directory() -> Path:
    """返回 uv 官方安装脚本的默认安装目录，选择顺序与安装脚本一致。"""
    if os.environ.get('XDG_BIN_HOME'):
        return Path(os.environ['XDG_BIN_HOME'])
    if os.environ.get('XDG_DATA_HOME'):
        return Path(os.environ['XDG_DATA_HOME']).parent / 'bin'
    return Path.home() / '.local' / 'bin'


def find_uv() -> Optional[str]:
    """依次查 uv run 传入的路径、PATH 与官方安装脚本的默认目录；都不存在时返回 None。"""
    found = os.environ.get('UV') or shutil.which('uv')
    if found:
        return found
    # systemd 直接启动项目解释器时既没有 UV 变量，PATH 也不含用户目录；
    # 只查前两处会让按安装文档部署的服务器无法自动安装插件依赖。
    candidate = _installer_directory() / ('uv.exe' if os.name == 'nt' else 'uv')
    return str(candidate) if candidate.is_file() else None


def write_constraints(python: str, uv: str, path: Path) -> Path:
    """冻结指定解释器的已安装版本，排除本地项目和可编辑安装行。

    :param python: 当前宿主解释器绝对路径。
    :param uv: uv 可执行文件路径。
    :param path: 约束文件路径；自动创建父目录并覆盖旧快照。
    :return: 写入的约束文件路径。
    :raises RuntimeError: freeze 退出失败，携带 stderr 原文。
    :raises OSError: 子进程启动或文件写入失败。
    """
    result = subprocess.run([uv, 'pip', 'freeze', '--python', python],
                            capture_output=True, text=True, encoding='utf-8', timeout=INSTALL_TIMEOUT_SECONDS)
    if result.returncode:
        raise RuntimeError(f'生成插件依赖约束失败：{result.stderr}')
    # 现象：本地项目的 file 地址会使约束解析失败；原因：uv 不接受无名路径约束；
    # 后果：直接保存 freeze 全文会阻止所有插件补装，因此仅写名称与精确版本。
    pins = [line for line in result.stdout.splitlines() if PIN_PATTERN.fullmatch(line)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(pins) + '\n', encoding='utf-8')
    return path


def install(requirements: Sequence[str], *, uv: str, python: str, constraints: Path, proxy: str) -> InstallResult:
    """按完整约束安装缺失声明，不调整部署者的下载源。

    :param requirements: 待安装的索引包声明，来自已校验清单。
    :param uv: uv 可执行文件路径。
    :param python: 目标解释器路径，仅允许由宿主传入当前运行环境。
    :param constraints: 全部已安装包的版本快照路径。
    :param proxy: 非空时覆盖子进程 HTTP_PROXY 与 HTTPS_PROXY，否则继承环境。
    :return: 成功标记、退出码与不超过 STDERR_LIMIT 字符的错误摘要。
    副作用：成功时为宿主环境新增包；启动失败和超时均转成可展示的结果。
    """
    env = os.environ.copy()
    if proxy:
        env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy)
    try:
        result = subprocess.run([uv, 'pip', 'install', '--python', python, '--constraint', str(constraints), *requirements],
                                capture_output=True, text=True, encoding='utf-8', env=env, timeout=INSTALL_TIMEOUT_SECONDS)
        return InstallResult(result.returncode == 0, result.returncode, result.stderr[-STDERR_LIMIT:])
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr.decode('utf-8', errors='replace') if isinstance(exc.stderr, bytes) else exc.stderr
        return InstallResult(False, None, f'安装超过 {INSTALL_TIMEOUT_SECONDS} 秒：{stderr or ""}'[-STDERR_LIMIT:])
    except OSError as exc:
        return InstallResult(False, None, str(exc)[-STDERR_LIMIT:])


def manual_command(requirements: Sequence[str], *, python: str, constraints: Path) -> str:
    """生成手动安装命令，解释器、约束路径和每项声明均加双引号。

    :param requirements: 已校验依赖；仅传尚未满足的项。
    :param python: 当前解释器绝对路径。
    :param constraints: 当前环境约束快照的绝对路径。
    :return: 可直接复制到终端的 uv 命令字符串。
    """
    def quote(value: str) -> str:
        """转义双引号内的命令展开字符，Windows 命令面向 PowerShell。"""
        escape = '`' if os.name == 'nt' else '\\'
        for character in (escape, '$', '`', '"') if os.name != 'nt' else ('`', '$', '"'):
            value = value.replace(character, escape + character)
        return '"' + value + '"'
    return f'uv pip install --python {quote(python)} --constraint {quote(str(constraints))} ' + ' '.join(quote(value) for value in requirements)


def _manual_constraints(path: Path) -> None:
    """为禁用自动安装或缺少 uv 的环境生成手动约束，不启动子进程。

    :param path: 当前解释器的约束文件路径，父目录缺失时创建。
    :return: 无；完整记录已安装发行包的版本，文件错误直接交由调用方报告。
    """
    pins = sorted({f'{dist.metadata["Name"]}=={dist.version}' for dist in metadata.distributions()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(pin for pin in pins if PIN_PATTERN.fullmatch(pin)) + '\n', encoding='utf-8')


def ensure_plugin_dependencies_locked(roots: Sequence[Tuple[Path, bool]], *, auto_install: bool, data_dir: Path, proxy: str, only: Optional[Path] = None) -> Dict[Path, str]:
    """按插件 id 逐一检查第三方插件，只返回仍缺依赖的目录及完整说明。

    :param roots: 插件根目录与第三方标记；忽略内置根、隐藏目录及已停用插件。
    :param auto_install: 是否允许补装；关闭时不创建任何子进程。
    :param data_dir: 宿主数据目录，约束写入 plugin-deps/constraints.txt。
    :param proxy: 子进程代理，空字符串表示继承环境。
    :param only: 市场操作只检查本次处理的插件目录；启动预检省略时检查全部插件。
    :return: 尚未满足依赖的插件目录到说明的映射，说明含手动命令。
    调用方须已持有 INSTALL_LOCK；清单错误由注册表报告，安装错误不阻止其他插件。
    """
    if not INSTALL_LOCK.locked():
        raise RuntimeError('调用依赖安装前必须持有 INSTALL_LOCK')
    candidates = []
    for root, third_party in roots:
        if not third_party or not root.is_dir():
            continue
        for directory in root.iterdir():
            if only is not None and directory.resolve() != only.resolve():
                continue
            if not directory.is_dir() or directory.name.startswith('.'):
                continue
            try:
                if read_enabled_flag(directory) is False:
                    continue
                manifest = load_manifest(directory / '_manifest.json')
            except (ValueError, OSError) as exc:
                logger.debug('依赖预检跳过无法读取的插件，交由注册表报告', directory=str(directory), error=str(exc))
                continue
            if manifest.python_dependencies:
                candidates.append((manifest.plugin_id, directory, manifest.python_dependencies))
    notes: Dict[Path, str] = {}
    installed_count = 0
    constraints = (data_dir / 'plugin-deps/constraints.txt').resolve()
    for plugin_id, directory, requirements in sorted(candidates):
        problems = unsatisfied(requirements)
        if not problems:
            continue
        missing = [item.requirement for item in problems]
        uv = find_uv()
        reason = ''
        try:
            if not auto_install or uv is None:
                _manual_constraints(constraints)
                reason = '自动安装已关闭，需手动安装' if not auto_install else '未找到 uv，需手动安装'
            else:
                write_constraints(sys.executable, uv, constraints)
                logger.info('开始补装插件依赖', plugin=plugin_id, packages=missing, result='开始')
                result = install(missing, uv=uv, python=sys.executable, constraints=constraints, proxy=proxy)
                if result.success:
                    installed_count += len(missing)
                    write_constraints(sys.executable, uv, constraints)
                    logger.info('插件依赖补装成功', plugin=plugin_id, packages=missing, result='成功')
                else:
                    reason = result.stderr
                    if any(item.installed is not None for item in problems) or 'unsatisfiable' in reason.lower():
                        reason += '\n去掉 --constraint 硬装会改动月璃自己用的库，可能导致月璃启动失败'
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            reason = str(exc)
        remaining = unsatisfied(requirements)
        if remaining:
            detail = '；'.join(f'{p.requirement}（已装 {p.installed if p.installed is not None else "未安装"}）' for p in remaining)
            command = manual_command([p.requirement for p in remaining], python=sys.executable, constraints=constraints)
            notes[directory] = f'插件依赖缺失：{detail}\n{reason}\n{command}'
            logger.error('自动安装已关闭需手动' if not auto_install else '插件依赖补装失败',
                         plugin=plugin_id, packages=missing, result=notes[directory])
    if candidates:
        logger.info('插件依赖检查完成', checked=len(candidates), installed=installed_count, missing=len(notes))
    return notes


def ensure_plugin_dependencies(roots: Sequence[Tuple[Path, bool]], *, auto_install: bool, data_dir: Path, proxy: str) -> Dict[Path, str]:
    """在共享互斥锁内检查第三方插件依赖，供同步启动入口调用。

    :param roots: 根目录与第三方标记的有序序列。
    :param auto_install: 是否允许安装，关闭时仅给出手动说明。
    :param data_dir: 约束快照所在的宿主数据根目录。
    :param proxy: 安装子进程代理，空值继承环境。
    :return: 仍缺依赖的目录与完整说明。
    副作用：串行化扫描与补装；市场已持锁时调用 ensure_plugin_dependencies_locked，避免死锁。
    """
    with INSTALL_LOCK:
        return ensure_plugin_dependencies_locked(roots, auto_install=auto_install, data_dir=data_dir, proxy=proxy)
