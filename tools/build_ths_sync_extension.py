# -*- coding: utf-8 -*-
"""构建 TSP 真 tick-extension-package：koolook.ths-sync-pack。

用法：
    "C:/Python314/python.exe" tools/build_ths_sync_extension.py
产物：
    release/koolook.ths-sync-pack-1.0.0.zip

设计要点
--------
* 包 ID 用 ``koolook.ths-sync-pack``，与源码级 custom 扩展
  ``app.custom.ths_sync`` 的 ``EXTENSION_ID = "koolook.ths-sync"`` 分开，
  避免 ExtensionPackageStore._prepare() 抛「扩展 ID 与主程序已有扩展冲突」。
* 后端七模块从 canonical ``backend/app/custom/ths_sync/`` 复制；
  只把 ``routes.py`` 的 router prefix 换成 ``/api/extensions/<id>``，
  因为外置包强制所有路由在该命名空间下。
* 前端是自包含 Page Pack SDK v1 的 ES 模块，不含任何宿主内部 import，
  数据走同源进程内端点，持仓行复用 ``ctx.ui.openStockPreview``。
* 不含 manifest.json / plugin.yaml / __pycache__ / .pyc。
"""
from __future__ import annotations

import ast
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(r"C:\Users\koolook\ashare-tsp")
CANON = ROOT / "backend" / "app" / "custom" / "ths_sync"
OUT = ROOT / "release" / "koolook.ths-sync-pack-1.0.0.zip"

PACKAGE_ID = "koolook.ths-sync-pack"
VERSION = "1.0.0"
API_PREFIX = "/api/extensions/" + PACKAGE_ID

# __init__.py 单独生成，其余七个从 canonical 复制
MODULES = (
    "reader.py",
    "routes.py",
    "service.py",
    "ths_agent.py",
    "tzzb_source.py",
    "writer.py",
    "xd_screen.py",
)

EXTENSION_YAML = """format: tick-extension-package
format_version: 1
id: koolook.ths-sync-pack
display_name: 同花顺同步（独立扩展包）
version: 1.0.0
extension_api_version: 1
author: koolook
description: 同花顺远航版自选/持仓/板块汇入 TSP；TZZB MCP 主源读取持仓/交易；canonical 代码可点开原生个股详情；ctypes 探测进程零弹框；写回只加不减
backend: backend.extension
frontend: frontend/extension.mjs
requires_app: '>=0.3.25'
requires_python: '>=3.11'
platforms: [any]
requires: []
"""

BACKEND_INIT = '''# -*- coding: utf-8 -*-
"""koolook.ths-sync-pack 后端包。"""
'''

BACKEND_EXTENSION = '''# -*- coding: utf-8 -*-
"""TSP 外部扩展入口。

后端逻辑与源码级 custom 扩展 (app.custom.ths_sync) 同源，但用独立 ID，
避免 ExtensionPackageStore._prepare() 的「扩展 ID 与主程序已有扩展冲突」。

二选一启用：装本包后请在源码仓库停用 custom 扩展，否则两个后台线程会
同时写 ths_sync_state.json 与 SelfStockCache.json。
"""
from __future__ import annotations

from app.extensions import BACKEND_EXTENSION_API_VERSION, ExtensionContext

from .ths_sync import service
from .ths_sync.routes import build_router

EXTENSION_ID = "koolook.ths-sync-pack"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar) -> None:
    registrar.include_router(build_router())


def startup(context: ExtensionContext) -> None:
    service.start(context.data_dir)
'''

SUBPKG_INIT = '''# -*- coding: utf-8 -*-
"""同花顺同步引擎（包内子模块集合）。扩展入口见 backend/extension.py。"""
'''

