"""验证索引、下载与市场文件操作；HTTP 用内存传输，安装包仅来自临时目录。"""

from importlib import import_module
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import errno
import hashlib
import json
import stat

import httpx
import pytest

from src.core.plugin_market.config import MarketSource
from src.plugin_system.manifest import check_host_compatibility, manifest_from_payload
from src.plugin_system.registry import PluginRegistry


def module(name):
    """延迟导入待验模块，使实现前每条用例独立报告缺失接口。"""
    return import_module('src.core.plugin_market.' + name)


def manifest(version='1.0.0', plugin_id='test.market'):
    """构造版本边界固定的合法清单，供索引和归档共同使用。"""
    return dict(manifest_version=2, id=plugin_id, plugin_type='tool', name='市场测试', version=version,
                description='用于离线验收', host_application={'min_version':'0.2.0', 'max_version':'0.2.5'})


def archive(version='1.0.0', extra=None):
    """生成确定字节的插件归档，防止 ZIP 时间戳导致摘要用例偶发失败。"""
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as zip_file:
        zip_file.writestr(ZipInfo('test.market/_manifest.json', (2026,10,27,0,0,0)), json.dumps(manifest(version)))
        zip_file.writestr(ZipInfo('test.market/plugin.py', (2026,10,27,0,0,0)), 'from src.plugin_system import ToolPlugin\nclass Example(ToolPlugin):\n    pass\n')
        for name, value in (extra or {}).items():
            zip_file.writestr(name, value)
    return output.getvalue()


def version_record(version='1.0.0', data=None):
    """为本地构造的归档生成完整索引版本条目。"""
    data = archive(version) if data is None else data
    return dict(version=version, commit='a'*40, host_application=manifest()['host_application'], python_dependencies=[],
                downloads=['https://one.example/package.zip','https://two.example/package.zip'], sha256=hashlib.sha256(data).hexdigest(),
                size=len(data), published_at='2026-10-27T00:00:00Z', yanked=False)


def index_payload():
    """构造符合协议的最小目录，不包含真实仓库或服务。"""
    return dict(schema_version=1, generated_at='2026-10-27T00:00:00Z', plugins=[dict(id='test.market', name='市场测试', description='测试',
                author='测试作者', license='MIT', repository='https://example.org/repository', tags=['测试'], versions=[version_record()])])


@pytest.mark.parametrize('kind', ['schema','missing','commit','sha256','size','downloads'])
def test_index_rejects_invalid(kind):
    """验证协议和逐版本字段拒绝错误类型，并保留插件身份。"""
    raw = index_payload()
    record = raw['plugins'][0]['versions'][0]
    if kind == 'schema':
        raw['schema_version'] = 2
    elif kind == 'missing':
        del record['commit']
    else:
        record[kind] = {'commit':'bad', 'sha256':'bad', 'size':True, 'downloads':['http://example.org/package.zip']}[kind]
    with pytest.raises(ValueError) as error:
        module('index').parse_index(raw)
    if kind != 'schema':
        assert 'test.market' in str(error.value)


def test_index_merge_cache_and_stats_failure():
    """验证优先源、冲突提示、缓存复用及统计失败隔离。"""
    calls = []
    def handler(request):
        calls.append(str(request.url))
        raw = index_payload()
        raw['stats_url'] = 'https://stats.example/stats.json'
        if request.url.host == 'stats.example':
            return httpx.Response(500)
        return httpx.Response(200, json=raw)
    sources = [MarketSource(name=name, index_url=f'https://{name}.example/index.json') for name in ('first','second')]
    client = module('index').IndexClient(sources, transport=httpx.MockTransport(handler))
    snapshot = client.fetch()
    assert len(snapshot.plugins) == 1 and snapshot.plugins[0].source.name == 'first'
    assert snapshot.conflicts and snapshot.plugins[0].downloads_count is None
    count = len(calls)
    assert client.fetch() is snapshot and len(calls) == count
    client.fetch(refresh=True)
    assert len(calls) > count


