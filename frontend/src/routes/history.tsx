import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { listActivity, type ActivityEvent } from '../api/activity'
import { login, logout } from '../api/auth'
import { getVaultStatus } from '../api/vault'

const PAGE_SIZE = 50

const ACTION_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'All actions' },
  { value: 'login', label: 'Login' },
  { value: 'logout', label: 'Logout' },
  { value: 'lock', label: 'Lock' },
  { value: 'upload', label: 'Upload' },
  { value: 'download', label: 'Download' },
  { value: 'download_zip', label: 'Download zip' },
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

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'success', label: 'Success' },
  { value: 'error', label: 'Error' },
  { value: 'cancelled', label: 'Cancelled' },
]

function formatWhen(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString()
  } catch {
    return String(ts)
  }
}

function actionLabel(action: string): string {
  return ACTION_OPTIONS.find((o) => o.value === action)?.label ?? action
}

function HistoryRow({ event }: { event: ActivityEvent }) {
  return (
    <div className="list-row history-row">
      <div className="history-row-when list-row-meta">{formatWhen(event.created_at)}</div>
      <div className="history-row-action">{actionLabel(event.action)}</div>
      <div className={`history-status history-status-${event.status}`}>{event.status}</div>
      <div className="history-row-path list-row-meta" title={event.path ?? ''}>
        {event.path || '—'}
      </div>
      <div className="history-row-detail" title={event.detail ?? ''}>
        {event.detail || ''}
      </div>
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

  const statusQuery = useQuery({
    queryKey: ['vault', 'status'],
    queryFn: getVaultStatus,
    refetchInterval: 15_000,
  })
  const unlocked = statusQuery.data?.unlocked ?? false

  const activityQuery = useInfiniteQuery({
    queryKey: ['activity', action, status],
    enabled: unlocked,
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) =>
      listActivity({
        limit: PAGE_SIZE,
        before: pageParam,
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
  const retentionDays = activityQuery.data?.pages[0]?.retention_days

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
            <div className="modal-header">Sign in</div>
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
        <section className="panel history-panel">
          <div className="panel-header">
            <div className="panel-title">Activity</div>
            <div className="panel-header-actions history-filters">
              <select
                className="input history-filter"
                value={action}
                onChange={(e) => setAction(e.target.value)}
                aria-label="Filter by action"
              >
                {ACTION_OPTIONS.map((o) => (
                  <option key={o.value || 'all'} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <select
                className="input history-filter"
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                aria-label="Filter by status"
              >
                {STATUS_OPTIONS.map((o) => (
                  <option key={o.value || 'all'} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
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
              <div className="history-empty">No activity in the retention window.</div>
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
      </div>
    </div>
  )
}
