"""验证 Python 入口对子进程的监护：桌宠开关、适配器可选性与进程原语。

覆盖入口反转之后新增的三块：

- ``src.core.services.host.desktop_shell``：桌宠关闭时不产出任何外壳进程（无头形态），
  开启时按仓库里实际存在的东西解析启动命令，解析不出来直接报错。
- ``src.core.services.host.adapter_host``：缺声明或缺连接配置时跳过适配器，不阻断后端。
- ``src.core.runtime.child_process``：输出按行贴标签转发、返回码 0 不重启、异常退出
  重启、``stop()`` 真的把进程收走。

对应 ``src.main._build_children`` 的装配逻辑一并在此验证。
"""

from __future__ import annotations

from pathlib import Path

import asyncio
import sys

import pytest

from src.core.runtime.child_process import ChildProcess
from src.core.config.schema import Config
from src.core.services.host.adapter_host import adapter_log_tag, build_adapter_process
from src.core.services.host.desktop_shell import build_desktop_shell_process, resolve_shell_command

import src.main as entry


def _config(pet_enabled: bool) -> Config:
    """构造只关心桌宠开关的配置对象。

    :param pet_enabled: ``[desktop_pet] enabled`` 的取值。
    :return: 其余字段全取默认值的配置对象。
    """
    config = Config()
    config.desktop_pet.enabled = pet_enabled
    return config


def test_桌宠关闭时不拉起桌面外壳() -> None:
    """`[desktop_pet] enabled = false` 是无头部署的形态，不得产出外壳进程。"""
    assert build_desktop_shell_process(
        False, entry.PROJECT_ROOT, Path('data'), Path('config')) is None


def test_桌宠关闭时装配结果里没有外壳() -> None:
    """经由入口装配也是同一结论：子进程列表里不会出现 desktop_shell。

    不断言适配器在不在：它取决于工作区里有没有 config/adapter.toml，
    与本条要证的桌宠开关无关。
    """
    children = entry._build_children(_config(False), Path('config'), Path('data'), True)

    assert 'desktop_shell' not in [child.name for child in children]


def test_桌宠开启时装配出外壳() -> None:
    """开着桌宠就该拉外壳；命令由仓库形态决定，这里只断言进程被装配进来。"""
    children = entry._build_children(_config(True), Path('config'), Path('data'), True)

    assert 'desktop_shell' in [child.name for child in children]


def test_no_shell_覆盖桌宠开关() -> None:
    """开发时外壳单独跑 npm run dev，入口不得再拉一个。"""
    children = entry._build_children(_config(True), Path('config'), Path('data'), False)

    assert 'desktop_shell' not in [child.name for child in children]


def test_外壳无法解析时报错而不是静默跳过(tmp_path: Path) -> None:
    """空目录里既没有开发依赖也没有构建产物，用户开着桌宠必须当场知道原因。"""
    with pytest.raises(RuntimeError) as excinfo:
        resolve_shell_command(tmp_path)

    assert 'npm install' in str(excinfo.value)


def test_外壳解析失败不阻断后端(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外壳是界面组件，起不来不该让 QQ 与 WebUI 一起停掉。"""
    monkeypatch.setattr(entry, 'PROJECT_ROOT', tmp_path)

    children = entry._build_children(_config(True), Path('config'), Path('data'), True)

    assert 'desktop_shell' not in [child.name for child in children]


def test_缺少适配器声明时跳过适配器(tmp_path: Path) -> None:
    """没有 config/adapter.toml 表示这台机器不接 QQ，只启动主体。"""
    assert build_adapter_process(entry.PROJECT_ROOT, tmp_path, tmp_path) is None


def test_适配器日志标签跟着插件目录走() -> None:
    """写死协议端名字会出现「拉起的是 A、日志写着 B」。"""
    assert adapter_log_tag('yueli-snowluma-adapter') == 'snowluma'
    assert adapter_log_tag('yueli-napcat-adapter') == 'napcat'


async def test_子进程输出按行贴标签转发(capsys: pytest.CaptureFixture[str]) -> None:
    """适配器与外壳的日志混在同一个控制台里，靠标签区分来源。"""
    child = ChildProcess(
        name='echo',
        tag='probe',
        argv=[sys.executable, '-c', "print('第一行'); print('第二行')"],
        cwd=entry.PROJECT_ROOT,
        restart_on_failure=False,
    )
    await child.start()
    # 等进程自己跑完；转发任务在退出监视里被 drain，输出此时已经写完。
    await asyncio.sleep(2)
    await child.stop()

    output = capsys.readouterr().out
    assert '[probe] 第一行' in output
    assert '[probe] 第二行' in output


async def test_返回码为零不触发重启() -> None:
    """用户主动关掉桌面外壳走的就是这一条，重启会让窗口关不掉。"""
    child = ChildProcess(
        name='clean-exit',
        tag='probe',
        argv=[sys.executable, '-c', 'raise SystemExit(0)'],
        cwd=entry.PROJECT_ROOT,
    )
    await child.start()
    await asyncio.sleep(2)

    assert not child.alive
    await child.stop()


async def test_stop_收走仍在运行的子进程() -> None:
    """退出链依赖这一条：子进程必须在后端开始收尾之前真的消失。"""
    child = ChildProcess(
        name='long-running',
        tag='probe',
        argv=[sys.executable, '-c', 'import time; time.sleep(300)'],
        cwd=entry.PROJECT_ROOT,
        restart_on_failure=False,
    )
    await child.start()
    assert child.alive

    await child.stop(grace=5.0)

    assert not child.alive


async def test_首次拉起失败直接抛出() -> None:
    """启动阶段的错误不做重试也不吞掉，由调用方决定降级还是终止。"""
    child = ChildProcess(
        name='missing',
        tag='probe',
        argv=[str(entry.PROJECT_ROOT / '不存在的可执行文件_yueli_test.exe')],
        cwd=entry.PROJECT_ROOT,
    )

    with pytest.raises(OSError):
        await child.start()
