"""同花顺自选/持仓 → TSP 自选池 的同步服务。

设计要点（与 tools/ths_tsp_sync.py 外围桥的区别）：
- 进程内直调 ``app.services.watchlist`` 的模块级函数完成写入，不再走 HTTP 回环；
- 只读同花顺本地缓存文件（SelfStockCache.json / transaction/xiadan-plus），
  不模拟界面、不抢焦点、不改同花顺任何文件；
- 持仓来源是 v1 A 股助手 的 /api/positions（成本含费，唯一出口 watchpool.position_map），
  v1 未启动时沿用上次快照，绝不把「读不到」当成「持有为空」去删池子。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from app.services import watchlist

logger = logging.getLogger(__name__)

# ---- 同花顺（远航版）本地只读路径 ----------------------------------------
THS_ROOT = Path(r"C:\同花顺远航版")
DEFAULT_THS_USER = "爱因思睿"

# ---- v1 A 股助手 ---------------------------------------------------------
V1_POSITIONS_URL = "http://127.0.0.1:17651/api/positions"
V1_TIMEOUT = 5.0

# ---- TSP 自选池写入约定 --------------------------------------------------
NOTE = "同花顺同步"
GROUP_SELF = "同花顺自选"
GROUP_POS = "同花顺持仓"
COLOR_SELF = "sky"
COLOR_POS = "rose"

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 10

_A_STOCK_RE = re.compile(r"^[03684]\d{5}$")

_LOCK = threading.RLock()
_RUNTIME: dict = {
    "started_at": "",
    "sync_count": 0,
    "last_sync": "",
    "last_result": None,
    "thread_alive": False,
}
_STATE: dict = {
    "auto": True,
    "interval": DEFAULT_INTERVAL,
    "sync_selfstock": True,
    "sync_positions": True,
    "ths_user": DEFAULT_THS_USER,
    "selfstock": [],
    "positions": {},
}
_DATA_DIR: Path | None = None
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_a_stock(code: str) -> bool:
    return bool(_A_STOCK_RE.match(code))


# --------------------------------------------------------------------------
# 配置 / 状态持久化
# --------------------------------------------------------------------------
def _state_path() -> Path | None:
    if _DATA_DIR is None:
        return None
    return _DATA_DIR / "ths_sync_state.json"


def load_state(data_dir: Path) -> None:
    """startup 时把磁盘上的上次快照读回内存（读失败就用默认值，不阻塞启动）。"""
    global _DATA_DIR
    _DATA_DIR = data_dir
    path = _state_path()
    if path is None or not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ths_sync: 读状态失败 %s (%s)", path, exc)
        return
    if not isinstance(raw, dict):
        return
    with _LOCK:
        for key in ("auto", "interval", "sync_selfstock", "sync_positions", "ths_user"):
            if key in raw:
                _STATE[key] = raw[key]
        if isinstance(raw.get("selfstock"), list):
            _STATE["selfstock"] = [str(x) for x in raw["selfstock"] if is_a_stock(str(x))]
        if isinstance(raw.get("positions"), dict):
            _STATE["positions"] = {
                str(k): v for k, v in raw["positions"].items() if is_a_stock(str(k))
            }
        if raw.get("last_sync"):
            _RUNTIME["last_sync"] = str(raw["last_sync"])
        try:
            _RUNTIME["sync_count"] = int(raw.get("sync_count") or 0)
        except (TypeError, ValueError):
            _RUNTIME["sync_count"] = 0


def save_state() -> None:
    path = _state_path()
    if path is None:
        return
    with _LOCK:
        payload = dict(_STATE)
        payload["last_sync"] = _RUNTIME["last_sync"]
        payload["sync_count"] = _RUNTIME["sync_count"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ths_sync: 写状态失败 %s (%s)", path, exc)


def get_config() -> dict:
    with _LOCK:
        return {
            "auto": bool(_STATE["auto"]),
            "interval": int(_STATE["interval"]),
            "sync_selfstock": bool(_STATE["sync_selfstock"]),
            "sync_positions": bool(_STATE["sync_positions"]),
            "ths_user": str(_STATE["ths_user"]),
        }


def set_config(
    *,
    auto: bool | None = None,
    interval: int | None = None,
    sync_selfstock: bool | None = None,
    sync_positions: bool | None = None,
    ths_user: str | None = None,
) -> dict:
    with _LOCK:
        if auto is not None:
            _STATE["auto"] = bool(auto)
        if interval is not None:
            _STATE["interval"] = max(MIN_INTERVAL, int(interval))
        if sync_selfstock is not None:
            _STATE["sync_selfstock"] = bool(sync_selfstock)
        if sync_positions is not None:
            _STATE["sync_positions"] = bool(sync_positions)
        if ths_user:
            _STATE["ths_user"] = str(ths_user).strip() or DEFAULT_THS_USER
    save_state()
    return get_config()


# --------------------------------------------------------------------------
# 读取两个来源
# --------------------------------------------------------------------------
def _ths_paths() -> tuple[Path, Path]:
    with _LOCK:
        user = str(_STATE["ths_user"]) or DEFAULT_THS_USER
    user_dir = THS_ROOT / "bin" / "users" / user
    return user_dir / "SelfStockCache.json", THS_ROOT / "transaction" / "xiadan-plus"


def _parse_selfstock_file(path: Path) -> list[str]:
    """SelfStockCache.json 结构与 xiadan 的 selfstock_*.json 兼容（大小写两套键）。"""
    raw = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not isinstance(raw, dict):
        return []
    body = raw.get("Data") or raw.get("data")
    if not isinstance(body, dict):
        return []
    text = body.get("Selfstock") or body.get("selfstock") or ""
    # 形如 "603628|603330|...|,17|17|33|..."：逗号前才是代码段
    codes_part = str(text).split(",")[0]
    out: list[str] = []
    for token in codes_part.split("|"):
        code = token.strip()
        if is_a_stock(code) and code not in out:
            out.append(code)
    return out


def read_selfstock() -> list[str] | None:
    """同花顺自选股代码。全部来源都读不到时返回 None（沿用上次快照，不误清空）。"""
    main_file, xiadan_dir = _ths_paths()
    found: list[str] = []
    hit = False
    candidates: list[Path] = []
    if main_file.exists():
        candidates.append(main_file)
    if xiadan_dir.exists():
        try:
            candidates.extend(sorted(xiadan_dir.glob("selfstock_*.json")))
        except OSError:
            pass
    for path in candidates:
        try:
            codes = _parse_selfstock_file(path)
        except Exception:  # noqa: BLE001
            continue
        if codes:
            hit = True
            for code in codes:
                if code not in found:
                    found.append(code)
    return found if hit else None


def read_positions() -> dict[str, dict] | None:
    """v1 A 股助手 的持仓（成本含费）。v1 未运行时返回 None。"""
    if not _STATE.get("sync_positions", True):
        return {}
    try:
        req = urllib.request.Request(V1_POSITIONS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=V1_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    rows = raw.get("positions") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return None
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if not is_a_stock(code):
            continue
        qty = row.get("qty") or 0
        if isinstance(qty, (int, float)) and qty <= 0:
            continue
        try:
            cost = float(row.get("avg_cost") or row.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        out[code] = {
            "code": code,
            "name": str(row.get("name") or ""),
            "qty": qty,
            "avail": row.get("avail"),
            "frozen": row.get("frozen"),
            "cost": cost,
            "price": row.get("price"),
            "pct": row.get("pct"),
            "pnl": row.get("pnl"),
            "day_pnl": row.get("day_pnl"),
            "pnl_pct": row.get("pnl_pct"),
        }
    return out


# --------------------------------------------------------------------------
# TSP 自选池写入（进程内直调 watchlist 服务）
# --------------------------------------------------------------------------
def _ensure_group(name: str, color: str) -> str:
    groups = watchlist.list_groups()
    for group in groups:
        if str(group.get("name") or "").casefold() == name.casefold():
            return str(group["id"])
    _groups, created = watchlist.create_group(name, color)
    return str(created["id"])


def _sync_group_members(group_id: str, desired: set[str], actions: list[str]) -> None:
    """只对差集打补丁：多的移出、少的加入，绝不整组重写。"""
    current = {
        str(row["symbol"])
        for row in watchlist.list_symbols()
        if group_id in (row.get("group_ids") or [])
    }
    for symbol in sorted(current - desired):
        try:
            watchlist.remove_from_group(symbol, group_id)
            actions.append(f"-{symbol}")
        except (KeyError, ValueError):
            pass
    for symbol in sorted(desired - current):
        if symbol not in {str(row["symbol"]) for row in watchlist.list_symbols()}:
            continue
        try:
            watchlist.add_to_group(symbol, group_id)
            actions.append(f"+{symbol}")
        except (KeyError, ValueError):
            pass


def run_once(*, force_selfstock: bool = False) -> dict:
    """执行一轮同步。返回结构化结果，结构同时供前端页面与接口消费。"""
    started = time.perf_counter()
    errors: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    group_actions: list[str] = []

    with _LOCK:
        want_self = bool(_STATE["sync_selfstock"]) or force_selfstock
        want_pos = bool(_STATE["sync_positions"])

    # 1) 读两端
    self_codes: list[str] | None = None
    if want_self:
        self_codes = read_selfstock()
        if self_codes is None:
            errors.append("同花顺自选读取失败，沿用上次快照")
            with _LOCK:
                self_codes = list(_STATE["selfstock"])
        else:
            with _LOCK:
                _STATE["selfstock"] = self_codes
    else:
        with _LOCK:
            self_codes = list(_STATE["selfstock"])

    pos_map: dict[str, dict] = {}
    if want_pos:
        fresh = read_positions()
        if fresh is None:
            errors.append("v1 持仓接口不可用，沿用上次快照")
            with _LOCK:
                pos_map = dict(_STATE["positions"])
        else:
            pos_map = fresh
            with _LOCK:
                _STATE["positions"] = fresh
    else:
        with _LOCK:
            pos_map = dict(_STATE["positions"])

    self_set = set(self_codes)
    pos_set = set(pos_map.keys())
    desired = self_set | pos_set

    # 2) 现状：note 命中本扩展的条目才算「我们拥有的」
    current = {str(row["symbol"]): row for row in watchlist.list_symbols()}
    owned = {sym for sym, row in current.items() if NOTE in str(row.get("note") or "")}

    # 3) 分组
    try:
        gid_self = _ensure_group(GROUP_SELF, COLOR_SELF)
        gid_pos = _ensure_group(GROUP_POS, COLOR_POS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ths_sync: 建组失败 %s", exc)
        return {
            "ok": False,
            "ts": _now(),
            "added": [],
            "removed": [],
            "group_actions": [],
            "errors": errors + [f"分组创建失败: {exc}"],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "selfstock": len(self_set),
            "positions": len(pos_set),
        }

    # 4) 删掉已不在两池里的
    for symbol in sorted(owned - desired):
        watchlist.remove(symbol)
        removed.append(symbol)

    # 5) 只推新增（add_batch 会把每只都置顶，重复推会打乱用户排序）
    fresh_codes = sorted(desired - set(current.keys()))
    if fresh_codes:
        bucket_self = [c for c in fresh_codes if c in self_set]
        bucket_pos = [c for c in fresh_codes if c not in self_set]
        try:
            if bucket_self:
                _rows, n = watchlist.add_batch(
                    bucket_self, note=NOTE, group_ids=[gid_self]
                )
                added.extend(bucket_self[:n] if n else [])
            if bucket_pos:
                _rows, n = watchlist.add_batch(
                    bucket_pos, note=NOTE, group_ids=[gid_pos]
                )
                added.extend(bucket_pos[:n] if n else [])
            if not added:
                added.extend(fresh_codes)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"写入自选池失败: {exc}")

    # 6) 分组差集对齐（含已存在标的的组归属修正）
    try:
        _sync_group_members(gid_self, self_set & desired, group_actions)
        _sync_group_members(gid_pos, pos_set, group_actions)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"分组对齐失败: {exc}")

    result = {
        "ok": not errors or len(errors) < 2,
        "ts": _now(),
        "added": added,
        "removed": removed,
        "group_actions": group_actions,
        "errors": errors,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "selfstock": len(self_set),
        "positions": len(pos_set),
    }
    with _LOCK:
        _RUNTIME["sync_count"] += 1
        _RUNTIME["last_sync"] = result["ts"]
        _RUNTIME["last_result"] = result
    save_state()
    return result


# --------------------------------------------------------------------------
# 状态汇总（前端页面唯一数据源）
# --------------------------------------------------------------------------
def build_status() -> dict:
    with _LOCK:
        cfg = dict(_STATE)
        runtime = dict(_RUNTIME)
    positions_rows = [
        {
            "code": row.get("code"),
            "name": row.get("name"),
            "qty": row.get("qty"),
            "cost": row.get("cost"),
            "price": row.get("price"),
            "pnl": row.get("pnl"),
            "pnl_pct": row.get("pnl_pct"),
        }
        for row in (cfg.get("positions") or {}).values()
    ]
    positions_rows.sort(key=lambda r: str(r.get("code") or ""))
    try:
        symbols = watchlist.list_symbols()
        groups = watchlist.list_groups()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ths_sync: 读自选池失败 %s", exc)
        symbols, groups = [], []
    return {
        "ok": True,
        "bridge": {
            "version": 3,
            "mode": "in-process",
            "auto": bool(cfg.get("auto")),
            "interval": int(cfg.get("interval") or DEFAULT_INTERVAL),
            "started_at": runtime.get("started_at") or "",
            "sync_count": int(runtime.get("sync_count") or 0),
            "last_sync": runtime.get("last_sync") or "",
            "thread_alive": bool(runtime.get("thread_alive")),
        },
        "tsp": {
            "online": True,
            "base": "in-process",
            "version": "",
            "watchlist_count": len(symbols),
            "groups": [
                {"id": g.get("id"), "name": g.get("name"), "color": g.get("color")}
                for g in groups
            ],
        },
        "sources": {
            "selfstock_count": len(cfg.get("selfstock") or []),
            "positions_count": len(positions_rows),
            "selfstock": list(cfg.get("selfstock") or []),
            "positions": positions_rows,
            "position_pnl_total": round(
                sum(
                    float(r.get("pnl") or 0)
                    for r in positions_rows
                    if isinstance(r.get("pnl"), (int, float))
                ),
                2,
            ),
        },
        "config": {
            "sync_selfstock": bool(cfg.get("sync_selfstock")),
            "sync_positions": bool(cfg.get("sync_positions")),
            "ths_user": str(cfg.get("ths_user") or DEFAULT_THS_USER),
            "ths_root": str(THS_ROOT),
        },
        "last_result": runtime.get("last_result"),
    }


# --------------------------------------------------------------------------
# 后台自动同步线程
# --------------------------------------------------------------------------
def _loop() -> None:
    _RUNTIME["thread_alive"] = True
    try:
        while not _STOP.is_set():
            with _LOCK:
                auto = bool(_STATE["auto"])
                interval = max(MIN_INTERVAL, int(_STATE["interval"] or DEFAULT_INTERVAL))
            if auto:
                try:
                    run_once()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ths_sync: 自动同步异常 %s", exc)
            _STOP.wait(interval)
    finally:
        _RUNTIME["thread_alive"] = False


def start(data_dir: Path) -> None:
    global _THREAD
    _RUNTIME["started_at"] = _now()
    load_state(data_dir)
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="ths-sync", daemon=True)
    _THREAD.start()
    logger.info("ths_sync: 后台同步线程已启动 (interval=%ss)", _STATE["interval"])


def stop() -> None:
    _STOP.set()
