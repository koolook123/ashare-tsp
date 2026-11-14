# -*- coding: utf-8 -*-
"""同花顺下单端「持仓表」屏幕读取（后台 PrintWindow + Tesseract，不抢前台）。

为什么需要这一层
----------------
持仓的精确来源只有同花顺进程内的 ``g_position``（成本 ``cbj`` 含费用），
而进程内取值必须靠内置 Python 脚本代理（``ths_agent.py`` 监听 17660），
它需要在同花顺的「Python策略编辑器」里人工运行一次，客户端一重启就断。

实测发现下单端（``xiadan.exe``）的「持仓」页本身就把完整持仓画在窗口上：
证券代码 / 证券名称 / 股票余额 / 可用余额 / 成本价 / 市价 / 持仓盈亏 /
盈亏比例 / 当日盈亏 / 当日盈亏比 / 市值 / 仓位占比。于是补一条**完全自动、
零人工**的旁路：后台 ``PrintWindow`` 把窗口画进位图，再 OCR 出这些列。

三条硬约束（沿用 v1 血的教训）
------------------------------
1. **只用 PrintWindow**。不 ``ShowWindow(SW_RESTORE)``、不 ``SetForegroundWindow``、
   不 ``MoveWindow`` —— BOSS 两次明令禁止抢焦点（2026-11-05 12:12、2026-11-10 10:00）。
2. **DPI 感知必须开**。200% 缩放下不开 DPI 感知，``GetWindowRect`` 只返回逻辑尺寸
   1295×815，持仓表根本不在图里。
3. **列归位靠右边界聚类，不靠固定像素**。表格列宽随窗口宽度伸缩，但数字列全部
   右对齐，所以「同一列的右边界」是稳定的；按右边界聚类定出列心，每个 token
   归到最近列心（超过 14px 就不归属），缺列留 ``None``——绝不向左错位。

不用 pywin32：冻结客户端 ``D:\\TSP\\_internal`` 里没有 win32gui / win32ui，
只有 PIL。所以 GDI 调用全部走 ctypes 手写，OCR 走 tesseract 命令行（不依赖
pytesseract）。
"""

from __future__ import annotations

import ctypes
import csv
import io
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from ctypes import wintypes as wt
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_u32 = ctypes.windll.user32
_g32 = ctypes.windll.gdi32

# 64 位下 GDI/User 句柄可能超出 32 位范围，ctypes 默认 restype=c_int 会截断，
# 结果就是「PrintWindow 返回 1 但图上全黑」。必须逐个声明真实原型。
_g32.CreateCompatibleDC.argtypes = [wt.HDC]
_g32.CreateCompatibleDC.restype = wt.HDC
_g32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_g32.CreateCompatibleBitmap.restype = wt.HBITMAP
_g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_g32.SelectObject.restype = wt.HGDIOBJ
_g32.DeleteObject.argtypes = [wt.HGDIOBJ]
_g32.DeleteObject.restype = wt.BOOL
_g32.DeleteDC.argtypes = [wt.HDC]
_g32.DeleteDC.restype = wt.BOOL
_g32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                           ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
_g32.GetDIBits.restype = ctypes.c_int
_u32.GetWindowDC.argtypes = [wt.HWND]
_u32.GetWindowDC.restype = wt.HDC
_u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_u32.ReleaseDC.restype = ctypes.c_int
_u32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
_u32.PrintWindow.restype = wt.BOOL
_u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.GetWindowRect.restype = wt.BOOL
# BitBlt 不声明 argtypes 时 ctypes 按 c_int 处理 HDC，句柄一超 32 位就
# 抛 OverflowError: int too long to convert。
_g32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
_g32.BitBlt.restype = wt.BOOL

# Tesseract 只装了 eng + osd，没有 chi_sim —— 中文列名读不出来，但数字和
# 6 位代码完全够用；股票名称另外从 stocknameV2.db 查，不靠 OCR。
TESS_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\koolook\bin\tesseract.exe",
    r"C:\Users\koolook\bin\tesseract.cmd",
)

STOCK_DB_CANDIDATES = (
    r"C:\同花顺远航版\bin\stockname\stocknameV2.db",
    r"C:\同花顺远航版\bin\stocknameV2.db",
)

TITLE_HINTS = ("网上股票交易系统", "下单")

