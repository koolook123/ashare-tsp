# -*- coding: utf-8 -*-
"""TZZB 投资账本 MCP 主数据源。

通过本机 mcporter CLI 调用已配置的 TZZB MCP，读取股票账户、持仓、盈亏、
清仓、资金流水与资产变动。所有子进程使用 shell=False 与无窗口启动参数，
不弹终端框、不抢前台。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NODE = Path(r"C:\Program Files\nodejs\node.exe")
CLI = Path(r"C:\Users\koolook\AppData\Roaming\npm\node_modules\mcporter\dist\cli.js")
DEFAULT_FUND_KEY = "193735580"

TOOLS = (
    "TZZB.promptAccountSelection",
    "TZZB.queryStockPosition",
    "TZZB.queryPnlSummary",
    "TZZB.queryClearedPosition",
    "TZZB.queryMoneyHistory",
    "TZZB.queryAssetChangeSummary",
    "TZZB.queryDayProfitSummary",
)


def _number(value: Any) -> float:
    try:
        return float(str(value or 0).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _no_window_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"shell": False}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def call_tool(tool: str, args: dict[str, Any], timeout: float = 30.0) -> Any:
    """调用 TZZB MCP 工具并解析 JSON 输出；失败抛 RuntimeError。"""
    if tool not in TOOLS:
        raise ValueError("unsupported TZZB tool: %s" % tool)
    cmd = [
        str(NODE),
        str(CLI),
        "call",
        tool,
        "--args",
        json.dumps(args, ensure_ascii=False, separators=(",", ":")),
        "--no-oauth",
        "--output",
        "json",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **_no_window_kwargs(),
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError("TZZB %s exit=%s stderr=%s" % (tool, proc.returncode, err[:300]))
    if not out:
        raise RuntimeError("TZZB %s empty stdout" % tool)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TZZB %s invalid JSON: %s head=%r" % (tool, exc, out[:300])) from exc


def _unwrap(payload: Any) -> Any:
    """兼容 MCP 工具的纯 JSON 与 content[].text JSON 包装。"""
    if isinstance(payload, dict) and "content" in payload and "queryStockPosition" not in payload:
        for item in payload.get("content") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return payload


def _reader():
    """兼容 TSP 包内导入与 TDD 独立加载。"""
    try:
        from . import reader as module
        return module
    except Exception:
        for name, module in sys.modules.items():
            if name == "reader" or name.endswith(".reader"):
                return module
        import reader as module
        return module


def accounts(scope: str = "stock") -> dict[str, Any]:
    payload = _unwrap(call_tool("TZZB.promptAccountSelection", {"scope": scope}))
    return payload if isinstance(payload, dict) else {"raw": payload}


def _fund_from_accounts(data: dict[str, Any]) -> str:
    for row in data.get("fund_key") or []:
        if isinstance(row, dict) and row.get("param") == "fundKey" and row.get("id"):
            return str(row["id"])
    return DEFAULT_FUND_KEY


def default_fund_key() -> str:
    try:
        return _fund_from_accounts(accounts())
    except Exception as exc:
        log.warning("TZZB account lookup failed, use default: %s", exc)
        return DEFAULT_FUND_KEY


def _stock_args(fund_key: str = "") -> dict[str, str]:
    return {"fundKey": fund_key or DEFAULT_FUND_KEY, "rzrqFundKey": "", "manualId": ""}


def _all_args(start_date: str, end_date: str, fund_key: str = "") -> dict[str, str]:
    args = _stock_args(fund_key)
    args.update({"startDate": start_date, "endDate": end_date, "fundId": "", "custId": ""})
    return args


def positions_payload(fund_key: str = "") -> dict[str, Any]:
    payload = _unwrap(call_tool("TZZB.queryStockPosition", _stock_args(fund_key)))
    return payload if isinstance(payload, dict) else {}


def positions_from_payload(payload: dict[str, Any]) -> list[Any]:
    """把 queryStockPosition 输出转换为 reader.Position 列表，过滤非 A 股。"""
    reader = _reader()
    out: list[Any] = []
    seen: set[str] = set()
    for row in payload.get("holdings") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("stockCode") or row.get("code") or "").strip()
        if not reader.is_a_stock(code) or code in seen:
            continue
        qty = int(_number(row.get("holdQuantity") or row.get("qty")))
        if qty <= 0:
            continue
        seen.add(code)
        cost = _number(row.get("costPrice") or row.get("cost"))
        price = _number(row.get("currentPrice") or row.get("price"))
        market_value = _number(row.get("marketValue") or qty * price)
        pnl = _number(row.get("holdProfit") or (price - cost) * qty)
        out.append(reader.Position(
            code=code,
            name=str(row.get("stockName") or row.get("name") or ""),
            qty=qty,
            avail=int(_number(row.get("availableQuantity") or row.get("avail") or qty)),
            cost=cost,
            price=price,
            market_value=market_value,
            pnl=pnl,
        ))
    return out


def _parallel_load(out: dict[str, Any], jobs: dict[str, tuple[str, dict[str, Any]]]) -> None:
    """并行调用 MCP 工具，结果在主线程写回，避免共享字典写入。"""
    def run(item: tuple[str, tuple[str, dict[str, Any]]]) -> tuple[str, Any]:
        key, (tool, args) = item
        try:
            return key, _unwrap(call_tool(tool, args))
        except Exception as exc:
            return key, {"__error__": str(exc)}

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(jobs)))) as pool:
        for key, value in pool.map(run, jobs.items()):
            if isinstance(value, dict) and "__error__" in value:
                out["errors"].append("%s: %s" % (key, value["__error__"]))
            else:
                out[key] = value


def read_positions(fund_key: str = "") -> tuple[list[Any] | None, dict[str, Any]]:
    """主源持仓读取，返回 (positions, meta)。失败或空列表时 positions=None。"""
    meta: dict[str, Any] = {"ok": False, "source": "TZZB MCP", "errors": []}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            account_future = pool.submit(accounts)
            payload_future = pool.submit(positions_payload, fund_key)
            account = account_future.result()
            payload = payload_future.result()
        positions = positions_from_payload(payload)
        if not positions:
            meta["errors"].append("TZZB 持仓为空或字段不可解析")
            return None, meta
        meta.update({
            "ok": True,
            "fund_key": fund_key or _fund_from_accounts(account),
            "account": account,
            "available_funds": payload.get("availableFunds"),
            "total_asset": payload.get("totalAsset"),
            "total_market_value": payload.get("totalMarketValue"),
            "position_ratio": payload.get("positionRatio"),
        })
        return positions, meta
    except Exception as exc:
        log.warning("TZZB MCP read_positions failed: %s", exc)
        meta["errors"].append(str(exc))
        return None, meta


def read_snapshot(start_date: str = "", end_date: str = "", fund_key: str = "") -> dict[str, Any]:
    """账户、持仓、盈亏与净值趋势快照。"""
    today = datetime.now().strftime("%Y%m%d")
    end = end_date or today
    start = start_date or (end[:4] + "0101")
    out: dict[str, Any] = {"ok": False, "source": "TZZB MCP", "start_date": start, "end_date": end, "errors": []}
    jobs = {
        "account": ("TZZB.promptAccountSelection", {"scope": "stock"}),
        "position": ("TZZB.queryStockPosition", _stock_args(fund_key)),
        "pnl": ("TZZB.queryPnlSummary", _all_args(start, end, fund_key)),
    }
    _parallel_load(out, jobs)
    payload = out.get("position") if isinstance(out.get("position"), dict) else {}
    positions = positions_from_payload(payload)
    out["positions"] = [p.as_dict() for p in positions]
    out["positions_count"] = len(positions)
    out["ok"] = not out["errors"]
    return out


def read_trade_summary(start_date: str = "", end_date: str = "", fund_key: str = "", date: str = "") -> dict[str, Any]:
    """交易信息汇总：清仓、资金流水、资产变动、逐日盈亏。"""
    today = datetime.now().strftime("%Y%m%d")
    end = end_date or today
    start = start_date or (end[:4] + "0101")
    day = date or end
    out: dict[str, Any] = {"ok": False, "source": "TZZB MCP", "start_date": start, "end_date": end, "date": day, "errors": []}
    jobs = {
        "cleared": ("TZZB.queryClearedPosition", {
            **_stock_args(fund_key),
            "openDate": "%s-%s-%s" % (start[:4], start[4:6], start[6:8]),
            "closeDate": "%s-%s-%s" % (end[:4], end[4:6], end[6:8]),
            "groupBy": "stock",
        }),
        "money_history": ("TZZB.queryMoneyHistory", {
            **_stock_args(fund_key), "page": "1", "count": "50", "startDate": start, "endDate": end,
        }),
        "asset_change": ("TZZB.queryAssetChangeSummary", _all_args(start, end, fund_key)),
        "day_profit": ("TZZB.queryDayProfitSummary", {
            **_all_args(day, day, fund_key), "date": day,
        }),
    }
    _parallel_load(out, jobs)
    out["ok"] = not out["errors"]
    return out
