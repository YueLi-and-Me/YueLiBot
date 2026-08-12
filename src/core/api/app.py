"""构建主体后端使用的 FastAPI 应用实例。

应用工厂挂载 HTTP 与 WebSocket 路由，并通过 lifespan 钩子启动和停止全局服务；
具体请求校验、鉴权和业务编排由 `src.core.api.http`、`src.core.api.ws` 及服务层负责。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .http import router as http_router
from .ws import router as ws_router

from src.core.services.lifecycle import lifecycle
from src.core.webui.app import mount_webui


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """在 ASGI 应用生命周期边界启动和停止全局服务。

    :param app: FastAPI 应用实例；当前实现只用于满足 lifespan 协议，不读取其属性。
    :yields: 服务启动完成后把控制权交回 FastAPI，直到应用开始关闭。
    :raises Exception: 任一服务启动或停止失败时向 ASGI 服务器传播原始异常。
    :side_effects: 调用全局 `lifecycle.start_all()` 与 `stop_all()` 协程。
    """
    # lifecycle.start_all()/stop_all() 是协程，必须跑在 uvicorn 拥有的事件
    # 循环里——uvicorn.run() 本身是阻塞调用，FastAPI 的 lifespan 正是它给的
    # 「循环起来之后跑、循环关之前跑」这个钩子。
    # lifespan 跑完时端口还没绑，所以就绪公告在 main.py 里发
    await lifecycle.start_all()
    yield
    await lifecycle.stop_all()


def create_app() -> FastAPI:
    """创建配置完成的 FastAPI 应用。

    :return: 已挂载 HTTP、WebSocket 和 WebUI 路由的 `FastAPI` 实例。
    :raises Exception: 路由或 WebUI 挂载初始化失败时向调用方传播异常。
    :side_effects: 构造路由树，但不会在调用阶段启动服务器或全局服务。
    """
    app = FastAPI(
        title="YueLiBot Backend",
        docs_url=None,   # 不对外暴露 Swagger（本地服务无需）
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.include_router(ws_router)
    app.include_router(http_router)
    mount_webui(app)
    return app
