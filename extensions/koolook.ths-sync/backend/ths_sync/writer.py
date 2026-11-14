# -*- coding: utf-8 -*-
"""同花顺本地文件写入。

只做两件事，都**不抢前台、不模拟界面**：

1. ``write_block`` 写 ``blockstockV3.xml`` 的某个板块。
2. ``write_selfstock`` 写 ``SelfStockCache.json``（自选股缓存）。

关于 ``blockstockV3.xml`` 的实测结论（2026-11-11，四组独立实验）：

* 客户端**启动时读入**该文件作为内存初值。实测：关闭同花顺 → 把 ``ai选股``
  改成 3 只 → 启动 happ.exe → 文件仍是 1682B、那 3 只还在，且持续 5 分钟以上
  未被改动（因为内存初值就等于文件内容，覆写时内容一致）。
* 客户端**运行期间**用内存状态周期/事件驱动整份覆写该文件，且从不读回。
  实测：19:41:50 写进 3 只 → 19:43:34 被刷回 12 只（存活 104 秒）。
* 但这只对**已存在的块**成立。新增一个客户端内存里没有的块（如 id=9999），
  启动时会被直接丢弃——客户端按自己的块清单重建文件，还带
  ``<Block id="35" IsRemove="true" />`` 这类删除标记，说明它做块级 diff + 云端同步。
* ``ai选股`` 是**服务端托管块**：19:33 那次启动它从 19 只变 12 只，不是本地改的，
  是客户端从云端拉的新内容。本地文件只是镜像，云端同步随时可能整块覆盖。

因此「TSP → 同花顺板块」的唯一可靠写通道是：

    **客户端关闭期间写文件 → 用户下次启动同花顺时被读入**

运行期间写进去的内容会在数分钟内被抹掉，属于「写入成功但立刻被覆盖」，
不能当成已生效。调用方必须在客户端运行时**不要直接写**，而是排队等
客户端退出后的下一个同步周期再落盘（见 ``service.py`` 的 pending 队列）。
``write_block`` 的返回值固定带 ``need_restart: True``，语义是「下次启动才可见」。

关于 ``SelfStockCache.json`` 的实测结论：

* 写入后**不会被覆写**（19:03 写入，19:14 复核内容仍在；19:39 写入的探针
  存活 8 分钟以上）。
* 但同花顺运行中不读该文件，内存里的自选才是权威；改动要等客户端下次启动
  才被读入，所以同样返回 ``need_restart: True``。
* 关闭期间写入的改动**确实会被启动时读入**（实测写入的 601988 启动后仍在，
  version 还自增了一版），这条通道是真实可用的。

设计约束：

* 写入必须原子：先写临时文件再 ``os.replace``。直接覆盖到一半会留下残文件，
  同花顺下次启动会把板块读成空。
* 写前必须备份，出问题能立刻还原。
* 只改目标块，别的块一个字节都不动。用正则定位该块的开闭标签区间做局部替换，
  不做 XML 全量序列化——那会丢掉 ``<hevo>`` 上的 ``sort_list`` 等私有属性。
* 同花顺可能正持有该文件的句柄，写入会短暂 ``Permission denied``。重试几次。

market 编号（来自客户端真实数据）：

* 板块文件：沪市主板/科创 ``6`` 开头 → ``USHA``；
  深市主板/创业板 ``0``/``3`` → ``USZA``；北交所 ``8``/``4`` → ``USZJ``。
* 自选缓存：沪市 ``17``，深市 ``33``（40 只真实样本逐条对齐得出）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 写操作全局锁。同一进程内多个请求可能并发触发同步，串行化避免撕裂。
_WRITE_LOCK = None

#: 写入重试次数与初间隔（秒）。客户端抓句柄时通常几十毫秒就放开。
_RETRY_TIMES = 5
_RETRY_SLEEP = 0.12

#: 备份后缀。
BACKUP_SUFFIX = ".bak_ths_sync"


def _lock():
    global _WRITE_LOCK
    if _WRITE_LOCK is None:
        import threading

        _WRITE_LOCK = threading.RLock()
    return _WRITE_LOCK


def ths_running() -> bool:
    """同花顺客户端是否正在运行。

    写文件通道对「运行中」和「已关闭」的语义完全不同：
    运行中写进去会被内存态在数分钟内整份覆盖；关闭时写的会在下次启动被读入。
    所以每次写之前必须先判这个，把真实后果告诉调用方和界面。
    """
    try:
        import subprocess

        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-Process happ,xiadan,xiadan-plus -ErrorAction SilentlyContinue "
                "| Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return int((out.stdout or "0").strip() or 0) > 0
    except Exception:  # noqa: BLE001 - 探测失败时按「可能在运行」保守处理
        return True


def market_of(code: str) -> str:
    """代码 → 同花顺 market 标识。"""
    if not code:
        return "USZA"
    head = code[0]
    if head == "6":
        return "USHA"
    if head in ("0", "3"):
        return "USZA"
    if head in ("8", "4"):
        return "USZJ"
    # 指数等其它品种，同花顺习惯用 USHI/USZI，这里对 A 股用不到
    return "USZA"


def _block_span(text: str, name: str) -> tuple[int, int, str] | None:
    """定位板块内容区间。

    返回 ``(内容起点, 内容终点, 该块整段文本)``。
    自闭合的 ``<Block ... />`` 没有内容区，返回 ``("", 0)`` 形态的空区间。
    """
    pattern = re.compile(
        r'<Block\s+[^>]*name="%s"[^>]*?(/?)>' % re.escape(name), re.S
    )
    match = pattern.search(text)
    if not match:
        return None
    if match.group(1) == "/":
        # 自闭合 ``<Block ... />``：没有内容区。用 (-1, -1) 当哨兵，
        # 调用方据此改用「整标签替换成开闭标签对」的写法。
        return (-1, -1, "")
    depth = 1
    pos = match.end()
    open_re = re.compile(r"<Block\b[^>]*?(/?)>")
    close_re = re.compile(r"</Block>")
    while pos < len(text) and depth > 0:
        nxt_open = open_re.search(text, pos)
        nxt_close = close_re.search(text, pos)
        if nxt_close is None:
            return None
        if nxt_open is not None and nxt_open.start() < nxt_close.start():
            if nxt_open.group(1) != "/":
                depth += 1
            pos = nxt_open.end()
        else:
            depth -= 1
            if depth == 0:
                return (match.end(), nxt_close.start(), "")
            pos = nxt_close.end()
    return None


def _render_block_body(codes: list[str], indent: str = "    ") -> str:
    """把代码列表渲染成 ``<security>`` 行。"""
    if not codes:
        return ""
    lines = [
        '%s<security market="%s" code="%s" />' % (indent, market_of(code), code)
        for code in codes
    ]
    return "\n" + "\n".join(lines) + "\n  "


def write_block(
    block_file: Path,
    block_name: str,
    codes: list[str],
    *,
    create_if_missing: bool = False,
    backup: bool = True,
) -> dict:
    """把 ``codes`` 精确写进 ``block_name`` 板块。

    "精确" 指：写完后该板块的成分**正好等于** ``codes``，
    多余的一律删掉，不缺不滥。

    参数
    ----
    block_file:
        ``blockstockV3.xml`` 路径。
    block_name:
        板块名。不存在时若 ``create_if_missing`` 为真则新建。
    codes:
        目标代码列表。已去重、已排序由调用方负责，这里按传入顺序写。

    返回
    ----
    ``{"ok": bool, "changed": bool, "before": [...], "after": [...], "error": str}``
    """
    result = {"ok": False, "changed": False, "before": [], "after": [], "error": ""}

    if not block_file.exists():
        result["error"] = "板块文件不存在：%s" % block_file
        return result

    cleaned: list[str] = []
    seen: set[str] = set()
    for code in codes:
        code = str(code).strip()
        if not code or not re.match(r"^[03684]\d{5}$", code):
            continue
        if code in seen:
            continue
        seen.add(code)
        cleaned.append(code)

    with _lock():
        try:
            text = block_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            result["error"] = "读取板块文件失败：%s" % exc
            return result

        span = _block_span(text, block_name)

        if span is None:
            if not create_if_missing:
                result["error"] = "板块不存在：%s" % block_name
                return result
            body = _render_block_body(cleaned)
            new_block = '  <Block name="%s" IsLock="false" IsNameChanged="false" IsSecuritiesChanged="false">%s</Block>\n' % (
                block_name,
                body,
            )
            idx = text.rfind("</hevo>")
            if idx < 0:
                result["error"] = "板块文件结构异常：找不到 </hevo>"
                return result
            new_text = text[:idx] + new_block + text[idx:]
            before: list[str] = []
        else:
            start, end, _ = span
            # 自闭合标签：整段替换成开闭标签对
            if start == -1 and end == -1:
                pattern = re.compile(
                    r'<Block\s+[^>]*name="%s"[^>]*?/>' % re.escape(block_name), re.S
                )
                match = pattern.search(text)
                if not match:
                    result["error"] = "板块定位失败：%s" % block_name
                    return result
                head = match.group(0)[:-2].rstrip()
                new_block = '%s>%s</Block>' % (head, _render_block_body(cleaned))
                new_text = text[: match.start()] + new_block + text[match.end() :]
                before = []
            else:
                before = re.findall(r'code="(\d+)"', text[start:end])
                if before == cleaned:
                    result.update(
                        {"ok": True, "changed": False, "before": before, "after": before}
                    )
                    return result
                new_text = text[:start] + _render_block_body(cleaned) + text[end:]

        if backup:
            try:
                shutil.copy2(block_file, block_file.with_name(block_file.name + BACKUP_SUFFIX))
            except OSError as exc:
                logger.warning("板块文件备份失败（继续写入）：%s", exc)

        tmp = block_file.with_name(block_file.name + ".tmp_ths_sync")
        try:
            tmp.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            result["error"] = "写临时文件失败：%s" % exc
            return result

        last_err = ""
        for attempt in range(_RETRY_TIMES):
            try:
                os.replace(tmp, block_file)
                last_err = ""
                break
            except PermissionError as exc:
                last_err = str(exc)
                time.sleep(_RETRY_SLEEP * (attempt + 1))
            except OSError as exc:
                last_err = str(exc)
                break
        if last_err:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            result["error"] = "写入板块文件失败：%s" % last_err
            return result

        # 回读校验：写进去的和期望的一致才算成功
        try:
            verify_text = block_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            result["error"] = "回读校验失败：%s" % exc
            return result
        vspan = _block_span(verify_text, block_name)
        after = re.findall(r'code="(\d+)"', verify_text[vspan[0] : vspan[1]]) if vspan else []

        result.update(
            {
                "ok": after == cleaned,
                "changed": True,
                "before": before,
                "after": after,
                "error": "" if after == cleaned else "回读不一致",
                # 实测：客户端启动时读入本文件，运行期间用内存态整份覆写。
                # 所以写入本身会成功，但要等客户端下次启动才在界面里可见；
                # 若客户端正在运行，本次写入会在数分钟内被抹掉。
                "need_restart": True,
                "volatile": ths_running(),
                "volatile_note": (
                    "同花顺正在运行，本次写入会在数分钟内被内存状态覆盖；"
                    "关闭同花顺后写入才会在下次启动时生效"
                    if ths_running()
                    else "同花顺已关闭，本次写入会在下次启动时被读入"
                ),
            }
        )
        return result


def selfstock_market(code: str) -> str:
    """代码 → ``SelfStockCache.json`` 里的市场编号。

    实测自选缓存里沪市是 ``17``、深市是 ``33``（40 只样本逐条对齐得出）。
    """
    if not code:
        return "33"
    head = code[0]
    if head == "6":
        return "17"
    return "33"


def write_selfstock(
    self_cache_file: Path,
    codes: list[str],
    *,
    backup: bool = True,
) -> dict:
    """把 ``codes`` 精确写进 ``SelfStockCache.json``。

    实测该文件**不会被客户端覆写**（19:03 写入，19:14 复核内容仍在），
    但同花顺运行中不读它——改动要等客户端下次启动才生效。
    因此返回 ``need_restart: True``，由界面提示用户。

    保持原文件的全部外壳字段（StatusCode/StatusMsg/version/MergeStatus），
    只替换 ``Data.Selfstock``、``Data.Count``、``Data.ModifyTime``，
    ``version`` 自增。
    """
    result = {"ok": False, "changed": False, "before": [], "after": [], "error": ""}
    running = ths_running()

    if not self_cache_file.exists():
        result["error"] = "自选缓存不存在：%s" % self_cache_file
        return result

    cleaned: list[str] = []
    seen: set[str] = set()
    for code in codes:
        code = str(code).strip()
        if not re.match(r"^[03684]\d{5}$", code):
            continue
        if code in seen:
            continue
        seen.add(code)
        cleaned.append(code)

    with _lock():
        try:
            raw = self_cache_file.read_text(encoding="utf-8-sig")
            doc = json.loads(raw)
        except (OSError, ValueError) as exc:
            result["error"] = "读取自选缓存失败：%s" % exc
            return result

        data = doc.get("Data")
        if not isinstance(data, dict):
            result["error"] = "自选缓存结构异常：缺少 Data 对象"
            return result

        raw_self = str(data.get("Selfstock") or data.get("selfstock") or "")
        head = raw_self.split(",")[0] if raw_self else ""
        before = [c for c in head.split("|") if c]

        if before == cleaned:
            result.update({"ok": True, "changed": False, "before": before, "after": before})
            return result

        text = "|".join(cleaned) + "|"
        markets = "|".join(selfstock_market(c) for c in cleaned) + "|"
        if not cleaned:
            text = ""
            markets = ""
        data["Selfstock"] = text + ("," + markets if cleaned else "")
        data["Count"] = len(cleaned)
        data["ModifyTime"] = time.strftime("%Y%m%d%H%M%S")
        try:
            data["version"] = int(data.get("version") or 0) + 1
        except (TypeError, ValueError):
            data["version"] = 1
        data.pop("selfstock", None)

        if backup:
            try:
                shutil.copy2(
                    self_cache_file,
                    self_cache_file.with_name(self_cache_file.name + BACKUP_SUFFIX),
                )
            except OSError as exc:
                logger.warning("自选缓存备份失败（继续写入）：%s", exc)

        tmp = self_cache_file.with_name(self_cache_file.name + ".tmp_ths_sync")
        payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        try:
            tmp.write_text(payload, encoding="utf-8")
        except OSError as exc:
            result["error"] = "写临时文件失败：%s" % exc
            return result

        last_err = ""
        for attempt in range(_RETRY_TIMES):
            try:
                os.replace(tmp, self_cache_file)
                last_err = ""
                break
            except PermissionError as exc:
                last_err = str(exc)
                time.sleep(_RETRY_SLEEP * (attempt + 1))
            except OSError as exc:
                last_err = str(exc)
                break
        if last_err:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            result["error"] = "写入自选缓存失败：%s" % last_err
            return result

        try:
            verify = json.loads(self_cache_file.read_text(encoding="utf-8-sig"))
            vdata = verify.get("Data") or {}
            after = [c for c in str(vdata.get("Selfstock") or "").split(",")[0].split("|") if c]
        except (OSError, ValueError) as exc:
            result["error"] = "回读校验失败：%s" % exc
            return result

        result.update(
            {
                "ok": after == cleaned,
                "changed": True,
                "before": before,
                "after": after,
                "error": "" if after == cleaned else "回读不一致",
                # 文件本身不会被覆写，但客户端运行中不读它，需重启才生效。
                "volatile": False,
                "need_restart": True,
                "volatile_note": (
                    "同花顺正在运行，本次写入要重启同花顺才会被读入"
                    if running
                    else "同花顺已关闭，本次写入会在下次启动时被读入"
                ),
            }
        )
        return result


def restore_backup(block_file: Path) -> bool:
    """从 ``.bak_ths_sync`` 还原。"""
    bak = block_file.with_name(block_file.name + BACKUP_SUFFIX)
    if not bak.exists():
        return False
    with _lock():
        try:
            shutil.copy2(bak, block_file)
            return True
        except OSError as exc:
            logger.warning("还原板块文件失败：%s", exc)
            return False