# 持仓表右侧列顺序（与下单端表头一致）：数量 / 可用 / 成本 / 市价 /
# 持仓盈亏 / 盈亏比例 / 当日盈亏 / 当日盈亏比 / 市值 / 仓位
COL_FIELDS = (
    "qty", "avail", "cost", "price",
    "pnl", "pnl_pct", "day_pnl", "day_pct", "market_value", "pos_ratio",
)

_NUM_RE = re.compile(r"^-?\d[\d,]*\.?\d*%?$")
_CODE_RE = re.compile(r"^\d{6}$")

# 逐列裁条 OCR 用：只允许数字、小数点与负号，逼 tesseract 别丢小数点
_DIGITS = "0123456789.-"
_CELL_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
_CELL_MIN_CONF = 45.0
_CROP_PAD = 16          # 裁条四周补白，tesseract 贴边会读错
_COL_TOL = 14           # 右边界聚类容差
_COL_GAP = 20           # 左边界 = 左邻列心 + 该值
_COL_PAD = 8            # 右边界 = 本列心 + 该值

_DPI_DONE = [False]
_TESS_PATH: list[str | None] = [None]
_NAME_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------- 基础工具

def ensure_dpi_aware() -> None:
    """开启 DPI 感知。必须在任何 GetWindowRect 之前调用一次。"""
    if _DPI_DONE[0]:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _DPI_DONE[0] = True


def tesseract_path() -> str | None:
    """定位 tesseract.exe，找不到返回 None（调用方降级为「屏幕读取不可用」）。"""
    if _TESS_PATH[0] is not None:
        return _TESS_PATH[0] or None
    for cand in TESS_CANDIDATES:
        if os.path.isfile(cand):
            _TESS_PATH[0] = cand
            return cand
    found = shutil.which("tesseract")
    _TESS_PATH[0] = found or ""
    return found


def _visible_windows() -> list[tuple[int, str, tuple[int, int, int, int]]]:
    ensure_dpi_aware()
    out: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001
        try:
            if not _u32.IsWindowVisible(hwnd):
                return True
            n = _u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            _u32.GetWindowTextW(hwnd, buf, n + 1)
            rect = wt.RECT()
            _u32.GetWindowRect(hwnd, ctypes.byref(rect))
            out.append((int(hwnd), buf.value,
                        (rect.left, rect.top, rect.right, rect.bottom)))
        except Exception:
            pass
        return True

    _u32.EnumWindows(_cb, 0)
    return out


def find_window() -> int | None:
    """找下单端主窗口。标题在不同模式下会变（「网上股票交易系统5.0」/
    「专业版下单」），所以按关键词匹配 + 宽度 > 400px 过滤掉悬浮小窗。"""
    best: tuple[int, int] | None = None
    for hwnd, title, (left, top, right, bottom) in _visible_windows():
        if not any(h in title for h in TITLE_HINTS):
            continue
        w, h = right - left, bottom - top
        if w < 400 or h < 300:
            continue
        if best is None or w * h > best[0]:
            best = (w * h, hwnd)
    return best[1] if best else None