FRONTEND_MJS = r'''/**
 * 同花顺同步 — TSP Page Pack SDK v1 (自包含，无宿主内部 import)
 *
 * 数据全部来自本扩展的进程内端点 /api/extensions/__PACKAGE_ID__/*，
 * 不再依赖旧本地桥；持仓行点开 TSP 原生个股详情。
 */
const API = '/api/extensions/__PACKAGE_ID__'
const PACK_ID = '__PACKAGE_ID__'

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function toTspSymbol(code) {
  const c = String(code || '').trim().toUpperCase()
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(c)) return c
  if (!/^[03684]\d{5}$/.test(c)) return c
  if (/^6/.test(c)) return c + '.SH'
  if (/^[03]/.test(c)) return c + '.SZ'
  if (/^[84]/.test(c)) return c + '.BJ'
  return c + '.SZ'
}

async function call(ctx, path, init) {
  const options = Object.assign({ cache: 'no-store' }, init || {})
  if (options.body && !options.headers) {
    options.headers = { 'Content-Type': 'application/json' }
  }
  const raw = ctx && ctx.api && typeof ctx.api.request === 'function'
    ? await ctx.api.request(path, options)
    : null
  if (raw !== null && raw !== undefined) return raw
  const res = await fetch(API + path, options)
  const text = await res.text()
  let data = {}
  try { data = text ? JSON.parse(text) : {} } catch (e) { data = {} }
  if (!res.ok || data.ok === false) {
    const detail = (data.errors && data.errors[0]) || data.detail || res.statusText
    throw new Error(detail || ('HTTP ' + res.status))
  }
  return data
}

function mount(el, ctx) {
  let alive = true
  let timer = null
  let toastTimer = null
  let busy = false
  let last = null

  const wrap = document.createElement('div')
  wrap.style.cssText = 'padding:24px 28px 40px;max-width:1120px;margin:0 auto;font-family:inherit;font-size:13px'
  el.appendChild(wrap)

  const head = document.createElement('div')
  head.style.cssText = 'display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap'
  head.innerHTML =
    '<div><h1 style="font-size:20px;font-weight:600;margin:0;letter-spacing:-.01em">同花顺同步</h1>' +
    '<p style="font-size:12px;opacity:.65;margin:6px 0 0;line-height:1.6">' +
    '独立扩展包 · 自选 / 持仓 / 板块汇入 TSP · canonical 代码可点开原生个股详情 · 零弹框不抢前台</p></div>' +
    '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
    '<span data-role="toast" style="font-size:12px;opacity:.8"></span>' +
    '<button data-role="refresh" style="height:34px;padding:0 16px;border-radius:9px;font-size:13px;cursor:pointer;border:1px solid rgba(125,125,125,.3);background:transparent;color:inherit;font-family:inherit">刷新状态</button>' +
    '<button data-role="dry" style="height:34px;padding:0 16px;border-radius:9px;font-size:13px;cursor:pointer;border:1px solid rgba(125,125,125,.3);background:transparent;color:inherit;font-family:inherit">预演</button>' +
    '<button data-role="positions" style="height:34px;padding:0 16px;border-radius:9px;font-size:13px;cursor:pointer;border:1px solid rgba(125,125,125,.3);background:transparent;color:inherit;font-family:inherit">一键同步持仓</button>' +
    '<button data-role="sync" style="height:34px;padding:0 16px;border-radius:9px;font-size:13px;cursor:pointer;border:none;background:var(--accent,#3b82f6);color:#fff;font-family:inherit">立即同步自选</button>' +
    '</div>'
  wrap.appendChild(head)

  const alert = document.createElement('div')
  alert.setAttribute('data-role', 'alert')
  wrap.appendChild(alert)

  const stats = document.createElement('div')
  stats.setAttribute('data-role', 'stats')
  stats.style.cssText = 'display:grid;gridTemplateColumns:repeat(auto-fit,minmax(210px,1fr));gap:12px;marginTop:20px'
  wrap.appendChild(stats)

  const tableCard = document.createElement('div')
  tableCard.setAttribute('data-role', 'table')
  wrap.appendChild(tableCard)

  const resultCard = document.createElement('div')
  resultCard.setAttribute('data-role', 'result')
  wrap.appendChild(resultCard)

  const toastEl = head.querySelector('[data-role="toast"]')
  const syncBtn = head.querySelector('[data-role="sync"]')
  const posBtn = head.querySelector('[data-role="positions"]')
  const dryBtn = head.querySelector('[data-role="dry"]')
  const refreshBtn = head.querySelector('[data-role="refresh"]')

  function flash(msg) {
    toastEl.textContent = msg
    if (toastTimer) clearTimeout(toastTimer)
    toastTimer = setTimeout(() => { if (alive) toastEl.textContent = '' }, 3500)
  }

  function setBusy(value) {
    busy = value
    syncBtn.disabled = value
    posBtn.disabled = value
    dryBtn.disabled = value
    refreshBtn.disabled = value
    const opacity = value ? '0.5' : '1'
    syncBtn.style.opacity = opacity
    posBtn.style.opacity = opacity
    dryBtn.style.opacity = opacity
    refreshBtn.style.opacity = opacity
  }

  function card(title, inner) {
    return '<div style="border:1px solid rgba(125,125,125,.22);border-radius:14px;background:rgba(125,125,125,.05);padding:18px 20px">' +
      '<div style="font-size:13px;font-weight:600;margin:0 0 14px">' + esc(title) + '</div>' + inner + '</div>'
  }

  function stat(num, label) {
    return '<div style="display:flex;flex-direction:column;gap:4px">' +
      '<span style="font-size:22px;font-weight:650;line-height:1.1;font-variant-numeric:tabular-nums">' + esc(num) + '</span>' +
      '<span style="font-size:11px;opacity:.6">' + esc(label) + '</span></div>'
  }

  function render(s) {
    last = s
    const engine = s.engine || {}
    const cfg = s.config || {}
    const ths = s.ths || {}
    const tsp = s.tsp || {}
    const positions = s.positions || []
    const groups = (tsp.groups || []).map(g => g.name).filter(Boolean).join(' / ') || '分组尚未建立'

    alert.innerHTML = ''

    let autoRow = '<label style="display:inline-flex;align-items:center;gap:8px;margin-top:12px;font-size:12px;opacity:.85;cursor:pointer">' +
      '<input type="checkbox" data-role="auto"' + (cfg.auto ? ' checked' : '') + '> 自动同步（每 ' + esc(cfg.interval || 60) + ' 秒，只跑自选）</label>'

    stats.innerHTML =
      card('同步引擎',
        '<div style="display:flex;gap:22px">' + stat(engine.thread_alive ? '运行中' : '—', '线程') +
        stat(engine.sync_count == null ? '—' : engine.sync_count, '累计同步') +
        stat(cfg.auto ? '自动' : '手动', '模式') + '</div>' +
        '<div style="font-size:11px;opacity:.6;margin-top:12px;line-height:1.7">启动 ' + esc(engine.started_at || '—') +
        '<br>上次 ' + esc(engine.last_sync || '尚未同步') + autoRow + '</div>') +
      card('同花顺侧',
        '<div style="display:flex;gap:22px">' + stat(ths.selfstock_count == null ? '—' : ths.selfstock_count, '自选') +
        stat(ths.positions_count == null ? '—' : ths.positions_count, '持仓') +
        stat(ths.block_count == null ? '—' : ths.block_count, '板块成分') + '</div>' +
        '<div style="font-size:11px;opacity:.6;margin-top:12px;line-height:1.7">用户 ' + esc(ths.user || '—') +
        ' · 持仓通道 ' + esc(ths.positions_channel || '未读') + '</div>') +
      card('TSP 自选池',
        '<div style="display:flex;gap:22px">' + stat(tsp.watchlist_count == null ? '—' : tsp.watchlist_count, '标的') +
        stat(tsp.synced_count == null ? '—' : tsp.synced_count, '同步标的') +
        stat(tsp.native_count == null ? '—' : tsp.native_count, '自建标的') + '</div>' +
        '<div style="font-size:11px;opacity:.6;margin-top:12px;line-height:1.7">' + esc(groups) + '</div>')

    const autoBox = stats.querySelector('[data-role="auto"]')
    autoBox.addEventListener('change', async () => {
      try {
        await call(ctx, '/config', { method: 'PUT', body: JSON.stringify({ auto: autoBox.checked }) })
        flash(autoBox.checked ? '已开启自动同步' : '已关闭自动同步')
        await refresh(true)
      } catch (e) {
        autoBox.checked = !autoBox.checked
        flash('切换失败：' + e.message)
      }
    })

    let rows = ''
    positions.forEach(p => {
      const up = Number(p.pnl || 0) >= 0
      const color = up ? '#ef4444' : '#22c55e'
      const symbol = p.symbol || toTspSymbol(p.code)
      rows += '<tr data-symbol="' + esc(symbol) + '" data-name="' + esc(p.name || '') + '" style="cursor:pointer" title="打开 TSP 原生个股详情">' +
        '<td style="padding:9px 10px;border-bottom:1px solid rgba(125,125,125,.1);font-family:ui-monospace,Menlo,monospace">' + esc(p.code) + '</td>' +
        '<td style="padding:9px 10px;border-bottom:1px solid rgba(125,125,125,.1)">' + esc(p.name || '—') + '</td>' +
        '<td style="padding:9px 10px;border-bottom:1px solid rgba(125,125,125,.1);text-align:right">' + esc(p.qty) + '</td>' +
        '<td style="padding:9px 10px;border-bottom:1px solid rgba(125,125,125,.1);text-align:right">' + esc(Number(p.cost || 0).toFixed(3)) + '</td>' +
        '<td style="padding:9px 10px;border-bottom:1px solid rgba(125,125,125,.1);text-align:right">' + esc(Number(p.price || 0).toFixed(2)) + '</td>' +
        '<td style="padding:9px 10px;border-bottom:1px solid rgba(125,125,125,.1);text-align:right;color:' + color + ';font-weight:600">' +
        (up ? '+' : '') + esc(Number(p.pnl || 0).toFixed(2)) + '</td></tr>'
    })

    tableCard.innerHTML = card('持仓明细（点击行查看 TSP 原生个股详情）',
      positions.length === 0
        ? '<div style="padding:22px 10px;opacity:.6;text-align:center">暂无持仓数据；代理未起时点「一键同步持仓」会走屏幕 OCR 兜底</div>'
        : '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse"><thead><tr>' +
          '<th style="text-align:left;font-size:11px;opacity:.6;padding:8px 10px;border-bottom:1px solid rgba(125,125,125,.2)">代码</th>' +
          '<th style="text-align:left;font-size:11px;opacity:.6;padding:8px 10px;border-bottom:1px solid rgba(125,125,125,.2)">名称</th>' +
          '<th style="text-align:right;font-size:11px;opacity:.6;padding:8px 10px;border-bottom:1px solid rgba(125,125,125,.2)">数量</th>' +
          '<th style="text-align:right;font-size:11px;opacity:.6;padding:8px 10px;border-bottom:1px solid rgba(125,125,125,.2)">成本</th>' +
          '<th style="text-align:right;font-size:11px;opacity:.6;padding:8px 10px;border-bottom:1px solid rgba(125,125,125,.2)">现价</th>' +
          '<th style="text-align:right;font-size:11px;opacity:.6;padding:8px 10px;border-bottom:1px solid rgba(125,125,125,.2)">盈亏</th>' +
          '</tr></thead><tbody>' + rows + '</tbody></table></div>') +
      '<div style="font-size:11px;opacity:.6;margin-top:8px">持仓浮动盈亏合计 ' +
      esc(Number(s.position_pnl_total || 0).toFixed(2)) + ' 元（成本含费）</div>'
    tableCard.firstChild.style.marginTop = '12px'
    tableCard.firstChild.style.paddingBottom = '8px'

    tableCard.querySelectorAll('tbody tr').forEach(tr => {
      tr.addEventListener('click', () => {
        const opened = ctx && ctx.ui && typeof ctx.ui.openStockPreview === 'function'
          ? ctx.ui.openStockPreview({ symbol: tr.getAttribute('data-symbol'), name: tr.getAttribute('data-name') })
          : false
        if (!opened) flash('宿主未提供原生个股详情能力，请确认 TSP 版本')
      })
    })

    const r = s.last_result || {}
    const rErrors = (r.errors || []).join('；')
    resultCard.innerHTML = card('上次同步结果',
      !s.last_result
        ? '<div style="font-size:11px;opacity:.6;line-height:1.8">尚无同步记录，点右上角「立即同步自选」。</div>'
        : '<div style="line-height:1.9"><div style="margin-bottom:6px"><b>' + (r.ok ? '成功' : '有告警') + '</b>' +
          '<span style="font-size:11px;opacity:.6;margin-left:8px">' + esc(r.ts || '') + ' · ' + esc(r.elapsed_ms || 0) + 'ms</span></div>' +
          '<div>新增 <b>' + ((r.added || []).length) + '</b> 只 · 移除 <b>' + ((r.removed || []).length) + '</b> 只 · 分组调整 <b>' +
          ((r.group_actions || []).length) + '</b> 项</div>' +
          (rErrors ? '<div style="margin-top:6px;color:#ef4444">' + esc(rErrors) + '</div>' : '') +
          ((r.skipped || []).length ? '<div style="font-size:11px;opacity:.6;margin-top:6px">' + esc((r.skipped || []).join('；')) + '</div>' : '') +
          '</div>')
    resultCard.firstChild.style.marginTop = '12px'
  }

  async function refresh(quiet) {
    if (!quiet) setBusy(true)
    try {
      const s = await call(ctx, '/status', { method: 'GET' })
      if (!alive) return
      render(s)
      alert.innerHTML = ''
    } catch (e) {
      if (!alive) return
      alert.innerHTML = card('扩展后端未就绪',
        '<div style="line-height:1.8">' + esc(e.message) +
        '<br>请在「设置 → 导入与备份 → 已安装扩展」确认本扩展包已启用，并重启 TSP 后刷新页面。</div>')
      alert.firstChild.style.marginTop = '20px'
      alert.firstChild.style.borderColor = 'rgba(239,68,68,.4)'
    } finally {
      if (alive) setBusy(false)
    }
  }

  async function run(path, label) {
    if (busy) return
    setBusy(true)
    try {
      await call(ctx, path, { method: 'POST', body: '{}' })
      flash(label + '完成')
      await refresh(true)
    } catch (e) {
      flash(label + '失败：' + e.message)
    } finally {
      if (alive) setBusy(false)
    }
  }

  syncBtn.addEventListener('click', () => run('/sync', '同步'))
  posBtn.addEventListener('click', () => run('/sync/positions', '持仓同步'))
  dryBtn.addEventListener('click', () => run('/sync/dry', '预演'))
  refreshBtn.addEventListener('click', () => refresh(false))

  refresh(false)
  timer = setInterval(() => { if (alive && !busy) refresh(true) }, 10000)

  return () => {
    alive = false
    if (timer) clearInterval(timer)
    if (toastTimer) clearTimeout(toastTimer)
    wrap.remove()
  }
}

const extension = {
  id: PACK_ID,
  apiVersion: 1,
  routes: [{
    id: 'ths-sync-pack',
    path: '/ths-sync-pack',
    mount,
  }],
  navigation: [{
    id: 'ths-sync-pack-nav',
    routeId: 'ths-sync-pack',
    label: '同花顺同步',
    icon: 'Database',
  }],
}

export default extension
'''


