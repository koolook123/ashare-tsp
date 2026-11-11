import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  Database,
  RefreshCw,
  RotateCcw,
  Zap,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'

const API = '/api/custom/ths-sync'

interface PositionRow {
  code: string
  name?: string | null
  qty?: number | null
  cost?: number | null
  price?: number | null
  pnl?: number | null
  pnl_pct?: number | null
}

interface SyncResult {
  ok: boolean
  ts: string
  added: string[]
  removed: string[]
  group_actions: string[]
  errors: string[]
  elapsed_ms: number
  selfstock: number
  positions: number
}

interface Status {
  ok: boolean
  bridge: {
    version: number
    mode: string
    auto: boolean
    interval: number
    started_at: string
    sync_count: number
    last_sync: string
    thread_alive: boolean
  }
  tsp: {
    online: boolean
    watchlist_count: number
    groups: { id: string; name: string; color?: string }[]
  }
  sources: {
    selfstock_count: number
    positions_count: number
    selfstock: string[]
    positions: PositionRow[]
    position_pnl_total: number
  }
  config: {
    sync_selfstock: boolean
    sync_positions: boolean
    ths_user: string
    ths_root: string
  }
  last_result: SyncResult | null
}

async function fetchStatus(): Promise<Status> {
  const res = await fetch(`${API}/status`, { cache: 'no-store' })
  if (!res.ok) throw new Error(`状态读取失败 (${res.status})`)
  return res.json()
}