@pytest.mark.parametrize('bounds,force', [({'min_version':'0.3.0','max_version':'0.3.1'}, False), ({'min_version':'0.2.0','max_version':'0.2.0'},False), ({'min_version':'0.3.0','max_version':'0.3.1'}, True)])
def test_index_reuses_host_compatibility(bounds, force):
    """对照宿主纯函数验证全部兼容判定与原因文本。"""
    raw = index_payload()
    raw['plugins'][0]['versions'][0]['host_application'] = bounds
    item = module('index').parse_index(raw).plugins[0]
    record = item.versions[0]
    comparison = manifest()
    comparison.update(name=item.name, host_application=bounds)
    assert module('index').compatibility(item, record, force=force) == check_host_compatibility(manifest_from_payload(comparison), third_party=True, force=force)


def test_download_retries_digest_and_cleans_failure(tmp_path):
    """校验失败换下一个源，全部失败后暂存目录无残留。"""
    tmp_path = tmp_path / 'staging'
    data = archive()
    record = module('index').parse_index(index_payload()).plugins[0].versions[0]
    calls = []
    def handler(request):
        calls.append(request.url.host)
        corrupted = bytes([data[0] ^ 1]) + data[1:]
        return httpx.Response(200, content=corrupted if request.url.host == 'one.example' else data)
    path = module('download').download_archive(record, tmp_path, transport=httpx.MockTransport(handler))
    assert path.read_bytes() == data and calls == ['one.example','two.example']
    path.unlink()
    with pytest.raises(ValueError) as error:
        module('download').download_archive(record, tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(502)))
    assert all(url in str(error.value) for url in record.downloads)
    assert list(tmp_path.iterdir()) == []


def test_download_size_limit(tmp_path, monkeypatch):
    """流式内容超过上限立即失败并清除半成品。"""
    tmp_path = tmp_path / 'staging'
    download = module('download')
    monkeypatch.setattr(download, 'MAX_DOWNLOAD_BYTES', 10)
    record = module('index').parse_index(index_payload()).plugins[0].versions[0]
    with pytest.raises(ValueError, match='上限'):
        download.download_archive(record, tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'x'*11)))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('bad', ['/absolute.py','test.market/../evil.py','C:/evil.py','other/file.py','symlink'])
