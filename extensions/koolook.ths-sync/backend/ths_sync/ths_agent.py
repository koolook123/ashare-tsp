# -*- coding: utf-8 -*-
"""同花顺持仓代理 —— 在同花顺远航版「脚本」界面里运行一次，常驻。

为什么需要它
============
同花顺**从不把持仓落盘**。翻过 ``bin/users/<用户>/`` 下所有文件：

* ``PositionBlock.xml`` 是面板布局（列/行/板块名索引），不是持仓
* ``BSData/<市场号>_<代码>`` 是历史成交流水，而且残缺
  （同一个代码可能只留下部分成交，按净额推导持仓必然算错）
* ``blockstockV3.xml`` 是手工板块，与账户持仓无关

精确的持仓只有一样东西拿得到：同花顺内置 Python 里的全局 ``g_position``。
它由交易查询回报填充，含 ``cbj``（成本价，已含费用）——正是资金账户口径。

所以这个脚本在同花顺进程内部把它读出来，用本地 TCP 暴露给外部。
它只读，不下单，不模拟界面，不抢焦点。

怎么运行
========
1. 打开同花顺远航版 → 顶部「工具」→「脚本」/「公式管理」里的「脚本」
   （路径：交易 → 脚本，或直接打开 ``transaction/script/我的策略/`` 目录）
2. 把本文件复制到 ``C:\\同花顺远航版\\transaction\\script\\我的策略\\ths_agent.py``
3. 在脚本界面里运行它。控制台会打印 ``[ths_agent] listening on 127.0.0.1:17660``
4. 之后它随同花顺生命周期常驻，客户端退出即停

注意：内置解释器是 **Python 3.5 + IronPython 内核**。
禁止 f-string、禁止 ``typing``、禁止 ``:=``、禁止 keyword-only 参数。
本文件已按 3.5 语法写死，改动时请守住这条线。
"""
from __future__ import print_function

import json
import socket
import sys
import threading
import time
import traceback

# 同花顺把 ths_api 注入到脚本的搜索路径里
from ths_api import *  # noqa: F401,F403  —— g_position / g_money 由它注入

HOST = "127.0.0.1"
PORT = 17660
MAX_BODY = 4 * 1024 * 1024

_LOG_LOCK = threading.Lock()


def log(msg):
    # type: (str) -> None
    with _LOG_LOCK:
        try:
            print("[ths_agent] " + msg)
        except Exception:
            pass


def _num(value):
    # type: (object) -> float
    """把同花顺的数字（可能是字符串、可能带千分位）转成 float。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).replace(",", "").replace(" ", "").strip()
        if not text or text == "--":
            return 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _text(value):
    # type: (object) -> str
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def snapshot_positions():
    # type: () -> list
    """把全局 g_position 转成可 JSON 序列化的列表。

    g_position 的键是代码，值是字典，字段含义见 ``帮助文档/xiadan.py``::

        gpye  股票余额（持仓数量）
        kyye  可用余额（可卖数量）
        cbj   成本价（含费用）
        sj    市价（最新价）
        sz    市值
        yk    盈亏
        zqmc  证券名称
        zqdm  证券代码
    """
    rows = []
    try:
        table = g_position  # noqa: F821  —— 由 ths_api 注入的全局
    except NameError:
        return rows

    if not table:
        return rows

    try:
        items = list(table.items())
    except Exception:
        return rows

    for key, value in items:
        try:
            if not isinstance(value, dict):
                continue
            code = _text(value.get("zqdm") or key).strip()
            if not code:
                continue
            # 只要 6 位数字的 A 股代码，滤掉基金/债券等占位行
            if len(code) != 6 or not code.isdigit():
                continue
            qty = _num(value.get("gpye"))
            if qty <= 0:
                continue
            rows.append(
                {
                    "code": code,
                    "name": _text(value.get("zqmc")),
                    "qty": qty,
                    "avail": _num(value.get("kyye")),
                    "cost": _num(value.get("cbj")),
                    "price": _num(value.get("sj")),
                    "market_value": _num(value.get("sz")),
                    "pnl": _num(value.get("yk")),
                }
            )
        except Exception:
            log("snapshot_positions 单条失败: " + traceback.format_exc())
            continue

    rows.sort(key=lambda r: r["code"])
    return rows


def snapshot_money():
    # type: () -> dict
    """资金账户。字段见 ``帮助文档/xiadan.py``。"""
    out = {}
    try:
        table = g_money  # noqa: F821
    except NameError:
        return out
    if not isinstance(table, dict):
        return out
    mapping = {
        "zjye": "balance",
        "kyje": "available",
        "djje": "frozen",
        "sz": "market_value",
        "zzc": "total_asset",
        "yk": "pnl",
        "hbdw": "currency",
    }
    for src, dst in mapping.items():
        if src in table:
            try:
                out[dst] = _num(table.get(src))
            except Exception:
                pass
    if "hbdw" in table:
        out["currency"] = _text(table.get("hbdw"))
    return out


def handle(payload):
    # type: (dict) -> dict
    """处理一次请求。"""
    kind = _text(payload.get("type") or "ping").strip().lower()

    if kind == "ping":
        return {
            "ok": True,
            "pong": True,
            "python": _text(sys.version.split()[0]),
            "impl": _text(getattr(sys, "implementation", None) and sys.implementation.name or "cpython"),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if kind == "get_position":
        rows = snapshot_positions()
        return {
            "ok": True,
            "positions": rows,
            "count": len(rows),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if kind == "get_money":
        return {"ok": True, "money": snapshot_money(), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    if kind == "snapshot":
        return {
            "ok": True,
            "positions": snapshot_positions(),
            "money": snapshot_money(),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if kind == "get_block":
        name = _text(payload.get("name") or "")
        try:
            hq = __import__("ths_api").hq  # noqa: F841
        except Exception:
            pass
        try:
            codes = hq.get_block_stocklist(name)  # noqa: F821
            return {"ok": True, "name": name, "codes": [_text(c) for c in (codes or [])]}
        except Exception:
            return {"ok": False, "error": traceback.format_exc()}

    return {"ok": False, "error": "unknown type: " + kind}


def serve_conn(conn, addr):
    # type: (object, object) -> None
    try:
        conn.settimeout(10.0)
        chunks = []
        total = 0
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BODY:
                break
            # 收到一个完整 JSON 就停：短连接一问一答
            try:
                json.loads(b"".join(chunks).decode("utf-8", "ignore"))
                break
            except Exception:
                continue

        raw = b"".join(chunks)
        if not raw:
            return
        try:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        try:
            reply = handle(payload)
        except Exception:
            reply = {"ok": False, "error": traceback.format_exc()}

        body = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        conn.sendall(body)
    except Exception:
        log("serve_conn 异常: " + traceback.format_exc())
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    # type: () -> None
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((HOST, PORT))
        server.listen(16)
    except Exception:
        log("端口 " + str(PORT) + " 绑定失败（可能已有实例在跑）: " + traceback.format_exc())
        return

    log("listening on " + HOST + ":" + str(PORT))
    log("python " + _text(sys.version.split()[0]))
    try:
        log("positions at startup: " + str(len(snapshot_positions())))
    except Exception:
        pass

    while True:
        try:
            conn, addr = server.accept()
        except Exception:
            log("accept 异常: " + traceback.format_exc())
            time.sleep(0.5)
            continue
        t = threading.Thread(target=serve_conn, args=(conn, addr))
        t.daemon = True
        t.start()


if __name__ == "__main__":
    main()
