import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Group, Panel, Separator } from 'react-resizable-panels'
import { Link } from 'react-router'
import {
  getActivitySummary,
  listActivity,
  type ActivityEvent,
  type ActivitySummary,
} from '../api/activity'
import { login, logout } from '../api/auth'
import { getVaultStatus } from '../api/vault'
import {
  ActionsIcon,
  ActivityIcon,
  FiltersIcon,
  InsightsIcon,
  SignInIcon,
  TransfersIcon,
} from '../uiIcons'

const PAGE_SIZE = 50

const ACTION_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'All actions' },
  { value: 'login', label: 'Login' },
  { value: 'logout', label: 'Logout' },
  { value: 'lock', label: 'Lock' },
  { value: 'upload', label: 'Upload' },
  { value: 'download', label: 'Download' },
  { value: 'download_zip', label: 'Download zip' },
  { value: 'copy', label: 'Copy' },
  { value: 'delete_file', label: 'Delete file' },
  { value: 'delete_dir', label: 'Delete folder' },
  { value: 'mkdir', label: 'Mkdir' },
  { value: 'move', label: 'Move / rename' },
  { value: 'account_connect', label: 'Account connect' },
  { value: 'account_disconnect', label: 'Account disconnect' },
  { value: 'account_evacuate', label: 'Account evacuate' },
  { value: 'account_enable', label: 'Account enable' },
  { value: 'account_disable', label: 'Account disable' },
  { value: 'prune_analyze', label: 'Prune analyze' },
  { value: 'prune', label: 'Prune' },
  { value: 'health', label: 'Health' },
]

type RangeKey = '24h' | '7d' | '30d' | 'all'

const RANGE_OPTIONS: { value: RangeKey; label: string }[] = [
  { value: '24h', label: 'Last 24h' },
  { value: '7d', label: 'Last 7d' },
  { value: '30d', label: 'Last 30d' },
  { value: 'all', label: 'All retained' },
]

function rangeSince(range: RangeKey): number | undefined {
  const now = Math.floor(Date.now() / 1000)
  if (range === '24h') return now - 86400
  if (range === '7d') return now - 7 * 86400
  if (range === '30d') return now - 30 * 86400
  return undefined
}

function formatWhen(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString()
  } catch {
    return String(ts)
  }
}

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function actionLabel(action: string): string {
  return ACTION_OPTIONS.find((o) => o.value === action)?.label ?? action
}

function pct(part: number, total: number): number {
  if (total <= 0) return 0
  return Math.min(100, Math.round((100 * part) / total))
}

function formatPct(part: number, total: number): string {
  return `${pct(part, total)}%`
}

function HistoryRow({ event }: { event: ActivityEvent }) {
  return (
    <div className={`list-row history-row history-row-${event.status}`}>
      <div className="history-row-when list-row-meta">{formatWhen(event.created_at)}</div>
      <div className="history-row-action">{actionLabel(event.action)}</div>
      <div className={`history-status history-status-${event.status}`}>{event.status}</div>
      <div className="history-row-path" title={event.path ?? ''}>
        {event.path || '—'}
      </div>
      <div className="history-row-detail" title={event.detail ?? ''}>
        {event.detail || ''}
      </div>
    </div>
  )
}

function StatusMixBar({
  success,
  errors,
  cancelled,
  total,
}: {
  success: number
  errors: number
  cancelled: number
  total: number
}) {
  if (total <= 0) {
    return <div className="history-mix-bar history-mix-bar-empty" aria-hidden />
  }
  return (
    <div
      className="history-mix-bar"
      role="img"
      aria-label={`Success ${success}, error ${errors}, cancelled ${cancelled}`}
    >
      {success > 0 ? (
        <span className="history-mix-seg is-success" style={{ flex: success }} />
      ) : null}
      {errors > 0 ? (
        <span className="history-mix-seg is-error" style={{ flex: errors }} />
      ) : null}
      {cancelled > 0 ? (
        <span className="history-mix-seg is-cancelled" style={{ flex: cancelled }} />
      ) : null}
    </div>
  )
}

