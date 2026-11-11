# -*- coding: utf-8 -*-
"""同花顺远航版 → TSP 同步桥。

设计原则（BOSS 硬约束）：
  1 绝不抢焦点、绝不做 UI 模拟点击 —— 只读文件 + 调 HTTP，同花顺该干嘛干嘛。
  2 自选股：读本地缓存文件（SelfStockCache.json + selfstock_*.json 取并集）。
  3 持仓：读 v1 老服务 /api/positions（v1 的 OCR 已把成本含费算好，直接复用，
    不重跑 OCR、不碰 xiadan.exe）。
  4 推送：走 TSP 开放接口（Token 六档权限）。
  5 两种模式：--once 一键同步 / --loop 自动轮询（默认 60 秒，只推变化）。

为什么不用 UI 模拟：
  v1 的 ths_ui.py 是为「写板块让手机可见」造的，必须抢前台。而本任务只是把
  数据「读出来」给 TSP，读文件是零打扰路径，不存在焦点竞争。

用法：
  python tools/ths_tsp_sync.py --discover          # 第一步：打印 TSP 全部端点
  python tools/ths_tsp_sync.py --dry-run --once    # 第二步：只看读到了什么，不推送
  python tools/ths_tsp_sync.py --once              # 一键同步
  python tools/ths_tsp_sync.py --loop              # 后台自动（60 秒一轮）

配置文件：tools/ths_tsp_sync.json（首次运行自动生成模板）
  {"tsp_base": "http://127.0.0.1:3018", "token": "tsp_xxx", "interval": 60}
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
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

DEFAULT_CONFIG = {
    "tsp_base": "http://127.0.0.1:3018",
    "token": "",
    "interval": 60,
    "sync_selfstock": True,
    "sync_positions": True,
}

# A 股白名单：6 位数字，首位 0/3/6/8/4，排除占位码
_A_STOCK_RE = re.compile(r"^[03684]\d{5}$")
_PLACEHOLDER = {"000000", "999999", "888888"}


def is_a_stock(code: str) -> bool:
    c = (code or "").strip()
    return bool(_A_STOCK_RE.match(c)) and c not in _PLACEHOLDER


# ---------------- 日志 ----------------

log = logging.getLogger("ths_tsp_sync")


def setup_log(verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass


# ---------------- 配置 ----------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception as e:
            log.warning("配置解析失败，用默认值: %s", e)
    else:
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("已生成配置模板: %s（填 token 后重跑）", CONFIG_PATH)
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"selfstock": [], "positions": {}, "ts": ""}


def save_state(st: dict) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as e:
        log.warning("状态落盘失败: %s", e)


# ---------------- 同花顺侧：只读 ----------------

def read_selfstock() -> list[str]:
    """自选股代码列表。

    SelfStockCache.json 结构：
      {"Data": {"Selfstock": "002029|601566|...,33|17|...", "Count": 50, ...}}
    逗号前是代码（| 分隔），逗号后是市场号，两段长度一一对应。
    另外 xiadan-plus/selfstock_*.json 是同花顺下单端的自选缓存，取并集更稳。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _eat(path: Path) -> None:
        try:
            j = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            log.debug("读 %s 失败: %s", path.name, e)
            return
        d = j.get("Data") or j.get("data") or {}
        raw = d.get("Selfstock") or d.get("selfstock") or ""
        head = raw.split(",")[0]
        for c in head.split("|"):
            c = c.strip()
            if is_a_stock(c) and c not in seen:
                seen.add(c)
                out.append(c)

    if THS_SELFSTOCK.exists():
        _eat(THS_SELFSTOCK)
    else:
        log.warning("自选股缓存不存在（同花顺没开过？）: %s", THS_SELFSTOCK)

    if THS_XIADAN_DIR.is_dir():
        for f in sorted(THS_XIADAN_DIR.glob("selfstock_*.json")):
            _eat(f)

    return out