def capture(hwnd: int) -> tuple[Any, int, int]:
    """后台 PrintWindow 抓整窗，返回 (PIL.Image, W, H)。失败抛异常。

    PrintWindow 在 DirectUI/Chromium 自绘窗口上经常「返回 1 但给一张纯黑图」
    （实测本机 xiadan.exe 专业版下单：flag=0/1/2 三种都全黑）。所以抓完必须
    验一下像素，全黑就退回 ``BitBlt``（从窗口 DC 直接拷屏内像素，窗口被遮挡
    时会拷到遮挡物，所以只在 PrintWindow 失败时用）。
    两条路都不调 ShowWindow/SetForegroundWindow，不抢 BOSS 的焦点。
    """
    ensure_dpi_aware()
    from PIL import Image

    rect = wt.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸非法：%dx%d" % (w, h))

    hdc = _u32.GetWindowDC(hwnd)
    if not hdc:
        raise RuntimeError("GetWindowDC 失败")

    class _BMIH(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    class _BMI(ctypes.Structure):
        _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wt.DWORD * 3)]

    def _render(mode: str) -> bytes:
        mdc = _g32.CreateCompatibleDC(hdc)
        if not mdc:
            raise RuntimeError("CreateCompatibleDC 失败")
        # 注意：必须用「窗口 DC」hdc 而不是内存 DC mdc 来建兼容位图。
        # CreateCompatibleBitmap(mdc, ...) 里 mdc 的默认位图是 1x1 单色，
        # 按它兼容出来的位图是**单色**的 —— PrintWindow 明明返回 1，
        # 读回来却是纯黑（本机实测踩过这个坑，BitBlt 那条路也只是黑白线框）。
        bmp = _g32.CreateCompatibleBitmap(hdc, w, h)
        if not bmp:
            _g32.DeleteDC(mdc)
            raise RuntimeError("CreateCompatibleBitmap 失败")
        old = _g32.SelectObject(mdc, bmp)
        try:
            if mode == "print":
                # 2 = PW_RENDERFULLCONTENT
                _u32.PrintWindow(hwnd, mdc, 2)
            else:
                _g32.BitBlt(mdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)  # SRCCOPY

            bi = _BMI()
            bi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
            bi.bmiHeader.biWidth = w
            bi.bmiHeader.biHeight = -h        # 负值 = 自上而下，免翻转
            bi.bmiHeader.biPlanes = 1
            bi.bmiHeader.biBitCount = 32
            bi.bmiHeader.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            got = _g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            if got != h:
                raise RuntimeError("GetDIBits 只取到 %d/%d 行" % (got, h))
            return buf.raw
        finally:
            _g32.SelectObject(mdc, old)
            _g32.DeleteObject(bmp)
            _g32.DeleteDC(mdc)

    try:
        raw = _render("print")
        if not _has_content(raw):
            logger.debug("PrintWindow 全黑，改用 BitBlt")
            raw = _render("bitblt")
            if not _has_content(raw):
                raise RuntimeError("截图全黑（窗口被完全遮挡？）")
    finally:
        _u32.ReleaseDC(hwnd, hdc)

    return Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1).convert("RGB"), w, h


def _has_content(raw: bytes) -> bool:
    """抽样判断位图是否非全黑。每 4096 字节取一个像素，够 3 个就认为有内容。"""
    hits = 0
    for i in range(0, min(len(raw) - 3, 4_000_000), 4096):
        if raw[i] or raw[i + 1] or raw[i + 2]:
            hits += 1
            if hits >= 3:
                return True
    return False


# ---------------------------------------------------------------- OCR

