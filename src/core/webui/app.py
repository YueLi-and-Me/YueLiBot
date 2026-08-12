"""将构建后的 WebUI 静态资源和 SPA 回退入口挂载到 FastAPI。

模块只负责静态资源路由，不处理人物数据；数据由后端只读 API 提供。构建产物
位于项目 ``out/webui`` 目录，缺失时返回说明页面而不阻断 API 服务启动。
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles


_WEBUI_DIST = Path(__file__).resolve().parents[2] / 'out' / 'webui'


def mount_webui(app: FastAPI) -> None:
    """在 API 路由之后挂载 WebUI 静态入口。

    :param app: 目标 FastAPI 应用。

    :return: ``None``。

    副作用：
        构建产物存在时注册人物 SPA 路由和根静态挂载；产物不存在时注册构建提示
        页面。路由注册顺序确保静态服务不会抢占后端 API。
    """
    if _WEBUI_DIST.is_dir():
        index_path = _WEBUI_DIST / 'index.html'

        @app.get('/persons', include_in_schema=False)
        async def person_list_page() -> FileResponse:
            """返回人物列表页共用的 SPA 入口文件。

            :return: ``index.html`` 文件响应；人物数据由前端调用只读 API 获取。

            :raises OSError: WebUI 构建文件不存在或无法读取时由文件响应层抛出。
            """

            return FileResponse(index_path)

        @app.get('/persons/{person_id}', include_in_schema=False)
        async def person_detail_page(person_id: int) -> FileResponse:
            """返回人物详情 SPA 入口。

            :param person_id: 路由中的人物 ID；实际数据由前端调用只读 API 获取。

            :return: WebUI ``index.html`` 文件响应。
            """

            # person_id 由前端再向只读 API 查询；路由只负责交付同一份 SPA 入口。
            return FileResponse(index_path)

        app.mount('/', StaticFiles(directory=_WEBUI_DIST, html=True), name='webui')
        return

    @app.get('/', response_class=HTMLResponse, include_in_schema=False)
    async def webui_not_built() -> str:
        """返回 WebUI 构建产物缺失时的提示 HTML。

        :return: 指导执行前端构建命令的简体中文 HTML 页面。

        副作用：
            不访问文件系统之外的服务，不触发 API 或人物数据读取。
        """

        return (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>Bot 观察面板</title><body>'
            '<h1>WebUI 尚未构建</h1><p>请先运行 npm run build，再重新打开本页。</p>'
            '</body></html>'
        )