def build_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    files["extension.yaml"] = EXTENSION_YAML.encode("utf-8")
    files["backend/__init__.py"] = BACKEND_INIT.encode("utf-8")
    files["backend/extension.py"] = BACKEND_EXTENSION.encode("utf-8")
    files["backend/ths_sync/__init__.py"] = SUBPKG_INIT.encode("utf-8")

    for name in MODULES:
        text = (CANON / name).read_text(encoding="utf-8")
        if name == "routes.py":
            old = 'router = APIRouter(prefix="/api/custom/ths-sync", tags=["ths-sync"])'
            new = 'router = APIRouter(prefix="%s", tags=["ths-sync-pack"])' % API_PREFIX
            if text.count(old) != 1:
                raise SystemExit("ANCHOR FAIL routes.py prefix count=%d" % text.count(old))
            text = text.replace(old, new)
            text = text.replace(
                '"""同步接口。挂在 ``/api/custom/ths-sync`` 下。"""',
                '"""同步接口。挂在 ``%s`` 下。"""' % API_PREFIX,
            )
            text = text.replace(
                "请在远航版脚本界面运行一次 backend/app/custom/ths_sync/ths_agent.py",
                "请在远航版脚本界面运行一次本扩展包 backend/ths_sync/ths_agent.py",
            )
        files["backend/ths_sync/" + name] = text.encode("utf-8")

    frontend = FRONTEND_MJS.replace("__PACKAGE_ID__", PACKAGE_ID)
    files["frontend/extension.mjs"] = frontend.encode("utf-8")
    return files


