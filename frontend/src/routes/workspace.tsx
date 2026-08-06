import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useSearchParams } from 'react-router'
import { Group, Panel, Separator } from 'react-resizable-panels'
import {
  connectMega,
  deleteAccount,
  getStorageStats,
  listAccounts,
  startGoogleOAuth,
  type Account,
  type AccountStorage,
} from '../api/accounts'
import {
  deleteVaultFile,
  downloadVaultFile,
  getVaultFile,
  getVaultStatus,
  listVaultFiles,
  lockVault,
  unlockVault,
  uploadVaultFile,
  type ProgressUpdate,
  type UploadAccountStats,
  type UploadJobStats,
  type VaultEntry,
} from '../api/vault'

const RATE_WINDOW_MS = 60_000
const ERROR_AUTO_HIDE_MS = 60_000

type RateSample = { t: number; shards: number; bytes: number }

type AccountRate = { shardsPerSec: number; mbPerSec: number }

type TransferState = {
  kind: 'upload' | 'download' | 'delete'
  percent: number
  message: string
  doneBytes?: number | null
  totalBytes?: number | null
  doneShards?: number
  expectedShards?: number | null
  activeShards?: number
  shardsPerSec?: number | null
  mbPerSec?: number | null
  byAccount?: UploadAccountStats[]
  accountRates?: Record<string, AccountRate>
}

function formatBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function formatRate(n: number | null | undefined, unit: string): string {
  if (n == null || !Number.isFinite(n)) return `— ${unit}`
  if (n < 10) return `${n.toFixed(2)} ${unit}`
  return `${n.toFixed(1)} ${unit}`
}

function storagePct(used: number | null, total: number | null): number | null {
  if (used == null || total == null || total <= 0) return null
  return Math.min(100, Math.round((100 * used) / total))
}

