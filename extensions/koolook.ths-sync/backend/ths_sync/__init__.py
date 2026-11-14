# -*- coding: utf-8 -*-
"""源码级后端扩展：TSP ↔ 同花顺远航版 双向同步。

架构
====
全部逻辑在 TSP 后端进程内，不额外起进程、不开监听端口、不注册 HTTP 回环。

* ``reader``：读同花顺自选（``SelfStockCache.json``）、持仓（进程内代理
  ``g_position``）、板块（``blockstockV3.xml``）
* ``writer``：原子写板块，带备份与回读校验
* ``service``：双向同步引擎 + 后台线程 + 状态汇总
* ``routes``：``/api/custom/ths-sync/*`` 接口
* ``ths_agent.py``：跑在同花顺内置 Python 里的只读代理，暴露持仓

自动与手动
----------
* 自动：后台 daemon 线程按 ``interval``（默认 60s）跑一轮
* 手动：TSP 界面「立即同步」按钮 → ``POST /api/custom/ths-sync/sync``

两条都走同一个 ``service.run_once()``，没有第二套逻辑。
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