function MeterRow({
  label,
  value,
  share,
  tone = 'data',
  hint,
}: {
  label: string
  value: string
  share: number
  tone?: 'data' | 'success' | 'error' | 'muted'
  hint?: string
}) {
  return (
    <div className="history-meter-row" title={hint}>
      <div className="history-meter-top">
        <span className="history-metric-label">{label}</span>
        <span className={`history-metric-value tone-${tone}`}>{value}</span>
      </div>
      <div className="history-meter-track">
        <div
          className={`history-meter-fill tone-${tone}`}
          style={{ width: `${Math.max(0, Math.min(100, share))}%` }}
        />
      </div>
    </div>
  )
}

function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
  ariaLabel: string
}) {
  return (
    <div className="history-seg" role="group" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value || 'all'}
          type="button"
          className={`history-seg-btn${value === o.value ? ' is-active' : ''}`}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

const STATUS_SEGMENTS: { value: string; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'success', label: 'Ok' },
  { value: 'error', label: 'Err' },
  { value: 'cancelled', label: 'Cancel' },
]

function MetricsPanel({
  summary,
  loading,
  error,
  range,
  action,
  status,
  onRangeChange,
  onActionChange,
  onStatusChange,
  onResetFilters,
}: {
  summary: ActivitySummary | undefined
  loading: boolean
  error: string | null
  range: RangeKey
  action: string
  status: string
  onRangeChange: (value: RangeKey) => void
  onActionChange: (value: string) => void
  onStatusChange: (value: string) => void
  onResetFilters: () => void
}) {
  const total = summary?.events ?? 0
  const success = summary?.by_status.success ?? 0
  const errors = summary?.by_status.error ?? 0
  const cancelled = summary?.by_status.cancelled ?? 0
  const topActions = (summary?.by_action ?? []).slice(0, 8)
  const uploadBytes = summary?.upload_bytes ?? 0
  const downloadBytes = summary?.download_bytes ?? 0
  const evacuatedBytes = summary?.evacuated_bytes ?? 0
  const maxTransfer = Math.max(uploadBytes, downloadBytes, evacuatedBytes, 1)
  const maxAction = Math.max(...topActions.map((r) => r.count), 1)
  const filtersDirty = range !== '7d' || action !== '' || status !== ''

  return (
    <div className="panel-body history-metrics-body">
      <div className="history-metric-block history-filters-block">
        <div className="history-metric-heading history-filters-heading">
          <span className="history-heading-label">
            <FiltersIcon />
            Filters
          </span>
          {filtersDirty ? (
            <button
              type="button"
              className="btn-micro"
              onClick={onResetFilters}
              title="Reset to last 7 days, all actions and statuses"
            >
              Reset
            </button>
          ) : null}
        </div>
        <div className="history-filters-body">
          <label className="history-filter-field">
            <span className="history-filter-label">Range</span>
            <SegmentedControl
              ariaLabel="Time range"
              options={RANGE_OPTIONS}
              value={range}
              onChange={onRangeChange}
            />
          </label>
          <label className="history-filter-field">
            <span className="history-filter-label">Status</span>
            <SegmentedControl
              ariaLabel="Status filter"
              options={STATUS_SEGMENTS}
              value={status}
              onChange={onStatusChange}
            />
          </label>
          <label className="history-filter-field">
            <span className="history-filter-label">Action</span>
            <select
              className="input history-filter-select"
              value={action}
              onChange={(e) => onActionChange(e.target.value)}
              aria-label="Filter by action"
            >
              {ACTION_OPTIONS.map((o) => (
                <option key={o.value || 'all'} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {loading && !summary ? (
        <div className="history-empty">Loading metrics…</div>
      ) : error ? (
        <div className="history-empty history-empty-error">{error}</div>
      ) : (
        <>
          <div className="history-metric-block history-metric-hero">
            <div className="history-hero-label">Events in range</div>
            <div className="history-hero-value">{total}</div>
            <StatusMixBar
              success={success}
              errors={errors}
              cancelled={cancelled}
              total={total}
            />
            <div className="history-hero-legend">
              <span className="history-legend is-success">
                ok <b>{success}</b> · {formatPct(success, total)}
              </span>
              <span className="history-legend is-error">
                err <b>{errors}</b> · {formatPct(errors, total)}
              </span>
              <span className="history-legend is-cancelled">
                cancel <b>{cancelled}</b> · {formatPct(cancelled, total)}
              </span>
            </div>
          </div>

          <div className="history-metric-block">
            <div className="history-metric-heading">
              <span className="history-heading-label">
                <TransfersIcon />
                Transfers
              </span>
            </div>
            <div className="history-meter-list">
              <MeterRow
                label="Uploaded"
                value={formatBytes(uploadBytes)}
                share={(100 * uploadBytes) / maxTransfer}
                hint={`${summary?.upload_ok ?? 0} successful upload(s)`}
              />
              <MeterRow
                label="Downloaded"
                value={formatBytes(downloadBytes)}
                share={(100 * downloadBytes) / maxTransfer}
                hint={`${summary?.download_ok ?? 0} successful download(s)`}
              />
              <MeterRow
                label="Evacuated"
                value={formatBytes(evacuatedBytes)}
                share={(100 * evacuatedBytes) / maxTransfer}
                hint={`${summary?.evacuate_ok ?? 0} successful evacuate(s)`}
              />
            </div>
          </div>

          <div className="history-metric-block">
            <div className="history-metric-heading">
              <span className="history-heading-label">
                <ActionsIcon />
                Top actions
              </span>
            </div>
            {topActions.length === 0 ? (
              <div className="history-empty">No events in range.</div>
            ) : (
              <div className="history-meter-list">
                {topActions.map((row) => (
                  <MeterRow
                    key={row.action}
                    label={actionLabel(row.action)}
                    value={`${row.count} · ${formatPct(row.count, total)}`}
                    share={(100 * row.count) / maxAction}
                    tone="muted"
                  />
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

export default function HistoryPage() {
  const qc = useQueryClient()
  const [username, setUsername] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [action, setAction] = useState('')
  const [status, setStatus] = useState('')
  const [range, setRange] = useState<RangeKey>('7d')

  const since = useMemo(() => rangeSince(range), [range])

  const statusQuery = useQuery({
    queryKey: ['vault', 'status'],
    queryFn: getVaultStatus,
    refetchInterval: 15_000,
  })
  const unlocked = statusQuery.data?.unlocked ?? false

  const filterKey = { action, status, since: since ?? null }

  const summaryQuery = useQuery({
    queryKey: ['activity', 'summary', filterKey],
    enabled: unlocked,
    queryFn: () =>
      getActivitySummary({
        since,
        action: action || undefined,
        status: status || undefined,
      }),
    refetchInterval: 30_000,
  })

  const activityQuery = useInfiniteQuery({
    queryKey: ['activity', 'list', filterKey],
    enabled: unlocked,
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) =>
      listActivity({
        limit: PAGE_SIZE,
        before: pageParam,
        since,
        action: action || undefined,
        status: status || undefined,
      }),
    getNextPageParam: (last) => {
      if (last.events.length < PAGE_SIZE) return undefined
      return last.events[last.events.length - 1]?.created_at
    },
  })

  const loginMut = useMutation({
    mutationFn: () => login(username, passphrase),
    onSuccess: async () => {
      setError(null)
      setPassphrase('')
      await qc.invalidateQueries({ queryKey: ['vault', 'status'] })
      await qc.invalidateQueries({ queryKey: ['activity'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const logoutMut = useMutation({
    mutationFn: logout,
    onSuccess: async () => {
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault', 'status'] })
      await qc.invalidateQueries({ queryKey: ['activity'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const events = useMemo(
    () => activityQuery.data?.pages.flatMap((p) => p.events) ?? [],
    [activityQuery.data],
  )
  const retentionDays =
    summaryQuery.data?.retention_days ?? activityQuery.data?.pages[0]?.retention_days

  if (!unlocked) {
    return (
      <div className="app-shell">
        <header className="top-bar">
          <div className="top-bar-title">Cloud Storage</div>
          <div className="top-bar-status">locked</div>
          <div className="toolbar">
            <Link to="/" className="btn">
              Vault
            </Link>
          </div>
        </header>
        {error ? (
          <div className="error-bar" role="alert">
            {error}
            <button type="button" className="btn" onClick={() => setError(null)}>
              Dismiss
            </button>
          </div>
        ) : null}
        <div className="panel-body" style={{ display: 'grid', placeItems: 'center' }}>
          <div className="modal" style={{ width: 'min(360px, 92vw)' }}>
            <div className="modal-header">
              <SignInIcon />
              Sign in
            </div>
            <div className="modal-body">
              <div>
                <div className="field-label">Username</div>
                <input
                  className="input"
                  type="text"
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoFocus
                />
              </div>
              <div>
                <div className="field-label">Passphrase</div>
                <input
                  className="input"
                  type="password"
                  autoComplete="current-password"
                  value={passphrase}
                  onChange={(e) => setPassphrase(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && username && passphrase) loginMut.mutate()
                  }}
                />
              </div>
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="btn-primary"
                disabled={!username || !passphrase || loginMut.isPending}
                onClick={() => loginMut.mutate()}
              >
                {loginMut.isPending ? 'Signing in…' : 'Sign in'}
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div className="top-bar-title">Cloud Storage</div>
        <div className="top-bar-status">
          history
          {retentionDays != null ? ` · ${retentionDays}d retention` : ''}
        </div>
        <div className="toolbar">
          <Link to="/" className="btn">
            Vault
          </Link>
          <button
            type="button"
            className="btn"
            onClick={() => logoutMut.mutate()}
            disabled={logoutMut.isPending}
          >
            Lock
          </button>
        </div>
      </header>

      {error ? (
        <div className="error-bar">
          <span className="error-bar-text">{error}</span>
          <button
            type="button"
            className="btn-micro btn-danger error-bar-close"
            aria-label="Dismiss error"
            onClick={() => setError(null)}
          >
            ×
          </button>
        </div>
      ) : null}

      <div className="workspace history-workspace">
        <Group orientation="horizontal">
          <Panel id="history-metrics" defaultSize="30" minSize="22" maxSize="48">
            <section className="panel history-panel">
              <div className="panel-header">
                <div className="panel-title">
                  <InsightsIcon />
                  Insights
                </div>
                <div className="panel-header-actions list-row-meta">
                  {RANGE_OPTIONS.find((o) => o.value === range)?.label}
                  {action ? ` · ${actionLabel(action)}` : ''}
                  {status ? ` · ${status}` : ''}
                </div>
              </div>
              <MetricsPanel
                summary={summaryQuery.data}
                loading={summaryQuery.isLoading}
                error={
                  summaryQuery.isError
                    ? (summaryQuery.error as Error).message
                    : null
                }
                range={range}
                action={action}
                status={status}
                onRangeChange={setRange}
                onActionChange={setAction}
                onStatusChange={setStatus}
                onResetFilters={() => {
                  setRange('7d')
                  setAction('')
                  setStatus('')
                }}
              />
            </section>
          </Panel>
          <Separator className="separator" style={{ width: 4 }} />
          <Panel id="history-activity" minSize="40">
            <section className="panel history-panel">
              <div className="panel-header">
                <div className="panel-title">
                  <ActivityIcon />
                  Activity
                </div>
                <div className="panel-header-actions list-row-meta">
                  {events.length > 0 ? `${events.length} shown` : null}
                </div>
              </div>
              <div className="panel-body history-body">
                <div className="history-table-head">
                  <div>When</div>
                  <div>Action</div>
                  <div>Status</div>
                  <div>Path</div>
                  <div>Detail</div>
                </div>
                {activityQuery.isLoading ? (
                  <div className="history-empty">Loading…</div>
                ) : activityQuery.isError ? (
                  <div className="history-empty history-empty-error">
                    {(activityQuery.error as Error).message}
                  </div>
                ) : events.length === 0 ? (
                  <div className="history-empty">No activity in the selected range.</div>
                ) : (
                  events.map((event) => <HistoryRow key={event.id} event={event} />)
                )}
                {activityQuery.hasNextPage ? (
                  <div className="history-more">
                    <button
                      type="button"
                      className="btn"
                      disabled={activityQuery.isFetchingNextPage}
                      onClick={() => void activityQuery.fetchNextPage()}
                    >
                      {activityQuery.isFetchingNextPage ? 'Loading…' : 'Load more'}
                    </button>
                  </div>
                ) : null}
              </div>
            </section>
          </Panel>
        </Group>
      </div>
    </div>
  )
}
