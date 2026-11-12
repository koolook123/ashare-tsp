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
* ``同花顺 → TSP``：自选股 ``SelfStockCache.json`` 自动汇入「同花顺自选」分组；
  持仓 ``g_position`` **只在手动一键同步时**汇入「同花顺持仓」分组。
* ``TSP → 同花顺``：写 ``SelfStockCache.json`` 自选缓存（TSP → 同花顺 的主通道）。
  写板块 ``ai选股`` 默认关闭，理由见下。

为什么「自动只跑自选、持仓走手动」
----------------------------------
持仓必须连同花顺进程内代理（``127.0.0.1:17660``）才能读。代理没起时每次读都要走满
socket 超时；自动线程每 60 秒一轮，会被它拖慢，而且用户没有「实时看持仓」的需求。
所以自动轮询只跑自选（纯读文件，几十毫秒），持仓由页面上的「一键同步」手动触发。

为什么 TSP → 同花顺 写自选而不是写板块
--------------------------------------
实测四组实验的结论（2026-11-11，见 ``writer.py`` 模块文档）：

* 同花顺**启动时**读入 ``blockstockV3.xml`` 与 ``SelfStockCache.json`` 作为内存初值。
* 同花顺**运行期间**用内存态周期/事件驱动整份覆写 ``blockstockV3.xml``，从不读回。
  实测写进 ``ai选股`` 的 3 只标的 104 秒后被刷回。
* ``SelfStockCache.json`` 运行期**不被覆写**：写进去就一直留着，只是要重启客户端才被读入。
* 新增一个客户端内存里没有的块，启动时会被直接丢弃（带 ``IsRemove`` 删除标记）。
* ``ai选股`` 是服务端托管块，云端同步随时可能整块覆盖本地镜像。

结论：写板块没有可用窗口（要么 2 分钟内被抹，要么被云端覆盖），写自选才是能真正
落地并长期存活的通道。因此 ``write_ths_selfstock`` 默认**开启**并作为主通道，
``sync_tsp_to_ths``（写板块）默认**关闭**，仅作为可选实验开关保留。

写自选的集合与保护
------------------
目标集合 = 本轮从同花顺读到的自选 ∪ TSP 里用户自己挑的标的。**只做加法，从不删除**。

这里踩过一次很重的坑，必须写下来：早前用「TSP 自选池里的「同花顺自选」分组成员」当基准，
并且把「我们写进同花顺的代码」从入向来源里剔除来切断回环。结果写自选时把整个目标集
记成「我写的」，下一轮它被整体排除，``desired_self`` 变成空集，
``owned - 空集`` 就把 TSP 里 40 只同步标的全部删光，写回又把同花顺自选截成 5 只——
一次同步把两边的数据都吃掉了一大半。

现在改成：基准取来源本身（``self_codes``），不取下游分组；不再维护「我写的」排除集。
写回就永远是「同花顺原有的 + 你新加的」，不可能把同花顺自选写少。
代价：在 TSP 里删一只不会让同花顺少一只，删除请直接在同花顺里删——比误删安全得多。

三条硬保护，宁可不动也不能清空用户自选：

1. 入向同步（``sync_ths_to_tsp``）关闭时直接跳过——此时没有可信来源。
2. 本轮没读到同花顺自选（``self_codes is None``）时跳过——来源不可信，算出来必然偏小。
3. 目标集合为空时跳过——空集写入等于清空。
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
from . import xd_screen
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

#: 状态文件结构版本。老版本文件里的开关默认值需要迁移，否则升级后仍然是旧行为。
#: v1：sync_positions=True（自动读持仓）、sync_tsp_to_ths=True（写板块）、
#:     write_ths_selfstock=False（写自选默认关）
#: v2：持仓改手动、板块写入关闭、写自选打开（自选成为 TSP → 同花顺 的主通道）
STATE_VERSION = 2

_RUNTIME: dict[str, Any] = {
    "started_at": "",
    "sync_count": 0,
    "last_sync": "",
    "last_result": None,
    "thread_alive": False,
    "last_error": "",
}

