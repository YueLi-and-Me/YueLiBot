"""FastAPI 应用工厂。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI

from src.services.lifecycle import lifecycle
from .ws import router as ws_router
from .http import router as http_router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # lifecycle.start_all()/stop_all() 是协程，必须跑在 uvicorn 拥有的事件
    # 循环里——uvicorn.run() 本身是阻塞调用，FastAPI 的 lifespan 正是它给的
    # 「循环起来之后跑、循环关之前跑」这个钩子。
    await lifecycle.start_all()
    if app.state.ready_callback is not None:
        app.state.ready_callback()
    yield
    await lifecycle.stop_all()


def create_app(on_ready: Callable[[], None] | None = None) -> FastAPI:
    app = FastAPI(
        title="YueLiBot Backend",
        docs_url=None,   # 不对外暴露 Swagger（本地服务无需）
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.ready_callback = on_ready
    app.include_router(ws_router)
    app.include_router(http_router)
    return app
