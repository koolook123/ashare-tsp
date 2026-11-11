import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  Cpu,
  Database,
  HardDriveDownload,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
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
  avail?: number | null
  cost?: number | null
  price?: number | null
  market_value?: number | null
  pnl?: number | null
  pnl_pct?: number | null
}

interface BlockWrite {
  name: string
  before: number
  after: number
  changed: boolean
  ok?: boolean
  dry?: boolean
  queued?: boolean
  need_restart?: boolean
}

interface SelfstockWrite {
  ok?: boolean
  changed?: boolean
  before: number
  after: number
  need_restart?: boolean
  written?: number
  dry?: boolean
  error?: string
}

interface SyncResult {
  ok: boolean
  dry: boolean
  ts: string
  added: string[]
  removed: string[]
  group_actions: string[]
  errors: string[]
  skipped: string[]
  elapsed_ms: number
  selfstock: number
  positions: number
  tsp_watchlist: number
  block: BlockWrite | null
  selfstock_write: SelfstockWrite | null
}

interface GroupRow {
  id: string
  name: string
  color?: string
  count: number
}

interface Status {
  ok: boolean
  engine: {
    mode: string
    version: number
    thread_alive: boolean
    started_at: string
    sync_count: number
    last_sync: string
    last_error: string
  }
  config: {
    auto: boolean
    interval: number
    sync_ths_to_tsp: boolean
    sync_tsp_to_ths: boolean
    sync_selfstock: boolean
    sync_positions: boolean
    block_name: string
    write_ths_selfstock: boolean
  }
  ths: {
    root: string
    user: string
    user_dir: string
    selfstock_ok: boolean
    selfstock_count: number
    agent_ok: boolean
    positions_ok: boolean
    positions_count: number
    block_name: string
    block_count: number | null
    block_ok: boolean
    block_volatile: boolean
    block_volatile_note: string
    selfstock_writable: boolean
    ths_written_count: number
  }
  pending: {
    block: number
    block_name: string
    ts: string
  }
  tsp: {
    watchlist_count: number
    synced_count: number
    native_count: number
    groups: GroupRow[]
  }
  positions: PositionRow[]
  position_pnl_total: number
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

/** 手动一键同步持仓：唯一会连 17660 代理的入口，自动轮询不碰它。 */
async function postSyncPositions(): Promise<SyncResult> {
  const res = await fetch(`${API}/sync/positions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!res.ok) throw new Error(`持仓同步失败 (${res.status})`)
  return res.json()
}

async function putConfig(patch: Record<string, unknown>): Promise<void> {
  const res = await fetch(`${API}/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error(`保存设置失败 (${res.status})`)
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
      <span className="shrink-0 text-[11px] text-muted">{label}</span>
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
      if (res.block) {
        parts.push(`板块 ${res.block.before}→${res.block.after}`)
      }
      if (res.selfstock_write) {
        parts.push(`写自选 ${res.selfstock_write.before}→${res.selfstock_write.after}`)
      }
      setToast(
        res.ok ? `同步完成 · ${parts.join(' / ')}` : `同步有警告 · ${res.errors.join('；')}`,
      )
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

  const posMutation = useMutation({
    mutationFn: postSyncPositions,
    onSuccess: (res) => {
      const n = res.positions
      setToast(
        res.ok
          ? `持仓同步完成 · ${n} 只 · 新增 ${res.added.length} / 移除 ${res.removed.length}`
          : `持仓同步有警告 · ${res.errors.join('；')}`,
      )
      void qc.invalidateQueries({ queryKey: ['ths-sync-status'] })
      void qc.invalidateQueries({ queryKey: ['watchlist'] })
      void qc.invalidateQueries({ queryKey: ['watchlist-groups'] })
      window.setTimeout(() => setToast(''), 4000)
    },
    onError: (err: unknown) => {
      setToast(err instanceof Error ? err.message : '持仓同步失败')
      window.setTimeout(() => setToast(''), 4000)
    },
  })

  const cfgMutation = useMutation({
    mutationFn: putConfig,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['ths-sync-status'] })
    },
    onError: (err: unknown) => {
      setToast(err instanceof Error ? err.message : '保存设置失败')
      window.setTimeout(() => setToast(''), 4000)
    },
  })

  const resetToast = useCallback(() => setToast(''), [])

  const data = statusQuery.data
  const loading = statusQuery.isLoading && !data
  const failed = statusQuery.isError && !data
  const positions = data?.positions ?? []
  const pnlTotal = data?.position_pnl_total ?? 0

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="同花顺同步"
        subtitle="同花顺自选自动汇入 TSP；持仓手动一键 · 进程内直读直写，不模拟界面、不抢焦点"
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
              disabled={posMutation.isPending}
              onClick={() => posMutation.mutate()}
              title="读同花顺持仓（g_position）并刷新「同花顺持仓」分组；自动轮询不做这件事"
              className={cn(
                'inline-flex items-center gap-1 rounded-btn border border-accent/40 bg-accent/[0.08] px-2.5 py-1.5 text-[11px] font-medium text-accent transition-opacity',
                posMutation.isPending ? 'opacity-60' : 'hover:opacity-90',
              )}
            >
              {posMutation.isPending ? (
                <RotateCcw className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <HardDriveDownload className="h-3.5 w-3.5" />
              )}
              一键同步持仓
            </button>
            <button
              type="button"
              disabled={syncMutation.isPending}
              onClick={() => syncMutation.mutate()}
              title="自选双向同步 + 按开关写回同花顺；不含持仓"
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
              立即同步自选
            </button>
          </div>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {toast && (
          <div className="mb-3 flex items-center gap-2 rounded-card border border-accent/30 bg-accent/[0.06] px-3 py-2 text-[11px] text-accent">
            <Check className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1">{toast}</span>
            <button
              type="button"
              onClick={resetToast}
              className="ml-auto shrink-0 text-muted hover:text-foreground"
            >
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
              <StatCard title="同步引擎" icon={<Cpu className="h-3.5 w-3.5" />}>
                <Metric
                  label="运行模式"
                  value={data.config.auto ? `自动 · ${data.config.interval}s` : '仅手动'}
                  tone={data.config.auto ? 'text-accent' : 'text-warning'}
                />
                <Metric
                  label="后台线程"
                  value={data.engine.thread_alive ? '运行中' : '未启动'}
                  tone={data.engine.thread_alive ? 'text-accent' : 'text-warning'}
                />
                <Metric label="累计同步" value={`${data.engine.sync_count} 次`} />
                <Metric label="上次同步" value={data.engine.last_sync || '—'} />
                <Metric label="执行位置" value={`${data.engine.mode} v${data.engine.version}`} />
              </StatCard>

              <StatCard title="TSP 自选池" icon={<Database className="h-3.5 w-3.5" />}>
                <Metric label="池内标的" value={`${data.tsp.watchlist_count} 只`} />
                <Metric label="同步而来" value={`${data.tsp.synced_count} 只`} />
                <Metric label="你自己挑的" value={`${data.tsp.native_count} 只`} tone="text-accent" />
                <Metric
                  label="分组"
                  value={
                    data.tsp.groups.length
                      ? data.tsp.groups.map(g => `${g.name}(${g.count})`).join(' / ')
                      : '—'
                  }
                />
              </StatCard>

              <StatCard title="同花顺侧" icon={<HardDriveDownload className="h-3.5 w-3.5" />}>
                <Metric
                  label="自选缓存"
                  value={
                    data.ths.selfstock_ok ? `${data.ths.selfstock_count} 只` : '读不到'
                  }
                  tone={data.ths.selfstock_ok ? undefined : 'text-warning'}
                />
                <Metric
                  label="持仓代理"
                  value={data.ths.agent_ok ? `${data.ths.positions_count} 只` : '未运行'}
                  tone={data.ths.agent_ok ? undefined : 'text-warning'}
                />
                <Metric
                  label={`板块 ${data.ths.block_name}`}
                  value={
                    data.ths.block_ok ? `${data.ths.block_count ?? 0} 只` : '读不到'
                  }
                />
                <Metric
                  label="待写入板块"
                  value={
                    data.pending.block > 0
                      ? `${data.pending.block} 只待落盘`
                      : '无排队'
                  }
                  tone={data.pending.block > 0 ? 'text-warning' : undefined}
                />
                <Metric label="同花顺客户端" value={data.ths.block_volatile ? '运行中' : '已关闭'} />
                <Metric label="账号" value={data.ths.user || '—'} />
              </StatCard>
            </div>

            {/* 真实状态提示：板块写入易失、自选写入需重启、持仓需代理 */}
            <div className="flex flex-col gap-2 rounded-card border border-warning/30 bg-warning/[0.05] px-4 py-3">
              <div className="flex items-center gap-1.5 text-[11px] font-medium text-warning">
                <ShieldAlert className="h-3.5 w-3.5" />
                四条通道的真实边界（实测结论，不是猜测）
              </div>
              <ul className="flex flex-col gap-1 text-[11px] leading-relaxed text-muted">
                <li>
                  · <span className="text-secondary">自选股 → TSP</span>：读本地
                  SelfStockCache.json，稳定可用，跟随「自动同步」开关每轮自动跑。
                </li>
                <li>
                  · <span className="text-secondary">持仓 → TSP</span>：磁盘上没有任何持仓快照，
                  只能由同花顺进程内代理（127.0.0.1:17660）提供。
                  <span className="text-warning">本版已取消自动持仓同步</span>，
                  改为右上角「一键同步持仓」手动触发；代理未运行时沿用上次快照，绝不清空。
                  {!data.ths.agent_ok && (
                    <span className="text-warning">
                      {' '}当前代理未运行——到同花顺「我的策略」里运行 ths_agent.py 即可常驻。
                    </span>
                  )}
                </li>
                <li>
                  · <span className="text-secondary">TSP → 同花顺自选</span>（主通道）：
                  写 SelfStockCache.json，运行期<span className="text-accent">不会被覆写</span>，
                  但要<span className="text-warning">重启同花顺才被读入</span>。
                  默认开启；为避免清空你手维护的自选，写入集合 = 同花顺自选分组成员 ∪ 你自己挑的。
                </li>
                <li>
                  · <span className="text-secondary">TSP → 板块 {data.ths.block_name}</span>
                  （默认关闭）：运行期间写文件会被内存状态覆盖（实测 104 秒被刷回），
                  该板块还是服务端托管块，云端同步可能整块覆盖。既然写不进去，本版不再拿它当主通道。
                  {data.ths.block_volatile_note && (
                    <span className="text-muted"> {data.ths.block_volatile_note}。</span>
                  )}
                </li>
              </ul>
            </div>

            <div className="overflow-hidden rounded-card border border-border bg-surface/40">
              <div className="flex items-center justify-between border-b border-border px-4 py-2">
                <span className="text-[11px] font-medium text-secondary">持仓明细</span>
                <span className="text-[10px] text-muted">
                  成本为同花顺成交回报成本价（cbj，含费）
                </span>
              </div>
              {positions.length === 0 ? (
                <div className="py-10 text-center text-[11px] text-muted">
                  当前无持仓，或同花顺持仓代理未运行（读不到时沿用上次快照，不会清空自选池）
                </div>
              ) : (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-[10px] text-muted">
                      <th className="px-4 py-1.5 text-left font-medium">代码</th>
                      <th className="px-2 py-1.5 text-left font-medium">名称</th>
                      <th className="px-2 py-1.5 text-right font-medium">数量</th>
                      <th className="px-2 py-1.5 text-right font-medium">可用</th>
                      <th className="px-2 py-1.5 text-right font-medium">成本</th>
                      <th className="px-2 py-1.5 text-right font-medium">现价</th>
                      <th className="px-2 py-1.5 text-right font-medium">市值</th>
                      <th className="px-2 py-1.5 text-right font-medium">盈亏</th>
                      <th className="px-4 py-1.5 text-right font-medium">盈亏%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map(row => (
                      <tr key={row.code} className="border-t border-border/50">
                        <td className="px-4 py-1.5 font-mono text-[11px] text-secondary">
                          {row.code}
                        </td>
                        <td className="px-2 py-1.5 text-foreground">{row.name || '—'}</td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {row.qty ?? '—'}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {row.avail ?? '—'}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {fmtPrice(row.cost)}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {fmtPrice(row.price)}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">
                          {typeof row.market_value === 'number'
                            ? row.market_value.toFixed(2)
                            : '—'}
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
                          {fmtPct(typeof row.pnl_pct === 'number' ? row.pnl_pct / 100 : null)}
                        </td>
                      </tr>
                    ))}
                    <tr className="border-t border-border bg-surface/60">
                      <td className="px-4 py-1.5 text-[11px] text-muted" colSpan={7}>
                        合计
                      </td>
                      <td
                        className={cn(
                          'px-2 py-1.5 text-right font-mono text-[11px]',
                          priceColorClass(pnlTotal),
                        )}
                      >
                        {`${pnlTotal >= 0 ? '+' : ''}${pnlTotal.toFixed(2)}`}
                      </td>
                      <td className="px-4 py-1.5" />
                    </tr>
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
                    <Metric
                      label="新增 / 移除"
                      value={`${data.last_result.added.length} / ${data.last_result.removed.length}`}
                    />
                    <Metric
                      label="分组调整"
                      value={`${data.last_result.group_actions.length} 项`}
                    />
                    {data.last_result.block && (
                      <Metric
                        label={`板块 ${data.last_result.block.name}`}
                        value={
                          data.last_result.block.queued
                            ? `已排队 ${data.last_result.block.after} 只（关闭同花顺后落盘）`
                            : `${data.last_result.block.before} → ${data.last_result.block.after}`
                        }
                        tone={
                          data.last_result.block.changed || data.last_result.block.queued
                            ? 'text-accent'
                            : undefined
                        }
                      />
                    )}
                    {data.last_result.selfstock_write && (
                      <Metric
                        label="写同花顺自选"
                        value={
                          data.last_result.selfstock_write.dry
                            ? `预演 ${data.last_result.selfstock_write.after} 只`
                            : `${data.last_result.selfstock_write.before} → ${data.last_result.selfstock_write.after}${
                                data.last_result.selfstock_write.need_restart ? '（重启生效）' : ''
                              }`
                        }
                        tone={
                          data.last_result.selfstock_write.changed ? 'text-accent' : undefined
                        }
                      />
                    )}
                    {data.last_result.skipped.length > 0 && (
                      <div className="mt-1 rounded border border-border bg-surface/60 px-2 py-1 text-[10px] text-muted">
                        {data.last_result.skipped.join('；')}
                      </div>
                    )}
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
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-[11px] font-medium text-secondary">同步开关</span>
                  <span className="text-[10px] text-muted">
                    {data.config.auto ? `自动运行中 · 每 ${data.config.interval}s` : '自动已关闭'}
                  </span>
                </div>

                {/* 主开关：自动同步。BOSS 要的就是这个能随手关掉 */}
                <label
                  className={cn(
                    'mb-2 flex cursor-pointer items-center justify-between gap-3 rounded-btn border px-3 py-2 transition-colors',
                    data.config.auto
                      ? 'border-accent/40 bg-accent/[0.06]'
                      : 'border-border bg-surface/60',
                  )}
                >
                  <span className="min-w-0">
                    <span
                      className={cn(
                        'text-[12px] font-medium',
                        data.config.auto ? 'text-accent' : 'text-secondary',
                      )}
                    >
                      自动同步
                    </span>
                    <span className="ml-2 text-[10px] text-muted">
                      关掉后只在点按钮时同步
                    </span>
                  </span>
                  <input
                    type="checkbox"
                    checked={data.config.auto}
                    disabled={cfgMutation.isPending}
                    onChange={e => cfgMutation.mutate({ auto: e.target.checked })}
                    className="h-4 w-4 shrink-0 accent-[var(--accent)]"
                  />
                </label>

                <div className="flex flex-col gap-2">
                  {[
                    { key: 'sync_selfstock', label: '同花顺自选 → TSP', hint: '读本地缓存 · 自动' },
                    {
                      key: 'write_ths_selfstock',
                      label: 'TSP → 同花顺自选',
                      hint: '主通道 · 需重启同花顺生效',
                    },
                    {
                      key: 'sync_tsp_to_ths',
                      label: 'TSP → 同花顺板块',
                      hint: '写不进去，默认关闭',
                    },
                  ].map(item => {
                    const on = Boolean(
                      (data.config as unknown as Record<string, unknown>)[item.key],
                    )
                    return (
                      <label
                        key={item.key}
                        className="flex cursor-pointer items-center justify-between gap-2"
                      >
                        <span className="min-w-0">
                          <span className="text-[11px] text-secondary">{item.label}</span>
                          <span className="ml-2 text-[10px] text-muted">{item.hint}</span>
                        </span>
                        <input
                          type="checkbox"
                          checked={on}
                          disabled={cfgMutation.isPending}
                          onChange={e => cfgMutation.mutate({ [item.key]: e.target.checked })}
                          className="h-3.5 w-3.5 shrink-0 accent-[var(--accent)]"
                        />
                      </label>
                    )
                  })}

                  {/* 持仓不是开关，是手动按钮 */}
                  <div className="flex items-center justify-between gap-2 border-t border-border/60 pt-2">
                    <span className="min-w-0">
                      <span className="text-[11px] text-secondary">同花顺持仓 → TSP</span>
                      <span className="ml-2 text-[10px] text-muted">
                        手动一键 · 自动已取消
                      </span>
                    </span>
                    <button
                      type="button"
                      disabled={posMutation.isPending}
                      onClick={() => posMutation.mutate()}
                      className="shrink-0 rounded-btn border border-accent/40 px-2 py-0.5 text-[10px] text-accent transition-opacity hover:opacity-80 disabled:opacity-50"
                    >
                      {posMutation.isPending ? '同步中…' : '立即同步持仓'}
                    </button>
                  </div>
                </div>
                <div className="mt-2 border-t border-border/60 pt-2 text-[10px] text-muted">
                  「自动同步」只跑自选（纯读本地文件，几十毫秒）；持仓要连本机
                  17660 代理，代理没起时会走满超时，所以本版改成手动一键，自动轮询不再碰它。
                  <br />
                  「TSP → 同花顺自选」是写回主通道：SelfStockCache.json 运行期不被覆写，
                  写入集合 = 同花顺自选分组成员 ∪ 你自己挑的标的，两条硬保护（入向关闭时跳过、
                  目标集为空时跳过）避免把你在同花顺里的自选整份抹掉。
                  <br />
                  「TSP → 同花顺板块」默认关闭：实测运行期写入 104 秒后被客户端内存态刷回，
                  且 ai选股 是服务端托管块，写它没有可用窗口。
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
