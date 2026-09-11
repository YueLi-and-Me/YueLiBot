"""发布后向指定群公告新版本。

这一包会往群里发消息，两条底线必须有断言盯着：默认配置下永不触发，以及
用户的 bot 拿不到触发条件。任何网络或投递失败都不得进入功能路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

import json

import pytest

from src.core.app_meta import APP_VERSION
from src.core.config.bootstrap import _feature_document
from src.core.config.schema import UpdateAnnounceConfig
from src.core.runtime import update_notes
from src.core.runtime.update_notes import announce_update, read_state
from src.core.services import update_announce
from src.core.services.update_announce import (
    ANNOUNCED_FIELD,
    UpdateAnnounceService,
    announcement_text,
    is_newer,
    parse_version,
)

ANNOUNCE_GROUP = '424949962'
CHANGELOG = """\
# 更新日志

## [9.9.9] - 2026-12-31

### 新增

- 未来版本的一号条目
- 未来版本的二号条目
"""


@dataclass
class _FakeStream:
    """出站投递需要的最小 stream 形状。"""

    id: int = 7
    platform: str = 'qq'
    kind: str = 'group'
    external_id: str = ANNOUNCE_GROUP


@dataclass
class _FakeRegistry:
    """记录被解析过的群号，并按需返回一个固定 stream。"""

    created: List[tuple] = field(default_factory=list)

    def get_or_create_stream(self, platform: str, kind: str, external_id: str) -> Any:
        self.created.append((platform, kind, external_id))
        return _FakeStream(external_id=external_id)


@dataclass
class _FakeBroker:
    """记录投递过的正文；按 ``fail`` 决定是否抛错。"""

    sent: List[str] = field(default_factory=list)
    fail: Exception | None = None

    async def dispatch(self, message: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append('\n'.join(message.segments))


def _service(
    tmp_path: Path,
    config: UpdateAnnounceConfig,
    *,
    broker: _FakeBroker | None = None,
    registry: _FakeRegistry | None = None,
    notes_text: str = CHANGELOG,
) -> UpdateAnnounceService:
    """构造一个只连本地假件的公告服务。"""
    project = tmp_path / 'repo'
    project.mkdir(exist_ok=True)
    (project / update_notes.CHANGELOG_FILENAME).write_text(notes_text, encoding='utf-8')
    registered: List[Any] = []
    service = UpdateAnnounceService(
        tmp_path,
        config,
        project_root=project,
        registry=registry or _FakeRegistry(),
        broker=broker or _FakeBroker(),
        register_stream=registered.append,
        endpoint='https://例子',
        interval_s=600.0,
    )
    # 把假件挂成实例属性，测试里直接读，不必再各自构造一遍。
    service.registered = registered  # type: ignore[attr-defined]
    return service


def _enabled() -> UpdateAnnounceConfig:
    return UpdateAnnounceConfig(enabled=True, group=ANNOUNCE_GROUP)


def _one_minor_above(version: str) -> str:
    """给出比 ``version`` 高一个次版本号的版本号，用于构造「本机还在旧版上」的场景。"""
    parts = [int(part) for part in version.split('.')]
    parts[1] += 1
    return '.'.join(str(part) for part in parts[:2] + [0] * (len(parts) - 2))


def _changelog_for(version: str) -> str:
    """造一份含指定版本小节的更新日志。"""
    return f'# 更新日志\n\n## [{version}] - 2026-12-31\n\n- {version} 的条目\n'


def test_版本号数字比较而不是字典序() -> None:
    """`0.10.0` 在字典序里小于 `0.9.0`，按字符串比会让新版本永远公告不出去。"""
    assert parse_version('0.1.3') == (0, 1, 3)
    assert parse_version(' 1.2.3 ') == (1, 2, 3)
    assert parse_version('1.2.3-beta') is None
    assert is_newer('0.10.0', '0.9.0') is True
    assert is_newer('0.1.3', '0.1.3') is False
    assert is_newer('0.1.2', '0.1.3') is False
    assert is_newer('乱七八糟', APP_VERSION) is False


def test_默认配置下永不触发() -> None:
    """这一段默认关闭，用户不改配置就不会有任何公告。"""
    assert UpdateAnnounceConfig().enabled is False
    assert UpdateAnnounceConfig().group == ''


def test_开关关闭时链路惰性(tmp_path: Path) -> None:
    """显式填了群号但没开开关，同样不许发。"""
    service = _service(tmp_path, UpdateAnnounceConfig(enabled=False, group=ANNOUNCE_GROUP))

    assert service.active is False


def test_群号为空时链路惰性(tmp_path: Path) -> None:
    """开了开关却没有目标群，什么都不该发生。"""
    service = _service(tmp_path, UpdateAnnounceConfig(enabled=True, group=''))

    assert service.active is False


def test_群号必须是数字() -> None:
    """把非数字当群号填进去会在投递时才炸，不如在加载配置时直接报错。"""
    with pytest.raises(ValueError, match='数字 QQ 群号'):
        UpdateAnnounceConfig(enabled=True, group='月璃官方群')


def test_状态文件损坏时按未公告处理(tmp_path: Path) -> None:
    """状态坏了最多多发一次公告，不能让异常带进启动路径。"""
    update_notes.state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    update_notes.state_path(tmp_path).write_text('{ 这不是 JSON', encoding='utf-8')

    assert read_state(tmp_path) == {}


def test_两份状态互不覆盖(tmp_path: Path) -> None:
    """「上次运行版本」与「已公告版本」写在同一个文件里，整体读改写才不会互相冲掉。"""
    announce_update(tmp_path, Path('/不存在'), '0.1.0')
    update_notes.write_state(tmp_path, {ANNOUNCED_FIELD: '9.9.9'})

    assert update_notes.read_last_version(tmp_path) == '0.1.0'
    assert read_state(tmp_path)[ANNOUNCED_FIELD] == '9.9.9'


def test_用户的配置模板里没有这一段() -> None:
    """这是「只有维护者那个 bot 会触发」的结构性保证，不是靠文档提醒。

    发布出去的模板里不存在 [update_announce]，用户不手写这一段就没有群号，
    也就没有任何东西能触发公告。
    """
    assert 'update_announce' not in _feature_document()


@pytest.mark.asyncio
async def test_远端版本不比本机新时不公告(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """远端落后或相同时什么都不做，否则每次发版都会公告一个「未来版本」。"""
    broker = _FakeBroker()
    service = _service(tmp_path, _enabled(), broker=broker)

    async def _latest() -> str:
        return '0.0.1'

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is False
    assert broker.sent == []


@pytest.mark.asyncio
async def test_升级先于公告时仍然公告(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本机已经升到远端说的那一版，也不能把公告吞掉。

    - 现象：发版后先把服务器升上去、再看公告，结果什么都没有。
    - 原因：门槛原先固定取「本机正在跑的版本」，远端升到同一版本后就不再算更新。
    - 后果：升级与公告常在同一分钟里先后发生，这个窗口一旦错开，公告永久丢失。
    """
    newer = _one_minor_above(APP_VERSION)
    broker = _FakeBroker()
    service = _service(tmp_path, _enabled(), broker=broker, notes_text=_changelog_for(newer))
    # 状态文件说上次跑的是旧版，而本机已经是新版：这就是「刚刚升上来」。
    update_notes.write_state(tmp_path, {'version': APP_VERSION})
    monkeypatch.setattr(update_announce, 'APP_VERSION', newer)

    async def _latest() -> str:
        return newer

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is True
    assert len(broker.sent) == 1
    assert broker.sent[0].startswith(f'月璃更新到 {newer}')
    assert read_state(tmp_path)[ANNOUNCED_FIELD] == newer