_STATE: dict[str, Any] = {
    "state_version": STATE_VERSION,
    "auto": True,
    "interval": DEFAULT_INTERVAL,
    "sync_ths_to_tsp": True,
    # TSP → 同花顺板块（blockstockV3.xml 的 ai选股）。
    # 默认关闭：实测客户端每 2 分钟内用内存态整份覆写该文件，本地写入存活极短，
    # 且 ai选股 是服务端托管块，云端同步随时覆盖。写它没有实际收益。
    "sync_tsp_to_ths": False,
    "block_name": DEFAULT_BLOCK,
    "sync_selfstock": True,
    # 持仓同步默认关闭：BOSS 要求改成手动一键，自动轮询不再读持仓。
    # 自动线程只跑「自选」；持仓由 POST /sync/positions 手动触发。
    "sync_positions": False,
    # TSP → 同花顺自选缓存。这是 TSP → 同花顺 的**主通道**：
    # 该文件运行期不被覆写，写入可持久存活，只需重启客户端读入。
    "write_ths_selfstock": True,
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
        old_version = 0
        try:
            old_version = int(payload.get("state_version") or 0)
        except (TypeError, ValueError):
            old_version = 0
        for key, value in payload.items():
            if key in _STATE:
                _STATE[key] = value
        # v1 → v2 迁移：v1 的开关组合是「自动读持仓 + 写板块 + 不写自选」，
        # 与现在的产品口径正好相反。老文件必须显式改写成新口径，
        # 只迁移一次（迁完写回 state_version=2，用户之后的手动改动不再被覆盖）。
        if old_version < STATE_VERSION:
            _STATE["sync_positions"] = False
            _STATE["sync_tsp_to_ths"] = False
            _STATE["write_ths_selfstock"] = True
            _STATE["pending_block"] = []
            _STATE["pending_block_name"] = ""
            _STATE["pending_block_ts"] = ""
            logger.info("ths_sync_state.json 从 v%d 迁移到 v%d", old_version, STATE_VERSION)
        _STATE["state_version"] = STATE_VERSION
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


_SCREEN_CACHE: dict[str, Any] = {"ts": 0.0, "positions": None, "note": ""}
_SCREEN_TTL = 4.0   # 秒；前端每 10s 轮询一次，缓存免得每次都开 tesseract


def _positions_from_screen() -> tuple[list[reader.Position] | None, str]:
    """屏幕 OCR 兜底读持仓。返回 (持仓, 说明)。

    为什么要这条通道
    ----------------
    精确持仓的同花顺进程内来源只有 ``g_position``，必须靠 ``ths_agent.py``
    在「Python策略编辑器」里人工跑一次才通（监听 17660）。但那一步
    BOSS 每重启一次同花顺就得重做一次，等于持仓永远在半自动状态。

    ``xiadan.exe`` 的下单端窗口本身就把完整持仓画在屏幕上（证券代码 /
    股票余额 / 可用余额 / 成本价 / 市价 / 持仓盈亏 / 盈亏比例 / 当日盈亏 /
    当日盈亏比 / 市值），所以 ``xd_screen`` 用后台 BitBlt + 逐列 OCR
    把这张表读回来——完全不抢前台、不需要脚本、不需要人工。

    代理活着时仍然优先用代理（``cbj`` 是券商回报口径，含费用）；代理不可用
    才降级走屏幕。两条通道的 ``cost`` 都是含费成本，语义一致。
    """
    now = time.time()
    if now - float(_SCREEN_CACHE.get("ts") or 0) < _SCREEN_TTL:
        return _SCREEN_CACHE.get("positions"), str(_SCREEN_CACHE.get("note") or "")
    try:
        snap = xd_screen.snapshot()
    except Exception as exc:  # snapshot 本身不抛，这里只是双保险
        return None, "屏幕读取异常：%s" % exc
    if not snap.get("ok"):
        return None, str(snap.get("error") or "屏幕读取失败")
    out: list[reader.Position] = []
    for row in snap.get("positions") or []:
        code = str(row.get("code") or "")
        if not reader.is_a_stock(code):
            continue
        qty = float(row.get("qty") or 0)
        if qty <= 0:
            continue
        out.append(
            reader.Position(
                code=code,
                name=str(row.get("name") or ""),
                qty=qty,
                avail=float(row.get("avail") or 0),
                cost=float(row.get("cost") or 0),
                price=float(row.get("price") or 0),
                market_value=float(row.get("market_value") or 0),
                pnl=float(row.get("pnl") or 0),
            )
        )
    out.sort(key=lambda p: p.code)
    if not out:
        _SCREEN_CACHE.update({"ts": now, "positions": None, "note": "屏幕没读出持仓行"})
        return None, "屏幕没读出持仓行"
    _SCREEN_CACHE.update({"ts": now, "positions": out, "note": "screen"})
    return out, "screen"


def read_positions_any() -> tuple[list[reader.Position] | None, str]:
    """读持仓：进程内代理优先，不可用时降级到下单端屏幕 OCR。"""
    try:
        positions = reader.read_positions()
    except Exception as exc:
        logger.info("代理读持仓异常：%s", exc)
        positions = None
    if positions is not None:
        return positions, "agent"
    return _positions_from_screen()


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


def run_once(
    *, dry: bool = False, with_positions: bool | None = None
) -> dict[str, Any]:
    """执行一次同步。异常不外抛，全部收进 ``errors``。

    ``with_positions``：本轮是否读同花顺持仓。
    * ``None``（默认）→ 按 ``state["sync_positions"]``；
    * ``False`` → 强制不读，自动线程走这条（持仓只能手动一键同步）；
    * ``True``  → 强制读，``POST /sync/positions`` 走这条。

    读持仓要连 127.0.0.1:17660 代理，代理未起时会走满 socket 超时；
    自动线程每 60 秒跑一次，不该被它拖慢，因此自动轮询不碰持仓。
    """
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
        "positions_channel": "",
        "skipped": [],
        "errors": [],
        "elapsed_ms": 0,
        "tsp_watchlist": 0,
    }

    with _LOCK:
        state = dict(_STATE)

    want_positions = (
        bool(state.get("sync_positions", False)) if with_positions is None else bool(with_positions)
    )

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

    # ---- 读同花顺持仓（只在手动一键同步时读）
    positions: list[reader.Position] | None = None
    if want_positions:
        positions, channel = read_positions_any()
        result["positions_channel"] = channel if positions is not None else ""
        if positions is None:
            result["skipped"].append(
                "持仓读取失败（%s），本轮保留 TSP 现有持仓条目" % channel
            )
        else:
            result["positions"] = len(positions)
    else:
        result["skipped"].append("持仓同步为手动一键模式，本轮未读取持仓")

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
    #
    # 不要在这里把「我们自己写进同花顺自选的代码」从入向来源里剔除。
    # 曾经那样做过，结果是灾难性的自我吞噬：写自选时把整个目标集记成「我写的」，
    # 下一轮它被整体从来源里排除，desired_self 变成空集，
    # 于是 owned - 空集 把所有同步来的标的从 TSP 删光，写回时又把同花顺自选清成几剩只。
    # 只增不减才是安全的：入向就是同花顺有什么就同步什么。
    want_apply = state.get("sync_ths_to_tsp", True)
    self_codes_native = self_codes
    desired_self: set[str] = set(self_codes_native or []) if want_apply else set()
    desired_pos: set[str] = {p.code for p in (positions or [])} if want_apply else set()
    desired_all = desired_self | desired_pos

    # 本轮没读到的那一路，其原有条目保留，不能因为 None 被当成空集删掉。
    # 两条通道各自独立保留：曾经用一个 keep 混着装，结果是「同花顺自选」的 40 多只
    # 被塞进「同花顺持仓」分组（_sync_group_members 是强制对齐语义），
    # 持仓池直接变成自选池的副本。
    keep_self: set[str] = set()
    keep_pos: set[str] = set()
    groups_now = {str(g.get("id") or ""): g for g in watchlist.list_groups()}
    if self_codes_native is None:
        group_self_id = None
        for gid, g in groups_now.items():
            if str(g.get("name") or "").casefold() == GROUP_SELF.casefold():
                group_self_id = gid
                break
        if group_self_id:
            keep_self |= _group_members(current_rows, group_self_id)
    if positions is None:
        group_pos_id = None
        for gid, g in groups_now.items():
            if str(g.get("name") or "").casefold() == GROUP_POS.casefold():
                group_pos_id = gid
                break
        if group_pos_id:
            keep_pos |= _group_members(current_rows, group_pos_id)

    desired_effective = desired_all | keep_self | keep_pos

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
        _sync_group_members(group_self_id, desired_self | keep_self, result["group_actions"])
        _sync_group_members(group_pos_id, desired_pos | keep_pos, result["group_actions"])
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

    # ---- 写同花顺自选缓存：TSP → 同花顺 的主通道（需重启客户端读入）
    #
    # 为什么改走自选而不是板块：实测 blockstockV3.xml 每 2 分钟内被客户端内存态
    # 整份覆写，本地写入存活极短，且 ai选股 是服务端托管块。而 SelfStockCache.json
    # 运行期不被覆写，写进去就一直在，只是要重启客户端才被读入——这才是能用的通道。
    #
    # 目标集合 = TSP 自选池里「同花顺自选」分组的成员 ∪ 你自己挑的标的。
    # 前者已经把同花顺原生自选全部包含进来（入向同步保证），所以这是**真双向**：
    # 你在 TSP 里删掉一只，下次写回同花顺也会少一只；同理你新加一只，同花顺会多一只。
    #
    # 两条硬保护，宁可不动也不能清空用户自选：
    # 1. 入向同步关闭时直接跳过——此时 TSP 里没有同花顺原生自选，算出来的目标集
    #    只是你自己挑的几只，整份替换会把同花顺原有的自选全部抹掉。
    # 2. 目标集合为空时跳过——同理，空集写入等于清空。
    selfstock_write: dict[str, Any] | None = None
    if state.get("write_ths_selfstock") and user is not None:
        if not state.get("sync_ths_to_tsp", True):
            result["skipped"].append(
                "「同花顺自选 → TSP」已关闭，为避免整份覆盖清空同花顺自选，本轮不写自选缓存"
            )
        elif self_codes is None:
            # 本轮没读到同花顺自选，说明来源不可信；此时算出的目标集可能远小于真实值，
            # 写下去就是整份截断。宁可不写。
            result["skipped"].append(
                "本轮没读到同花顺自选，来源不可信，跳过写自选缓存（避免截断）"
            )
        else:
            try:
                rows_now = watchlist.list_symbols()
                native_codes = [
                    str(r.get("symbol") or "")
                    for r in rows_now
                    if NOTE not in str(r.get("note") or "")
                ]
                native_codes = [c for c in native_codes if reader.is_a_stock(c)]
            except Exception as exc:
                native_codes = []
                result["errors"].append("计算待写自选缓存清单失败：%s" % exc)

            # 目标集合 = 同花顺现有自选 ∪ TSP 里你自己挑的（只做加法）。
            #
            # 用 self_codes（本轮从同花顺读来的）而不是 TSP 分组反查，是关键：
            # 分组是下游状态，一旦上游算错它也跟着错；用来源本身做基准，
            # 写回就永远是「同花顺原有的 + 你新加的」，不可能把同花顺自选写少。
            # 代价是「在 TSP 删一只」不会让同花顺少一只——删除请直接在同花顺里删，
            # 这比误删用户数据安全得多。
            target_codes = sorted(
                {c for c in (self_codes or []) if reader.is_a_stock(c)}
                | {c for c in native_codes if c}
            )

            if not target_codes:
                result["skipped"].append(
                    "目标自选集合为空（TSP 侧没有可写回的标的），本轮保留同花顺自选原样"
                )
            elif dry:
                before_codes = reader.read_selfstock(user) or []
                selfstock_write = {
                    "dry": True,
                    "before": len(before_codes),
                    "after": len(target_codes),
                    "changed": sorted(before_codes) != target_codes,
                    "need_restart": True,
                }
            else:
                wr = writer.write_selfstock(user.self_cache, target_codes)
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
                    # 只作信息展示（「上次写了几只」），不再用于排除入向来源。
                    # 曾用它做回环切断，结果下一轮把整份同花顺自选当成「自己写的」
                    # 全部排除，desired_self 变空，级联清空两边。见目标集合那段注释。
                    with _LOCK:
                        _STATE["ths_written_selfstock"] = list(target_codes)
                    save_state()
                    selfstock_write["written"] = len(target_codes)
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
    positions_channel = ""
    if agent_ok:
        positions = reader.read_positions() or []
        if positions:
            positions_channel = "agent"
    if not positions:
        # 代理没起时不要空着——下单端窗口上就有持仓表，屏幕 OCR 兜底
        screen_positions, channel = _positions_from_screen()
        if screen_positions:
            positions = screen_positions
            positions_channel = channel

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
            "positions_ok": bool(positions),
            "positions_channel": positions_channel,
            "positions_count": len(positions),
            "block_name": block_name,
            "block_count": len(block_codes or []) if block_codes is not None else None,
            "block_ok": block_codes is not None,
            "block_volatile": writer.ths_running(),
            "block_volatile_note": (
                "同花顺正在运行：写板块会被内存态覆盖，本版已默认关闭板块写入"
                if writer.ths_running()
                else "同花顺已关闭：此时写板块才会被下次启动读入"
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
            # 自动轮询只跑自选，不碰持仓：持仓是手动一键通道，
            # 且代理未起时会走满 socket 超时，不能拖慢 60 秒的自动节奏。
            run_once(with_positions=False)
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