def read_positions() -> dict:
    """持仓：从 v1 /api/positions 读（成本含费，OCR 已算好）。

    返回 {code: {"code","name","qty","avail","cost","price","pnl","day_pnl"}}
    v1 没跑或没持仓时返回空 dict，不抛异常。
    """
    try:
        req = urllib.request.Request(V1_POSITIONS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.warning("v1 /api/positions 不可用（v1 没跑？）: %s", e)
        return {}

    out = {}
    for p in j.get("positions") or []:
        c = (p.get("code") or "").strip()
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
            "pnl": float(p.get("pnl") or 0),
            "day_pnl": float(p.get("day_pnl") or 0),
            "pnl_pct": float(p.get("pnl_pct") or 0),
        }
    return out


# ---------------- TSP 侧：开放接口 ----------------

class TspClient:
    """TSP 开放接口薄客户端。

    端点契约由 TSP 自带：GET /api/openapi.json?tier=a
    首次运行先 --discover 把端点清单打出来，再按下面的匹配规则确认落点。
    """

    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token
        self._spec = None

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
                except Exception:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            return e.code, body_txt
        except Exception as e:
            return 0, str(e)

    def openapi(self) -> dict:
        if self._spec is not None:
            return self._spec
        code, j = self.request("GET", "/api/openapi.json?tier=a")
        if code == 200 and isinstance(j, dict) and j.get("paths"):
            self._spec = j
        else:
            self._spec = {}
            log.warning("取 openapi 失败 code=%s resp=%s", code, str(j)[:200])
        return self._spec

    def find_paths(self, *keywords: str) -> list[str]:
        """按关键词在 path + summary + tags 里找端点。"""
        spec = self.openapi()
        hits = []
        for path, ops in (spec.get("paths") or {}).items():
            blob = path.lower()
            for _m, op in (ops or {}).items():
                if isinstance(op, dict):
                    blob += " " + str(op.get("summary", "")) + " " + str(op.get("tags", ""))
            if all(k.lower() in blob for k in keywords):
                hits.append(path)
        return hits

    def dump_endpoints(self) -> None:
        spec = self.openapi()
        paths = spec.get("paths") or {}
        print("=" * 78)
        print("TSP 端点清单（共 %d 条）— 把这份贴给 MOSS，用来锁定同步落点" % len(paths))
        print("=" * 78)
        for path in sorted(paths):
            ops = paths[path] or {}
            methods = ",".join(m.upper() for m in ops if m in ("get", "post", "put", "patch", "delete"))
            summary = ""
            for op in ops.values():
                if isinstance(op, dict) and op.get("summary"):
                    summary = op["summary"]
                    break
            print("%-8s %-52s %s" % (methods, path, summary))
        print("=" * 78)


# ---------------- 同步逻辑 ----------------

def sync_selfstock(cli: TspClient, codes: list[str], st: dict, dry: bool) -> bool:
    """自选股同步。返回是否有变化。"""
    prev = st.get("selfstock") or []
    if prev == codes:
        log.info("自选股无变化（%d 只），跳过", len(codes))
        return False

    added = [c for c in codes if c not in prev]
    removed = [c for c in prev if c not in codes]
    log.info("自选股变化：+%d -%d（共 %d 只）", len(added), len(removed), len(codes))
    if dry:
        log.info("[dry-run] 将推送自选股 %d 只: %s", len(codes), ",".join(codes[:10]))
        return True

    # 端点猜测顺序：订阅/自选/watchlist/favorite
    tried = []
    for kws in (("watchlist",), ("selfstock",), ("favorite",), ("订阅",), ("自选",)):
        for path in cli.find_paths(*kws):
            body = {"codes": codes, "symbols": codes, "items": codes}
            code, resp = cli.request("POST", path, body)
            tried.append((path, code))
            if code in (200, 201, 204):
                log.info("自选股已推送 → %s (%s)", path, code)
                st["selfstock"] = codes
                return True
            if code == 404:
                continue
            log.warning("推 %s 返回 %s: %s", path, code, str(resp)[:200])
            break
    log.warning("自选股未推送：没找到可用端点。已尝试 %s", tried or "（openapi 为空）")
    return False


