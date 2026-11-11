"""源码级后端扩展：同花顺自选股与持仓同步进 TSP 自选池。

用法（随宿主一起启动，无需额外进程）：
- 后台线程按 config 的 interval（默认 60s）自动同步；
- 页面或脚本可 POST /api/custom/ths-sync/sync 立即同步一轮；
- GET /api/custom/ths-sync/status 返回前端页面所需的全部状态。

与 ``tools/ths_tsp_sync.py`` 外围桥的区别：本扩展在 TSP 进程内运行，
直接调用 ``app.services.watchlist`` 写入，不再走 HTTP 回环；两者可同时存在
（写的是同一份 parquet，幂等差集更新，不会互相破坏）。
"""

from __future__ import annotations

from app.extensions.contracts import BACKEND_EXTENSION_API_VERSION, ExtensionContext

from . import service

EXTENSION_ID = "koolook.ths-sync"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())


def startup(context: ExtensionContext) -> None:
    service.start(context.data_dir)
