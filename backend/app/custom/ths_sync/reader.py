# -*- coding: utf-8 -*-
"""同花顺数据读取层。

三个数据来源，特性完全不同，必须分别对待：

1. 自选股 —— ``SelfStockCache.json``
   客户端自己持续写盘（交易时段内随增删刷新），格式稳定，可直接读。

2. 持仓 —— 客户端内存 ``g_position``
   同花顺从不把持仓落盘。``blockstockV3.xml`` 是手动板块，
   ``PositionBlock/PositionBlock.xml`` 只是面板布局，``BSData/*`` 是历史成交流水
   且不完整（同一个代码可能只留下部分成交记录），用它推导持仓会算错。
   唯一精确来源是同花顺内置 Python 的全局 ``g_position``，
   由同花顺进程内的代理脚本通过 127.0.0.1:17660 暴露。
   代理脚本在同花顺脚本界面运行一次即常驻线程，之后不抢前台。

3. 板块 —— ``blockstockV3.xml``
   可直接读写。实测：客户端运行期间改写该文件不会被覆盖。
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 路径解析

#: 同花顺远航版安装根目录。可用环境变量覆盖。
THS_ROOT_ENV = "THS_ROOT"
DEFAULT_THS_ROOT = Path(r"C:\同花顺远航版")

#: 代理脚本默认端口。
AGENT_PORT_ENV = "THS_AGENT_PORT"
DEFAULT_AGENT_PORT = 17660

#: 有效 A 股代码：沪 6、深 0/3、北 8/4。
_A_STOCK_RE = re.compile(r"^[03684]\d{5}$")

#: 板块文件名。
BLOCK_FILE = "blockstockV3.xml"

#: 自选缓存文件名。
SELF_CACHE_FILE = "SelfStockCache.json"


def ths_root() -> Path:
    raw = os.environ.get(THS_ROOT_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_THS_ROOT


def agent_port() -> int:
    raw = os.environ.get(AGENT_PORT_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_AGENT_PORT
    except ValueError:
        return DEFAULT_AGENT_PORT


def is_a_stock(code: str) -> bool:
    return bool(_A_STOCK_RE.match(code or ""))


@dataclass
class ThsUser:
    """一个同花顺登录用户的目录。"""

    name: str
    user_dir: Path

    @property
    def self_cache(self) -> Path:
        return self.user_dir / SELF_CACHE_FILE

    @property
    def block_file(self) -> Path:
        return self.user_dir / BLOCK_FILE


def find_user() -> ThsUser | None:
    """定位同花顺当前用户目录。

    同花顺把用户数据放在 ``<根>/bin/users/<用户名>/``。用户名是登录账号昵称，
    换账号就换目录，所以不能写死。这里按两个文件的最近修改时间挑最活跃的目录。
    """
    users_root = ths_root() / "bin" / "users"
    if not users_root.is_dir():
        return None

    best: tuple[float, ThsUser] | None = None
    try:
        entries = list(users_root.iterdir())
    except OSError:
        return None

    for entry in entries:
        if not entry.is_dir():
            continue
        stamps: list[float] = []
        for name in (SELF_CACHE_FILE, BLOCK_FILE):
            p = entry / name
            try:
                stamps.append(p.stat().st_mtime)
            except OSError:
                continue
        if not stamps:
            continue
        user = ThsUser(name=entry.name, user_dir=entry)
        score = max(stamps)
        if best is None or score > best[0]:
            best = (score, user)

    return best[1] if best else None


# ---------------------------------------------------------------- 自选股


def read_selfstock(user: ThsUser | None = None) -> list[str] | None:
    """读同花顺自选股代码列表。

    文件结构::

        {"StatusCode":0,"Data":{"Selfstock":"600135|600815|...|,17|17|33|",...}}

    ``Selfstock`` 是一个逗号分段字符串；第一段是代码、``|`` 分隔，
    后面几段是每个代码的市场编号。只取第一段。

    读不到返回 ``None``（调用方据此保留上次快照，避免把 TSP 自选池误清空）。
    """
    user = user or find_user()
    if user is None:
        return None
    path = user.self_cache
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.warning("读取同花顺自选失败 %s: %s", path, exc)
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("同花顺自选 JSON 解析失败: %s", exc)
        return None

    data = payload.get("Data") or payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    blob = data.get("Selfstock") or data.get("selfstock") or ""
    head = str(blob).split(",")[0]
    codes: list[str] = []
    seen: set[str] = set()
    for item in head.split("|"):
        code = item.strip()
        if is_a_stock(code) and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


# ---------------------------------------------------------------- 持仓（代理）


class AgentUnavailable(RuntimeError):
    """同花顺代理脚本没有在运行。"""


def agent_request(payload: dict[str, Any], *, timeout: float = 3.0) -> dict[str, Any]:
    """向同花顺内置代理发一次请求。

    协议：TCP 短连接，一次请求一个 JSON，一次响应一个 JSON。
    """
    port = agent_port()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > 4 * 1024 * 1024:
                    break
    except (OSError, socket.timeout) as exc:
        raise AgentUnavailable(f"同花顺代理未连接（127.0.0.1:{port}）：{exc}") from exc

    body = b"".join(chunks)
    if not body:
        raise AgentUnavailable("同花顺代理返回空响应")
    try:
        reply = json.loads(body.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        raise AgentUnavailable(f"同花顺代理响应不是 JSON：{exc}") from exc
    if not isinstance(reply, dict):
        raise AgentUnavailable("同花顺代理响应结构异常")
    return reply


@dataclass
class Position:
    """一只持仓。成本用同花顺的 ``cbj``——已含费用，是资金账户口径。"""

    code: str
    name: str
    qty: float
    avail: float
    cost: float
    price: float
    market_value: float
    pnl: float

    def as_dict(self) -> dict[str, Any]:
        pnl_pct = None
        if self.cost > 0 and self.qty > 0:
            pnl_pct = round((self.price - self.cost) / self.cost * 100, 2)
        return {
            "code": self.code,
            "name": self.name,
            "qty": self.qty,
            "avail": self.avail,
            "cost": self.cost,
            "price": self.price,
            "market_value": self.market_value,
            "pnl": self.pnl,
            "pnl_pct": pnl_pct,
        }


def read_positions() -> list[Position] | None:
    """读实时持仓。代理不可用时返回 ``None``。

    代理返回的每条含 ``code/name/qty/avail/cost/price/market_value/pnl``，
    其中 ``cost`` 来自 ``g_position[...]['cbj']``，即同花顺成交回报的成本价，含费用。
    """
    try:
        reply = agent_request({"type": "get_position"}, timeout=4.0)
    except AgentUnavailable as exc:
        logger.info("持仓通道不可用：%s", exc)
        return None

    if not reply.get("ok"):
        logger.warning("同花顺代理返回失败：%s", reply)
        return None

    rows = reply.get("positions") or []
    out: list[Position] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if not is_a_stock(code):
            continue
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        out.append(
            Position(
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
    return out


def agent_alive() -> bool:
    try:
        reply = agent_request({"type": "ping"}, timeout=1.5)
    except AgentUnavailable:
        return False
    return bool(reply.get("ok"))


def agent_python_version() -> str | None:
    try:
        reply = agent_request({"type": "ping"}, timeout=1.5)
    except AgentUnavailable:
        return None
    return str(reply.get("python") or "") or None


# ---------------------------------------------------------------- 板块


_BLOCK_RE_TPL = r'<Block name="%s"[^>]*>(.*?)</Block>'


def read_block_codes(name: str, user: ThsUser | None = None) -> list[str] | None:
    """读某个自定义板块的成分股代码。板块不存在返回空列表，文件读不到返回 None。"""
    user = user or find_user()
    if user is None:
        return None
    path = user.block_file
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.warning("读取板块文件失败 %s: %s", path, exc)
        return None

    match = re.search(_BLOCK_RE_TPL % re.escape(name), text, re.S)
    if not match:
        return []
    return re.findall(r'code="(\d+)"', match.group(1))


def list_blocks(user: ThsUser | None = None) -> list[dict[str, Any]] | None:
    """列出所有板块名与成分数量。"""
    user = user or find_user()
    if user is None:
        return None
    try:
        text = user.block_file.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    for match in re.finditer(r'<Block name="([^"]*)"[^>]*id="(\d+)"([^>]*)>', text):
        name = match.group(1)
        block_id = match.group(2)
        tail = match.group(3) or ""
        self_closing = tail.rstrip().endswith("/")
        count = 0 if self_closing else len(
            read_block_codes(name, user) or []
        )
        out.append({"name": name, "id": block_id, "count": count})
    return out
