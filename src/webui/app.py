"""把 Vite 构建出的 WebUI 静态资源挂到 FastAPI。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles


_WEBUI_DIST = Path(__file__).resolve().parents[2] / 'out' / 'webui'


def mount_webui(app: FastAPI) -> None:
    """在 API 路由之后挂首页，避免静态服务抢占后端接口。"""
    if _WEBUI_DIST.is_dir():
        index_path = _WEBUI_DIST / 'index.html'

        @app.get('/persons', include_in_schema=False)
        async def person_list_page() -> FileResponse:
            return FileResponse(index_path)

        @app.get('/persons/{person_id}', include_in_schema=False)
        async def person_detail_page(person_id: int) -> FileResponse:
            # person_id 由前端再向只读 API 查询；路由只负责交付同一份 SPA 入口。
            return FileResponse(index_path)

        app.mount('/', StaticFiles(directory=_WEBUI_DIST, html=True), name='webui')
        return

    @app.get('/', response_class=HTMLResponse, include_in_schema=False)
    async def webui_not_built() -> str:
        return (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>Bot 观察面板</title><body>'
            '<h1>WebUI 尚未构建</h1><p>请先运行 npm run build，再重新打开本页。</p>'
            '</body></html>'
        )
