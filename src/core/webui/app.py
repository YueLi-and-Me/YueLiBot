"""将构建后的 WebUI 静态资源和 SPA 回退入口挂载到 FastAPI。

模块只负责静态资源路由，不处理人物数据；数据由后端只读 API 提供。构建产物
位于项目 ``out/webui`` 目录，缺失时返回说明页面而不阻断 API 服务启动。
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles


# 本模块位于 src/core/webui/，项目根在第 3 层父目录；构建产物由前端写到根下的 out/webui。
# 层数随模块位置变化：本文件从 src/webui/ 移到 src/core/webui/ 时层数没跟着加，
# 于是目录指向了不存在的 src/out/webui，页面长期显示「尚未构建」而构建本身一直是成功的。
# 移动本文件时必须同步复核这个层数。
_WEBUI_DIST = Path(__file__).resolve().parents[3] / 'out' / 'webui'


def mount_webui(app: FastAPI) -> None:
    """在 API 路由之后挂载 WebUI 静态入口。

    :param app: 目标 FastAPI 应用。

    :return: ``None``。

    副作用：
        构建产物存在时注册全部 SPA 入口路由和根静态挂载；产物不存在时注册构建
        提示页面。路由注册顺序确保静态服务不会抢占后端 API。
    """
    if _WEBUI_DIST.is_dir():
        index_path = _WEBUI_DIST / 'index.html'

        # 下面每条 SPA 入口都必须与前端路由表（webui/src/app/App.tsx 的 <Routes>）
        # 一一对应。根挂载的 StaticFiles 只按文件名交付，前端路由在磁盘上没有对应
        # 文件，漏注册的那条就只有页内点导航能进、直接敲地址或刷新一律 404；
        # /jargon 与 /expressions 就是这样漏了一段时间。新增前端页面时同步补一条。
        @app.get('/models', include_in_schema=False)
        async def models_page() -> FileResponse:
            """返回模型与厂商工作台共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/emojis', include_in_schema=False)
        async def emojis_page() -> FileResponse:
            """返回表情包管理页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/settings', include_in_schema=False)
        async def settings_page() -> FileResponse:
            """返回月璃设置页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/developer/commands', include_in_schema=False)
        async def developer_commands_page() -> FileResponse:
            """返回开发者命令只读页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/persons', include_in_schema=False)
        async def person_list_page() -> FileResponse:
            """返回人物列表页共用的 SPA 入口文件。

            :return: ``index.html`` 文件响应；人物数据由前端调用只读 API 获取。

            :raises OSError: WebUI 构建文件不存在或无法读取时由文件响应层抛出。
            """

            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/persons/{person_id}', include_in_schema=False)
        async def person_detail_page(person_id: int) -> FileResponse:
            """返回人物详情 SPA 入口。

            :param person_id: 路由中的人物 ID；实际数据由前端调用只读 API 获取。

            :return: WebUI ``index.html`` 文件响应。
            """

            # person_id 由前端再向只读 API 查询；路由只负责交付同一份 SPA 入口。
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/jargon', include_in_schema=False)
        async def jargon_page() -> FileResponse:
            """返回黑话词表页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/expressions', include_in_schema=False)
        async def expressions_page() -> FileResponse:
            """返回表达方式页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/memory', include_in_schema=False)
        async def memory_graph_page() -> FileResponse:
            """返回记忆联想网络页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/memory/manage', include_in_schema=False)
        async def memory_manage_page() -> FileResponse:
            """返回记忆人工管理页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/memory/tuning', include_in_schema=False)
        async def memory_tuning_page() -> FileResponse:
            """返回检索调优页共用的 SPA 入口文件。"""
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/memory/import', include_in_schema=False)
        async def memory_import_page() -> FileResponse:
            """返回导入中心页共用的 SPA 入口文件。

            SPA 路由在此逐条声明，新增页面必须同步登记：漏登的路径只有从站内跳转
            才能打开，直接访问或刷新会落到静态挂载并返回 404。检索调优与导入中心
            两页上线时都漏了这一步。
            """
            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        @app.get('/', include_in_schema=False)
        async def index_page() -> FileResponse:
            """返回 SPA 根入口，并禁止浏览器缓存这份 HTML。

            其余 SPA 路由都已显式声明 ``no-store``，只有根路径原来落在静态挂载上，
            由 StaticFiles 交付且不带缓存头。

            - 现象：前端重新构建后，从根路径进入的浏览器仍加载旧界面，新增的配置
              项看不到，硬刷新才出来。
            - 原因：index.html 被浏览器缓存，其中引用的是上一次构建的带 hash 资源名，
              于是整个旧 bundle 都命中缓存。
            - 后果：每次前端更新都要手动清缓存，且很容易误判成「后端没生效」。

            :return: ``index.html`` 文件响应；带 hash 的静态资源仍走静态挂载。
            """

            return FileResponse(index_path, headers={'Cache-Control': 'no-store'})

        app.mount('/', StaticFiles(directory=_WEBUI_DIST, html=True), name='webui')
        return

    @app.get('/', response_class=HTMLResponse, include_in_schema=False)
    async def webui_not_built() -> str:
        """返回 WebUI 构建产物缺失时的提示 HTML。

        :return: 指导执行前端构建命令的简体中文 HTML 页面。

        副作用：
            不访问文件系统之外的服务，不触发 API 或人物数据读取。
        """

        # 带上实际查找的目录：只说「尚未构建」时，路径算错和真的没构建看起来一模一样。
        return (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>Bot 观察面板</title><body>'
            '<h1>WebUI 尚未构建</h1><p>请先运行 npm run build，再重新打开本页。</p>'
            f'<p>查找目录：{_WEBUI_DIST}</p>'
            '</body></html>'
        )
