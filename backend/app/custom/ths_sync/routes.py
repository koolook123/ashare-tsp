"""同花顺同步扩展的 HTTP 接口。

前缀固定在 ``/api/custom/ths-sync``，与核心 API 无交叠。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import service

router = APIRouter(prefix="/api/custom/ths-sync", tags=["ths-sync"])


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto: bool | None = None
    interval: int | None = Field(default=None, ge=service.MIN_INTERVAL, le=3600)
    sync_selfstock: bool | None = None
    sync_positions: bool | None = None
    ths_user: str | None = Field(default=None, max_length=64)


@router.get("/status")
def get_status() -> dict:
    """页面唯一数据入口：同步桥状态 + 两池来源 + 上次结果。"""
    return service.build_status()


@router.post("/sync")
def post_sync() -> dict:
    """立即同步一轮（幂等：只处理差集）。"""
    try:
        return service.run_once()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"同步失败: {exc}") from exc


@router.get("/config")
def get_config() -> dict:
    return service.get_config()


@router.put("/config")
def put_config(body: ConfigBody) -> dict:
    return service.set_config(
        auto=body.auto,
        interval=body.interval,
        sync_selfstock=body.sync_selfstock,
        sync_positions=body.sync_positions,
        ths_user=body.ths_user,
    )


@router.get("/preview")
def get_preview() -> dict:
    """只读预览：当前能从同花顺与 v1 各读到什么，不写任何数据。"""
    selfstock = service.read_selfstock()
    positions = service.read_positions()
    return {
        "ok": True,
        "selfstock": selfstock if selfstock is not None else [],
        "selfstock_ok": selfstock is not None,
        "positions": list((positions or {}).values()),
        "positions_ok": positions is not None,
        "ths_root": str(service.THS_ROOT),
        "positions_url": service.V1_POSITIONS_URL,
    }


def build_router() -> APIRouter:
    return router
