"""把 Vite 构建出的 WebUI 静态资源挂到 FastAPI。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


_WEBUI_DIST = Path(__file__).resolve().parents[2] / 'out' / 'webui'


def mount_webui(app: FastAPI) -> None:
    """在 API 路由之后挂首页，避免静态服务抢占后端接口。"""
    if _WEBUI_DIST.is_dir():
        app.mount('/', StaticFiles(directory=_WEBUI_DIST, html=True), name='webui')
        return

    @app.get('/', response_class=HTMLResponse, include_in_schema=False)
    async def webui_not_built() -> str:
        return (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>月璃观察面板</title><body>'
            '<h1>WebUI 尚未构建</h1><p>请先运行 npm run build，再重新打开本页。</p>'
            '</body></html>'
        )
