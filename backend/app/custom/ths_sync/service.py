# -*- coding: utf-8 -*-
"""TSP ↔ 同花顺远航版 双向同步引擎。

设计原则
========
1. **不用桥**。同步逻辑跑在 TSP 后端进程内，直接调 ``app.services.watchlist``
   的函数写自选池，不经过 HTTP、不起额外进程、不注册端口。
2. **不抢前台**。所有与同花顺的交互只有两种：读磁盘文件、读写磁盘文件。
   不模拟点击、不切窗口、不抢焦点。持仓靠同花顺进程内代理取，也不需要窗口。
3. **数据精准**。「同步」的语义是：同步后目标端**正好等于**源端，
   而不是「只增不删」。写板块带回归校验，写 TSP 只删自己标记过的条目。
4. **不误伤**。读失败一律视为「本轮跳过」，绝不把空集当成「用户清空了」，
   否则一次读盘抖动就会把自选池清空。

两个方向
========
* ``同花顺 → TSP``：自选股 ``SelfStockCache.json`` + 持仓 ``g_position``
  分别汇入 TSP 自选池的两个分组（``同花顺自选`` / ``同花顺持仓``）。
* ``TSP → 同花顺``：TSP 自选池里**用户自己挑的**标的分两路写回同花顺：
  ``ai选股`` 板块（写 ``blockstockV3.xml``）与 ``SelfStockCache.json`` 自选缓存。

写同花顺为什么必须「等客户端退出」
----------------------------------
实测四组实验的结论（2026-11-11，见 ``writer.py`` 模块文档）：

* 同花顺**启动时**读入 ``blockstockV3.xml`` 与 ``SelfStockCache.json`` 作为内存初值。
* 同花顺**运行期间**用内存态周期/事件驱动整份覆写这两个文件，从不读回。
  实测写进 ``ai选股`` 的 3 只标的 104 秒后被刷回。
* 新增一个客户端内存里没有的块，启动时会被直接丢弃（带 ``IsRemove`` 删除标记）。
* ``ai选股`` 是服务端托管块，云端同步随时可能整块覆盖本地镜像。

因此本模块对「TSP → 同花顺」的处理是：

1. 客户端**正在运行** → 不写文件，把待写内容存进 ``pending_block`` 队列，
   本轮 skipped 里说明「已排队，等同花顺关闭后落盘」。
2. 客户端**已关闭** → 立即落盘（此时写入会被下次启动读入，真正生效）。
3. 每轮 ``run_once`` 开头先检查队列：若客户端已关闭且队列非空，先把它刷出去。

这样既不会写进一个「马上被抹掉」的状态骗用户，也不需要用户手动卡时机。

为什么 TSP→同花顺 只写「用户自己挑的」
--------------------------------------
自选池里有两个分组是同步进来的（同花顺自选、同花顺持仓）。如果把它们也算进
「TSP 的股票」，写回 ai选股 就等于把同花顺自己的数据又推回同花顺，形成回路，
而且会把同花顺原生 ai选股 的内容整份覆盖掉。
所以写入集合 = 自选池全部标的 − 同花顺同步进来的标的。
该集合为空时**跳过写入**，原样保留 ai选股 现有成分——宁可不动，不猜。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from app.services import watchlist

from . import reader
from . import writer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量

#: 写进 TSP 自选池条目的备注，用来识别「这是同步来的」。
#: 只有带这个标记的条目才会被同步逻辑删除，用户自己加的一律不碰。
NOTE = "同花顺同步"

GROUP_SELF = "同花顺自选"
GROUP_POS = "同花顺持仓"
COLOR_SELF = "sky"
COLOR_POS = "rose"

#: 默认同步间隔（秒）。太密没意义——同花顺自选缓存本身不是实时刷的。
DEFAULT_INTERVAL = 60
MIN_INTERVAL = 15
MAX_INTERVAL = 3600

#: 默认写入的板块名。
DEFAULT_BLOCK = "ai选股"

#: 状态文件（落在 TSP 的 data_dir）。
STATE_FILENAME = "ths_sync_state.json"

_RUNTIME: dict[str, Any] = {
    "started_at": "",
    "sync_count": 0,
    "last_sync": "",
    "last_result": None,
    "thread_alive": False,
    "last_error": "",
}

_STATE: dict[str, Any] = {
    "auto": True,
    "interval": DEFAULT_INTERVAL,
    "sync_ths_to_tsp": True,
    "sync_tsp_to_ths": True,
    "block_name": DEFAULT_BLOCK,
    "sync_selfstock": True,
    "sync_positions": True,
    # TSP → 同花顺自选缓存（持久，需重启客户端读入）。
    # 默认关闭：自选缓存同时是本同步的**入向数据源**，写回会造成回环，
    # 而且会把用户自己维护的同花顺自选整份替换掉。需要时手动开。
    "write_ths_selfstock": False,
    # 我们写进同花顺自选缓存的代码，用于「只删自己加过的」与切断入向回环。
    "ths_written_selfstock": [],
    # 待写入同花顺的板块内容。同花顺运行期间写文件会被内存态覆盖，
    # 所以不在运行时写，改存这里，等客户端关闭后的第一个同步周期落盘。
    "pending_block": [],
    "pending_block_name": "",
    "pending_block_ts": "",
}

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_DATA_DIR: Path | None = None


# ---------------------------------------------------------------- 状态持久化


def _state_path() -> Path | None:
    if _DATA_DIR is None:
        return None
    return _DATA_DIR / STATE_FILENAME


def load_state(data_dir: Path) -> None:
    global _DATA_DIR
    with _LOCK:
        _DATA_DIR = Path(data_dir)
        path = _state_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 ths_sync_state.json 失败：%s", exc)
            return
        if not isinstance(payload, dict):
            return
        for key, value in payload.items():
            if key in _STATE:
                _STATE[key] = value
        # 修正越界值
        try:
            interval = int(_STATE.get("interval") or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        _STATE["interval"] = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))


def save_state() -> None:
    path = _state_path()
    if path is None:
        return
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(_STATE, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("保存 ths_sync_state.json 失败：%s", exc)


def get_state() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def update_state(patch: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        for key, value in patch.items():
            if key in _STATE and value is not None:
                _STATE[key] = value
        try:
            interval = int(_STATE.get("interval") or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        _STATE["interval"] = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))
        save_state()
        return dict(_STATE)


# ---------------------------------------------------------------- 分组工具


def _ensure_group(name: str, color: str) -> tuple[str | None, str]:
    """确保分组存在，返回 (group_id, 说明)。"""
    groups = watchlist.list_groups()
    folded = name.casefold()
    for group in groups:
        if str(group.get("name") or "").casefold() == folded:
            return str(group.get("id") or ""), ""
    try:
        _, created = watchlist.create_group(name, color)
        return str(created.get("id") or ""), "created"
    except ValueError as exc:
        # 并发下可能已被别人建好，重列一次
        if "已存在" in str(exc):
            for group in watchlist.list_groups():
                if str(group.get("name") or "").casefold() == folded:
                    return str(group.get("id") or ""), ""
        logger.warning("建分组 %s 失败：%s", name, exc)
        return None, str(exc)


def _group_members(rows: list[dict], group_id: str) -> set[str]:
    """从自选条目反查某分组的成员。

    ``list_groups()`` 只返回 ``{id, name, color}``，成员关系存在每条目
    ``group_ids`` 列表里（M:N），所以必须扫条目。
    """
    out: set[str] = set()
    for row in rows:
        gids = row.get("group_ids") or []
        if group_id in {str(g) for g in gids}:
            out.add(str(row.get("symbol") or ""))
    return out


def _sync_group_members(
    group_id: str | None, desired: set[str], actions: list[str]
) -> None:
    """让某分组的成员正好等于 desired。"""
    if not group_id:
        return
    groups = {str(g.get("id") or "") for g in watchlist.list_groups()}
    if group_id not in groups:
        return
    members = _group_members(watchlist.list_symbols(), group_id)
    for code in sorted(desired - members):
        try:
            watchlist.add_to_group(code, group_id)
            actions.append("group+%s:%s" % (code, group_id[:8]))
        except (KeyError, ValueError) as exc:
            logger.warning("加分组失败 %s: %s", code, exc)
    for code in sorted(members - desired):
        try:
            watchlist.remove_from_group(code, group_id)
            actions.append("group-%s:%s" % (code, group_id[:8]))
        except (KeyError, ValueError) as exc:
            logger.warning("移分组失败 %s: %s", code, exc)


# ---------------------------------------------------------------- 主流程


def run_once(*, dry: bool = False) -> dict[str, Any]:
    """执行一次双向同步。异常不外抛，全部收进 ``errors``。"""
    started = time.time()
    result: dict[str, Any] = {
        "ok": False,
        "dry": dry,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "selfstock": 0,
        "positions": 0,
        "added": [],
        "removed": [],
        "group_actions": [],
        "block": None,
        "selfstock_write": None,
        "skipped": [],
        "errors": [],
        "elapsed_ms": 0,
        "tsp_watchlist": 0,
    }

    with _LOCK:
        state = dict(_STATE)

    user = reader.find_user()
    if user is None:
        result["errors"].append("找不到同花顺用户目录（%s/bin/users/）" % reader.ths_root())

    # ---- 读同花顺自选
    self_codes: list[str] | None = None
    if state.get("sync_selfstock", True):
        self_codes = reader.read_selfstock(user)
        if self_codes is None:
            result["skipped"].append("同花顺自选读取失败，本轮保留 TSP 现有内容")
        else:
            result["selfstock"] = len(self_codes)
    else:
        result["skipped"].append("已关闭「同花顺自选 → TSP」")

    # ---- 读同花顺持仓
    positions: list[reader.Position] | None = None
    if state.get("sync_positions", True):
        positions = reader.read_positions()
        if positions is None:
            result["skipped"].append(
                "持仓通道不可用（同花顺未运行或代理脚本未启动），本轮保留 TSP 现有内容"
            )
        else:
            result["positions"] = len(positions)
    else:
        result["skipped"].append("已关闭「同花顺持仓 → TSP」")

    # ---- TSP 现状
    try:
        current_rows = watchlist.list_symbols()
    except Exception as exc:  # 数据文件损坏等
        result["errors"].append("读取 TSP 自选池失败：%s" % exc)
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        _record(result)
        return result

    current = {str(r.get("symbol") or ""): r for r in current_rows}
    result["tsp_watchlist"] = len(current)
    owned = {
        sym
        for sym, row in current.items()
        if NOTE in str(row.get("note") or "")
    }

    # ---- 计算目标集合
    want_apply = state.get("sync_ths_to_tsp", True)
    # 我们自己写进同花顺自选的代码不算「同花顺原生自选」，
    # 否则它们会被当成同步来源再导回 TSP，分组归属来回抖。
    written_th = {str(c) for c in (state.get("ths_written_selfstock") or [])}
    self_codes_native = (
        [c for c in self_codes if c not in written_th] if self_codes is not None else None
    )
    desired_self: set[str] = set(self_codes_native or []) if want_apply else set()
    desired_pos: set[str] = {p.code for p in (positions or [])} if want_apply else set()
    desired_all = desired_self | desired_pos

    # 本轮没读到的那一路，其原有条目保留，不能因为 None 被当成空集删掉
    keep: set[str] = set()
    groups_now = {str(g.get("id") or ""): g for g in watchlist.list_groups()}
    if self_codes_native is None:
        group_self_id = None
        for gid, g in groups_now.items():
            if str(g.get("name") or "").casefold() == GROUP_SELF.casefold():
                group_self_id = gid
                break
        if group_self_id:
            keep |= _group_members(current_rows, group_self_id)
    if positions is None:
        group_pos_id = None
        for gid, g in groups_now.items():
            if str(g.get("name") or "").casefold() == GROUP_POS.casefold():
                group_pos_id = gid
                break
        if group_pos_id:
            keep |= _group_members(current_rows, group_pos_id)

    desired_effective = desired_all | keep

    # ---- 写 TSP：新增
    to_add_self = sorted(desired_self - set(current))
    to_add_pos = sorted(desired_pos - set(current) - set(to_add_self))
    to_add = to_add_self + to_add_pos

    # ---- 写 TSP：删除（只删自己标记过的）
    to_remove = sorted(owned - desired_effective)

    if not dry:
        group_self_id, act_self = _ensure_group(GROUP_SELF, COLOR_SELF)
        if act_self:
            result["group_actions"].append("ensure %s" % GROUP_SELF)
        group_pos_id, act_pos = _ensure_group(GROUP_POS, COLOR_POS)
        if act_pos:
            result["group_actions"].append("ensure %s" % GROUP_POS)

        # 先删
        for code in to_remove:
            try:
                watchlist.remove(code)
                result["removed"].append(code)
            except (KeyError, ValueError) as exc:
                result["errors"].append("移除 %s 失败：%s" % (code, exc))

        # 再按分组批量加：同花顺自选
        if to_add_self:
            try:
                watchlist.add_batch(
                    to_add_self, note=NOTE, group_ids=[group_self_id] if group_self_id else None
                )
                result["added"].extend(to_add_self)
            except (KeyError, ValueError) as exc:
                result["errors"].append("写入自选组失败：%s" % exc)

        # 同花顺持仓（可能已在自选组里，追加组归属即可）
        if to_add_pos:
            try:
                watchlist.add_batch(
                    to_add_pos, note=NOTE, group_ids=[group_pos_id] if group_pos_id else None
                )
                result["added"].extend(to_add_pos)
            except (KeyError, ValueError) as exc:
                result["errors"].append("写入持仓组失败：%s" % exc)
        overlap = sorted(desired_self & desired_pos)
        for code in overlap:
            if code in to_add_pos:
                continue
            if group_pos_id:
                try:
                    watchlist.add_to_group(code, group_pos_id)
                    result["group_actions"].append("group+%s:%s" % (code, group_pos_id[:8]))
                except (KeyError, ValueError):
                    pass

        # 校正两个分组的成员集合（把不该在的移出去）
        _sync_group_members(group_self_id, desired_self | keep, result["group_actions"])
        _sync_group_members(group_pos_id, desired_pos | keep, result["group_actions"])
    else:
        result["added"] = to_add
        result["removed"] = to_remove

    # ---- 写同花顺板块：TSP 自选池中用户自己挑的标的
    #
    # 关键约束（实测）：同花顺运行期间写 blockstockV3.xml 会在数分钟内被内存态
    # 整份覆盖，客户端从不读回。所以运行中不写——只把目标内容存进 pending 队列，
    # 等客户端关闭后的第一个同步周期落盘。这样用户看到的「待写入」是真实的，
    # 不会出现「写了又消失」的假象。
    block_info: dict[str, Any] | None = None
    ths_running = writer.ths_running()
    if state.get("sync_tsp_to_ths", True) and user is not None:
        block_name = str(state.get("block_name") or DEFAULT_BLOCK)
        try:
            rows_now = watchlist.list_symbols()
            native = [
                str(r.get("symbol") or "")
                for r in rows_now
                if NOTE not in str(r.get("note") or "")
            ]
            native = [c for c in native if reader.is_a_stock(c)]
            seen: set[str] = set()
            native_unique = [c for c in native if not (c in seen or seen.add(c))]
        except Exception as exc:
            native_unique = []
            result["errors"].append("计算待写入板块清单失败：%s" % exc)

        before_codes = reader.read_block_codes(block_name, user) or []

        if not native_unique:
            result["skipped"].append(
                "TSP 自选池里没有用户自己挑的标的，保留同花顺「%s」板块原样" % block_name
            )
        elif dry:
            block_info = {
                "name": block_name,
                "before": len(before_codes),
                "after": len(native_unique),
                "changed": before_codes != native_unique,
                "dry": True,
                "queued": ths_running,
            }
        elif before_codes == native_unique and not state.get("pending_block"):
            # 已经是目标状态，不必排队也不必写盘
            block_info = {
                "name": block_name,
                "before": len(before_codes),
                "after": len(before_codes),
                "changed": False,
                "ok": True,
                "queued": False,
            }
        elif ths_running:
            # 运行中：排队，不写文件
            with _LOCK:
                _STATE["pending_block"] = list(native_unique)
                _STATE["pending_block_name"] = block_name
                _STATE["pending_block_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_state()
            block_info = {
                "name": block_name,
                "before": len(before_codes),
                "after": len(native_unique),
                "changed": before_codes != native_unique,
                "ok": True,
                "queued": True,
                "need_restart": True,
            }
            result["skipped"].append(
                "同花顺正在运行：板块写入已排队（%d 只），关闭同花顺后自动落盘，"
                "下次启动同花顺才能看到" % len(native_unique)
            )
        else:
            # 客户端已关闭：直接落盘，下次启动会被读入
            write_result = writer.write_block(user.block_file, block_name, native_unique)
            block_info = {
                "name": block_name,
                "before": len(write_result.get("before") or []),
                "after": len(write_result.get("after") or []),
                "changed": bool(write_result.get("changed")),
                "ok": bool(write_result.get("ok")),
                "queued": False,
                "need_restart": True,
            }
            if not write_result.get("ok"):
                result["errors"].append(
                    "写入板块失败：%s" % (write_result.get("error") or "未知原因")
                )
            elif write_result.get("changed") or state.get("pending_block"):
                # 落盘成功，队列清空（无论本次是否有实际改动，目标状态已达成）
                with _LOCK:
                    _STATE["pending_block"] = []
                    _STATE["pending_block_name"] = ""
                    _STATE["pending_block_ts"] = ""
                save_state()
    elif user is None:
        result["skipped"].append("找不到同花顺用户目录，跳过写板块")
    else:
        result["skipped"].append("已关闭「TSP → 同花顺板块」")

    result["block"] = block_info

    # ---- 写同花顺自选缓存：持久通道（需重启客户端读入）
    selfstock_write: dict[str, Any] | None = None
    if state.get("write_ths_selfstock") and user is not None:
        try:
            rows_now = watchlist.list_symbols()
            native_codes = [
                str(r.get("symbol") or "")
                for r in rows_now
                if NOTE not in str(r.get("note") or "")
            ]
            native_codes = [c for c in native_codes if reader.is_a_stock(c)]
            seen2: set[str] = set()
            native_codes = [c for c in native_codes if not (c in seen2 or seen2.add(c))]
        except Exception as exc:
            native_codes = []
            result["errors"].append("计算待写自选缓存清单失败：%s" % exc)

        if dry:
            before_codes = reader.read_selfstock(user) or []
            selfstock_write = {
                "dry": True,
                "before": len(before_codes),
                "after": len(native_codes),
                "changed": sorted(before_codes) != sorted(native_codes),
                "need_restart": True,
            }
        else:
            wr = writer.write_selfstock(user.self_cache, native_codes)
            selfstock_write = {
                "ok": bool(wr.get("ok")),
                "changed": bool(wr.get("changed")),
                "before": len(wr.get("before") or []),
                "after": len(wr.get("after") or []),
                "need_restart": bool(wr.get("need_restart")),
                "error": wr.get("error") or "",
            }
            if not wr.get("ok"):
                result["errors"].append(
                    "写入同花顺自选缓存失败：%s" % (wr.get("error") or "未知原因")
                )
            elif wr.get("changed"):
                # 记下我们写进去的代码，供下一轮排除回环 + 精确回滚
                with _LOCK:
                    _STATE["ths_written_selfstock"] = list(native_codes)
                save_state()
                selfstock_write["written"] = len(native_codes)
    elif user is None:
        pass
    elif not state.get("write_ths_selfstock"):
        result["skipped"].append("「TSP → 同花顺自选」未开启")

    result["selfstock_write"] = selfstock_write
    result["ok"] = not result["errors"]
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    _record(result)
    return result


def _record(result: dict[str, Any]) -> None:
    with _LOCK:
        _RUNTIME["sync_count"] = int(_RUNTIME.get("sync_count") or 0) + 1
        _RUNTIME["last_sync"] = result.get("ts") or ""
        _RUNTIME["last_result"] = result
        _RUNTIME["last_error"] = "; ".join(result.get("errors") or [])[:500]


# ---------------------------------------------------------------- 状态汇总


def build_status() -> dict[str, Any]:
    """给前端的一站式状态。"""
    user = reader.find_user()
    state = get_state()

    agent_ok = reader.agent_alive()
    positions: list[reader.Position] = []
    if agent_ok:
        positions = reader.read_positions() or []

    self_codes = reader.read_selfstock(user) if user else None

    try:
        rows = watchlist.list_symbols()
    except Exception:
        rows = []
    owned = [r for r in rows if NOTE in str(r.get("note") or "")]
    native = [r for r in rows if NOTE not in str(r.get("note") or "")]

    groups = []
    try:
        for g in watchlist.list_groups():
            gid = str(g.get("id") or "")
            groups.append(
                {
                    "id": gid,
                    "name": str(g.get("name") or ""),
                    "color": str(g.get("color") or ""),
                    "count": len(_group_members(rows, gid)),
                }
            )
    except Exception:
        groups = []

    block_name = str(state.get("block_name") or DEFAULT_BLOCK)
    block_codes = reader.read_block_codes(block_name, user) if user else None

    pos_dicts = [p.as_dict() for p in positions]
    return {
        "ok": True,
        "engine": {
            "mode": "in-process",
            "version": 4,
            "thread_alive": bool(_RUNTIME.get("thread_alive")),
            "started_at": _RUNTIME.get("started_at") or "",
            "sync_count": int(_RUNTIME.get("sync_count") or 0),
            "last_sync": _RUNTIME.get("last_sync") or "",
            "last_error": _RUNTIME.get("last_error") or "",
        },
        "config": {
            "auto": bool(state.get("auto")),
            "interval": int(state.get("interval") or DEFAULT_INTERVAL),
            "sync_ths_to_tsp": bool(state.get("sync_ths_to_tsp")),
            "sync_tsp_to_ths": bool(state.get("sync_tsp_to_ths")),
            "sync_selfstock": bool(state.get("sync_selfstock")),
            "sync_positions": bool(state.get("sync_positions")),
            "block_name": block_name,
            "write_ths_selfstock": bool(state.get("write_ths_selfstock")),
        },
        "pending": {
            "block": len(state.get("pending_block") or []),
            "block_name": str(state.get("pending_block_name") or ""),
            "ts": str(state.get("pending_block_ts") or ""),
        },
        "ths": {
            "root": str(reader.ths_root()),
            "user": user.name if user else "",
            "user_dir": str(user.user_dir) if user else "",
            "selfstock_ok": self_codes is not None,
            "selfstock_count": len(self_codes or []),
            "agent_ok": agent_ok,
            "positions_ok": agent_ok,
            "positions_count": len(positions),
            "block_name": block_name,
            "block_count": len(block_codes or []) if block_codes is not None else None,
            "block_ok": block_codes is not None,
            "block_volatile": writer.ths_running(),
            "block_volatile_note": (
                "同花顺正在运行：板块写入会排队，关闭同花顺后自动落盘，下次启动可见"
                if writer.ths_running()
                else "同花顺已关闭：板块写入立即落盘，下次启动同花顺时被读入"
            ),
            "selfstock_writable": bool(user is not None),
            "ths_written_count": len(state.get("ths_written_selfstock") or []),
        },
        "tsp": {
            "watchlist_count": len(rows),
            "synced_count": len(owned),
            "native_count": len(native),
            "groups": groups,
        },
        "positions": pos_dicts,
        "position_pnl_total": round(sum(p.pnl for p in positions), 2),
        "last_result": _RUNTIME.get("last_result"),
    }


# ---------------------------------------------------------------- 后台线程


def _loop() -> None:
    with _LOCK:
        _RUNTIME["thread_alive"] = True
    logger.info("ths-sync 后台线程启动")
    while not _STOP.is_set():
        state = get_state()
        interval = int(state.get("interval") or DEFAULT_INTERVAL)
        if not state.get("auto"):
            _STOP.wait(min(interval, 30))
            continue
        try:
            run_once()
        except Exception:
            logger.exception("ths-sync 自动同步异常")
        _STOP.wait(interval)
    with _LOCK:
        _RUNTIME["thread_alive"] = False
    logger.info("ths-sync 后台线程退出")


def start(data_dir: Path) -> None:
    global _THREAD
    load_state(data_dir)
    with _LOCK:
        _RUNTIME["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="ths-sync", daemon=True)
        _THREAD.start()


def stop() -> None:
    _STOP.set()
