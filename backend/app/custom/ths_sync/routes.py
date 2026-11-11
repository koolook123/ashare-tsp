# -*- coding: utf-8 -*-
"""同步接口。挂在 ``/api/custom/ths-sync`` 下。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import reader
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/custom/ths-sync", tags=["ths-sync"])


class ConfigBody(BaseModel):
    """可调项。全部可选，只改传进来的字段。"""

    auto: bool | None = None
    interval: int | None = Field(default=None, ge=service.MIN_INTERVAL, le=service.MAX_INTERVAL)
    sync_ths_to_tsp: bool | None = None
    sync_tsp_to_ths: bool | None = None
    sync_selfstock: bool | None = None
    sync_positions: bool | None = None
    block_name: str | None = None
    write_ths_selfstock: bool | None = None

    model_config = {"extra": "forbid"}


@router.get("/status")
def get_status() -> dict:
    """一站式状态：引擎、配置、同花顺侧、TSP 侧、持仓明细。"""
    return service.build_status()


@router.post("/sync")
def post_sync() -> dict:
    """手动触发一次同步。等价于 TSP 界面上的「立即同步」。

    这一轮只跑自选：持仓是独立的手动一键通道（``POST /sync/positions``），
    因为读持仓要连本机 17660 代理，代理没起时会走满 socket 超时。
    """
    try:
        return service.run_once(with_positions=False)
    except Exception as exc:
        logger.exception("手动同步失败")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/sync/positions")
def post_sync_positions() -> dict:
    """手动一键同步持仓：读同花顺 ``g_position`` 并刷新「同花顺持仓」分组。

    这是持仓唯一的同步入口——自动线程不碰持仓。返回体与 ``POST /sync`` 同构，
    但 ``positions`` / ``position_pnl_total`` 一定有值（除非代理未启动）。
    """
    try:
        return service.run_once(with_positions=True)
    except Exception as exc:
        logger.exception("手动同步持仓失败")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/sync/dry")
def post_sync_dry() -> dict:
    """预演：只报告会改什么，不落到任何一端。"""
    try:
        return service.run_once(dry=True)
    except Exception as exc:
        logger.exception("同步预演失败")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/config")
def get_config() -> dict:
    return {"ok": True, "config": service.get_state()}


@router.put("/config")
def put_config(body: ConfigBody) -> dict:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "block_name" in patch:
        name = str(patch["block_name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="板块名不能为空")
        patch["block_name"] = name
    return {"ok": True, "config": service.update_state(patch)}


@router.get("/preview")
def get_preview() -> dict:
    """只读预览：两边各读到什么。用于排查「为什么没同步过来」。"""
    user = reader.find_user()
    self_codes = reader.read_selfstock(user)
    positions = reader.read_positions()
    agent_ok = reader.agent_alive()
    block_name = str(service.get_state().get("block_name") or service.DEFAULT_BLOCK)
    block_codes = reader.read_block_codes(block_name, user)

    return {
        "ok": True,
        "ths_root": str(reader.ths_root()),
        "user": user.name if user else "",
        "user_dir": str(user.user_dir) if user else "",
        "selfstock_ok": self_codes is not None,
        "selfstock": self_codes or [],
        "selfstock_count": len(self_codes or []),
        "agent_ok": agent_ok,
        "positions_ok": positions is not None,
        "positions": [p.as_dict() for p in (positions or [])],
        "positions_count": len(positions or []),
        "block_name": block_name,
        "block_ok": block_codes is not None,
        "block_codes": block_codes or [],
        "block_count": len(block_codes or []),
    }


@router.get("/blocks")
def get_blocks() -> dict:
    """列出同花顺所有板块名与成分数量，便于挑选写入目标。"""
    blocks = reader.list_blocks()
    return {
        "ok": blocks is not None,
        "blocks": blocks or [],
        "error": "" if blocks is not None else "读取板块文件失败",
    }


@router.get("/agent")
def get_agent() -> dict:
    """同花顺持仓代理的连通性自检。"""
    alive = reader.agent_alive()
    version = reader.agent_python_version() if alive else None
    return {
        "ok": True,
        "alive": alive,
        "port": reader.agent_port(),
        "python": version,
        "hint": ""
        if alive
        else (
            "同花顺持仓代理未运行。持仓只能从同花顺进程内读（客户端不落盘），"
            "请在远航版脚本界面运行一次 backend/app/custom/ths_sync/ths_agent.py"
        ),
    }


def build_router() -> APIRouter:
    return router