def sync_positions(cli: TspClient, pos: dict, st: dict, dry: bool) -> bool:
    """持仓同步。持仓是"写数据"，TSP 走 write:ext 扩展表最稳。"""
    prev = st.get("positions") or {}
    if prev == pos:
        log.info("持仓无变化（%d 只），跳过", len(pos))
        return False

    log.info("持仓变化：当前 %d 只 %s", len(pos), ",".join(sorted(pos)))
    if dry:
        for c, p in sorted(pos.items()):
            log.info("[dry-run] %s %s qty=%.0f cost=%.3f price=%.2f pnl=%.0f",
                     c, p.get("name"), p["qty"], p["cost"], p["price"], p["pnl"])
        return True

    rows = [
        {
            "code": p["code"], "name": p["name"], "qty": p["qty"], "avail": p["avail"],
            "frozen": p["frozen"], "cost": p["cost"], "price": p["price"],
            "pnl": p["pnl"], "day_pnl": p["day_pnl"], "pnl_pct": p["pnl_pct"],
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        for p in pos.values()
    ]

    # 首选：扩展表写入（write:ext）
    for path in cli.find_paths("ext") + cli.find_paths("扩展"):
        if "get" in (cli.openapi().get("paths", {}).get(path) or {}):
            continue
        for body in (
            {"table": "hold_positions", "rows": rows},
            {"table_name": "hold_positions", "rows": rows},
            {"name": "hold_positions", "data": rows},
            {"rows": rows},
        ):
            code, resp = cli.request("POST", path, body)
            if code in (200, 201, 204):
                log.info("持仓已写入扩展表 → %s (%s)", path, code)
                st["positions"] = pos
                return True
        log.debug("扩展表写入失败 path=%s", path)

    for kws in (("position",), ("holding",), ("持仓",)):
        for path in cli.find_paths(*kws):
            code, resp = cli.request("POST", path, {"positions": rows})
            if code in (200, 201, 204):
                log.info("持仓已推送 → %s (%s)", path, code)
                st["positions"] = pos
                return True
            log.warning("推 %s 返回 %s: %s", path, code, str(resp)[:200])

    log.warning("持仓未推送：没找到可用端点（先跑 --discover 把端点清单发出来）")
    return False


# ---------------- 主流程 ----------------

def run_once(cli: TspClient, cfg: dict, dry: bool) -> dict:
    st = load_state()
    changed = {"selfstock": False, "positions": False}

    if cfg.get("sync_selfstock", True):
        codes = read_selfstock()
        if codes:
            changed["selfstock"] = sync_selfstock(cli, codes, st, dry)
        else:
            log.warning("自选股读到 0 只 —— 同花顺没启动过或缓存为空")

    if cfg.get("sync_positions", True):
        pos = read_positions()
        if pos:
            changed["positions"] = sync_positions(cli, pos, st, dry)
        else:
            log.warning("持仓读到 0 只 —— v1 服务没跑或确实空仓")

    if not dry and any(changed.values()):
        st["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="同花顺远航版 → TSP 同步桥（只读文件 + HTTP，不抢焦点）")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（一键同步）")
    ap.add_argument("--loop", action="store_true", help="常驻轮询（默认 60 秒）")
    ap.add_argument("--interval", type=int, default=0, help="轮询间隔秒，覆盖配置")
    ap.add_argument("--dry-run", action="store_true", help="只读不推，看读到了什么")
    ap.add_argument("--discover", action="store_true", help="打印 TSP 端点清单后退出")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    setup_log(args.verbose)
    cfg = load_config()
    cli = TspClient(cfg["tsp_base"], cfg.get("token") or "")

    if args.discover:
        cli.dump_endpoints()
        return 0

    # 先探活
    code, _ = cli.request("GET", "/api/health", timeout=5)
    if code == 0:
        log.error("连不上 TSP：%s（Docker/桌面端起了吗？）", cfg["tsp_base"])
        return 2
    log.info("TSP 在线 %s (health=%s)", cfg["tsp_base"], code)
    if not (cfg.get("token") or ""):
        log.warning("token 为空 —— 去 TSP「设置 → 开放接口」建一个，填进 %s", CONFIG_PATH)
        if not args.dry_run:
            return 3

    if args.loop:
        interval = args.interval or int(cfg.get("interval") or 60)
        log.info("进入轮询模式，间隔 %d 秒（Ctrl-C 退出）", interval)
        while True:
            try:
                run_once(cli, cfg, args.dry_run)
            except Exception as e:
                log.exception("本轮失败: %s", e)
            time.sleep(interval)

    run_once(cli, cfg, args.dry_run)
    log.info("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
