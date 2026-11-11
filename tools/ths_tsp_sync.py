# -*- coding: utf-8 -*-
"""同花顺远航版 → TSP 同步桥 (v2 · 实测可跑版)

设计原则（BOSS 硬约束）：
  1 绝不抢焦点、绝不做 UI 模拟点击 —— 只读本地缓存文件 + 调 HTTP。
  2 自选股：读 SelfStockCache.json + xiadan-plus/selfstock_*.json 取并集。
  3 持仓：读 v1 老服务 /api/positions（v1 的 OCR 已把成本含费算好，直接复用）。
  4 推送：直写 TSP 现有 POST /api/watchlist/batch，并用两个自选分组区分两池
     （「同花顺自选」盯买点 / 「同花顺持仓」盯卖点，对齐 v1 铁律）。
  5 自动同步 --loop（默认 60 秒，纯后台线程）；一键按钮走 127.0.0.1:17652
     的 GET /api/status、POST /api/sync；--once 供 .bat 双击调用。

为什么不用 /api/openapi.json?tier=a 动态找端点：
  实测桌面端 v0.3.34 该接口返回空 paths；扩展表写入端点需 admin 且要建表。
  而 POST /api/watchlist/batch 已实测可用且无需 token（auth configured=false），
  因此这里全部硬编码为已验证契约，不再猜端点。

用法：
  python ths_tsp_sync.py --once                 # 跑一轮就退出（.bat 一键同步）
  python ths_tsp_sync.py --loop                # 自动轮询 + 本地按钮服务（推荐）
  python ths_tsp_sync.py --serve               # 只起按钮服务，不自动轮询
  python ths_tsp_sync.py --dry-run             # 只读不写，打印将同步什么
  python ths_tsp_sync.py --install             # 写入 Windows 启动项（pythonw 隐藏运行）
  python ths_tsp_sync.py --discover            # 打印 TSP 自选/分组现状
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- 路径与常量 ----------------

TOOLS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOLS_DIR / "ths_tsp_sync.json"
STATE_PATH = TOOLS_DIR / "ths_tsp_sync.state.json"
LOG_PATH = TOOLS_DIR / "ths_tsp_sync.log"

# 同花顺远航版（与 v1 ashare/config.py 保持一致）
THS_ROOT = Path(r"C:\同花顺远航版")
THS_USER = "爱因思睿"
THS_USER_DIR = THS_ROOT / "bin" / "users" / THS_USER
THS_SELFSTOCK = THS_USER_DIR / "SelfStockCache.json"
THS_XIADAN_DIR = THS_ROOT / "transaction" / "xiadan-plus"

# v1 老服务（只读，绝不改它）
V1_POSITIONS_URL = "http://127.0.0.1:17651/api/positions"

# TSP 已验证端点
TSP_WATCHLIST = "/api/watchlist"
TSP_BATCH = "/api/watchlist/batch"
TSP_GROUPS = "/api/watchlist/groups"
# 注意：/api/health 返回前端 HTML（SPA），真正的健康 JSON 在根 /health
TSP_HEALTH = "/health"

# 桌面端端口是动态的：desktop.py:437 用 _find_free_port(3018)，3018 被占会漂到 3019+。
# 桥不能写死 3018 —— 启动、每轮同步、状态查询都会重新探测（resolve_tsp_base）。
TSP_BASE_PORT = 3018
TSP_PORT_RANGE = 50        # 与 desktop.py:33 _PORT_PROBE_RANGE 对齐
TSP_PROBE_TIMEOUT = 0.6    # 单端口探测超时；本机拒绝连接是瞬时的，全离线另有 6s 总闸

NOTE = "同花顺同步"           # 标记本桥写入的条目，删除时只删自己写的
GROUP_SELF = "同花顺自选"     # 盯买点
GROUP_POS = "同花顺持仓"      # 盯卖点
COLOR_SELF = "sky"
COLOR_POS = "rose"

DEFAULT_CONFIG = {
    "tsp_base": "http://127.0.0.1:3018",
    "token": "",              # 桌面端无鉴权，留空即可；Docker 版可填 Bearer
    "interval": 60,
    "bridge_host": "127.0.0.1",
    "bridge_port": 17652,
    "sync_selfstock": True,
    "sync_positions": True,
}

_A_STOCK_RE = re.compile(r"^[03684]\d{5}$")
log = logging.getLogger("ths_tsp_sync")

# 全局运行态（供 HTTP 状态接口读）
_RUNTIME = {
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "auto": False,
    "interval": 60,
    "last_sync": "",
    "last_result": None,
    "sync_count": 0,
    "lock": threading.Lock(),
}


def is_a_stock(c: str) -> bool:
    return bool(_A_STOCK_RE.match(c))


def setup_log(verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fh)
    except Exception as e:  # noqa: BLE001
        log.debug("日志文件不可用: %s", e)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception as e:  # noqa: BLE001
            log.warning("配置文件读取失败，用默认值: %s", e)
    else:
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("已生成配置: %s", CONFIG_PATH)
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(st: dict) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("状态写入失败: %s", e)


# ---------------- 读取同花顺 / v1 ----------------

def read_selfstock():
    """返回代码 list；缓存文件缺失/损坏返回 None（表示「沿用上次」，不误清空）。"""
    out: list[str] = []
    seen: set[str] = set()
    ok = False

    def _eat(path: Path) -> None:
        nonlocal ok
        try:
            j = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            log.debug("读 %s 失败: %s", path.name, e)
            return
        d = j.get("Data") or j.get("data") or {}
        raw = d.get("Selfstock") or d.get("selfstock") or ""
        head = str(raw).split(",")[0]
        for c in head.split("|"):
            c = c.strip()
            if is_a_stock(c) and c not in seen:
                seen.add(c)
                out.append(c)
        ok = True

    if THS_SELFSTOCK.exists():
        _eat(THS_SELFSTOCK)
    else:
        log.warning("自选股缓存不存在: %s", THS_SELFSTOCK)

    if THS_XIADAN_DIR.is_dir():
        for f in sorted(THS_XIADAN_DIR.glob("selfstock_*.json")):
            _eat(f)

    if not ok:
        return None
    return out


def read_positions():
    """返回 {code: {...}}；v1 不可用返回 None（沿用上次，不误清空）。"""
    try:
        req = urllib.request.Request(V1_POSITIONS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("v1 /api/positions 不可用: %s", e)
        return None

    out = {}
    for p in j.get("positions") or []:
        c = str(p.get("code") or "").strip()
        if not is_a_stock(c):
            continue
        out[c] = {
            "code": c,
            "name": p.get("name") or "",
            "qty": float(p.get("qty") or 0),
            "avail": float(p.get("avail") or 0),
            "frozen": float(p.get("frozen") or 0),
            "cost": float(p.get("avg_cost") or p.get("cost") or 0),
            "price": float(p.get("price") or 0),
            "pct": float(p.get("pct") or 0),
            "pnl": float(p.get("pnl") or 0),
            "day_pnl": float(p.get("day_pnl") or 0),
            "pnl_pct": float(p.get("pnl_pct") or 0),
        }
    return out


# ---------------- TSP HTTP ----------------

def probe_tsp_health(base: str, token: str = "", timeout: float = TSP_PROBE_TIMEOUT) -> bool:
    """GET /health 必须真的是 TSP 的 {status:ok,version/mode} JSON，不是 SPA HTML 或别的服务。"""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(base + TSP_HEALTH, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            d = json.loads(raw) if raw else None
            return (r.status == 200 and isinstance(d, dict)
                    and d.get("status") == "ok" and ("version" in d or "mode" in d))
    except Exception:  # noqa: BLE001
        return False


def resolve_tsp_base(configured_base: str, token: str = "") -> str | None:
    """返回 TSP 当前实际监听的 base：先试配置值，再扫 3018..3067。
    桌面端重启时端口会从 3018 漂移；写死端口会让整条同步链失联。"""
    configured_base = (configured_base or f"http://127.0.0.1:{TSP_BASE_PORT}").rstrip("/")
    if probe_tsp_health(configured_base, token):
        return configured_base
    p = urllib.parse.urlsplit(configured_base)
    scheme, host = p.scheme or "http", p.hostname or "127.0.0.1"
    deadline = time.monotonic() + 6.0
    for port in range(TSP_BASE_PORT, TSP_BASE_PORT + TSP_PORT_RANGE):
        if time.monotonic() > deadline:
            break
        cand = f"{scheme}://{host}:{port}"
        if cand == configured_base:
            continue
        if probe_tsp_health(cand, token):
            log.info("探测到 TSP 漂移端口 %s", cand)
            return cand
    return None


class TspClient:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def request(self, method: str, path: str, body=None, timeout: int = 15):
        url = self.base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
                try:
                    return r.status, json.loads(raw) if raw else None
                except Exception:  # noqa: BLE001
                    return r.status, raw
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            return e.code, txt
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    def ok(self, code: int) -> bool:
        return code in (200, 201, 204)

    def refresh_base(self) -> bool:
        """当前 base 失联时重新探测；成功则原地更新 self.base（所有引用自动生效）。"""
        found = resolve_tsp_base(self.base, self.token)
        if found and found != self.base:
            log.warning("TSP 地址更新 %s → %s", self.base, found)
            self.base = found
            return True
        return bool(found)

    def ensure_online(self) -> bool:
        """每轮一次快速健康探测；失联才触发端口扫描。"""
        if probe_tsp_health(self.base, self.token):
            return True
        return self.refresh_base()


def ensure_group(cli: TspClient, groups: list[dict], name: str, color: str) -> tuple[str, list[dict]]:
    """按名称找分组，没有就建。返回 (group_id, 最新分组列表)。"""
    for g in groups:
        if str(g.get("name") or "").strip() == name:
            return g["id"], groups
    code, resp = cli.request("POST", TSP_GROUPS, {"name": name, "color": color})
    if cli.ok(code) and isinstance(resp, dict):
        group = resp.get("group") or {}
        if group.get("id"):
            log.info("已建分组「%s」(%s)", name, group["id"])
            return group["id"], resp.get("groups") or groups
    # 400「分组名称已存在」等并发情况：重列一次
    code, resp = cli.request("GET", TSP_GROUPS)
    if cli.ok(code) and isinstance(resp, dict):
        groups = resp.get("groups") or []
        for g in groups:
            if str(g.get("name") or "").strip() == name:
                return g["id"], groups
    raise RuntimeError(f"无法创建/找到分组「{name}」: {code} {str(resp)[:200]}")


def sync_positions_to_groups(cli: TspClient, rows: list[dict], self_set: set[str],
                             pos_set: set[str], gid_self: str, gid_pos: str) -> list[str]:
    """把每个标的的分组归属对齐到两池；只打差异请求。返回操作日志。"""
    actions: list[str] = []
    owned = {gid_self, gid_pos}
    for row in rows:
        sym = str(row.get("symbol") or "")
        if not sym:
            continue
        have = set(row.get("group_ids") or [])
        want: set[str] = set()
        if sym in self_set:
            want.add(gid_self)
        if sym in pos_set:
            want.add(gid_pos)
        # 只接管本桥管理的两个分组，用户自己建的其他分组不动
        stale = (have & owned) - want
        fresh = want - have
        for gid in fresh:
            c, _ = cli.request("POST", f"{TSP_GROUPS}/{gid}/members/{sym}")
            if cli.ok(c):
                actions.append(f"+{sym}→{GROUP_SELF if gid == gid_self else GROUP_POS}")
            else:
                log.warning("加分组失败 %s %s: %s", sym, gid, c)
        for gid in stale:
            c, _ = cli.request("DELETE", f"{TSP_GROUPS}/{gid}/members/{sym}")
            if cli.ok(c):
                actions.append(f"-{sym}←{GROUP_SELF if gid == gid_self else GROUP_POS}")
            else:
                log.warning("摘分组失败 %s %s: %s", sym, gid, c)
    return actions


# ---------------- 同步主流程 ----------------

def run_once(cli: TspClient, cfg: dict, dry: bool = False, force: bool = False) -> dict:
    st = load_state()
    t0 = time.time()
    result = {
        "ok": False, "dry": dry, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selfstock": [], "positions": [], "added": [], "removed": [],
        "group_actions": [], "errors": [], "elapsed_ms": 0,
    }

    # 1) 读两端；读不到就沿用上次，绝不误清空
    self_codes = read_selfstock() if cfg.get("sync_selfstock", True) else []
    pos = read_positions() if cfg.get("sync_positions", True) else {}
    if self_codes is None:
        self_codes = st.get("selfstock") or []
        result["errors"].append("同花顺自选缓存暂不可读，沿用上次快照")
    if pos is None:
        pos = st.get("positions") or {}
        result["errors"].append("v1 持仓接口暂不可用，沿用上次快照")

    self_set = set(self_codes)
    pos_set = set(pos)
    desired = list(dict.fromkeys(list(self_codes) + list(pos_set)))
    result["selfstock"] = self_codes
    result["positions"] = [
        {"code": c, "name": p["name"], "qty": p["qty"], "cost": round(p["cost"], 3),
         "price": p["price"], "pnl": round(p["pnl"], 2), "pnl_pct": round(p["pnl_pct"], 2)}
        for c, p in sorted(pos.items())
    ]
    log.info("读到 自选 %d 只 / 持仓 %d 只 / 合并 %d 只", len(self_set), len(pos_set), len(desired))

    if dry:
        result["ok"] = True
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        log.info("[dry-run] 将同步 %d 只，不写入 TSP", len(desired))
        return result

    if not desired:
        result["errors"].append("两端都为空，跳过本轮（防止误清空 TSP）")
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result

    # 桌面端重启可能换端口；每轮先确认/重探地址
    if not cli.ensure_online():
        result["errors"].append(
            f"TSP 不在线（已探测 {TSP_BASE_PORT}..{TSP_BASE_PORT + TSP_PORT_RANGE - 1}）")
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result

    # 2) TSP 现状
    code, resp = cli.request("GET", TSP_WATCHLIST)
    if not cli.ok(code) or not isinstance(resp, dict):
        result["errors"].append(f"读 TSP 自选失败: {code} {str(resp)[:160]}")
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result
    cur_rows = resp.get("symbols") or []
    cur_map = {str(r.get("symbol") or ""): r for r in cur_rows}
    owned_now = {s for s, r in cur_map.items() if NOTE in str(r.get("note") or "")}

    # 3) 两个分组
    code, gresp = cli.request("GET", TSP_GROUPS)
    groups = (gresp.get("groups") or []) if (cli.ok(code) and isinstance(gresp, dict)) else []
    try:
        gid_self, groups = ensure_group(cli, groups, GROUP_SELF, COLOR_SELF)
        gid_pos, groups = ensure_group(cli, groups, GROUP_POS, COLOR_POS)
    except Exception as e:  # noqa: BLE001
        result["errors"].append(str(e))
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result
    result["groups"] = {GROUP_SELF: gid_self, GROUP_POS: gid_pos}

    # 4) 删除本桥以前写、现在不在两池中的标的（用户手动加的绝不删）
    for s in sorted(owned_now - set(desired)):
        c, _ = cli.request("DELETE", f"{TSP_WATCHLIST}/{s}")
        if cli.ok(c):
            result["removed"].append(s)
            log.info("移除过期标的 %s", s)
        else:
            log.warning("移除 %s 失败: %s", s, c)

    # 5) 新增标的（按两池归属分 3 批，避免 80 只每 60 秒重排整个列表）
    buckets: dict[tuple, list[str]] = {}
    for s in desired:
        if s in cur_map:
            continue
        key = (s in self_set, s in pos_set)
        buckets.setdefault(key, []).append(s)
    for (in_self, in_pos), syms in buckets.items():
        gids = []
        if in_self:
            gids.append(gid_self)
        if in_pos:
            gids.append(gid_pos)
        c, _ = cli.request("POST", TSP_BATCH, {"symbols": syms, "note": NOTE, "group_ids": gids})
        if cli.ok(c):
            result["added"].extend(syms)
            log.info("新增 %d 只 → TSP（分组 %s）", len(syms), gids)
        else:
            msg = f"批量新增失败 {syms[:3]}...: {c}"
            result["errors"].append(msg)
            log.warning(msg)

    # 6) 对齐分组归属（含用户手动加过、后来进入同花顺自选的标的）
    code, resp2 = cli.request("GET", TSP_WATCHLIST)
    rows2 = (resp2.get("symbols") or []) if (cli.ok(code) and isinstance(resp2, dict)) else cur_rows
    targets = [r for r in rows2 if str(r.get("symbol") or "") in set(desired)]
    result["group_actions"] = sync_positions_to_groups(
        cli, targets, self_set, pos_set, gid_self, gid_pos
    )

    # 7) 落状态
    st["selfstock"] = self_codes
    st["positions"] = pos
    st["ts"] = result["ts"]
    st["last_result"] = {k: v for k, v in result.items() if k != "positions"}
    save_state(st)

    result["ok"] = not result["errors"]
    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    _RUNTIME["last_sync"] = result["ts"]
    _RUNTIME["last_result"] = result
    _RUNTIME["sync_count"] += 1
    log.info("同步完成：+%d -%d，分组调整 %d 项，耗时 %dms",
             len(result["added"]), len(result["removed"]),
             len(result["group_actions"]), result["elapsed_ms"])
    return result


def discover(cli: TspClient) -> None:
    for label, path in (("health", TSP_HEALTH), ("watchlist", TSP_WATCHLIST), ("groups", TSP_GROUPS)):
        c, r = cli.request("GET", path, timeout=5)
        print(f"--- {label} GET {path} -> {c}")
        print(json.dumps(r, ensure_ascii=False)[:800])
    print("--- write contract: POST /api/watchlist/batch {symbols,note,group_ids}")


# ---------------- 本地按钮服务 ----------------

class BridgeHandler(BaseHTTPRequestHandler):
    cli: TspClient = None
    cfg: dict = {}

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/api/health"):
            self._send(200, {"ok": True, "service": "ths-tsp-sync-bridge", "version": 2})
        elif path == "/api/status":
            self._send(200, self._status())
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/sync":
            with _RUNTIME["lock"]:
                try:
                    res = run_once(self.cli, self.cfg, dry=False, force=True)
                except Exception as e:  # noqa: BLE001
                    log.exception("按钮同步失败")
                    res = {"ok": False, "errors": [f"{type(e).__name__}: {e}"]}
            self._send(200 if res.get("ok") else 500, res)
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _status(self):
        cli = self.cli
        cli.ensure_online()   # 端口漂移时自动重探
        c, r = cli.request("GET", TSP_HEALTH, timeout=5)
        tsp_ok = cli.ok(c)
        c2, r2 = cli.request("GET", TSP_WATCHLIST, timeout=5)
        wl = (r2.get("symbols") or []) if (cli.ok(c2) and isinstance(r2, dict)) else []
        c3, r3 = cli.request("GET", TSP_GROUPS, timeout=5)
        groups = (r3.get("groups") or []) if (cli.ok(c3) and isinstance(r3, dict)) else []
        last = _RUNTIME.get("last_result") or {}
        return {
            "ok": True,
            "bridge": {"version": 2, "auto": _RUNTIME["auto"],
                       "interval": _RUNTIME["interval"],
                       "started_at": _RUNTIME["started_at"],
                       "sync_count": _RUNTIME["sync_count"],
                       "last_sync": _RUNTIME["last_sync"]},
            "tsp": {"online": tsp_ok, "base": cli.base,
                    "version": (r or {}).get("version") if isinstance(r, dict) else "",
                    "watchlist_count": len(wl),
                    "groups": [{"id": g.get("id"), "name": g.get("name")} for g in groups]},
            "sources": {
                "selfstock_count": len(last.get("selfstock") or []),
                "positions_count": len(last.get("positions") or []),
                "positions": last.get("positions") or [],
                "selfstock": last.get("selfstock") or [],
            },
            "last_result": last,
        }


def serve_forever(cli: TspClient, cfg: dict, auto: bool) -> None:
    _RUNTIME["auto"] = auto
    _RUNTIME["interval"] = int(cfg.get("interval") or 60)
    BridgeHandler.cli = cli
    BridgeHandler.cfg = cfg
    host = cfg.get("bridge_host") or "127.0.0.1"
    port = int(cfg.get("bridge_port") or 17652)
    server = ThreadingHTTPServer((host, port), BridgeHandler)
    server.daemon_threads = True
    log.info("按钮服务已监听 http://%s:%d （auto=%s, interval=%ss）", host, port,
             auto, _RUNTIME["interval"])
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    if auto:
        def _loop():
            # 启动即先跑一轮，之后每 interval 秒一轮
            while True:
                with _RUNTIME["lock"]:
                    try:
                        run_once(cli, cfg, dry=False)
                    except Exception as e:  # noqa: BLE001
                        log.exception("自动同步本轮失败: %s", e)
                time.sleep(_RUNTIME["interval"])
        threading.Thread(target=_loop, daemon=True, name="auto-sync").start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("收到 Ctrl-C，退出")
        server.shutdown()


# ---------------- 开机自启 ----------------

def install_autostart() -> int:
    pyw = Path(r"C:\Python314\pythonw.exe")
    if not pyw.exists():
        pyw = Path(sys.executable).with_name("pythonw.exe")
    script = Path(__file__).resolve()
    startup = Path(os.environ.get("APPDATA", "")) / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    vbs = startup / "ths_tsp_sync.vbs"
    cmd = f'"{pyw}" "{script}" --loop'
    body = (
        "' 同花顺 → TSP 同步桥：隐藏窗口启动，零 UI 模拟、不抢焦点\n"
        "Set sh = CreateObject(\"WScript.Shell\")\n"
        f'sh.CurrentDirectory = "{script.parent}"\n'
        'sh.Run "' + cmd.replace('"', '""') + '", 0, False\n'
    )
    vbs.write_text(body, encoding="utf-8")
    print(f"已写入启动项: {vbs}")
    print(f"启动命令: {cmd}")
    print(f"删除该文件即可取消开机自启")
    return 0


# ---------------- CLI ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="同花顺远航版 → TSP 同步桥（只读文件 + HTTP，不抢焦点）")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（一键同步）")
    ap.add_argument("--loop", action="store_true", help="自动轮询 + 本地按钮服务（推荐）")
    ap.add_argument("--serve", action="store_true", help="只起本地按钮服务，不自动轮询")
    ap.add_argument("--interval", type=int, default=0, help="轮询间隔秒，覆盖配置")
    ap.add_argument("--dry-run", action="store_true", help="只读不写，打印将同步什么")
    ap.add_argument("--discover", action="store_true", help="打印 TSP 自选/分组现状")
    ap.add_argument("--install", action="store_true", help="写入 Windows 开机自启（隐藏窗口）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    setup_log(args.verbose)
    if args.install:
        return install_autostart()

    cfg = load_config()
    if args.interval:
        cfg["interval"] = args.interval
    cli = TspClient(cfg["tsp_base"], cfg.get("token") or "")

    if args.discover:
        cli.ensure_online()
        discover(cli)
        return 0

    if not cli.ensure_online():
        log.error("连不上 TSP（探测 %d..%d 均失败，桌面端起了吗？）",
                  TSP_BASE_PORT, TSP_BASE_PORT + TSP_PORT_RANGE - 1)
        return 2
    log.info("TSP 在线 %s", cli.base)

    if args.loop or args.serve:
        serve_forever(cli, cfg, auto=args.loop)
        return 0

    res = run_once(cli, cfg, dry=args.dry_run)
    print(json.dumps({k: v for k, v in res.items() if k not in ("positions", "selfstock")},
                     ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
