/**
 * 同花顺同步 — TSP 页面插件 (Page Pack SDK v1)
 *
 * 与本地同步桥 (tools/ths_tsp_sync.py, 127.0.0.1:17652) 配套：
 *   - 只读桥的 /api/status 展示两池状态，POST /api/sync 触发一键同步
 *   - 桥本身是纯后台「读文件 + HTTP」，不做 UI 模拟、不抢焦点
 *   - 持仓明细来自 v1 /api/positions（含费成本，TSP 原生没有这一口径）
 *
 * 遵循 TSP 原版设计：布局用内联样式，主题色取宿主 CSS 变量 var(--accent)，
 * 不依赖宿主 Tailwind 类名；页面插件自带 React，不 import 宿主内部模块。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ctx } from './tsp'

const BRIDGE = 'http://127.0.0.1:17652'

type Position = {
  code: string
  name: string
  qty: number
  cost: number
  price: number
  pnl: number
  pnl_pct: number
}

type Status = {
  ok?: boolean
  bridge?: {
    version?: number
    auto?: boolean
    interval?: number
    started_at?: string
    sync_count?: number
    last_sync?: string
  }
  tsp?: {
    online?: boolean
    base?: string
    version?: string
    watchlist_count?: number
    groups?: { id?: string; name?: string }[]
  }
  sources?: {
    selfstock_count?: number
    positions_count?: number
    positions?: Position[]
    selfstock?: string[]
  }
  last_result?: {
    ok?: boolean
    ts?: string
    added?: string[]
    removed?: string[]
    group_actions?: string[]
    errors?: string[]
    elapsed_ms?: number
  }
  error?: string
}

function toTspSymbol(code: string): string {
  if (/^6/.test(code)) return `${code}.SH`
  if (/^[03]/.test(code)) return `${code}.SZ`
  if (/^[84]/.test(code)) return `${code}.BJ`
  return `${code}.SZ`
}

async function getStatus(): Promise<Status> {
  const r = await fetch(`${BRIDGE}/api/status`, { cache: 'no-store' })
  if (!r.ok) throw new Error(`桥返回 HTTP ${r.status}`)
  return (await r.json()) as Status
}

async function postSync(): Promise<Status> {
  const r = await fetch(`${BRIDGE}/api/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  const j = (await r.json()) as Status
  if (!r.ok || j.ok === false) throw new Error((j.errors && j.errors[0]) || `同步失败 HTTP ${r.status}`)
  return j
}

function SyncPage() {
  const [status, setStatus] = useState<Status | null>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')
  const alive = useRef(true)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const flash = useCallback((msg: string) => {
    setToast(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => alive.current && setToast(''), 3500)
  }, [])

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const s = await getStatus()
      if (!alive.current) return
      setStatus(s)
      setError('')
    } catch (e) {
      if (!alive.current) return
      setError(`连不上同步桥（${BRIDGE}）：${(e as Error).message}。请先运行 tools/run_sync_once.bat，或用 --loop 后台启动。`)
      setStatus(null)
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    alive.current = true
    refresh()
    const timer = setInterval(() => refresh(true), 10000)
    return () => {
      alive.current = false
      clearInterval(timer)
      if (toastTimer.current) clearTimeout(toastTimer.current)
    }
  }, [refresh])

  const doSync = useCallback(async () => {
    setSyncing(true)
    try {
      await postSync()
      flash('同步完成')
      await refresh(true)
    } catch (e) {
      flash(`同步失败：${(e as Error).message}`)
    } finally {
      if (alive.current) setSyncing(false)
    }
  }, [refresh, flash])

  // ---- 样式：全部内联，取宿主主题变量，明暗主题通吃 ----
  const wrap: React.CSSProperties = { padding: '24px 28px 40px', maxWidth: 1120, margin: '0 auto', fontFamily: 'inherit', fontSize: 13 }
  const h1: React.CSSProperties = { fontSize: 20, fontWeight: 600, margin: 0, letterSpacing: '-0.01em' }
  const sub: React.CSSProperties = { fontSize: 12, opacity: 0.65, margin: '6px 0 0', lineHeight: 1.6 }
  const card: React.CSSProperties = {
    border: '1px solid rgba(125,125,125,0.22)', borderRadius: 14,
    background: 'rgba(125,125,125,0.05)', padding: '18px 20px',
  }
  const cardTitle: React.CSSProperties = { fontSize: 13, fontWeight: 600, margin: '0 0 14px', display: 'flex', alignItems: 'center', gap: 8 }
  const btn = (primary: boolean, disabled: boolean): React.CSSProperties => ({
    height: 34, padding: '0 16px', borderRadius: 9, fontSize: 13, cursor: disabled ? 'not-allowed' : 'pointer',
    border: primary ? 'none' : '1px solid rgba(125,125,125,0.3)',
    background: primary ? 'var(--accent, #3b82f6)' : 'transparent',
    color: primary ? '#fff' : 'inherit', opacity: disabled ? 0.5 : 1,
    fontFamily: 'inherit', transition: 'opacity .15s',
  })
  const dot = (on: boolean): React.CSSProperties => ({
    width: 8, height: 8, borderRadius: '50%', display: 'inline-block',
    background: on ? '#22c55e' : '#ef4444', boxShadow: `0 0 0 3px ${on ? 'rgba(34,197,94,.14)' : 'rgba(239,68,68,.14)'}`,
  })
  const stat: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 4 }
  const statNum: React.CSSProperties = { fontSize: 22, fontWeight: 650, lineHeight: 1.1, fontVariantNumeric: 'tabular-nums' }
  const statLabel: React.CSSProperties = { fontSize: 11, opacity: 0.6 }
  const th: React.CSSProperties = { textAlign: 'left', fontWeight: 600, fontSize: 11, opacity: 0.6, padding: '8px 10px', borderBottom: '1px solid rgba(125,125,125,0.2)', whiteSpace: 'nowrap' }
  const td: React.CSSProperties = { padding: '9px 10px', borderBottom: '1px solid rgba(125,125,125,0.1)', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }
  const tag: React.CSSProperties = { display: 'inline-block', padding: '2px 8px', borderRadius: 6, fontSize: 11, border: '1px solid rgba(125,125,125,0.25)', opacity: 0.85 }
  const code: React.CSSProperties = { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12, padding: '2px 6px', borderRadius: 5, background: 'rgba(125,125,125,0.1)' }

  const b = status?.bridge
  const t = status?.tsp
  const s = status?.sources
  const last = status?.last_result
  const positions = s?.positions || []
  const posTotal = positions.reduce((a, p) => a + (Number(p.pnl) || 0), 0)

  return (
    <div style={wrap}>
      {/* 头部 */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h1 style={h1}>同花顺同步</h1>
          <p style={sub}>自选股与持仓自动汇入 TSP 自选池 · 纯后台读写，不模拟界面、不抢焦点</p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {toast && <span style={{ fontSize: 12, opacity: 0.8, marginRight: 4 }}>{toast}</span>}
          <button style={btn(false, loading || syncing)} disabled={loading || syncing} onClick={() => refresh()}>
            {loading ? '读取中…' : '刷新状态'}
          </button>
          <button style={btn(true, syncing || !status)} disabled={syncing || !status} onClick={doSync}>
            {syncing ? '同步中…' : '一键同步'}
          </button>
        </div>
      </div>

      {error && (
        <div style={{ ...card, marginTop: 20, borderColor: 'rgba(239,68,68,.4)', background: 'rgba(239,68,68,.06)', lineHeight: 1.8 }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>同步桥未就绪</div>
          <div style={{ opacity: 0.85 }}>{error}</div>
          <div style={{ marginTop: 10 }}>
            <code style={code}>pythonw tools/ths_tsp_sync.py --loop</code>
            <span style={{ marginLeft: 8, opacity: 0.65 }}>或双击 tools/run_sync_once.bat</span>
          </div>
          <button style={{ ...btn(false, false), marginTop: 14 }} onClick={() => refresh()}>重试连接</button>
        </div>
      )}

      {/* 状态卡 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 12, marginTop: 20 }}>
        <div style={card}>
          <div style={cardTitle}><span style={dot(!!status)} /> 同步桥</div>
          <div style={{ display: 'flex', gap: 22 }}>
            <div style={stat}><span style={statNum}>{status ? (b?.auto ? '自动' : '手动') : '—'}</span><span style={statLabel}>运行模式</span></div>
            <div style={stat}><span style={statNum}>{b?.interval ?? '—'}s</span><span style={statLabel}>轮询间隔</span></div>
            <div style={stat}><span style={statNum}>{b?.sync_count ?? '—'}</span><span style={statLabel}>累计同步</span></div>
          </div>
          <div style={{ ...statLabel, marginTop: 12, lineHeight: 1.7 }}>
            启动 {b?.started_at || '—'}<br />上次 {b?.last_sync || '尚未同步'}
          </div>
        </div>
        <div style={card}>
          <div style={cardTitle}><span style={dot(!!t?.online)} /> TSP 工作台</div>
          <div style={{ display: 'flex', gap: 22 }}>
            <div style={stat}><span style={statNum}>{t?.watchlist_count ?? '—'}</span><span style={statLabel}>自选池标的</span></div>
            <div style={stat}><span style={statNum}>{(t?.groups || []).length}</span><span style={statLabel}>同步分组</span></div>
          </div>
          <div style={{ ...statLabel, marginTop: 12, lineHeight: 1.7 }}>
            版本 {t?.version || '—'} · {t?.base || '—'}<br />
            {(t?.groups || []).map(g => g.name).filter(Boolean).join(' / ') || '分组尚未建立'}
          </div>
        </div>
        <div style={card}>
          <div style={cardTitle}>两池来源</div>
          <div style={{ display: 'flex', gap: 22 }}>
            <div style={stat}><span style={statNum}>{s?.selfstock_count ?? '—'}</span><span style={statLabel}>同花顺自选（盯买点）</span></div>
            <div style={stat}><span style={statNum}>{s?.positions_count ?? '—'}</span><span style={statLabel}>当前持仓（盯卖点）</span></div>
          </div>
          <div style={{ ...statLabel, marginTop: 12, lineHeight: 1.7 }}>
            持仓浮动盈亏合计 <b style={{ color: posTotal >= 0 ? '#ef4444' : '#22c55e', fontWeight: 600 }}>
              {posTotal >= 0 ? '+' : ''}{posTotal.toFixed(2)}
            </b> 元（成本含费，来自 v1）
          </div>
        </div>
      </div>

      {/* 持仓明细 */}
      <div style={{ ...card, marginTop: 12, padding: '18px 20px 8px' }}>
        <div style={cardTitle}>
          <span>持仓明细</span>
          <span style={tag}>v1 含费成本</span>
          <span style={{ ...statLabel, fontWeight: 400, marginLeft: 'auto' }}>点击行查看 TSP 个股详情</span>
        </div>
        {positions.length === 0 ? (
          <div style={{ padding: '22px 10px', opacity: 0.6, textAlign: 'center' }}>暂无持仓数据</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={th}>代码</th><th style={th}>名称</th>
                  <th style={{ ...th, textAlign: 'right' }}>数量</th>
                  <th style={{ ...th, textAlign: 'right' }}>成本</th>
                  <th style={{ ...th, textAlign: 'right' }}>现价</th>
                  <th style={{ ...th, textAlign: 'right' }}>盈亏</th>
                  <th style={{ ...th, textAlign: 'right' }}>盈亏%</th>
                  <th style={th}>池</th>
                </tr>
              </thead>
              <tbody>
                {positions.map(p => {
                  const up = (Number(p.pnl) || 0) >= 0
                  const c = up ? '#ef4444' : '#22c55e'
                  const inSelf = (s?.selfstock || []).includes(p.code)
                  return (
                    <tr key={p.code} style={{ cursor: 'pointer' }}
                      onClick={() => ctx?.ui?.openStockPreview?.({ symbol: toTspSymbol(p.code), name: p.name })}
                      title="打开 TSP 个股详情">
                      <td style={td}><code style={code}>{p.code}</code></td>
                      <td style={td}>{p.name || '—'}</td>
                      <td style={{ ...td, textAlign: 'right' }}>{p.qty}</td>
                      <td style={{ ...td, textAlign: 'right' }}>{Number(p.cost).toFixed(3)}</td>
                      <td style={{ ...td, textAlign: 'right' }}>{Number(p.price).toFixed(2)}</td>
                      <td style={{ ...td, textAlign: 'right', color: c, fontWeight: 600 }}>{up ? '+' : ''}{Number(p.pnl).toFixed(2)}</td>
                      <td style={{ ...td, textAlign: 'right', color: c }}>{up ? '+' : ''}{Number(p.pnl_pct).toFixed(2)}%</td>
                      <td style={td}>
                        <span style={tag}>持仓</span>{inSelf && <span style={{ ...tag, marginLeft: 4 }}>自选</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 上次同步结果 + 机制说明 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 12, marginTop: 12 }}>
        <div style={card}>
          <div style={cardTitle}>上次同步结果</div>
          {!last ? <div style={{ ...statLabel, lineHeight: 1.8 }}>尚无同步记录，点右上角「一键同步」。</div> : (
            <div style={{ lineHeight: 1.9 }}>
              <div style={{ marginBottom: 6 }}>
                <span style={{ ...dot(!!last.ok), marginRight: 6 }} />
                <b>{last.ok ? '成功' : '有告警'}</b>
                <span style={{ ...statLabel, marginLeft: 8 }}>{last.ts} · {last.elapsed_ms}ms</span>
              </div>
              <div>新增 <b>{(last.added || []).length}</b> 只 · 移除 <b>{(last.removed || []).length}</b> 只 · 分组调整 <b>{(last.group_actions || []).length}</b> 项</div>
              {(last.errors || []).length > 0 && (
                <div style={{ marginTop: 6, color: '#ef4444' }}>{(last.errors || []).join('；')}</div>
              )}
              {(last.added || []).length > 0 && (
                <div style={{ ...statLabel, marginTop: 6, wordBreak: 'break-all' }}>新增：{(last.added || []).join(' ')}</div>
              )}
            </div>
          )}
        </div>
        <div style={card}>
          <div style={cardTitle}>同步机制</div>
          <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.9, opacity: 0.82 }}>
            <li>自选：只读同花顺 <code style={code}>SelfStockCache.json</code> 与下单端 <code style={code}>selfstock_*.json</code>，取并集</li>
            <li>持仓：只读 v1 <code style={code}>/api/positions</code>，复用含费成本口径</li>
            <li>写入：TSP 原生 <code style={code}>POST /api/watchlist/batch</code>，分「同花顺自选」「同花顺持仓」两组</li>
            <li>只推增量、只删自己写的过期标的；你手动加的和自建分组一律不动</li>
            <li>自动模式每 {b?.interval || 60} 秒一轮；一键按钮随时补跑，全程零 UI 模拟</li>
          </ul>
        </div>
      </div>

      <p style={{ ...sub, marginTop: 18 }}>
        数据来自本地同花顺缓存与 v1 老服务，仅供个人盯盘使用；本页面不会下达任何委托。
      </p>
    </div>
  )
}

const extension = {
  id: 'ths.sync',
  apiVersion: 1 as const,
  routes: [
    {
      id: 'ths-sync',
      path: '/ths-sync',
      mount: (el: HTMLElement) => {
        const root = createRoot(el)
        root.render(<SyncPage />)
        return () => root.unmount()
      },
    },
  ],
  navigation: [
    { id: 'ths-sync-nav', routeId: 'ths-sync', label: '同花顺同步', icon: 'Database' },
  ],
}

export default extension