def test_archive_rejects_unsafe_entries(tmp_path, bad):
    """拒绝逃逸、链接和多顶层路径，禁止在校验前释放文件。"""
    path = tmp_path / 'bad.zip'
    path.write_bytes(archive())
    with ZipFile(path, 'a') as zip_file:
        if bad == 'symlink':
            info = ZipInfo('test.market/link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zip_file.writestr(info, 'outside')
        else:
            zip_file.writestr(bad, 'bad')
    with pytest.raises(ValueError):
        module('archive').extract_archive(path, tmp_path / '.staging', plugin_id='test.market', version='1.0.0')


def test_archive_limits_and_identity(tmp_path, monkeypatch):
    """拒绝条目超限以及与索引不符的清单身份。"""
    path = tmp_path / 'package.zip'
    path.write_bytes(archive())
    extractor = module('archive')
    for plugin_id, version in [('other','1.0.0'),('test.market','2.0.0')]:
        with pytest.raises(ValueError):
            extractor.extract_archive(path, tmp_path / '.staging', plugin_id=plugin_id, version=version)
    monkeypatch.setattr(extractor, 'MAX_ARCHIVE_ENTRIES', 1)
    with pytest.raises(ValueError, match='条目'):
        extractor.extract_archive(path, tmp_path / '.staging', plugin_id='test.market', version='1.0.0')


def test_receipt_tracks_only_distributed_files(tmp_path):
    """核对收据字段与新增、删除、修改文件的排除规则。"""
    tmp_path = tmp_path / 'plugin'
    tmp_path.mkdir()
    receipt = module('receipt')
    (tmp_path / 'plugin.py').write_text('first', encoding='utf-8')
    (tmp_path / 'config.toml').write_text('secret', encoding='utf-8')
    (tmp_path / '__pycache__').mkdir()
    (tmp_path / '__pycache__/a.pyc').write_bytes(b'cache')
    receipt.write_receipt(tmp_path, source='测试', index_url='https://example.org/index.json', plugin_id='test.market', version='1.0.0', commit='a'*40, sha256='b'*64)
    raw = json.loads((tmp_path / '.yueli-install.json').read_text(encoding='utf-8'))
    assert set(raw) == {'source','index_url','id','version','commit','sha256','installed_at','files'}
    assert set(raw['files']) == {'plugin.py'}
    assert receipt.modified_files(tmp_path) == []
    (tmp_path / 'plugin.py').unlink()
    (tmp_path / 'new.py').write_text('new', encoding='utf-8')
    assert receipt.modified_files(tmp_path) == ['new.py','plugin.py']


def market(tmp_path, plugins_dir=None, data_dir=None):
    """组装真实市场操作与内存 HTTP 索引，所有插件包由本用例生成。

    :param plugins_dir: 插件根目录，默认 ``tmp_path/plugins``。
    :param data_dir: 宿主数据目录，默认 ``tmp_path/data``；与插件根目录可以互不包含。
    """
    raw = index_payload()
    raw['plugins'][0]['versions'] = [version_record(f'{number}.0.0') for number in range(1,6)]
    for record in raw['plugins'][0]['versions']:
        record['downloads'] = [f'https://one.example/{record["version"]}.zip']
    def handler(request):
        if request.url.path.endswith('index.json'):
            return httpx.Response(200, json=raw)
        return httpx.Response(200, content=archive(request.url.path.rsplit('/',1)[1][:-4]))
    index = module('index').IndexClient([MarketSource(name='测试', index_url='https://one.example/index.json')], transport=httpx.MockTransport(handler))
    registry = PluginRegistry()
    return module('operations').MarketOperations(index=index, registry=registry, plugins_dir=plugins_dir or tmp_path/'plugins',
                                                  data_dir=data_dir or tmp_path/'data', auto_install=False)


def test_install_receipt_and_existing_id(tmp_path):
    """安装成功写收据；存在目标目录或同 id 的其他目录均明确拒绝。"""
    ops = market(tmp_path)
    result = ops.install(source='测试', plugin_id='test.market', version='1.0.0')
    assert result['ok'] and result['restart_required']
    target = tmp_path/'plugins/test.market'
    assert (target/'.yueli-install.json').is_file()
    with pytest.raises(ValueError, match='已存在'):
        ops.install(source='测试', plugin_id='test.market', version='1.0.0')
    target.rename(tmp_path/'plugins/renamed')
    ops.registry.discover([(tmp_path/'plugins', True)])
    with pytest.raises(ValueError, match='已存在'):
        ops.install(source='测试', plugin_id='test.market', version='1.0.0')


def test_upgrade_preserves_config_and_refuses_modified_code(tmp_path):
    """升级保护用户配置，检测到代码改动时不下载或覆盖该目录。"""
    ops = market(tmp_path)
    ops.install(source='测试', plugin_id='test.market', version='1.0.0')
    target = tmp_path/'plugins/test.market'
    (target/'config.toml').write_text('[plugin]\nenabled = false\n', encoding='utf-8')
    ops.upgrade(plugin_id='test.market', version='2.0.0')
    assert (target/'config.toml').read_text(encoding='utf-8') == '[plugin]\nenabled = false\n'
    backups = list((tmp_path/'plugins/.backups').iterdir())
    assert len(backups) == 1 and (backups[0]/'config.toml').is_file()
    (target/'plugin.py').write_text('user modification', encoding='utf-8')
    with pytest.raises(ValueError, match='plugin.py'):
        ops.upgrade(plugin_id='test.market', version='3.0.0')


def test_upgrade_keeps_three_backups_and_restores_on_failure(tmp_path, monkeypatch):
    """保留最新三份备份，新目录替换失败时恢复原目录及全部内容。"""
    ops = market(tmp_path)
    ops.install(source='测试', plugin_id='test.market', version='1.0.0')
    for version in ('2.0.0','3.0.0','4.0.0','5.0.0'):
        ops.upgrade(plugin_id='test.market', version=version)
    assert len(list((tmp_path/'plugins/.backups').iterdir())) == 3
    target = tmp_path/'plugins/test.market'
    original = (target/'_manifest.json').read_bytes()
    operations = module('operations')
    replace = operations.os.replace
    def fail_new(source, destination):
        if '.staging' in Path(source).parts and Path(destination) == target:
            raise OSError('模拟新目录无法就位')
        return replace(source, destination)
    monkeypatch.setattr(operations.os, 'replace', fail_new)
    with pytest.raises(OSError, match='无法就位'):
        ops.upgrade(plugin_id='test.market', version='4.0.0')
    assert (target/'_manifest.json').read_bytes() == original


def test_takeover_and_uninstall_preserve_data(tmp_path):
    """接管手动插件后写收据；卸载完整备份配置，不触碰宿主数据。"""
    ops = market(tmp_path)
    target = tmp_path/'plugins/manual-name'
    target.mkdir(parents=True)
    (target/'_manifest.json').write_text(json.dumps(manifest()), encoding='utf-8')
    (target/'config.toml').write_text('[plugin]\nenabled = true\n', encoding='utf-8')
    ops.registry.discover([(target.parent, True)])
    with pytest.raises(ValueError, match='手动'):
        ops.uninstall(plugin_id='test.market')
    ops.takeover(plugin_id='test.market', version='2.0.0')
    assert (target/'.yueli-install.json').is_file()
    data = tmp_path/'data/own-data.txt'
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text('preserve', encoding='utf-8')
    assert ops.uninstall(plugin_id='test.market')['restart_required']
    assert not target.exists() and data.read_text(encoding='utf-8') == 'preserve'
    assert any((backup/'config.toml').is_file() for backup in (tmp_path/'plugins/.backups').iterdir())


def test_backups_stay_beside_plugins_across_filesystems(tmp_path, monkeypatch):
    """数据目录在另一文件系统时升级与卸载仍成功，备份目录不被当作插件。"""
    disk_a, disk_b = tmp_path/'disk-a', tmp_path/'disk-b'
    ops = market(tmp_path, plugins_dir=disk_a/'plugins', data_dir=disk_b/'data')
    operations = module('operations')
    replace = operations.os.replace
    def same_filesystem_only(source, destination):
        if Path(source).is_relative_to(disk_a) != Path(destination).is_relative_to(disk_a):
            raise OSError(errno.EXDEV, '模拟跨文件系统改名')
        return replace(source, destination)
    monkeypatch.setattr(operations.os, 'replace', same_filesystem_only)
    ops.install(source='测试', plugin_id='test.market', version='1.0.0')
    ops.upgrade(plugin_id='test.market', version='2.0.0')
    ops.registry.discover([(disk_a/'plugins', True)])
    assert {status.directory.name for status in ops.registry.statuses()} == {'test.market'}
    assert set(ops.installed()) == {'test.market'}
    ops.uninstall(plugin_id='test.market')
    backups = sorted(path.name.split('-')[1] for path in (disk_a/'plugins/.backups').iterdir())
    assert backups == ['1.0.0', '2.0.0'] and not (disk_b/'data/plugin-backups').exists()


def test_operations_wait_for_shared_lock(tmp_path, monkeypatch):
    """用事件阻塞首个下载，确认卸载在同一锁外等待，不能交错修改目录。"""
    ops = market(tmp_path)
    operations = module('operations')
    download = operations.download_archive
    downloading, release, finished = Event(), Event(), Event()
    errors = []
    def blocked(*args, **kwargs):
        downloading.set()
        assert release.wait(5)
        return download(*args, **kwargs)
    def install():
        try:
            ops.install(source='测试', plugin_id='test.market', version='1.0.0')
        except Exception as exc:
            errors.append(exc)
    def uninstall():
        try:
            ops.uninstall(plugin_id='test.market')
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    monkeypatch.setattr(operations, 'download_archive', blocked)
    first, second = Thread(target=install), Thread(target=uninstall)
    first.start()
    try:
        assert downloading.wait(5)
        second.start()
        assert not finished.wait(0.1)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not errors and finished.is_set()