function pathParts(path: string): string[] {
  if (!path || path === '/') return []
  return path.replace(/^\//, '').split('/').filter(Boolean)
}

function rateFromSamples(samples: RateSample[]): AccountRate | null {
  if (samples.length < 2) return null
  const first = samples[0]
  const last = samples[samples.length - 1]
  const dt = (last.t - first.t) / 1000
  if (dt < 0.5) return null
  const dShards = Math.max(0, last.shards - first.shards)
  const dBytes = Math.max(0, last.bytes - first.bytes)
  return {
    shardsPerSec: dShards / dt,
    mbPerSec: dBytes / (1024 * 1024) / dt,
  }
}

function pruneSamples(samples: RateSample[], now: number): RateSample[] {
  const cutoff = now - RATE_WINDOW_MS
  let i = 0
  while (i < samples.length && samples[i].t < cutoff) i++
  return samples.slice(i)
}

function pushSample(samples: RateSample[], shards: number, bytes: number, now: number): RateSample[] {
  // Always return a new array so callers can replace refs without clearing in place.
  const next = pruneSamples(samples, now)
  const last = next[next.length - 1]
  if (last && last.shards === shards && last.bytes === bytes && now - last.t < 250) {
    return next
  }
  next.push({ t: now, shards, bytes })
  return next
}

function applyTransferRates(
  stats: UploadJobStats,
  overallRef: { current: RateSample[] },
  byAccount: Map<string, RateSample[]>,
): Pick<
  TransferState,
  | 'shardsPerSec'
  | 'mbPerSec'
  | 'byAccount'
  | 'accountRates'
  | 'doneBytes'
  | 'totalBytes'
  | 'doneShards'
  | 'expectedShards'
  | 'activeShards'
> {
  const now = Date.now()
  overallRef.current = pushSample(
    overallRef.current,
    stats.done_shards,
    stats.done_bytes,
    now,
  )
  const overallRate = rateFromSamples(overallRef.current)

  const accountRates: Record<string, AccountRate> = {}
  for (const row of stats.by_account) {
    const next = pushSample(byAccount.get(row.account_id) ?? [], row.shards, row.bytes, now)
    byAccount.set(row.account_id, next)
    const rate = rateFromSamples(next)
    if (rate) accountRates[row.account_id] = rate
  }

  const activeShards =
    stats.active_shards ??
    stats.by_account.reduce((sum, row) => sum + (row.active ?? 0), 0)

  return {
    doneBytes: stats.done_bytes,
    totalBytes: stats.total_bytes,
    doneShards: stats.done_shards,
    expectedShards: stats.expected_shards,
    activeShards,
    shardsPerSec: overallRate?.shardsPerSec ?? null,
    mbPerSec: overallRate?.mbPerSec ?? null,
    byAccount: stats.by_account,
    accountRates,
  }
}

function batchProgress(
  index: number,
  total: number,
  update: ProgressUpdate,
): { percent: number; message: string } {
  const span = 100 / total
  const percent = Math.min(100, Math.round(index * span + (update.percent / 100) * span))
  const message = total > 1 ? `${index + 1}/${total}: ${update.message}` : update.message
  return { percent, message }
}

function ShardBoxesBar({
  done,
  active = 0,
  total,
  variant = 'transfer',
}: {
  done: number
  /** Boxes after `done` that are in-flight on this provider. */
  active?: number
  total: number
  variant?: 'transfer' | 'account'
}) {
  const filled = Math.max(0, Math.floor(done))
  const inFlight = Math.max(0, Math.floor(active))
  const n = Math.max(1, Math.floor(total), filled + inFlight)
  const prevFilledRef = useRef(filled)
  const [pulseFrom, setPulseFrom] = useState<number | null>(null)

  useEffect(() => {
    const prev = prevFilledRef.current
    prevFilledRef.current = filled
    if (filled <= prev) {
      setPulseFrom(null)
      return
    }
    setPulseFrom(prev)
    const t = window.setTimeout(() => setPulseFrom(null), 700)
    return () => window.clearTimeout(t)
  }, [filled])

  return (
    <div
      className={`shard-boxes shard-boxes-${variant}`}
      role="progressbar"
      aria-valuenow={filled}
      aria-valuemin={0}
      aria-valuemax={n}
      title={`${filled} done · ${inFlight} active / ${n} shards`}
    >
      {Array.from({ length: n }, (_, i) => {
        const isDone = i < filled
        const isActive = !isDone && i < filled + inFlight
        const isPulse = isDone && pulseFrom != null && i >= pulseFrom
        return (
          <span
            key={i}
            className={[
              'shard-box',
              isDone ? 'shard-box-done' : '',
              isActive ? 'shard-box-active' : '',
              isPulse ? 'shard-box-pulse' : '',
            ]
              .filter(Boolean)
              .join(' ')}
          />
        )
      })}
    </div>
  )
}

export default function Workspace() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [path, setPath] = useState('/')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [anchorId, setAnchorId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [megaOpen, setMegaOpen] = useState(false)
  const [unlockOpen, setUnlockOpen] = useState(false)
  const [megaEmail, setMegaEmail] = useState('')
  const [megaPassword, setMegaPassword] = useState('')
  const [megaMfa, setMegaMfa] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [transfer, setTransfer] = useState<TransferState | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const uploadAbortRef = useRef<AbortController | null>(null)
  const downloadAbortRef = useRef<AbortController | null>(null)
  const transferRateSamplesRef = useRef<RateSample[]>([])
  const transferAccountRateSamplesRef = useRef<Map<string, RateSample[]>>(new Map())

  function resetTransferRates() {
    transferRateSamplesRef.current = []
    transferAccountRateSamplesRef.current = new Map()
  }

  function onShardTransferProgress(
    kind: 'upload' | 'download',
    index: number,
    total: number,
    update: ProgressUpdate,
  ) {
    const mapped = batchProgress(index, total, update)
    if (!update.stats) {
      setTransfer({ kind, ...mapped })
      return
    }
    const rates = applyTransferRates(
      update.stats,
      transferRateSamplesRef,
      transferAccountRateSamplesRef.current,
    )
    setTransfer({ kind, ...mapped, ...rates })
  }

  const accountsQuery = useQuery({
    queryKey: ['accounts'],
    queryFn: listAccounts,
  })

  const storageQuery = useQuery({
    queryKey: ['accounts', 'storage'],
    queryFn: getStorageStats,
    enabled: (accountsQuery.data?.length ?? 0) > 0,
    staleTime: 60_000,
  })

  const storageById = useMemo(() => {
    const map = new Map<string, AccountStorage>()
    for (const row of storageQuery.data?.accounts ?? []) map.set(row.id, row)
    return map
  }, [storageQuery.data])

  const statusQuery = useQuery({
    queryKey: ['vault', 'status'],
    queryFn: getVaultStatus,
  })

  const unlocked = statusQuery.data?.unlocked ?? false

  const filesQuery = useQuery({
    queryKey: ['vault', 'files', path],
    queryFn: () => listVaultFiles(path),
    enabled: unlocked,
  })

  const entries = filesQuery.data
  const selectedEntries = useMemo(() => {
    if (!entries) return []
    const byId = new Map(entries.map((e) => [e.id, e]))
    return selectedIds.flatMap((id) => {
      const entry = byId.get(id)
      return entry ? [entry] : []
    })
  }, [entries, selectedIds])
  const selectedFiles = useMemo(
    () => selectedEntries.filter((e) => e.kind === 'file'),
    [selectedEntries],
  )
  const fileEntries = useMemo(
    () => (entries ?? []).filter((e) => e.kind === 'file'),
    [entries],
  )
  const allFilesSelected =
    fileEntries.length > 0 && fileEntries.every((e) => selectedIds.includes(e.id))
  const inspectTarget = selectedFiles.length === 1 ? selectedFiles[0] : null

  const detailQuery = useQuery({
    queryKey: ['vault', 'file', inspectTarget?.id],
    queryFn: () => getVaultFile(inspectTarget!.id),
    enabled: unlocked && !!inspectTarget,
  })

  useEffect(() => {
    if (searchParams.get('connected') === 'gdrive') {
      void qc.invalidateQueries({ queryKey: ['accounts'] })
      void qc.invalidateQueries({ queryKey: ['vault', 'status'] })
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, setSearchParams, qc])

  useEffect(() => {
    setSelectedIds([])
    setAnchorId(null)
  }, [path])

  const crumbs = useMemo(() => pathParts(path), [path])

  const unlockMut = useMutation({
    mutationFn: unlockVault,
    onSuccess: async () => {
      setUnlockOpen(false)
      setPassphrase('')
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const lockMut = useMutation({
    mutationFn: lockVault,
    onSuccess: async () => {
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const megaMut = useMutation({
    mutationFn: () => connectMega(megaEmail, megaPassword, megaMfa),
    onSuccess: async () => {
      setMegaOpen(false)
      setMegaEmail('')
      setMegaPassword('')
      setMegaMfa('')
      setError(null)
      await qc.invalidateQueries({ queryKey: ['accounts'] })
      await qc.invalidateQueries({ queryKey: ['vault', 'status'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const deleteAccountMut = useMutation({
    mutationFn: deleteAccount,
    onSuccess: async () => {
      setError(null)
      await qc.invalidateQueries({ queryKey: ['accounts'] })
      await qc.invalidateQueries({ queryKey: ['vault', 'status'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const uploadMut = useMutation({
    mutationFn: async (files: File[]) => {
      const ac = new AbortController()
      uploadAbortRef.current = ac
      resetTransferRates()
      try {
        for (let i = 0; i < files.length; i++) {
          if (ac.signal.aborted) throw new Error('Upload cancelled')
          resetTransferRates()
          await uploadVaultFile(
            path,
            files[i],
            (u) => onShardTransferProgress('upload', i, files.length, u),
            { signal: ac.signal },
          )
        }
      } finally {
        uploadAbortRef.current = null
        resetTransferRates()
      }
    },
    onSuccess: async () => {
      setTransfer(null)
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
      await qc.invalidateQueries({ queryKey: ['accounts', 'storage'] })
    },
    onError: (e: Error) => {
      setTransfer(null)
      setError(e.message)
    },
  })

  useEffect(() => {
    if (!error) return
    const t = window.setTimeout(() => setError(null), ERROR_AUTO_HIDE_MS)
    return () => window.clearTimeout(t)
  }, [error])

  const downloadMut = useMutation({
    mutationFn: async (files: VaultEntry[]) => {
      const ac = new AbortController()
      downloadAbortRef.current = ac
      resetTransferRates()
      try {
        for (let i = 0; i < files.length; i++) {
          if (ac.signal.aborted) throw new Error('Download cancelled')
          resetTransferRates()
          const entry = files[i]
          await downloadVaultFile(
            entry.id,
            entry.name,
            (u) => onShardTransferProgress('download', i, files.length, u),
            { signal: ac.signal },
          )
        }
      } finally {
        downloadAbortRef.current = null
        resetTransferRates()
      }
    },
    onSuccess: () => setTransfer(null),
    onError: (e: Error) => {
      setTransfer(null)
      setError(e.message)
    },
  })

  useEffect(() => {
    if (!uploadMut.isPending && !downloadMut.isPending) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      // Chrome requires returnValue to be set to show the confirmation dialog.
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [uploadMut.isPending, downloadMut.isPending])

  const deleteFileMut = useMutation({
    mutationFn: async (files: VaultEntry[]) => {
      for (let i = 0; i < files.length; i++) {
        await deleteVaultFile(files[i].id, (u) => {
          const mapped = batchProgress(i, files.length, u)
          setTransfer({ kind: 'delete', ...mapped })
        })
      }
    },
    onSuccess: async () => {
      setTransfer(null)
      setSelectedIds([])
      setAnchorId(null)
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
      await qc.invalidateQueries({ queryKey: ['accounts', 'storage'] })
    },
    onError: (e: Error) => {
      setTransfer(null)
      setError(e.message)
    },
  })

  const busy = uploadMut.isPending || downloadMut.isPending || deleteFileMut.isPending

  const statusText = unlocked
    ? `unlocked · ${statusQuery.data?.file_count ?? 0} files · ${statusQuery.data?.account_count ?? 0} accounts`
    : statusQuery.data?.initialized
      ? 'locked'
      : 'locked · new vault'

  function openDir(entry: VaultEntry) {
    if (entry.kind !== 'dir') return
    setPath(entry.path)
  }

  function goCrumb(index: number) {
    if (index < 0) {
      setPath('/')
      return
    }
    setPath('/' + crumbs.slice(0, index + 1).join('/'))
  }

  function selectEntry(entry: VaultEntry, e: MouseEvent) {
    const list = entries ?? []
    if (e.shiftKey && anchorId) {
      const i1 = list.findIndex((x) => x.id === anchorId)
      const i2 = list.findIndex((x) => x.id === entry.id)
      if (i1 >= 0 && i2 >= 0) {
        const [a, b] = i1 < i2 ? [i1, i2] : [i2, i1]
        setSelectedIds(list.slice(a, b + 1).map((x) => x.id))
        return
      }
    }
    if (e.ctrlKey || e.metaKey) {
      setSelectedIds((prev) =>
        prev.includes(entry.id) ? prev.filter((id) => id !== entry.id) : [...prev, entry.id],
      )
      setAnchorId(entry.id)
      return
    }
    setSelectedIds([entry.id])
    setAnchorId(entry.id)
  }

  function toggleFileChecked(entry: VaultEntry, checked: boolean) {
    if (entry.kind !== 'file') return
    setSelectedIds((prev) => {
      if (checked) return prev.includes(entry.id) ? prev : [...prev, entry.id]
      return prev.filter((id) => id !== entry.id)
    })
    setAnchorId(entry.id)
  }

  function toggleSelectAllFiles() {
    if (allFilesSelected) {
      setSelectedIds((prev) => prev.filter((id) => !fileEntries.some((e) => e.id === id)))
      return
    }
    setSelectedIds((prev) => {
      const next = new Set(prev)
      for (const e of fileEntries) next.add(e.id)
      return [...next]
    })
  }

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div className="top-bar-title">Cloud Storage</div>
        <div className="top-bar-status">{statusText}</div>
        <div className="toolbar">
          {unlocked ? (
            <button
              type="button"
              className="btn"
              onClick={() => lockMut.mutate()}
              disabled={lockMut.isPending}
            >
              Lock
            </button>
          ) : (
            <button type="button" className="btn-primary" onClick={() => setUnlockOpen(true)}>
              Unlock
            </button>
          )}
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

      {transfer ? (
        <div className="transfer-bar">
          <div className="transfer-meta">
            <span className="transfer-kind">
              {transfer.kind === 'upload'
                ? 'Upload'
                : transfer.kind === 'download'
                  ? 'Download'
                  : 'Delete'}
            </span>
            <span className="transfer-msg">{transfer.message}</span>
            <span className="transfer-pct">{transfer.percent}%</span>
            {(transfer.kind === 'upload' || transfer.kind === 'download') &&
            transfer.doneShards != null ? (
              <span className="transfer-stats" title="Shards done / expected · active">
                {transfer.doneShards}
                {transfer.expectedShards != null ? `/${transfer.expectedShards}` : ''} sh
                {(transfer.activeShards ?? 0) > 0 ? (
                  <span className="transfer-active"> · {transfer.activeShards} active</span>
                ) : null}
              </span>
            ) : null}
            {(transfer.kind === 'upload' || transfer.kind === 'download') &&
            transfer.doneBytes != null ? (
              <span className="transfer-stats" title="Transferred / total">
                {formatBytes(transfer.doneBytes)}
                {transfer.totalBytes != null ? ` / ${formatBytes(transfer.totalBytes)}` : ''}
              </span>
            ) : null}
            {transfer.kind === 'upload' || transfer.kind === 'download' ? (
              <span className="transfer-stats" title="1 min average">
                {formatRate(transfer.shardsPerSec, 'sh/s')} · {formatRate(transfer.mbPerSec, 'MB/s')}
              </span>
            ) : null}
            {transfer.kind === 'upload' ? (
              <button
                type="button"
                className="btn-micro btn-danger"
                onClick={() => uploadAbortRef.current?.abort()}
              >
                Cancel
              </button>
            ) : null}
            {transfer.kind === 'download' ? (
              <button
                type="button"
                className="btn-micro btn-danger"
                onClick={() => downloadAbortRef.current?.abort()}
              >
                Cancel
              </button>
            ) : null}
          </div>
          {(transfer.kind === 'upload' || transfer.kind === 'download') &&
          (transfer.expectedShards != null || (transfer.doneShards ?? 0) > 0) ? (
            <ShardBoxesBar
              done={transfer.doneShards ?? 0}
              active={transfer.activeShards ?? 0}
              total={Math.max(
                transfer.expectedShards ?? 0,
                (transfer.doneShards ?? 0) + (transfer.activeShards ?? 0),
                1,
              )}
            />
          ) : (
            <div className="transfer-track">
              <div className="transfer-fill" style={{ width: `${transfer.percent}%` }} />
            </div>
          )}
        </div>
      ) : null}

      <div className="workspace">
        <Group orientation="horizontal">
          <Panel defaultSize={22} minSize={14}>
            <div className="panel">
              <div className="panel-header">
                <div className="panel-title">Accounts</div>
                <div className="panel-header-actions">
                  <button type="button" className="btn-micro" onClick={() => startGoogleOAuth()}>
                    + Google
                  </button>
                  <button type="button" className="btn-micro" onClick={() => setMegaOpen(true)}>
                    + MEGA
                  </button>
                </div>
              </div>
              <div className="panel-body">
                {accountsQuery.isLoading ? (
                  <div className="empty-hint">Loading accounts…</div>
                ) : accountsQuery.data && accountsQuery.data.length > 0 ? (
                  <div className="accounts-panel">
                    <div className="list">
                      {accountsQuery.data.map((account: Account) => {
                        const stats = storageById.get(account.id)
                        const pct = storagePct(stats?.used ?? null, stats?.total ?? null)
                        const uploadRow =
                          transfer?.kind === 'upload' || transfer?.kind === 'download'
                            ? transfer.byAccount?.find((a) => a.account_id === account.id)
                            : undefined
                        const uploadRate = uploadRow
                          ? transfer?.accountRates?.[account.id]
                          : undefined
                        return (
                          <div key={account.id} className="account-card">
                            <div className="list-row">
                              <span className={`provider-swatch ${account.provider}`} />
                              <span className="list-row-label" title={account.label}>
                                {account.label}
                              </span>
                              <span className="list-row-meta">{account.provider}</span>
                              <button
                                type="button"
                                className="btn-micro btn-danger"
                                onClick={() => deleteAccountMut.mutate(account.id)}
                              >
                                ×
                              </button>
                            </div>
                            <div className="storage-stats">
                              {storageQuery.isLoading && !stats ? (
                                <span className="storage-meta">Loading quota…</span>
                              ) : stats?.error ? (
                                <span className="storage-meta storage-error" title={stats.error}>
                                  Quota unavailable
                                </span>
                              ) : (
                                <>
                                  <div className="storage-track">
                                    <div
                                      className={`storage-fill${stats?.full ? ' storage-fill-full' : ''}`}
                                      style={{ width: `${pct ?? 0}%` }}
                                    />
                                  </div>
                                  <div className="storage-meta">
                                    <span>
                                      {formatBytes(stats?.used ?? null)}
                                      {' / '}
                                      {stats?.total == null ? '∞' : formatBytes(stats.total)}
                                      {stats?.full ? ' · full' : ''}
                                    </span>
                                    <span>
                                      {stats?.placeable == null
                                        ? stats?.available == null
                                          ? '—'
                                          : `${formatBytes(stats.available)} free`
                                        : `${formatBytes(stats.placeable)} placeable`}
                                    </span>
                                  </div>
                                </>
                              )}
                              {uploadRow ? (
                                <div className="upload-account-stats">
                                  <ShardBoxesBar
                                    variant="account"
                                    done={uploadRow.shards}
                                    active={uploadRow.active ?? 0}
                                    total={Math.max(
                                      uploadRow.estimated_shards,
                                      uploadRow.shards + (uploadRow.active ?? 0),
                                      1,
                                    )}
                                  />
                                  <div className="storage-meta">
                                    <span title="Shards done / estimated share (+ active)">
                                      {uploadRow.shards}/{uploadRow.estimated_shards} sh
                                      {(uploadRow.active ?? 0) > 0
                                        ? ` (+${uploadRow.active})`
                                        : ''}{' '}
                                      · {formatBytes(uploadRow.bytes)}
                                    </span>
                                    <span title="1 min average">
                                      {formatRate(uploadRate?.shardsPerSec, 'sh/s')} ·{' '}
                                      {formatRate(uploadRate?.mbPerSec, 'MB/s')}
                                    </span>
                                  </div>
                                </div>
                              ) : null}
                            </div>
                          </div>
                        )
                      })}
                    </div>
                    <div className="storage-combined">
                      <div className="storage-combined-label">
                        Placeable
                        {storageQuery.data?.full_ratio != null
                          ? ` (under ${Math.round(storageQuery.data.full_ratio * 100)}%)`
                          : ''}
                      </div>
                      <div className="storage-combined-value">
                        {storageQuery.isLoading
                          ? '…'
                          : formatBytes(storageQuery.data?.combined.placeable ?? null)}
                      </div>
                      <div className="storage-combined-sub">
                        {storageQuery.data?.combined.complete
                          ? `${formatBytes(storageQuery.data.combined.used)} used · ${formatBytes(storageQuery.data.combined.available)} free`
                          : storageQuery.data?.combined.placeable != null
                            ? 'Sum of known headroom'
                            : storageQuery.isError
                              ? 'Could not load quotas'
                              : 'Quota incomplete'}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="empty-hint">Connect Google Drive or MEGA to store shards.</div>
                )}
              </div>
            </div>
          </Panel>

          <Separator className="separator" style={{ width: 4 }} />

          <Panel minSize={40}>
            <div className="panel">
              <div className="panel-header">
                <div className="panel-title">Vault</div>
                <div className="panel-header-actions">
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    hidden
                    onChange={(e) => {
                      const files = Array.from(e.target.files ?? [])
                      if (files.length > 0) uploadMut.mutate(files)
                      e.target.value = ''
                    }}
                  />
                  <button
                    type="button"
                    className="btn-micro"
                    disabled={!unlocked || busy}
                    onClick={() => fileInputRef.current?.click()}
                  >
                    {uploadMut.isPending ? 'Uploading…' : 'Upload'}
                  </button>
                  <button
                    type="button"
                    className="btn-micro"
                    disabled={selectedFiles.length === 0 || busy}
                    onClick={() => downloadMut.mutate(selectedFiles)}
                  >
                    {downloadMut.isPending
                      ? 'Downloading…'
                      : selectedFiles.length > 1
                        ? `Download (${selectedFiles.length})`
                        : 'Download'}
                  </button>
                  <button
                    type="button"
                    className="btn-micro btn-danger"
                    disabled={selectedFiles.length === 0 || busy}
                    onClick={() => deleteFileMut.mutate(selectedFiles)}
                  >
                    {deleteFileMut.isPending
                      ? 'Deleting…'
                      : selectedFiles.length > 1
                        ? `Delete (${selectedFiles.length})`
                        : 'Delete'}
                  </button>
                </div>
              </div>

              <div className="breadcrumbs">
                <button type="button" className="crumb" onClick={() => goCrumb(-1)}>
                  /
                </button>
                {crumbs.map((part, i) => (
                  <span key={`${part}-${i}`} style={{ display: 'inline-flex', gap: 4 }}>
                    <span className="crumb-sep">/</span>
                    <button type="button" className="crumb" onClick={() => goCrumb(i)}>
                      {part}
                    </button>
                  </span>
                ))}
              </div>

              <div className="panel-body">
                {!unlocked ? (
                  <div className="empty-hint">Unlock the vault to browse encrypted files.</div>
                ) : filesQuery.isLoading ? (
                  <div className="empty-hint">Loading…</div>
                ) : entries && entries.length > 0 ? (
                  <table className="file-table">
                    <thead>
                      <tr>
                        <th className="col-check">
                          <input
                            type="checkbox"
                            className="file-check"
                            checked={allFilesSelected}
                            disabled={fileEntries.length === 0 || busy}
                            onChange={toggleSelectAllFiles}
                            aria-label="Select all files"
                          />
                        </th>
                        <th>Name</th>
                        <th>Size</th>
                        <th>Shards</th>
                      </tr>
                    </thead>
                    <tbody>
                      {entries.map((entry) => {
                        const isSelected = selectedIds.includes(entry.id)
                        return (
                          <tr
                            key={entry.id}
                            className={isSelected ? 'selected' : undefined}
                            onClick={(e) => selectEntry(entry, e)}
                            onDoubleClick={() => openDir(entry)}
                          >
                            <td className="col-check" onClick={(e) => e.stopPropagation()}>
                              {entry.kind === 'file' ? (
                                <input
                                  type="checkbox"
                                  className="file-check"
                                  checked={isSelected}
                                  disabled={busy}
                                  onChange={(e) => toggleFileChecked(entry, e.target.checked)}
                                  aria-label={`Select ${entry.name}`}
                                />
                              ) : null}
                            </td>
                            <td>
                              <span className={`file-name ${entry.kind}`}>{entry.name}</span>
                            </td>
                            <td className="list-row-meta">{formatBytes(entry.size)}</td>
                            <td className="list-row-meta">
                              {entry.kind === 'file' ? entry.shard_count : '—'}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                ) : (
                  <div className="empty-hint">Vault is empty. Upload a file to encrypt and shard it.</div>
                )}
              </div>
            </div>
          </Panel>

          <Separator className="separator" style={{ width: 4 }} />

          <Panel defaultSize={24} minSize={16}>
            <div className="panel">
              <div className="panel-header">
                <div className="panel-title">Inspect</div>
              </div>
              <div className="panel-body">
                {!unlocked ? (
                  <div className="empty-hint">Unlock to inspect files.</div>
                ) : selectedFiles.length > 1 ? (
                  <div className="empty-hint">{selectedFiles.length} files selected.</div>
                ) : !inspectTarget ? (
                  <div className="empty-hint">Select a file to inspect shards.</div>
                ) : detailQuery.isLoading ? (
                  <div className="empty-hint">Loading…</div>
                ) : detailQuery.isError ? (
                  <div className="empty-hint">{(detailQuery.error as Error).message}</div>
                ) : detailQuery.data ? (
                  <div className="inspect">
                    <div className="inspect-name" title={detailQuery.data.path}>
                      {detailQuery.data.name}
                    </div>
                    <div className="inspect-row">
                      <span className="inspect-label">Path</span>
                      <span className="inspect-value">{detailQuery.data.path}</span>
                    </div>
                    <div className="inspect-row">
                      <span className="inspect-label">Size</span>
                      <span className="inspect-value">{formatBytes(detailQuery.data.size)}</span>
                    </div>
                    <div className="inspect-row">
                      <span className="inspect-label">Chunks</span>
                      <span className="inspect-value">
                        {formatBytes(detailQuery.data.chunk_size)}
                      </span>
                    </div>
                    <div className="inspect-row">
                      <span className="inspect-label">Crypto</span>
                      <span className="inspect-value">{detailQuery.data.alg ?? '—'}</span>
                    </div>
                    <div className="inspect-row">
                      <span className="inspect-label">Total shards</span>
                      <span className="inspect-value">{detailQuery.data.shard_count}</span>
                    </div>

                    <div className="inspect-section">Shards by account</div>
                    {detailQuery.data.shards_by_account.map((row) => (
                      <div key={row.account_id} className="inspect-account">
                        <div className="inspect-account-head">
                          <span className={`provider-swatch ${row.provider}`} />
                          <span className="list-row-label" title={row.label}>
                            {row.label}
                          </span>
                          <span className="list-row-meta">{row.count}</span>
                        </div>
                        <div className="inspect-value">
                          {row.provider} · indices [{row.shard_indices.join(', ')}]
                        </div>
                      </div>
                    ))}

                    <div className="inspect-section">Shard map</div>
                    <table className="file-table">
                      <thead>
                        <tr>
                          <th>#</th>
                          <th>Account</th>
                          <th>Remote</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detailQuery.data.shards.map((s) => (
                          <tr key={s.i}>
                            <td className="list-row-meta">{s.i}</td>
                            <td>
                              <span className="list-row-meta" title={s.label}>
                                {s.provider}
                              </span>
                            </td>
                            <td>
                              <span className="inspect-remote" title={s.remote_id}>
                                {s.remote_id.slice(0, 10)}…
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : null}
              </div>
            </div>
          </Panel>
        </Group>
      </div>

      {megaOpen ? (
        <div className="modal-backdrop" onClick={() => setMegaOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">Connect MEGA</div>
            <div className="modal-body">
              <div>
                <div className="field-label">Email</div>
                <input
                  className="input"
                  type="email"
                  value={megaEmail}
                  onChange={(e) => setMegaEmail(e.target.value)}
                  autoComplete="username"
                />
              </div>
              <div>
                <div className="field-label">Password</div>
                <input
                  className="input"
                  type="password"
                  value={megaPassword}
                  onChange={(e) => setMegaPassword(e.target.value)}
                  autoComplete="current-password"
                />
              </div>
              <div>
                <div className="field-label">2FA code (if enabled)</div>
                <input
                  className="input"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder="6-digit code"
                  value={megaMfa}
                  onChange={(e) => setMegaMfa(e.target.value)}
                />
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                Use your MEGA email/password (not Google SSO alone). If you only
                use “Continue with Google” on mega.nz, set a password under
                Account → Security first.
              </div>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => setMegaOpen(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={!megaEmail || !megaPassword || megaMut.isPending}
                onClick={() => megaMut.mutate()}
              >
                {megaMut.isPending ? 'Connecting…' : 'Connect'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {unlockOpen ? (
        <div className="modal-backdrop" onClick={() => setUnlockOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              {statusQuery.data?.initialized ? 'Unlock Vault' : 'Create Vault'}
            </div>
            <div className="modal-body">
              <div>
                <div className="field-label">Passphrase</div>
                <input
                  className="input"
                  type="password"
                  value={passphrase}
                  onChange={(e) => setPassphrase(e.target.value)}
                  autoFocus
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && passphrase) unlockMut.mutate(passphrase)
                  }}
                />
              </div>
              {!statusQuery.data?.initialized ? (
                <div style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                  First unlock sets the vault passphrase. It cannot be recovered if lost.
                </div>
              ) : null}
            </div>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => setUnlockOpen(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={!passphrase || unlockMut.isPending}
                onClick={() => unlockMut.mutate(passphrase)}
              >
                {unlockMut.isPending ? 'Unlocking…' : 'Unlock'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