async function postSync(): Promise<SyncResult> {
  const res = await fetch(`${API}/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!res.ok) throw new Error(`同步失败 (${res.status})`)
  return res.json()
}

function StatCard({
  title,
  icon,
  children,
}: {
  title: string
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-card border border-border bg-surface/40 px-4 py-3">
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-muted">
        {icon}
        <span>{title}</span>
      </div>
      {children}
    </div>
  )
}

function Metric({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[11px] text-muted">{label}</span>
      <span className={cn('truncate font-mono text-xs text-secondary', tone)}>{value}</span>
    </div>
  )
}

export function ThsSync() {
  const qc = useQueryClient()
  const [toast, setToast] = useState<string>('')

  const statusQuery = useQuery({
    queryKey: ['ths-sync-status'],
    queryFn: fetchStatus,
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  })

  const syncMutation = useMutation({
    mutationFn: postSync,
    onSuccess: (res) => {
      const parts = [`新增 ${res.added.length}`, `移除 ${res.removed.length}`]
      setToast(res.ok ? `同步完成 · ${parts.join(' / ')}` : `同步有警告 · ${res.errors.join('；')}`)
      void qc.invalidateQueries({ queryKey: ['ths-sync-status'] })
      void qc.invalidateQueries({ queryKey: ['watchlist'] })
      void qc.invalidateQueries({ queryKey: ['watchlist-groups'] })
      window.setTimeout(() => setToast(''), 4000)
    },
    onError: (err: unknown) => {
      setToast(err instanceof Error ? err.message : '同步失败')
      window.setTimeout(() => setToast(''), 4000)
    },
  })

  const resetToast = useCallback(() => setToast(''), [])

  const data = statusQuery.data
  const loading = statusQuery.isLoading && !data
  const failed = statusQuery.isError && !data
  const positions = data?.sources.positions ?? []
  const pnlTotal = data?.sources.position_pnl_total ?? 0

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="同花顺同步"
        subtitle="同花顺自选股与持仓自动汇入 TSP 自选池 · 进程内直写，不模拟界面、不抢焦点"
        right={
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void statusQuery.refetch()}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-surface px-2.5 py-1.5 text-[11px] text-secondary transition-colors hover:border-accent/40 hover:text-accent"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              刷新状态
            </button>
            <button
              type="button"
              disabled={syncMutation.isPending}
              onClick={() => syncMutation.mutate()}
              className={cn(
                'inline-flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1.5 text-[11px] font-medium text-white transition-opacity',
                syncMutation.isPending ? 'opacity-60' : 'hover:opacity-90',
              )}
            >
              {syncMutation.isPending ? (
                <RotateCcw className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Zap className="h-3.5 w-3.5" />
              )}
              立即同步
            </button>
          </div>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {toast && (
          <div className="mb-3 flex items-center gap-2 rounded-card border border-accent/30 bg-accent/[0.06] px-3 py-2 text-[11px] text-accent">
            <Check className="h-3.5 w-3.5" />
            {toast}
            <button type="button" onClick={resetToast} className="ml-auto text-muted hover:text-foreground">
              关闭
            </button>
          </div>
        )}

        {loading && <div className="py-16 text-center text-xs text-muted">正在读取同步状态…</div>}

        {failed && (
          <div className="flex flex-col items-center gap-2 py-16 text-center">
            <AlertTriangle className="h-5 w-5 text-warning" />
            <div className="text-xs text-secondary">同花顺同步扩展未响应</div>
            <div className="max-w-md text-[11px] text-muted">
              请确认 TSP 后端已随本页启动；扩展位于 backend/app/custom/ths_sync，
              接口前缀 /api/custom/ths-sync。
            </div>
            <button
              type="button"
              onClick={() => void statusQuery.refetch()}
              className="mt-1 rounded-btn border border-border px-2.5 py-1 text-[11px] text-secondary hover:text-accent"
            >
              重试
            </button>
          </div>
        )}

        {data && (
          <div className="flex flex-col gap-3">
            <div className="grid gap-3 md:grid-cols-3">
              <StatCard title="同步引擎" icon={<Database className="h-3.5 w-3.5" />}>
                <Metric
                  label="运行模式"
                  value={data.bridge.auto ? `自动 · ${data.bridge.interval}s` : '仅手动'}
                  tone={data.bridge.auto ? 'text-accent' : 'text-warning'}
                />
                <Metric label="累计同步" value={`${data.bridge.sync_count} 次`} />
                <Metric label="上次同步" value={data.bridge.last_sync || '—'} />
                <Metric label="启动时间" value={data.bridge.started_at || '—'} />
              </StatCard>

              <StatCard title="TSP 自选池" icon={<Database className="h-3.5 w-3.5" />}>
                <Metric label="池内标的" value={`${data.tsp.watchlist_count} 只`} />
                <Metric label="同步分组" value={`${data.tsp.groups.length} 个`} />
                <Metric
                  label="分组"
                  value={data.tsp.groups.map(g => g.name).join(' / ') || '—'}
                />
                <Metric label="写入方式" value="进程内直写" />
              </StatCard>

              <StatCard title="两池来源" icon={<Database className="h-3.5 w-3.5" />}>
                <Metric label="同花顺自选" value={`${data.sources.selfstock_count} 只`} />
                <Metric label="当前持仓" value={`${data.sources.positions_count} 只`} />
                <Metric
                  label="持仓浮动盈亏"
                  value={`${pnlTotal >= 0 ? '+' : ''}${pnlTotal.toFixed(2)} 元`}
                  tone={pnlTotal >= 0 ? 'text-danger' : 'text-bear'}
                />
                <Metric label="同花顺账号" value={data.config.ths_user} />
              </StatCard>
            </div>

            <div className="overflow-hidden rounded-card border border-border bg-surface/40">
              <div className="flex items-center justify-between border-b border-border px-4 py-2">
                <span className="text-[11px] font-medium text-secondary">持仓明细</span>
                <span className="text-[10px] text-muted">成本含费，来自 v1 A 股助手 position_map</span>
              </div>
              {positions.length === 0 ? (
                <div className="py-10 text-center text-[11px] text-muted">
                  当前无持仓，或 v1 助手未启动（读不到持仓时沿用上次快照，不会清空自选池）
                </div>
              ) : (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-[10px] text-muted">
                      <th className="px-4 py-1.5 text-left font-medium">代码</th>
                      <th className="px-2 py-1.5 text-left font-medium">名称</th>
                      <th className="px-2 py-1.5 text-right font-medium">数量</th>
                      <th className="px-2 py-1.5 text-right font-medium">成本</th>
                      <th className="px-2 py-1.5 text-right font-medium">现价</th>
                      <th className="px-2 py-1.5 text-right font-medium">盈亏</th>
                      <th className="px-4 py-1.5 text-right font-medium">盈亏%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map(row => (
                      <tr key={row.code} className="border-t border-border/50">
                        <td className="px-4 py-1.5 font-mono text-[11px] text-secondary">{row.code}</td>
                        <td className="px-2 py-1.5 text-foreground">{row.name || '—'}</td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {row.qty ?? '—'}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {fmtPrice(row.cost)}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {fmtPrice(row.price)}
                        </td>
                        <td
                          className={cn(
                            'px-2 py-1.5 text-right font-mono',
                            priceColorClass(row.pnl),
                          )}
                        >
                          {typeof row.pnl === 'number'
                            ? `${row.pnl >= 0 ? '+' : ''}${row.pnl.toFixed(2)}`
                            : '—'}
                        </td>
                        <td
                          className={cn(
                            'px-4 py-1.5 text-right font-mono',
                            priceColorClass(row.pnl_pct),
                          )}
                        >
                          {fmtPct(
                            typeof row.pnl_pct === 'number' ? row.pnl_pct / 100 : null,
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-card border border-border bg-surface/40 px-4 py-3">
                <div className="mb-2 text-[11px] font-medium text-secondary">上次同步结果</div>
                {data.last_result ? (
                  <div className="flex flex-col gap-1.5">
                    <Metric
                      label="结果"
                      value={data.last_result.ok ? '成功' : '有警告'}
                      tone={data.last_result.ok ? 'text-accent' : 'text-warning'}
                    />
                    <Metric label="时间" value={data.last_result.ts} />
                    <Metric label="耗时" value={`${data.last_result.elapsed_ms} ms`} />
                    <Metric label="新增 / 移除" value={`${data.last_result.added.length} / ${data.last_result.removed.length}`} />
                    <Metric label="分组调整" value={`${data.last_result.group_actions.length} 项`} />
                    {data.last_result.errors.length > 0 && (
                      <div className="mt-1 rounded border border-warning/30 bg-warning/[0.06] px-2 py-1 text-[10px] text-warning">
                        {data.last_result.errors.join('；')}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="text-[11px] text-muted">本次启动尚未执行同步</div>
                )}
              </div>

              <div className="rounded-card border border-border bg-surface/40 px-4 py-3">
                <div className="mb-2 text-[11px] font-medium text-secondary">同步机制</div>
                <ul className="flex flex-col gap-1 text-[11px] text-muted">
                  <li>· 自选股：读同花顺远航版本地缓存 SelfStockCache.json 与下单端 selfstock 快照，取并集</li>
                  <li>· 持仓：读 v1 A 股助手 /api/positions，成本含费</li>
                  <li>· 写入：按差集增量更新，只把新增标的置顶，不重排你已有顺序</li>
                  <li>· 分组：自动维护「同花顺自选」「同花顺持仓」两组，标的可同时在两池</li>
                  <li>· 降级：任一侧读不到时沿用上次快照并记警告，绝不误清空自选池</li>
                </ul>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