def validate(files: dict[str, bytes]) -> None:
    allowed = {".py", ".yaml", ".yml", ".json", ".txt", ".md", ".csv",
               ".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".webp"}
    must = ["extension.yaml", "backend/__init__.py", "backend/extension.py",
            "backend/ths_sync/__init__.py", "frontend/extension.mjs"]
    must += ["backend/ths_sync/" + n for n in MODULES]
    for name in must:
        if name not in files:
            raise SystemExit("MISSING " + name)
    for name, payload in files.items():
        p = PurePosixPath(name)
        if name.startswith("/") or ".." in p.parts or p.suffix.lower() not in allowed:
            raise SystemExit("BAD PATH " + name)
        if name.endswith(".py"):
            ast.parse(payload.decode("utf-8"), filename=name)
    for banned in ("manifest.json", "plugin.yaml", "plugin.yml"):
        if any(PurePosixPath(n).name == banned for n in files):
            raise SystemExit("BANNED " + banned)
    if any("__pycache__" in PurePosixPath(n).parts or n.endswith(".pyc") for n in files):
        raise SystemExit("BANNED pycache")
    if "format: tick-extension-package" not in files["extension.yaml"].decode("utf-8"):
        raise SystemExit("BAD extension.yaml format")
    frontend = files["frontend/extension.mjs"].decode("utf-8")
    if "id: PACK_ID" not in frontend or "export default extension" not in frontend:
        raise SystemExit("BAD frontend default export")
    if "17652" in frontend or "ths.sync" in frontend:
        raise SystemExit("STALE bridge/id in frontend")
    if PACKAGE_ID not in frontend:
        raise SystemExit("FRONTEND ID mismatch")
    routes = files["backend/ths_sync/routes.py"].decode("utf-8")
    if API_PREFIX not in routes or "/api/custom/ths-sync" in routes:
        raise SystemExit("BAD routes prefix")
    if len(files) > 1000:
        raise SystemExit("TOO MANY FILES")


def main() -> None:
    files = build_files()
    validate(files)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    buf = None
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in sorted(files.items()):
            zf.writestr(name, payload)
    data = OUT.read_bytes()
    if len(data) > 20 * 1024 * 1024:
        raise SystemExit("ZIP TOO LARGE")
    print("BUILD_OK out=%s bytes=%d files=%d" % (OUT, len(data), len(files)))
    for name in sorted(files):
        print("  %-42s %7d" % (name, len(files[name])))


if __name__ == "__main__":
    main()