@pytest.mark.asyncio
async def test_全新安装不会把历史版本刷进群(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """刚装好、状态文件里什么都没有时，不能把远端那个更旧的版本当更新发出来。"""
    older = '0.0.1'
    broker = _FakeBroker()
    service = _service(tmp_path, _enabled(), broker=broker, notes_text=_changelog_for(older))

    async def _latest() -> str:
        return older

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is False
    assert broker.sent == []


@pytest.mark.asyncio
async def test_已公告的同版本不再重复(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复公告由状态文件兜住，与门槛无关。"""
    version = _one_minor_above(APP_VERSION)
    broker = _FakeBroker()
    service = _service(tmp_path, _enabled(), broker=broker, notes_text=_changelog_for(version))
    update_notes.write_state(tmp_path, {ANNOUNCED_FIELD: version})

    async def _latest() -> str:
        return version

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is False
    assert broker.sent == []


@pytest.mark.asyncio
async def test_新版本公告一次且只公告一次(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """公告正文来自更新日志，状态记在同一个状态文件里。"""
    broker = _FakeBroker()
    registry = _FakeRegistry()
    service = _service(tmp_path, _enabled(), broker=broker, registry=registry)

    async def _latest() -> str:
        return '9.9.9'

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is True
    assert len(broker.sent) == 1
    assert broker.sent[0].startswith('月璃更新到 9.9.9')
    assert '- 未来版本的一号条目' in broker.sent[0]
    # 群号经注册表解析成 stream，且驱动注册回调被调用过：否则首次投递会直接失败。
    assert registry.created == [('qq', 'group', ANNOUNCE_GROUP)]
    assert len(service.registered) == 1
    assert read_state(tmp_path)[ANNOUNCED_FIELD] == '9.9.9'
    # 再检查一次不得重复发。
    assert await service.check_once() is False
    assert len(broker.sent) == 1


@pytest.mark.asyncio
async def test_更新日志缺该版本时不公告也不记状态(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """广播端点说发了新版、更新日志却还没补：等补上以后下一轮自动补发。"""
    broker = _FakeBroker()
    service = _service(tmp_path, _enabled(), broker=broker, notes_text='# 更新日志\n')

    async def _latest() -> str:
        return '9.9.9'

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is False
    assert broker.sent == []
    assert ANNOUNCED_FIELD not in read_state(tmp_path)


@pytest.mark.asyncio
async def test_投递失败不记状态(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """发不出去就不算公告过，下一轮还要再试。"""
    broker = _FakeBroker(fail=RuntimeError('适配器没有订阅者'))
    service = _service(tmp_path, _enabled(), broker=broker)

    async def _latest() -> str:
        return '9.9.9'

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is False
    assert broker.sent == []
    assert ANNOUNCED_FIELD not in read_state(tmp_path)


@pytest.mark.asyncio
async def test_公告正文与更新日志逐字一致(tmp_path: Path) -> None:
    """群里看到的与更新日志同源，不在群里另造一套排版。"""
    _date, lines = update_notes.parse_changelog(CHANGELOG)['9.9.9']
    text = announcement_text('9.9.9', lines)

    assert text.splitlines()[0] == '月璃更新到 9.9.9'
    assert text.splitlines()[2:] == lines


@pytest.mark.asyncio
async def test_端点尚未广播过时不公告也不报错(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次发布之前端点的 version 是空串，那是空态而不是畸形响应。

    当成错误处理的话，每次检查都会在日志里留一条没人能处理的警告。
    """
    broker = _FakeBroker()
    service = _service(tmp_path, _enabled(), broker=broker)

    async def _latest() -> str:
        return ''

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    assert await service.check_once() is False
    assert broker.sent == []


@pytest.mark.asyncio
async def test_远端返回结构不符时不公告(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """结构不对就按"没有更新"处理，而不是把畸形内容发进群。"""
    broker = _FakeBroker()
    service = _service(tmp_path, _enabled(), broker=broker)

    async def _latest() -> str:
        raise ValueError('/update/latest 返回的顶层不是对象')

    monkeypatch.setattr(UpdateAnnounceService, 'fetch_latest', lambda self: _latest())

    with pytest.raises(ValueError):
        await service.check_once()
    assert broker.sent == []


def test_检查间隔必须为正() -> None:
    """间隔写错会在启动期就暴露，而不是变成每秒钟一次的忙轮询。"""
    with pytest.raises(ValueError, match='间隔必须为正数'):
        UpdateAnnounceService(
            Path('/tmp'),
            _enabled(),
            project_root=Path('/tmp'),
            registry=_FakeRegistry(),
            broker=_FakeBroker(),
            register_stream=lambda _stream: None,
            endpoint='https://例子',
            interval_s=0,
        )


def test_状态文件始终可解析(tmp_path: Path) -> None:
    """两份功能共用这个文件，格式必须始终可解析。"""
    update_notes.write_state(tmp_path, {'version': '0.1.3'})
    update_notes.write_state(tmp_path, {ANNOUNCED_FIELD: '0.1.3'})

    document = json.loads(update_notes.state_path(tmp_path).read_text(encoding='utf-8'))

    assert document == {'version': '0.1.3', ANNOUNCED_FIELD: '0.1.3'}