def ocr_tokens(img: Any) -> list[dict[str, Any]]:
    """tesseract --psm 6 全图识别，返回带绝对坐标的 token 列表。"""
    tess = tesseract_path()
    if not tess:
        raise RuntimeError("找不到 tesseract.exe")
    tmpdir = tempfile.mkdtemp(prefix="ths_ocr_")
    png = os.path.join(tmpdir, "shot.png")
    base = os.path.join(tmpdir, "out")
    try:
        img.save(png)
        subprocess.run(
            [tess, png, base, "-l", "eng", "--psm", "6", "tsv"],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        tsv = base + ".tsv"
        if not os.path.isfile(tsv):
            raise RuntimeError("tesseract 没有产出 tsv")
        rows = list(csv.DictReader(io.open(tsv, encoding="utf-8"), delimiter="\t"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    toks: list[dict[str, Any]] = []
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            continue
        if conf < 20:
            continue
        try:
            toks.append({
                "t": text,
                "x": int(row["left"]),
                "y": int(row["top"]),
                "w": int(row["width"]),
                "c": conf,
            })
        except (KeyError, ValueError):
            continue
    return toks


def _right_edge(tok: dict[str, Any]) -> int:
    return tok["x"] + tok["w"]


def _cluster(values: list[int], tol: int) -> list[float]:
    vals = sorted(values)
    groups: list[list[int]] = []
    for v in vals:
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _tess_tsv(img: Any, *, psm: int = 6, whitelist: str | None = None) -> list[dict[str, str]]:
    """把图交给 tesseract 命令行，返回 tsv 行。"""
    tess = tesseract_path()
    if not tess:
        raise RuntimeError("找不到 tesseract.exe")
    tmpdir = tempfile.mkdtemp(prefix="ths_ocr_")
    png = os.path.join(tmpdir, "shot.png")
    base = os.path.join(tmpdir, "out")
    cmd = [tess, png, base, "-l", "eng", "--psm", str(psm)]
    if whitelist:
        cmd += ["-c", "tessedit_char_whitelist=" + whitelist]
    try:
        img.save(png)
        cmd.append("tsv")
        subprocess.run(
            cmd, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        tsv = base + ".tsv"
        if not os.path.isfile(tsv):
            raise RuntimeError("tesseract 没有产出 tsv")
        return list(csv.DictReader(io.open(tsv, encoding="utf-8"), delimiter="\t"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _rows_to_tokens(rows: list[dict[str, str]], *, dx: int = 0, dy: int = 0,
                    min_conf: float = 20.0) -> list[dict[str, Any]]:
    toks: list[dict[str, Any]] = []
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            continue
        if conf < min_conf:
            continue
        try:
            toks.append({
                "t": text,
                "x": int(row["left"]) + dx,
                "y": int(row["top"]) + dy,
                "w": int(row["width"]),
                "c": conf,
            })
        except (KeyError, ValueError):
            continue
    return toks


def ocr_tokens(img: Any) -> list[dict[str, Any]]:
    """tesseract --psm 6 全图识别，返回带绝对坐标的 token 列表。

    只用它定位「证券代码」行与顶部资金区；持仓的数值列另外走
    :func:`_ocr_column` 逐列裁条识别（整图会把小数点吃掉）。
    """
    return _rows_to_tokens(_tess_tsv(img, psm=6))


def _ocr_column(img: Any, x0: int, x1: int, y0: int, y1: int) -> list[dict[str, Any]]:
    """把一列竖直裁条，单独跑一次 tesseract（数字白名单）。

    为什么必须逐列裁
    ----------------
    整图 ``--psm 6`` 会把 ``2.50`` 读成 ``250``、``-1.15`` 读成 ``-115``——
    小数点被当成噪点丢掉。单独裁出一列、加上数字白名单之后，小数点与负号
    就能稳定识别（实测 9 列 × 4 行 100% 正确）。

    坐标换算：tesseract 返回的 ``left/top`` 是相对裁剪图的，必须加回
    ``(x0 - PAD, y0 - PAD)`` 才是整图坐标。
    """
    from PIL import ImageOps

    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    crop = img.crop((x0, y0, x1, y1))
    crop = ImageOps.expand(crop, border=_CROP_PAD, fill=(255, 255, 255))
    rows = _tess_tsv(crop, psm=6, whitelist=_DIGITS)
    return _rows_to_tokens(
        rows,
        dx=x0 - _CROP_PAD,
        dy=y0 - _CROP_PAD,
        min_conf=_CELL_MIN_CONF,
    )


def parse_positions(tokens: list[dict[str, Any]], width: int, img: Any = None
                    ) -> list[dict[str, Any]]:
    """解析持仓表。

    步骤：
      1. 6 位数字 + 靠左（x < 15% 宽）= 证券代码，确定行位置与行距
      2. 这些行里的数字 token 按**右边界**聚类 → 列心（数字列右对齐，稳定）
      3. 逐列在图上竖直裁条、加数字白名单重新 OCR（这一步才拿得到小数点）
      4. 每列结果按 y 归到最近的代码行
      5. 缺列留 None —— 绝不允许把后一列的值左移顶上来
    """
    codes = [t for t in tokens
             if _CODE_RE.match(t["t"]) and t["x"] < width * 0.15]
    if not codes:
        return []
    codes.sort(key=lambda a: a["y"])
    # 同一行可能出现两遍（代码被拆成两段），按 y 去重
    dedup: list[dict[str, Any]] = []
    for c in codes:
        if not dedup or c["y"] - dedup[-1]["y"] > 12:
            dedup.append(c)
    codes = dedup
    ys = [c["y"] for c in codes]
    pitch = (ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else 45.0
    band = max(18.0, pitch * 0.45)

    # 列心：只统计「代码行带内、位于代码右侧、且形如数字」的 token 右边界。
    # 上限取 0.66W —— 更右侧是滚动条与空白，混进来会把列心搞乱。
    rights: list[int] = []
    for c in codes:
        for t in tokens:
            if abs(t["y"] - c["y"]) > band:
                continue
            if t["x"] <= width * 0.13 or t["x"] > width * 0.66:
                continue
            if _NUM_RE.match(t["t"]):
                rights.append(_right_edge(t))
    centers = _cluster(rights, _COL_TOL)
    need = max(2, len(codes) - 1)
    centers = sorted(c for c in centers
                     if sum(1 for r in rights if abs(r - c) <= _COL_TOL) >= need)
    if not centers:
        return []

    # 逐列裁条 OCR：x0 取「左邻列心 + 20」（左邻列心本身不可作边界，
    # 会把左列切掉一半），x1 取「本列心 + 8」（再宽会把左邻列的小数点吸进来）。
    # 竖直方向覆盖全部代码行，±半行距，留出上下行高。
    top = int(ys[0] - pitch * 0.5)
    bottom = int(ys[-1] + pitch * 0.5)
    column_values: list[list[tuple[int, str]]] = []
    for i, cx in enumerate(centers):
        if img is None:
            column_values.append([])
            continue
        x0 = int(centers[i - 1] + _COL_GAP) if i else int(width * 0.13)
        x1 = int(cx + _COL_PAD)
        try:
            toks = _ocr_column(img, x0, x1, top, bottom)
        except Exception as exc:  # OCR 单列失败不该让整表读不出来
            logger.info("第 %d 列 OCR 失败：%s", i, exc)
            toks = []
        hits: list[tuple[int, str]] = []
        for t in toks:
            if _CELL_NUM.match(t["t"]):
                hits.append((t["y"], t["t"]))
        column_values.append(hits)

    rows: list[dict[str, Any]] = []
    for c in codes:
        rec: dict[str, Any] = {"code": c["t"]}
        for i, field in enumerate(COL_FIELDS):
            rec[field] = None
            if i >= len(column_values):
                continue
            near = [(y, txt) for (y, txt) in column_values[i] if abs(y - c["y"]) <= band]
            if not near:
                continue
            # 同一格里若被拆成多个 token（例如 "-" + "6.53"），按顺序拼回去
            joined = "".join(txt for _y, txt in sorted(near))
            rec[field] = joined if _CELL_NUM.match(joined) else sorted(near)[0][1]
        rows.append(rec)
    return rows


def parse_account(tokens: list[dict[str, Any]], width: int, height: int) -> dict[str, Any]:
    """解析顶部资金区。

    专业版布局是两行：第一行 5 个指标（资金余额 / 可取金额 / 参考市值 /
    当日盈亏 / 仓位），第二行 4 个（可用金额 / 总资产 / 持仓盈亏 /
    当日盈亏比）。中文标签 OCR 不出来（只有 eng 语言包），所以按「行内
    从左到右的序号」取数——布局固定，序号稳定。

    行归组必须用**容差聚类**（20px）：同一行的数字 top 会有 1~3px 抖动，
    按精确 y 去重会被拆成好几行，第二行就会误取到第一行的前几个数字。
    """
    # 只取资金区左侧 62% 的数字：右侧是滚动条/涨跌色块，OCR 会吐出零散数字
    # （实测 2314px 处有个假 "0"），会把行聚类带偏。
    band = [t for t in tokens
            if t["y"] < height * 0.20
            and t["x"] < width * 0.62
            and _NUM_RE.match(t["t"])]
    if len(band) < 6:
        return {}
    # 行归组用容差聚类，并按 token 数排序：只认「一行至少 4 个数」的行，
    # 这样零散噪点不会占掉第 0 行/第 1 行的位置。
    ys = sorted(t["y"] for t in band)
    row_ys: list[int] = []
    for y in ys:
        if not row_ys or y - row_ys[-1] > 20:
            row_ys.append(y)
    grouped: dict[int, list[dict[str, Any]]] = {}
    for t in band:
        idx = min(range(len(row_ys)), key=lambda i: abs(t["y"] - row_ys[i]))
        grouped.setdefault(idx, []).append(t)
    dense = [k for k in sorted(grouped) if len(grouped[k]) >= 4]
    if len(dense) < 2:
        # 退一步：只要有两行非空就用它们（窗口较窄时数字会少）
        dense = [k for k in sorted(grouped) if len(grouped[k]) >= 2]
    if len(dense) < 1:
        return {}

    def _num(text: str | None) -> float | None:
        if not text:
            return None
        try:
            return float(text.replace(",", "").rstrip("%"))
        except ValueError:
            return None

    out: dict[str, Any] = {}
    r0 = sorted(grouped.get(dense[0], []), key=lambda a: a["x"])
    r1 = sorted(grouped.get(dense[1], []), key=lambda a: a["x"]) if len(dense) > 1 else []
    if len(r0) >= 5:
        out["cash"] = _num(r0[0]["t"])          # 资金余额
        out["takeout"] = _num(r0[1]["t"])       # 可取金额
        out["market_value"] = _num(r0[2]["t"])  # 参考市值
        out["day_pnl"] = _num(r0[3]["t"])       # 当日盈亏
        out["pos_ratio"] = _num(r0[4]["t"])     # 仓位 %
    if len(r1) >= 4:
        out["avail"] = _num(r1[0]["t"])         # 可用金额
        out["asset"] = _num(r1[1]["t"])         # 总资产
        out["pos_pnl"] = _num(r1[2]["t"])       # 持仓盈亏
        out["day_pct"] = _num(r1[3]["t"])       # 当日盈亏比 %
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------- 名称与快照

def stock_name(code: str) -> str:
    """从同花顺本地股票名字典查名称（OCR 读中文不可靠，改查库）。"""
    if code in _NAME_CACHE:
        return _NAME_CACHE[code]
    for cand in STOCK_DB_CANDIDATES:
        if not os.path.isfile(cand) or os.path.getsize(cand) == 0:
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % Path(cand).as_posix(), uri=True)
            try:
                row = con.execute(
                    "select NAME from tablestock where CODE = ? limit 1", (code,)
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                # 库里格式是「浦发银行|浦发银行@f」，取第一段；
                # 「七 匹 狼」这种带内嵌空格的也要压平，否则显示成「七 匹 狼」
                name = "".join(str(row[0]).split("|")[0].split())
                if name:
                    _NAME_CACHE[code] = name
                    return name
        except Exception as exc:
            logger.debug("查股票名失败 %s: %s", code, exc)
    _NAME_CACHE[code] = ""
    return ""


def _is_a_stock(code: str) -> bool:
    return bool(re.match(r"^[03684]\d{5}$", code))


def snapshot(*, lookup_names: bool = True) -> dict[str, Any]:
    """读一次持仓 + 资金区。返回 dict，永不抛异常。"""
    started = time.time()
    result: dict[str, Any] = {
        "ok": False, "error": "", "positions": [], "account": {},
        "hwnd": None, "window_title": "", "elapsed_ms": 0, "raw_rows": 0,
    }
    hwnd = find_window()
    if not hwnd:
        result["error"] = "未找到下单端窗口（xiadan.exe 未打开或全部最小化）"
        return result
    result["hwnd"] = hwnd
    title = ""
    for h, t, _r in _visible_windows():
        if h == hwnd:
            title = t
            break
    result["window_title"] = title
    try:
        img, w, h = capture(hwnd)
    except Exception as exc:
        result["error"] = "截图失败：%s" % exc
        return result
    try:
        tokens = ocr_tokens(img)
    except Exception as exc:
        result["error"] = "OCR 失败：%s" % exc
        return result

    rows = parse_positions(tokens, w, img)
    result["raw_rows"] = len(rows)
    out: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "")
        if not _is_a_stock(code):
            continue

        def _f(key: str) -> float | None:
            v = row.get(key)
            if v is None:
                return None
            try:
                return float(str(v).replace(",", "").rstrip("%"))
            except ValueError:
                return None

        qty = _f("qty")
        cost = _f("cost")
        price = _f("price")
        pnl = _f("pnl")
        pnl_pct = _f("pnl_pct")
        mv = _f("market_value")
        # 一致性校验：市值 ≈ 数量 × 市价（容差 2%），否则判定列错位、整行丢弃。
        # 宁可少一行，也不能把成本当市价喂出去。
        if qty and price and mv:
            if abs(mv - qty * price) > max(1.0, qty * price * 0.02):
                logger.info("列校验失败，丢弃 %s：qty=%s price=%s mv=%s",
                            code, qty, price, mv)
                continue
        out.append({
            "code": code,
            "name": stock_name(code) if lookup_names else "",
            "qty": qty,
            "avail": _f("avail"),
            "cost": cost,
            "price": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "day_pnl": _f("day_pnl"),
            "day_pct": _f("day_pct"),
            "market_value": mv,
            "pos_ratio": _f("pos_ratio"),
        })
    result["positions"] = out
    result["account"] = parse_account(tokens, w, h)
    result["ok"] = bool(out)
    if not out:
        result["error"] = "OCR 没读出任何持仓行（窗口是否停在持仓页？）"
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def available() -> tuple[bool, str]:
    """通道自检：能不能用。返回 (可用, 原因)。"""
    if not tesseract_path():
        return False, "未安装 tesseract"
    if not find_window():
        return False, "下单端窗口不可见"
    return True, ""
