"""校验 WebUI 静态资源目录解析到项目根，而不是模块所在包的上级。

`_WEBUI_DIST` 用 `Path(__file__).parents[N]` 定位项目根，层数与模块位置耦合：
`cb3d3d2` 把本模块从 `src/webui/` 移到 `src/core/webui/` 时层数没跟着加，
目录于是指向不存在的 `src/out/webui`，页面长期显示「尚未构建」而构建一直是成功的。
本测试不依赖构建产物是否存在，只钉住「解析出的根是仓库根」这条不变式。
"""

from src.core.webui.app import _WEBUI_DIST


def test_webui_dist_resolves_under_repository_root() -> None:
    """构建产物目录必须是仓库根下的 out/webui。"""
    assert _WEBUI_DIST.name == 'webui'
    assert _WEBUI_DIST.parent.name == 'out'
    repository_root = _WEBUI_DIST.parent.parent
    # package.json 只存在于仓库根；用它判定根目录，避免再写一次层数假设。
    assert (repository_root / 'package.json').is_file()
    assert (repository_root / 'src' / 'core' / 'webui' / 'app.py').is_file()
