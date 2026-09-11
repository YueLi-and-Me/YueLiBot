"""匿名安装统计的客户端。

这一包默认开启且会出网，两条底线各自要有断言盯着：载荷范围不许扩大，
以及遥测的任何失败都不得进入功能路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncio
import json

import pytest

from src.core.app_meta import APP_VERSION
from src.core.config.schema import LogConfig
from src.core.logging.logger import initialize_logging
from src.core.runtime import telemetry


def test_载荷只有三个字段() -> None:
    """载荷范围是拍板过的。加字段容易，减字段要重新征求同意。"""
    body = telemetry.payload()

    assert set(body) == {'app_version', 'os_type', 'python_version'}
    assert body['app_version'] == APP_VERSION


def test_载荷不含任何身份或内容() -> None:
    """逐字扫一遍，确认没有把别的东西夹带进去。"""
    serialized = json.dumps(telemetry.payload(), ensure_ascii=False).lower()

    for token in ('uuid', 'token', 'key', 'qq', 'message', 'model', 'path'):
        assert token not in serialized, f'载荷里出现了 {token}'


def test_端点为空时保持惰性(tmp_path: Path) -> None:
    """服务端尚未立起时不得发出任何请求。

    默认开启意味着发行版里这条路径是活的；端点常量为空时整条链路必须惰性，
    否则每个安装每十分钟做一次注定失败的请求。
    """
    service = telemetry.TelemetryService(tmp_path, enabled=True, endpoint='')

    assert service.active is False


def test_开关关闭时保持惰性(tmp_path: Path) -> None:
    """开关关掉之后，即使端点填了也不启动。"""
    service = telemetry.TelemetryService(tmp_path, enabled=False, endpoint='https://例子')

    assert service.active is False


@pytest.mark.asyncio
async def test_惰性时启动不建任何任务(tmp_path: Path) -> None:
    """惰性状态下 startup 必须是空操作，且不留下后台任务。"""
    service = telemetry.TelemetryService(tmp_path, enabled=True, endpoint='')

    await service.startup()
    await service.shutdown()

    assert telemetry.identity_path(tmp_path).exists() is False


@pytest.mark.asyncio
async def test_网络失败不抛给调用方(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """遥测绝不能影响正常使用：失败只记 warning，不得抛出。

    这里直接让注册炸掉，断言一轮心跳照常返回——它一旦抛出，就会顺着生命周期
    把启动或收尾一起带崩。
    """
    service = telemetry.TelemetryService(tmp_path, enabled=True, endpoint='https://例子')

    async def _boom(self: Any) -> None:
        raise RuntimeError('网络炸了')

    monkeypatch.setattr(telemetry.TelemetryService, '_register', _boom)

    await service._beat_once()

    assert telemetry.identity_path(tmp_path).exists() is False


def test_身份读写往返(tmp_path: Path) -> None:
    """写进去的 UUID 要能原样读回来。"""
    telemetry.write_identity(tmp_path, 'abc-123')

    assert telemetry.read_identity(tmp_path) == 'abc-123'


def test_端点候选展开与惰性() -> None:
    """显式空地址必须解析为空候选：否则「服务端尚未立起」会被默认地址悄悄推翻。"""
    assert telemetry.resolve_endpoints('') == ()
    assert telemetry.resolve_endpoints('   ') == ()
    assert telemetry.resolve_endpoints('https://例子/') == ('https://例子',)
    assert telemetry.resolve_endpoints(None) == telemetry.TELEMETRY_ENDPOINTS
    # 首选是自建域名，被 DNS 污染的历史地址排在它后面兜底。
    assert telemetry.TELEMETRY_ENDPOINTS[0] == telemetry.TELEMETRY_ENDPOINT
    assert telemetry.TELEMETRY_ENDPOINT_LEGACY in telemetry.TELEMETRY_ENDPOINTS


def test_失败后重试间隔先压缩再放大() -> None:
    """600 秒的常规节拍照搬到首轮失败上，会让只开机几分钟的安装整段不上报。"""
    assert (
        telemetry.next_retry_interval(telemetry.HEARTBEAT_INTERVAL_S)
        == telemetry.RETRY_INTERVAL_S
    )
    assert telemetry.next_retry_interval(telemetry.RETRY_INTERVAL_S) == 2 * telemetry.RETRY_INTERVAL_S
    assert telemetry.next_retry_interval(telemetry.MAX_RETRY_INTERVAL_S) == telemetry.MAX_RETRY_INTERVAL_S
    assert telemetry.next_retry_interval(60.0) <= telemetry.MAX_RETRY_INTERVAL_S


@pytest.mark.asyncio
async def test_首选端点失败时改用兜底端点(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """主备两个地址是这条链路在国内可用的前提，顺序与短路行为都要钉住。"""
    service = telemetry.TelemetryService(tmp_path, enabled=True, endpoint=('https://首选', 'https://兜底'))
    called: list[str] = []

    async def _fake_attempt(self: Any, endpoint: str) -> tuple[bool, bool]:
        called.append(endpoint)
        if endpoint == 'https://首选':
            raise OSError('首选连不上')
        telemetry.write_identity(tmp_path, '兜底给的')
        return True, False

    monkeypatch.setattr(telemetry.TelemetryService, '_attempt', _fake_attempt)

    assert await service._beat_once() is True
    assert called == ['https://首选', 'https://兜底']
    assert telemetry.read_identity(tmp_path) == '兜底给的'


@pytest.mark.asyncio
async def test_全部端点拒绝身份后才丢本地身份(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """403 只在两个端点都不认这个 UUID 时才算身份失效。

    单端点被拒就删文件，会把「两套库各存一半」这种部署差异变成反复重注册，
    统计出来的装机量会凭空膨胀。
    """
    telemetry.write_identity(tmp_path, '旧身份')
    service = telemetry.TelemetryService(tmp_path, enabled=True, endpoint=('https://甲', 'https://乙'))

    async def _fake_attempt(self: Any, endpoint: str) -> tuple[bool, bool]:
        return False, True

    monkeypatch.setattr(telemetry.TelemetryService, '_attempt', _fake_attempt)

    assert await service._beat_once() is False
    assert telemetry.identity_path(tmp_path).exists() is False


@pytest.mark.asyncio
async def test_失败日志带异常类型与端点(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """连接类失败的 str(exc) 常常是空串，日志必须自带异常类型。

    这条断言盯着的是排障能力本身：远程用户报「统计不到」时，服务端那条路径上
    什么都不会留下，唯一的线索就是本机这行日志。
    """
    log_dir = tmp_path / 'logs'
    initialize_logging(LogConfig(to_file=True), log_dir)
    service = telemetry.TelemetryService(tmp_path, enabled=True, endpoint='https://连不上')

    async def _boom(self: Any, endpoint: str) -> tuple[bool, bool]:
        raise ConnectionError('')

    monkeypatch.setattr(telemetry.TelemetryService, '_attempt', _boom)

    assert await service._beat_once() is False
    written = [
        json.loads(line)
        for path in log_dir.glob('app_*.log.jsonl')
        for line in path.read_text(encoding='utf-8').splitlines()
    ]
    events = [entry for entry in written if entry['event'] == 'telemetry_failed']
    assert len(events) == 1
    assert events[0]['fields']['kind'] == 'ConnectionError'
    assert events[0]['fields']['endpoint'] == 'https://连不上'


def test_损坏的身份按未注册处理(tmp_path: Path) -> None:
    """解析失败时重新注册，而不是把遥测的失败带进启动路径。"""
    telemetry.identity_path(tmp_path).write_text('{ 这不是 JSON', encoding='utf-8')

    assert telemetry.read_identity(tmp_path) is None


def test_控制台告知说清了传什么和怎么关() -> None:
    """默认开启的功能必须当面讲清楚，埋在文档里不算说过。"""
    rows = '\n'.join(telemetry.describe_for_console(True, endpoint=''))

    assert 'app_version' in rows
    assert 'features.toml' in rows
    assert '不含聊天内容' in rows
    assert '尚未启用' in rows, '端点为空时要说明本版本不会发出请求'


def test_关闭时的告知只有一句() -> None:
    """关掉之后不必再念一遍传什么。"""
    rows = telemetry.describe_for_console(False)

    assert rows == ['匿名统计：已关闭。']


def test_配置默认开启() -> None:
    """默认值是拍板过的，改动要连协议一起改。"""
    from src.core.config.schema import Config

    assert Config().telemetry.enabled is True
