import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type MouseEvent,
} from 'react'
import { Link, useSearchParams } from 'react-router'
import { Group, Panel, Separator, usePanelRef } from 'react-resizable-panels'
import { login, logout } from '../api/auth'
import {
  AccountInUseError,
  analyzePrune,
  applyPrune,
  connectMega,
  deleteAccount,
  evacuateAccount,
  getStorageStats,
  listAccounts,
  runHealthCheck,
  setAccountEnabled,
  startGoogleOAuth,
  type Account,
  type AccountInUseDetail,
  type AccountStorage,
  type HealthCheckResult,
  type PruneAnalyzeResult,
} from '../api/accounts'
import {
  deleteVaultDir,
  deleteVaultFile,
  downloadVaultFile,
  downloadVaultZip,
  getVaultFile,
  getVaultStatus,
  listVaultFiles,
  MAX_PARALLEL_UPLOAD_FILES,
  mkdirVault,
  moveVaultEntry,
  searchVaultFiles,
  uploadVaultFile,
  type ProgressUpdate,
  type UploadJobStats,
  type VaultEntry,
  type VaultSearchKind,
  type VaultSearchScope,
  type VaultStatus,
} from '../api/vault'
import {
  collectFromDataTransfer,
  collectFromFileList,
  dataTransferHasFiles,
  dataTransferIsVaultMove,
  joinVaultDir,
  splitRelativePath,
  type CollectedUploads,
  type UploadItem,
} from '../collectUploadItems'
import {
  buildUploadPlan,
  detectUploadConflicts,
  type ConflictAction,
  type ConflictScan,
} from '../uploadConflicts'
import {
  ensureDownloadFolder,
  getDownloadFolderName,
  supportsDownloadFolder,
} from '../downloadFolder'
import { FilePreviewDialog } from '../FilePreviewDialog'
import { FileThumb } from '../FileThumb'
import {
  DEFAULT_PREVIEW_MAX_BYTES,
  previewableImagesInFolder,
  previewOpenKind,
  type PreviewOpenKind,
} from '../filePreview'
import {
  ShardBoxesBar,
  useTransfer,
  type AccountRate,
  type TransferKind,
  type TransferState,
} from '../TransferContext'
import {
  buildTransferTree,
  initialFileStatuses,
  type TransferFileStatus,
} from '../transferTree'

const SKIP_UPLOAD_NAMES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini'])

type DragPayload = { kind: 'file' | 'dir'; path: string; name: string }

const RATE_WINDOW_MS = 60_000
const ERROR_AUTO_HIDE_MS = 60_000

type RateSample = { t: number; shards: number; bytes: number }

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

function PanelChevron({ dir }: { dir: 'left' | 'right' }) {
  return (
    <svg
      width={12}
      height={12}
      viewBox="0 0 14 16"
      aria-hidden
      className="btn-icon-svg"
      style={{ transform: dir === 'right' ? 'rotate(180deg)' : undefined }}
    >
      <path
        d="M10 3.5L5.5 8 10 12.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  )
}

function ListViewIcon() {
  return (
    <svg width={12} height={12} viewBox="0 0 14 14" aria-hidden className="btn-icon-svg">
      <path d="M2 3h10M2 7h10M2 11h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

function GridViewIcon() {
  return (
    <svg width={12} height={12} viewBox="0 0 14 14" aria-hidden className="btn-icon-svg">
      <rect x="2" y="2" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="8" y="2" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="2" y="8" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="8" y="8" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  )
}

type FileViewMode = 'list' | 'grid'
const FILE_VIEW_KEY = 'cloud-storage.fileView'

function loadFileView(): FileViewMode {
  try {
    return localStorage.getItem(FILE_VIEW_KEY) === 'grid' ? 'grid' : 'list'
  } catch {
    return 'list'
  }
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
  const span = 100 / Math.max(total, 1)
  const percent = Math.min(100, Math.round(index * span + (update.percent / 100) * span))
  return { percent, message: update.message }
}

/** Run up to ``concurrency`` async workers over ``items`` (fail-fast). */
async function mapPool<T>(
  items: T[],
  concurrency: number,
  worker: (item: T, index: number) => Promise<void>,
): Promise<void> {
  if (items.length === 0) return
  let next = 0
  let firstError: unknown = null
  const n = Math.max(1, Math.min(concurrency, items.length))
  await Promise.all(
    Array.from({ length: n }, async () => {
      for (;;) {
        if (firstError) return
        const i = next++
        if (i >= items.length) return
        try {
          await worker(items[i], i)
        } catch (err) {
          if (firstError == null) firstError = err
          return
        }
      }
    }),
  )
  if (firstError) throw firstError
}


export default function Workspace() {
  const qc = useQueryClient()
  const {
    transfer,
    setTransfer,
    setBatchQueue,
    uploadAbortRef,
    downloadAbortRef,
    jobAbortRef,
  } = useTransfer()
  const [searchParams, setSearchParams] = useSearchParams()
  const [path, setPath] = useState('/')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [anchorId, setAnchorId] = useState<string | null>(null)
  const [searchInput, setSearchInput] = useState('')
  const [searchScope, setSearchScope] = useState<VaultSearchScope>('folder')
  const [searchKind, setSearchKind] = useState<VaultSearchKind>('all')
  const [searchRegex, setSearchRegex] = useState(false)
  const deferredSearch = useDeferredValue(searchInput.trim())
  const searchActive = deferredSearch.length > 0
  const pendingSelectRef = useRef<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [megaOpen, setMegaOpen] = useState(false)
  const [megaEmail, setMegaEmail] = useState('')
  const [megaPassword, setMegaPassword] = useState('')
  const [megaMfa, setMegaMfa] = useState('')
  const [username, setUsername] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [mkdirOpen, setMkdirOpen] = useState(false)
  const [mkdirName, setMkdirName] = useState('')
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [uploadConflict, setUploadConflict] = useState<{
    destDir: string
    files: UploadItem[]
    emptyDirs: string[]
    scan: ConflictScan
    actions: Record<string, ConflictAction>
  } | null>(null)
  const [uploadConflictBusy, setUploadConflictBusy] = useState(false)
  const [filePreview, setFilePreview] = useState<{
    entry: VaultEntry
    kind: PreviewOpenKind
  } | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const [pruneOpen, setPruneOpen] = useState(false)
  const [pruneReport, setPruneReport] = useState<PruneAnalyzeResult | null>(null)
  const [pruneAnalyzing, setPruneAnalyzing] = useState(false)
  const [healthOpen, setHealthOpen] = useState(false)
  const [healthReport, setHealthReport] = useState<HealthCheckResult | null>(null)
  const [healthRunning, setHealthRunning] = useState(false)
  const [evacuateOpen, setEvacuateOpen] = useState(false)
  const [evacuateDetail, setEvacuateDetail] = useState<AccountInUseDetail | null>(null)
  const [accountsManage, setAccountsManage] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const transferRateSamplesRef = useRef<RateSample[]>([])
  const transferAccountRateSamplesRef = useRef<Map<string, RateSample[]>>(new Map())
  const uploadFilePercentsRef = useRef<Map<number, number>>(new Map())
  const leftPanelRef = usePanelRef()
  const rightPanelRef = usePanelRef()
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightCollapsed, setRightCollapsed] = useState(false)
  const [fileView, setFileView] = useState<FileViewMode>(loadFileView)
  const [downloadFolderName, setDownloadFolderName] = useState<string | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(FILE_VIEW_KEY, fileView)
    } catch {
      /* ignore */
    }
  }, [fileView])

  useEffect(() => {
    if (!supportsDownloadFolder()) return
    void getDownloadFolderName().then(setDownloadFolderName)
  }, [])

  const syncLeftCollapsed = () => {
    setLeftCollapsed(leftPanelRef.current?.isCollapsed() ?? false)
  }
  const syncRightCollapsed = () => {
    setRightCollapsed(rightPanelRef.current?.isCollapsed() ?? false)
  }
  const toggleLeftPanel = () => {
    const panel = leftPanelRef.current
    if (!panel) return
    if (panel.isCollapsed()) panel.expand()
    else panel.collapse()
  }
  const toggleRightPanel = () => {
    const panel = rightPanelRef.current
    if (!panel) return
    if (panel.isCollapsed()) panel.expand()
    else panel.collapse()
  }

  function resetTransferRates() {
    transferRateSamplesRef.current = []
    transferAccountRateSamplesRef.current = new Map()
  }

  function onShardTransferProgress(
    kind: 'upload' | 'download',
    index: number,
    total: number,
    update: ProgressUpdate,
    meta: { itemLabel: string },
    opts?: { parallelFiles?: boolean },
  ) {
    let percent: number
    let message = update.message
    if (opts?.parallelFiles && kind === 'upload') {
      uploadFilePercentsRef.current.set(index, update.percent)
      let sum = 0
      for (let i = 0; i < total; i++) {
        sum += uploadFilePercentsRef.current.get(i) ?? 0
      }
      percent = Math.min(100, Math.round(sum / Math.max(total, 1)))
    } else {
      const mapped = batchProgress(index, total, update)
      percent = mapped.percent
      message = mapped.message
    }
    const base: TransferState = {
      kind,
      percent,
      message,
      itemLabel: meta.itemLabel,
      itemIndex: index,
      itemTotal: total,
    }
    if (!update.stats) {
      setTransfer(base)
      return
    }
    const rates = applyTransferRates(
      update.stats,
      transferRateSamplesRef,
      transferAccountRateSamplesRef.current,
    )
    setTransfer({ ...base, ...rates })
  }

  function startBatchQueue(
    kind: 'upload' | 'download',
    filePaths: string[],
    emptyDirs: string[] = [],
  ) {
    setBatchQueue({
      kind,
      tree: buildTransferTree(filePaths, emptyDirs),
      statuses: initialFileStatuses(filePaths),
      visible: true,
    })
  }

  function setBatchFileStatus(path: string, status: TransferFileStatus) {
    setBatchQueue((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        statuses: { ...prev.statuses, [path]: status },
      }
    })
  }

  function markBatchCurrent(labels: string[], index: number) {
    setBatchQueue((prev) => {
      if (!prev) return prev
      const statuses = { ...prev.statuses }
      for (let i = 0; i < index; i++) statuses[labels[i]] = 'done'
      statuses[labels[index]] = 'current'
      for (let i = index + 1; i < labels.length; i++) {
        if (statuses[labels[i]] !== 'error') statuses[labels[i]] = 'queued'
      }
      return { ...prev, statuses }
    })
  }

  const statusQuery = useQuery({
    queryKey: ['vault', 'status'],
    queryFn: getVaultStatus,
  })

  const unlocked = statusQuery.data?.unlocked ?? false
  const accountsLocked = statusQuery.data?.accounts_locked ?? false

  const accountsQuery = useQuery({
    queryKey: ['accounts'],
    queryFn: listAccounts,
    enabled: unlocked,
  })

  const storageQuery = useQuery({
    queryKey: ['accounts', 'storage'],
    queryFn: getStorageStats,
    enabled: unlocked && (accountsQuery.data?.length ?? 0) > 0,
    staleTime: 60_000,
  })

  const storageById = useMemo(() => {
    const map = new Map<string, AccountStorage>()
    for (const row of storageQuery.data?.accounts ?? []) map.set(row.id, row)
    return map
  }, [storageQuery.data])

  const filesQuery = useQuery({
    queryKey: ['vault', 'files', path],
    queryFn: () => listVaultFiles(path),
    enabled: unlocked,
  })

  const searchQuery = useQuery({
    queryKey: ['vault', 'search', deferredSearch, searchScope, path, searchKind, searchRegex],
    queryFn: () =>
      searchVaultFiles({
        q: deferredSearch,
        scope: searchScope,
        path,
        kind: searchKind,
        regex: searchRegex,
      }),
    enabled: unlocked && searchActive,
    // Don't spin on 404/400 (e.g. stale backend without /search, or bad regex).
    retry: false,
  })

  const entries = searchActive ? searchQuery.data?.results : filesQuery.data
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

  useEffect(() => {
    const id = pendingSelectRef.current
    if (!id || searchActive) return
    const list = filesQuery.data
    if (!list?.some((e) => e.id === id)) return
    setSelectedIds([id])
    setAnchorId(id)
    pendingSelectRef.current = null
  }, [filesQuery.data, searchActive, path])

  const crumbs = useMemo(() => pathParts(path), [path])

  const loginMut = useMutation({
    mutationFn: () => login(username, passphrase),
    onSuccess: async () => {
      setPassphrase('')
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
      await qc.invalidateQueries({ queryKey: ['accounts'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const logoutMut = useMutation({
    mutationFn: logout,
    onSuccess: async () => {
      setError(null)
      setSelectedIds([])
      setPath('/')
      await qc.invalidateQueries({ queryKey: ['vault'] })
      await qc.invalidateQueries({ queryKey: ['accounts'] })
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
      setEvacuateOpen(false)
      setEvacuateDetail(null)
      await qc.invalidateQueries({ queryKey: ['accounts'] })
      await qc.invalidateQueries({ queryKey: ['vault', 'status'] })
    },
    onError: (e: Error) => {
      if (e instanceof AccountInUseError) {
        setError(null)
        setEvacuateDetail(e.detail)
        setEvacuateOpen(true)
        return
      }
      setError(e.message)
    },
  })

  const evacuateMut = useMutation({
    mutationFn: async (accountId: string) => {
      const ac = new AbortController()
      jobAbortRef.current = ac
      try {
        return await withTransfer('evacuate', 'Moving account data…', (onProgress) =>
          evacuateAccount(accountId, onProgress, { signal: ac.signal }),
        )
      } finally {
        jobAbortRef.current = null
      }
    },
    onSuccess: async () => {
      setError(null)
      setEvacuateOpen(false)
      setEvacuateDetail(null)
      await qc.invalidateQueries({ queryKey: ['accounts'] })
      await qc.invalidateQueries({ queryKey: ['accounts', 'storage'] })
      await qc.invalidateQueries({ queryKey: ['vault'] })
      await qc.invalidateQueries({ queryKey: ['vault', 'status'] })
    },
    onError: (e: Error) => {
      if (e.message === 'Evacuate cancelled') {
        setError(null)
        return
      }
      setError(e.message)
    },
  })

  const setAccountEnabledMut = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setAccountEnabled(id, enabled),
    onSuccess: async () => {
      setError(null)
      await qc.invalidateQueries({ queryKey: ['accounts'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  async function withTransfer<T>(
    kind: TransferKind,
    startMessage: string,
    apiFn: (onProgress: (u: ProgressUpdate) => void) => Promise<T>,
  ): Promise<T> {
    setTransfer({ kind, percent: 0, message: startMessage })
    try {
      return await apiFn((u) =>
        setTransfer({ kind, percent: u.percent, message: u.message }),
      )
    } finally {
      setTransfer(null)
    }
  }

  const pruneAnalyzeMut = useMutation({
    mutationFn: async () => {
      setPruneOpen(true)
      setPruneReport(null)
      setPruneAnalyzing(true)
      try {
        return await withTransfer('prune', 'Analyzing accounts…', analyzePrune)
      } finally {
        setPruneAnalyzing(false)
      }
    },
    onSuccess: (report) => {
      setError(null)
      setPruneReport(report)
    },
    onError: (e: Error) => {
      setError(e.message)
      setPruneOpen(false)
      setPruneReport(null)
    },
  })

  const healthMut = useMutation({
    mutationFn: async () => {
      setHealthOpen(true)
      setHealthReport(null)
      setHealthRunning(true)
      const ac = new AbortController()
      jobAbortRef.current = ac
      try {
        return await withTransfer('health', 'Checking shards…', (onProgress) =>
          runHealthCheck(onProgress, { signal: ac.signal }),
        )
      } finally {
        jobAbortRef.current = null
        setHealthRunning(false)
      }
    },
    onSuccess: (report) => {
      setError(null)
      setHealthReport(report)
    },
    onError: (e: Error) => {
      setHealthOpen(false)
      setHealthReport(null)
      if (e.message === 'Health check cancelled') {
        setError(null)
        return
      }
      setError(e.message)
    },
  })

  const pruneApplyMut = useMutation({
    mutationFn: async () => {
      return await withTransfer('prune', 'Cleaning orphans…', applyPrune)
    },
    onSuccess: async (result) => {
      setError(null)
      await qc.invalidateQueries({ queryKey: ['accounts', 'storage'] })
      if (result.errors > 0) {
        setError(
          `Prune finished with ${result.errors} delete error(s). Some orphans may remain (often Drive folders the app cannot fully access).`,
        )
        setPruneReport(null)
        setPruneAnalyzing(true)
        try {
          const report = await withTransfer('prune', 'Re-analyzing…', analyzePrune)
          setPruneReport(report)
        } catch (e) {
          setPruneOpen(false)
          setError(e instanceof Error ? e.message : String(e))
        } finally {
          setPruneAnalyzing(false)
        }
        return
      }
      setPruneOpen(false)
      setPruneReport(null)
    },
    onError: (e: Error) => setError(e.message),
  })

  async function ensureVaultDirTree(fullPath: string) {
    if (!fullPath || fullPath === '/') return
    const parts = fullPath.replace(/^\/+/, '').split('/').filter(Boolean)
    let current = '/'
    for (const part of parts) {
      const parent = current
      try {
        await mkdirVault(parent, part)
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        if (!/already exists/i.test(msg)) throw err
      }
      current = parent === '/' ? `/${part}` : `${parent}/${part}`
    }
  }

  function filterUploadItems(items: UploadItem[]): UploadItem[] {
    return items.filter((item) => {
      const name = splitRelativePath(item.relativePath).name
      return Boolean(name) && !SKIP_UPLOAD_NAMES.has(name)
    })
  }

  const uploadMut = useMutation({
    mutationFn: async (
      job: {
        destDir: string
        replacements?: Array<{ id: string; label: string }>
      } & CollectedUploads,
    ) => {
      const files = filterUploadItems(job.files)
      const emptyDirs = job.emptyDirs
        .map((d) => d.replace(/\\/g, '/').replace(/^\/+|\/+$/g, ''))
        .filter(Boolean)
      const replacements = job.replacements ?? []
      if (files.length === 0 && emptyDirs.length === 0 && replacements.length === 0) {
        throw new Error('Nothing to upload')
      }
      const ac = new AbortController()
      uploadAbortRef.current = ac
      resetTransferRates()
      uploadFilePercentsRef.current = new Map()
      const labels = files.map((f) => f.relativePath)
      const status = qc.getQueryData<VaultStatus>(['vault', 'status'])
      // Cap below browser HTTP/1.1 pool (SSE + PUT per file); see MAX_PARALLEL_UPLOAD_FILES.
      const fileConcurrency = Math.max(
        1,
        Math.min(
          status?.shard_upload_concurrency ?? 1,
          MAX_PARALLEL_UPLOAD_FILES,
        ),
      )
      startBatchQueue('upload', labels, emptyDirs)
      try {
        if (replacements.length > 0) {
          setTransfer({
            kind: 'upload',
            percent: 0,
            message: `Replacing ${replacements.length} existing file(s)…`,
            itemLabel: null,
            itemIndex: 0,
            itemTotal: files.length,
          })
          for (let i = 0; i < replacements.length; i++) {
            if (ac.signal.aborted) throw new Error('Upload cancelled')
            const r = replacements[i]
            setTransfer({
              kind: 'upload',
              percent: Math.round((i / Math.max(replacements.length, 1)) * 8),
              message: `Deleting ${r.label}…`,
              itemLabel: r.label,
              itemIndex: 0,
              itemTotal: files.length,
            })
            await deleteVaultFile(r.id)
          }
        }
        if (emptyDirs.length > 0) {
          setTransfer({
            kind: 'upload',
            percent: 0,
            message: `Creating ${emptyDirs.length} folder(s)…`,
            itemLabel: null,
            itemIndex: 0,
            itemTotal: files.length,
          })
        }
        for (const dir of emptyDirs) {
          if (ac.signal.aborted) throw new Error('Upload cancelled')
          await ensureVaultDirTree(joinVaultDir(job.destDir, dir))
        }
        if (files.length === 0) return
        // Overlap small files so free shard slots stay busy (shared backend pool).
        await mapPool(files, fileConcurrency, async (item, i) => {
          if (ac.signal.aborted) throw new Error('Upload cancelled')
          const meta = { itemLabel: item.relativePath }
          setBatchFileStatus(item.relativePath, 'current')
          const { dir, name } = splitRelativePath(item.relativePath)
          const parent = joinVaultDir(job.destDir, dir)
          const file =
            item.file.name === name
              ? item.file
              : new File([item.file], name, {
                  type: item.file.type,
                  lastModified: item.file.lastModified,
                })
          try {
            await uploadVaultFile(
              parent,
              file,
              (u) =>
                onShardTransferProgress('upload', i, files.length, u, meta, {
                  parallelFiles: true,
                }),
              { signal: ac.signal },
            )
            uploadFilePercentsRef.current.set(i, 100)
            setBatchFileStatus(item.relativePath, 'done')
          } catch (err) {
            setBatchFileStatus(item.relativePath, 'error')
            ac.abort()
            throw err
          }
        })
      } finally {
        uploadAbortRef.current = null
        resetTransferRates()
        uploadFilePercentsRef.current = new Map()
      }
    },
    onSuccess: async () => {
      setTransfer(null)
      setBatchQueue(null)
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
      await qc.invalidateQueries({ queryKey: ['accounts', 'storage'] })
    },
    onError: (e: Error) => {
      setTransfer(null)
      setError(e.message)
    },
  })

  function queueUpload(destDir: string, collected: CollectedUploads) {
    const files = filterUploadItems(collected.files)
    const emptyDirs = collected.emptyDirs
    if (files.length === 0 && emptyDirs.length === 0) return
    void (async () => {
      try {
        setUploadConflictBusy(true)
        const scan = await detectUploadConflicts(destDir, files)
        if (scan.conflicts.length === 0) {
          uploadMut.mutate({ destDir, files, emptyDirs })
          return
        }
        const actions: Record<string, ConflictAction> = {}
        for (const c of scan.conflicts) {
          // Default: rename keeps both; replace only when a vault file exists.
          actions[c.relativePath] = 'rename'
        }
        setUploadConflict({ destDir, files, emptyDirs, scan, actions })
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        setUploadConflictBusy(false)
      }
    })()
  }

  function applyUploadConflictAction(action: ConflictAction) {
    setUploadConflict((prev) => {
      if (!prev) return prev
      const actions = { ...prev.actions }
      for (const c of prev.scan.conflicts) {
        if (action === 'replace' && c.existingId == null) {
          // Batch-only duplicate: replace still means keep the later file.
          actions[c.relativePath] = 'replace'
        } else {
          actions[c.relativePath] = action
        }
      }
      return { ...prev, actions }
    })
  }

  function confirmUploadConflicts() {
    if (!uploadConflict) return
    const { destDir, files, emptyDirs, scan, actions } = uploadConflict
    const plan = buildUploadPlan(
      destDir,
      files,
      scan.conflicts,
      actions,
      scan.occupiedByParent,
    )
    setUploadConflict(null)
    if (plan.files.length === 0 && emptyDirs.length === 0) {
      setError('Nothing to upload after skipping conflicts')
      return
    }
    uploadMut.mutate({
      destDir,
      files: plan.files,
      emptyDirs,
      replacements: plan.replacements,
    })
  }

  async function uploadFromDataTransfer(destDir: string, dt: DataTransfer) {
    try {
      const collected = await collectFromDataTransfer(dt)
      queueUpload(destDir, collected)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  useEffect(() => {
    if (!error) return
    const t = window.setTimeout(() => setError(null), ERROR_AUTO_HIDE_MS)
    return () => window.clearTimeout(t)
  }, [error])

  const downloadMut = useMutation({
    mutationFn: async (entries: VaultEntry[]) => {
      const ac = new AbortController()
      downloadAbortRef.current = ac
      resetTransferRates()
      const labels = entries.map((f) => (f.path || f.name).replace(/^\//, ''))
      // Pick / reuse folder once for the whole batch (Chromium).
      let directory: FileSystemDirectoryHandle | null = null
      if (supportsDownloadFolder()) {
        directory = await ensureDownloadFolder()
        if (!directory) throw new Error('Download cancelled')
        setDownloadFolderName(directory.name)
      }
      const onlyOneFile =
        entries.length === 1 && entries[0].kind === 'file'
      startBatchQueue('download', labels)
      try {
        if (onlyOneFile) {
          const entry = entries[0]
          markBatchCurrent(labels, 0)
          setTransfer({
            kind: 'download',
            percent: 0,
            message: 'Starting…',
            itemLabel: labels[0],
            itemIndex: 0,
            itemTotal: 1,
          })
          try {
            await downloadVaultFile(
              entry.id,
              entry.name,
              (u) =>
                onShardTransferProgress('download', 0, 1, u, {
                  itemLabel: labels[0],
                }),
              { signal: ac.signal, directory },
            )
            setBatchFileStatus(labels[0], 'done')
          } catch (err) {
            setBatchFileStatus(labels[0], 'error')
            throw err
          }
          return
        }

        // Multi-select or any folder: one streaming ZIP, paths relative to common base.
        markBatchCurrent(labels, 0)
        setTransfer({
          kind: 'download',
          percent: 0,
          message: 'Starting zip…',
          itemLabel: labels.join(', '),
          itemIndex: 0,
          itemTotal: 1,
        })
        try {
          await downloadVaultZip(
            entries.map((e) =>
              e.kind === 'dir'
                ? { kind: 'dir' as const, path: e.path }
                : { kind: 'file' as const, path: e.path, id: e.id },
            ),
            (u) =>
              onShardTransferProgress('download', 0, 1, u, {
                itemLabel: 'archive.zip',
              }),
            { signal: ac.signal, directory },
          )
          for (const label of labels) setBatchFileStatus(label, 'done')
        } catch (err) {
          for (const label of labels) setBatchFileStatus(label, 'error')
          throw err
        }
      } finally {
        downloadAbortRef.current = null
        resetTransferRates()
      }
    },
    onSuccess: () => {
      setTransfer(null)
      setBatchQueue(null)
    },
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
    mutationFn: async (entries: VaultEntry[]) => {
      const ac = new AbortController()
      jobAbortRef.current = ac
      try {
        for (let i = 0; i < entries.length; i++) {
          if (ac.signal.aborted) throw new Error('Delete cancelled')
          const entry = entries[i]
          const onProg = (u: ProgressUpdate) => {
            const mapped = batchProgress(i, entries.length, u)
            setTransfer({ kind: 'delete', ...mapped })
          }
          if (entry.kind === 'dir') {
            await deleteVaultDir(entry.path, onProg, { signal: ac.signal })
          } else {
            await deleteVaultFile(entry.id, onProg, { signal: ac.signal })
          }
        }
      } finally {
        jobAbortRef.current = null
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
    onError: async (e: Error) => {
      setTransfer(null)
      if (e.message === 'Delete cancelled') {
        setError(null)
        setSelectedIds([])
        setAnchorId(null)
        await qc.invalidateQueries({ queryKey: ['vault'] })
        await qc.invalidateQueries({ queryKey: ['accounts', 'storage'] })
        return
      }
      setError(e.message)
    },
  })

  const mkdirMut = useMutation({
    mutationFn: () => mkdirVault(path, mkdirName.trim()),
    onSuccess: async () => {
      setMkdirOpen(false)
      setMkdirName('')
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault', 'files'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const renameMut = useMutation({
    mutationFn: async () => {
      const entry = selectedEntries[0]
      if (!entry) throw new Error('Nothing selected')
      const parent = entry.path === '/' ? '/' : entry.path.replace(/\/[^/]+$/, '') || '/'
      const dest =
        parent === '/' ? `/${renameValue.trim()}` : `${parent}/${renameValue.trim()}`
      return moveVaultEntry(entry.path, dest, entry.kind)
    },
    onSuccess: async () => {
      setRenameOpen(false)
      setRenameValue('')
      setSelectedIds([])
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const moveMut = useMutation({
    mutationFn: ({
      src,
      destDir,
      kind,
    }: {
      src: string
      destDir: string
      kind: 'file' | 'dir'
    }) => {
      const name = src.split('/').filter(Boolean).pop() || ''
      const dest = destDir === '/' ? `/${name}` : `${destDir}/${name}`
      return moveVaultEntry(src, dest, kind)
    },
    onSuccess: async () => {
      setDropTarget(null)
      setSelectedIds([])
      setError(null)
      await qc.invalidateQueries({ queryKey: ['vault'] })
    },
    onError: (e: Error) => {
      setDropTarget(null)
      setError(e.message)
    },
  })

  const busy =
    uploadMut.isPending ||
    downloadMut.isPending ||
    deleteFileMut.isPending ||
    mkdirMut.isPending ||
    renameMut.isPending ||
    moveMut.isPending ||
    uploadConflictBusy

  const accountsOpBusy =
    accountsLocked ||
    busy ||
    !!transfer ||
    healthMut.isPending ||
    pruneAnalyzeMut.isPending ||
    pruneApplyMut.isPending ||
    deleteAccountMut.isPending ||
    evacuateMut.isPending

  useEffect(() => {
    if (accountsOpBusy && accountsManage) setAccountsManage(false)
  }, [accountsOpBusy, accountsManage])

  const transferActive = transfer != null
  const statusInvalidatePrimed = useRef(false)
  useEffect(() => {
    if (!statusInvalidatePrimed.current) {
      statusInvalidatePrimed.current = true
      return
    }
    void qc.invalidateQueries({ queryKey: ['vault', 'status'] })
  }, [busy, transferActive, qc])

  const selectedDirs = useMemo(
    () => selectedEntries.filter((e) => e.kind === 'dir'),
    [selectedEntries],
  )
  const canRename = selectedEntries.length === 1
  const canDelete = selectedEntries.length > 0

  const statusText = unlocked
    ? `${statusQuery.data?.username ?? 'user'} · ${statusQuery.data?.file_count ?? 0} files · ${statusQuery.data?.account_count ?? 0} accounts`
    : 'locked'

  const previewMaxBytes =
    statusQuery.data?.preview_max_bytes ?? DEFAULT_PREVIEW_MAX_BYTES

  function parentDirOf(entryPath: string): string {
    const i = entryPath.lastIndexOf('/')
    if (i <= 0) return '/'
    return entryPath.slice(0, i)
  }

  function openEntry(entry: VaultEntry) {
    if (searchActive) {
      if (entry.kind === 'dir') {
        setPath(entry.path)
      } else {
        pendingSelectRef.current = entry.id
        setPath(parentDirOf(entry.path))
      }
      setSearchInput('')
      return
    }
    if (entry.kind === 'dir') {
      setPath(entry.path)
      return
    }
    const kind = previewOpenKind(entry, previewMaxBytes)
    if (!kind) return
    setFilePreview({ entry, kind })
  }

  const previewImagePeers = useMemo(
    () => previewableImagesInFolder(entries ?? [], previewMaxBytes),
    [entries, previewMaxBytes],
  )

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

  function onEntryCheckChange(entry: VaultEntry, checked: boolean) {
    if (entry.kind === 'file') {
      toggleFileChecked(entry, checked)
      return
    }
    setSelectedIds((prev) =>
      checked
        ? prev.includes(entry.id)
          ? prev
          : [...prev, entry.id]
        : prev.filter((id) => id !== entry.id),
    )
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

  function startRename() {
    const entry = selectedEntries[0]
    if (!entry) return
    setRenameValue(entry.name)
    setRenameOpen(true)
  }

  function onDragStartEntry(entry: VaultEntry, e: DragEvent) {
    const payload: DragPayload = { kind: entry.kind, path: entry.path, name: entry.name }
    const raw = JSON.stringify(payload)
    e.dataTransfer.setData('application/x-vault-entry', raw)
    e.dataTransfer.setData('text/plain', raw)
    e.dataTransfer.effectAllowed = 'move'
  }

  function parseDrag(e: DragEvent): DragPayload | null {
    const raw =
      e.dataTransfer.getData('application/x-vault-entry') ||
      e.dataTransfer.getData('text/plain')
    if (!raw) return null
    try {
      return JSON.parse(raw) as DragPayload
    } catch {
      return null
    }
  }

  function canDropOn(destDir: string, payload: DragPayload | null): boolean {
    if (!payload) return false
    if (payload.path === destDir) return false
    if (payload.kind === 'dir' && (destDir === payload.path || destDir.startsWith(payload.path + '/'))) {
      return false
    }
    const parent = payload.path.replace(/\/[^/]+$/, '') || '/'
    if (parent === destDir) return false
    return true
  }

  function onDropOnto(destDir: string, e: DragEvent) {
    e.preventDefault()
    e.stopPropagation()
    setDropTarget(null)
    if (busy) return

    if (dataTransferIsVaultMove(e.dataTransfer)) {
      const payload = parseDrag(e)
      if (!canDropOn(destDir, payload) || !payload) return
      moveMut.mutate({ src: payload.path, destDir, kind: payload.kind })
      return
    }

    if (dataTransferHasFiles(e.dataTransfer) && e.dataTransfer) {
      void uploadFromDataTransfer(destDir, e.dataTransfer)
    }
  }

  function onDragOverDest(destDir: string, e: DragEvent) {
    if (busy) return
    const dt = e.dataTransfer
    if (dataTransferIsVaultMove(dt) || dataTransferHasFiles(dt)) {
      e.preventDefault()
      e.stopPropagation()
      if (dt) dt.dropEffect = dataTransferIsVaultMove(dt) ? 'move' : 'copy'
      setDropTarget(destDir)
    }
  }

  // Shared drag/drop wiring for a file-or-folder entry, spread into both the
  // grid item and the table row. Only directories accept drops.
  function entryDragProps(entry: VaultEntry) {
    const isDir = entry.kind === 'dir'
    return {
      draggable: !busy,
      onDragStart: (e: DragEvent) => onDragStartEntry(entry, e),
      onDragOver: isDir ? (e: DragEvent) => onDragOverDest(entry.path, e) : undefined,
      onDragLeave: isDir
        ? () => setDropTarget((t) => (t === entry.path ? null : t))
        : undefined,
      onDrop: isDir ? (e: DragEvent) => onDropOnto(entry.path, e) : undefined,
    }
  }

  if (!unlocked) {
    return (
      <div className="app-shell">
        <header className="top-bar">
          <div className="top-bar-title">Cloud Storage</div>
          <div className="top-bar-status">locked</div>
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
        <div className="top-bar-status">{statusText}</div>
        <div className="toolbar">
          <Link to="/history" className="btn">
            History
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

      <div className="workspace">
        <Group orientation="horizontal">
          <Panel
            id="accounts"
            panelRef={leftPanelRef}
            defaultSize="22"
            minSize="14"
            collapsible
            collapsedSize={0}
            onResize={syncLeftCollapsed}
          >
            <div className="panel">
              <div className="panel-header">
                <div className="panel-title">Accounts</div>
                <div className="panel-header-actions">
                  <button
                    type="button"
                    className={`btn-micro${accountsManage ? ' active' : ''}`}
                    disabled={accountsOpBusy}
                    title={
                      accountsOpBusy
                        ? 'Finish the current operation before managing accounts'
                        : accountsManage
                          ? 'Hide account management'
                          : 'Manage provider accounts'
                    }
                    aria-pressed={accountsManage}
                    onClick={() => setAccountsManage((v) => !v)}
                  >
                    Manage
                  </button>
                  {accountsManage ? (
                    <>
                      <button
                        type="button"
                        className="btn-micro"
                        disabled={
                          accountsOpBusy || !accountsQuery.data?.length
                        }
                        title="Verify all file shards still exist on providers"
                        onClick={() => healthMut.mutate()}
                      >
                        Health
                      </button>
                      <button
                        type="button"
                        className="btn-micro"
                        disabled={
                          accountsOpBusy || !accountsQuery.data?.length
                        }
                        title="Find orphan remotes across all accounts"
                        onClick={() => pruneAnalyzeMut.mutate()}
                      >
                        Prune
                      </button>
                      <button
                        type="button"
                        className="btn-micro"
                        disabled={accountsOpBusy}
                        onClick={() => startGoogleOAuth()}
                      >
                        + Google
                      </button>
                      <button
                        type="button"
                        className="btn-micro"
                        disabled={accountsOpBusy}
                        onClick={() => setMegaOpen(true)}
                      >
                        + MEGA
                      </button>
                    </>
                  ) : null}
                  <button
                    type="button"
                    className="btn-micro btn-icon"
                    onClick={toggleLeftPanel}
                    title="Collapse accounts panel"
                    aria-label="Collapse accounts panel"
                  >
                    <PanelChevron dir="left" />
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
                        const vaultBytes = stats?.vault_used ?? 0
                        const usedBytes = stats?.used
                        // Clamp vault segment inside used so estimate noise cannot overflow the bar.
                        const vaultInUsed =
                          usedBytes != null ? Math.min(vaultBytes, Math.max(0, usedBytes)) : vaultBytes
                        const vaultPct = storagePct(vaultInUsed, stats?.total ?? null)
                        const otherPct =
                          pct != null && vaultPct != null
                            ? Math.max(0, pct - vaultPct)
                            : pct != null
                              ? pct
                              : null
                        const uploadEnabled = account.enabled !== false
                        const uploadRow =
                          transfer?.kind === 'upload' || transfer?.kind === 'download'
                            ? transfer.byAccount?.find((a) => a.account_id === account.id)
                            : undefined
                        const uploadRate = uploadRow
                          ? transfer?.accountRates?.[account.id]
                          : undefined
                        return (
                          <div
                            key={account.id}
                            className={`account-card${uploadEnabled ? '' : ' is-disabled'}`}
                          >
                            <div className="list-row">
                              <span className={`provider-swatch ${account.provider}`} />
                              <span className="list-row-label" title={account.label}>
                                {account.label}
                              </span>
                              <span className="list-row-meta">
                                {account.provider}
                                {uploadEnabled ? '' : ' · off'}
                              </span>
                              {accountsManage ? (
                                <>
                                  <button
                                    type="button"
                                    className={`btn-micro${uploadEnabled ? ' active' : ''}`}
                                    disabled={accountsOpBusy || setAccountEnabledMut.isPending}
                                    title={
                                      uploadEnabled
                                        ? 'Disable for new uploads'
                                        : 'Enable for new uploads'
                                    }
                                    onClick={() =>
                                      setAccountEnabledMut.mutate({
                                        id: account.id,
                                        enabled: !uploadEnabled,
                                      })
                                    }
                                  >
                                    {uploadEnabled ? 'On' : 'Off'}
                                  </button>
                                  <button
                                    type="button"
                                    className="btn-micro btn-danger"
                                    disabled={accountsOpBusy}
                                    title="Disconnect account"
                                    onClick={() => deleteAccountMut.mutate(account.id)}
                                  >
                                    ×
                                  </button>
                                </>
                              ) : null}
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
                                  <div
                                    className="storage-track"
                                    title={[
                                      `Used ${formatBytes(stats?.used ?? null)}`,
                                      `vault ${formatBytes(vaultBytes)}`,
                                      stats?.total == null
                                        ? null
                                        : `of ${formatBytes(stats.total)}`,
                                    ]
                                      .filter(Boolean)
                                      .join(' · ')}
                                  >
                                    {vaultPct != null && vaultPct > 0 ? (
                                      <div
                                        className="storage-fill storage-fill-vault"
                                        style={{ width: `${vaultPct}%` }}
                                      />
                                    ) : null}
                                    {otherPct != null && otherPct > 0 ? (
                                      <div
                                        className={`storage-fill${stats?.full ? ' storage-fill-full' : ''}`}
                                        style={{ width: `${otherPct}%` }}
                                      />
                                    ) : null}
                                    {vaultPct == null && otherPct == null && (pct ?? 0) > 0 ? (
                                      <div
                                        className={`storage-fill${stats?.full ? ' storage-fill-full' : ''}`}
                                        style={{ width: `${pct ?? 0}%` }}
                                      />
                                    ) : null}
                                  </div>
                                  <div className="storage-meta">
                                    <span>
                                      {formatBytes(stats?.used ?? null)}
                                      {' / '}
                                      {stats?.total == null ? '∞' : formatBytes(stats.total)}
                                      {stats?.full ? ' · full' : ''}
                                      {' · '}
                                      <span
                                        className="storage-vault-label"
                                        title="Estimated ciphertext for vault shards + thumbs"
                                      >
                                        vault {formatBytes(vaultBytes)}
                                      </span>
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
                  <div className="empty-hint">
                    {accountsManage
                      ? 'Connect Google Drive or MEGA to store shards.'
                      : 'Turn on Manage to connect provider accounts.'}
                  </div>
                )}
              </div>
            </div>
          </Panel>

          <Separator className="separator" style={{ width: 4 }} disabled={leftCollapsed} />

          <Panel id="vault" minSize="40">
            <div className="panel">
              <div className="panel-header">
                {leftCollapsed ? (
                  <button
                    type="button"
                    className="btn-micro btn-icon"
                    onClick={toggleLeftPanel}
                    title="Expand accounts panel"
                    aria-label="Expand accounts panel"
                  >
                    <PanelChevron dir="right" />
                  </button>
                ) : null}
                <div className="panel-title">Vault</div>
                <div className="panel-header-actions">
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    hidden
                    onChange={(e) => {
                      const list = e.target.files
                      if (list && list.length > 0) {
                        queueUpload(path, collectFromFileList(list))
                      }
                      e.target.value = ''
                    }}
                  />
                  <input
                    ref={(el) => {
                      folderInputRef.current = el
                      if (el) {
                        el.setAttribute('webkitdirectory', '')
                        el.setAttribute('directory', '')
                      }
                    }}
                    type="file"
                    multiple
                    hidden
                    onChange={(e) => {
                      const list = e.target.files
                      if (list && list.length > 0) {
                        queueUpload(path, collectFromFileList(list))
                      }
                      e.target.value = ''
                    }}
                  />
                  <button
                    type="button"
                    className="btn-micro"
                    disabled={busy}
                    onClick={() => {
                      setMkdirName('')
                      setMkdirOpen(true)
                    }}
                  >
                    New folder
                  </button>
                  <button
                    type="button"
                    className="btn-micro"
                    disabled={busy}
                    title={`Upload files into ${path}`}
                    onClick={() => fileInputRef.current?.click()}
                  >
                    {uploadMut.isPending
                      ? 'Uploading…'
                      : uploadConflictBusy
                        ? 'Checking…'
                        : 'Upload'}
                  </button>
                  <button
                    type="button"
                    className="btn-micro"
                    disabled={busy}
                    title={`Upload a folder into ${path}`}
                    onClick={() => folderInputRef.current?.click()}
                  >
                    {uploadConflictBusy ? 'Checking…' : 'Upload folder'}
                  </button>
                  <button
                    type="button"
                    className="btn-micro"
                    disabled={!canRename || busy}
                    onClick={startRename}
                  >
                    Rename
                  </button>
                  <button
                    type="button"
                    className="btn-micro"
                    disabled={selectedEntries.length === 0 || busy}
                    onClick={() => downloadMut.mutate(selectedEntries)}
                    title={
                      downloadFolderName
                        ? `Save into ${downloadFolderName}`
                        : selectedEntries.length > 1 || selectedDirs.length > 0
                          ? 'Download selection as zip'
                          : 'Download selected file'
                    }
                  >
                    {downloadMut.isPending
                      ? 'Downloading…'
                      : selectedEntries.length > 1
                        ? `Download (${selectedEntries.length})`
                        : selectedDirs.length === 1
                          ? 'Download folder'
                          : 'Download'}
                  </button>
                  {supportsDownloadFolder() ? (
                    <button
                      type="button"
                      className="btn-micro"
                      disabled={busy}
                      title={
                        downloadFolderName
                          ? `Download folder: ${downloadFolderName} (click to change)`
                          : 'Choose download folder'
                      }
                      onClick={() => {
                        void ensureDownloadFolder({ forcePick: true }).then((dir) => {
                          if (dir) setDownloadFolderName(dir.name)
                        })
                      }}
                    >
                      {downloadFolderName ? `To: ${downloadFolderName}` : 'Save folder…'}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="btn-micro btn-danger"
                    disabled={!canDelete || busy}
                    onClick={() => {
                      const dirs = selectedDirs.length
                      const msg =
                        dirs > 0
                          ? `Delete ${selectedEntries.length} item(s), including ${dirs} folder(s) and their contents?`
                          : null
                      if (msg && !window.confirm(msg)) return
                      deleteFileMut.mutate(selectedEntries)
                    }}
                  >
                    {deleteFileMut.isPending
                      ? 'Deleting…'
                      : selectedEntries.length > 1
                        ? `Delete (${selectedEntries.length})`
                        : 'Delete'}
                  </button>
                  <div className="view-toggle" role="group" aria-label="File view">
                    <button
                      type="button"
                      className={`btn-micro btn-icon${fileView === 'list' ? ' active' : ''}`}
                      onClick={() => setFileView('list')}
                      title="List view"
                      aria-label="List view"
                      aria-pressed={fileView === 'list'}
                    >
                      <ListViewIcon />
                    </button>
                    <button
                      type="button"
                      className={`btn-micro btn-icon${fileView === 'grid' ? ' active' : ''}`}
                      onClick={() => setFileView('grid')}
                      title="Grid view"
                      aria-label="Grid view"
                      aria-pressed={fileView === 'grid'}
                    >
                      <GridViewIcon />
                    </button>
                  </div>
                  {rightCollapsed ? (
                    <button
                      type="button"
                      className="btn-micro btn-icon"
                      onClick={toggleRightPanel}
                      title="Expand inspect panel"
                      aria-label="Expand inspect panel"
                    >
                      <PanelChevron dir="left" />
                    </button>
                  ) : null}
                </div>
              </div>

              <div className="breadcrumbs">
                <button
                  type="button"
                  className={`crumb${dropTarget === '/' ? ' drop-target' : ''}`}
                  onClick={() => goCrumb(-1)}
                  onDragOver={(e) => onDragOverDest('/', e)}
                  onDragLeave={() => setDropTarget((t) => (t === '/' ? null : t))}
                  onDrop={(e) => onDropOnto('/', e)}
                >
                  /
                </button>
                {crumbs.map((part, i) => {
                  const crumbPath = '/' + crumbs.slice(0, i + 1).join('/')
                  return (
                    <span key={`${part}-${i}`} style={{ display: 'inline-flex', gap: 4 }}>
                      <span className="crumb-sep">/</span>
                      <button
                        type="button"
                        className={`crumb${dropTarget === crumbPath ? ' drop-target' : ''}`}
                        onClick={() => goCrumb(i)}
                        onDragOver={(e) => onDragOverDest(crumbPath, e)}
                        onDragLeave={() =>
                          setDropTarget((t) => (t === crumbPath ? null : t))
                        }
                        onDrop={(e) => onDropOnto(crumbPath, e)}
                      >
                        {part}
                      </button>
                    </span>
                  )
                })}
                <span className="list-row-meta" style={{ marginLeft: 8 }}>
                  upload → {path}
                </span>
              </div>

              <div className="vault-search">
                <input
                  className="input vault-search-input"
                  type="search"
                  placeholder={
                    searchRegex ? 'Regex on name…' : 'Search names…'
                  }
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  aria-label="Search files and folders"
                />
                <select
                  className="input vault-search-select"
                  value={searchKind}
                  onChange={(e) => setSearchKind(e.target.value as VaultSearchKind)}
                  aria-label="Search kind"
                >
                  <option value="all">All</option>
                  <option value="file">Files</option>
                  <option value="dir">Folders</option>
                </select>
                <select
                  className="input vault-search-select"
                  value={searchScope}
                  onChange={(e) =>
                    setSearchScope(e.target.value as VaultSearchScope)
                  }
                  aria-label="Search scope"
                >
                  <option value="folder">This folder</option>
                  <option value="global">Global</option>
                </select>
                <label className="vault-search-regex">
                  <input
                    type="checkbox"
                    checked={searchRegex}
                    onChange={(e) => setSearchRegex(e.target.checked)}
                  />
                  Regex
                </label>
                {searchInput ? (
                  <button
                    type="button"
                    className="btn-micro"
                    onClick={() => setSearchInput('')}
                  >
                    Clear
                  </button>
                ) : null}
                {searchActive && searchQuery.data?.truncated ? (
                  <span className="list-row-meta">Truncated</span>
                ) : null}
              </div>

              <div
                className={`panel-body${dropTarget === path ? ' vault-drop-active' : ''}`}
                onDragOver={(e) => onDragOverDest(path, e)}
                onDragLeave={() => setDropTarget((t) => (t === path ? null : t))}
                onDrop={(e) => onDropOnto(path, e)}
              >
                {searchActive ? (
                  searchQuery.isError ? (
                    <div className="empty-hint" style={{ color: 'var(--error)' }}>
                      {(searchQuery.error as Error).message}
                      {(searchQuery.error as Error).message
                        .toLowerCase()
                        .includes('not found')
                        ? ' (restart the backend if you just added search)'
                        : ''}
                    </div>
                  ) : searchQuery.isLoading && !searchQuery.data ? (
                    <div className="empty-hint">Searching…</div>
                  ) : entries && entries.length > 0 ? (
                    fileView === 'grid' ? (
                      <>
                        <div className="file-grid-toolbar">
                          <input
                            type="checkbox"
                            className="file-check"
                            checked={allFilesSelected}
                            disabled={fileEntries.length === 0 || busy}
                            onChange={toggleSelectAllFiles}
                            aria-label="Select all files"
                          />
                          <span className="list-row-meta">
                            {selectedIds.length > 0
                              ? `${selectedIds.length} selected`
                              : `${entries.length} match${entries.length === 1 ? '' : 'es'}`}
                          </span>
                        </div>
                        <div className="file-grid">
                          {entries.map((entry) => {
                            const isSelected = selectedIds.includes(entry.id)
                            return (
                              <div
                                key={entry.id}
                                className={[
                                  'file-grid-item',
                                  isSelected ? 'selected' : '',
                                ]
                                  .filter(Boolean)
                                  .join(' ')}
                                onClick={(e) => selectEntry(entry, e)}
                                onDoubleClick={() => openEntry(entry)}
                                title={entry.path}
                              >
                                <input
                                  type="checkbox"
                                  className="file-grid-check"
                                  checked={isSelected}
                                  disabled={busy}
                                  onClick={(e) => e.stopPropagation()}
                                  onChange={(e) =>
                                    onEntryCheckChange(entry, e.target.checked)
                                  }
                                  aria-label={`Select ${entry.name}`}
                                />
                                <FileThumb entry={entry} size="lg" />
                                <span className={`file-grid-name ${entry.kind}`}>
                                  {entry.name}
                                </span>
                                <span className="file-grid-meta" title={entry.path}>
                                  {entry.path}
                                </span>
                              </div>
                            )
                          })}
                        </div>
                      </>
                    ) : (
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
                            <th className="col-thumb" aria-label="Preview" />
                            <th>Name</th>
                            <th>Path</th>
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
                                onDoubleClick={() => openEntry(entry)}
                              >
                                <td
                                  className="col-check"
                                  onClick={(e) => e.stopPropagation()}
                                >
                                  <input
                                    type="checkbox"
                                    className="file-check"
                                    checked={isSelected}
                                    disabled={busy}
                                    onChange={(e) =>
                                      onEntryCheckChange(entry, e.target.checked)
                                    }
                                    aria-label={`Select ${entry.name}`}
                                  />
                                </td>
                                <td className="col-thumb">
                                  <FileThumb entry={entry} />
                                </td>
                                <td>
                                  <span className={`file-name ${entry.kind}`}>
                                    {entry.name}
                                  </span>
                                </td>
                                <td className="list-row-meta" title={entry.path}>
                                  {entry.path}
                                </td>
                                <td className="list-row-meta">
                                  {formatBytes(entry.size)}
                                </td>
                                <td className="list-row-meta">
                                  {entry.kind === 'file' ? entry.shard_count : '—'}
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    )
                  ) : (
                    <div className="empty-hint">No matches.</div>
                  )
                ) : filesQuery.isLoading ? (
                  <div className="empty-hint">Loading…</div>
                ) : entries && entries.length > 0 ? (
                  fileView === 'grid' ? (
                    <>
                      <div className="file-grid-toolbar">
                        <input
                          type="checkbox"
                          className="file-check"
                          checked={allFilesSelected}
                          disabled={fileEntries.length === 0 || busy}
                          onChange={toggleSelectAllFiles}
                          aria-label="Select all files"
                        />
                        <span className="list-row-meta">
                          {selectedIds.length > 0
                            ? `${selectedIds.length} selected`
                            : `${entries.length} items`}
                        </span>
                      </div>
                      <div className="file-grid">
                        {entries.map((entry) => {
                          const isSelected = selectedIds.includes(entry.id)
                          const isDrop =
                            entry.kind === 'dir' && dropTarget === entry.path
                          return (
                            <div
                              key={entry.id}
                              className={[
                                'file-grid-item',
                                isSelected ? 'selected' : '',
                                isDrop ? 'drop-target' : '',
                              ]
                                .filter(Boolean)
                                .join(' ')}
                              {...entryDragProps(entry)}
                              onClick={(e) => selectEntry(entry, e)}
                              onDoubleClick={() => openEntry(entry)}
                              title={entry.name}
                            >
                              <input
                                type="checkbox"
                                className="file-grid-check"
                                checked={isSelected}
                                disabled={busy}
                                onClick={(e) => e.stopPropagation()}
                                onChange={(e) =>
                                  onEntryCheckChange(entry, e.target.checked)
                                }
                                aria-label={`Select ${entry.name}`}
                              />
                              <FileThumb entry={entry} size="lg" />
                              <span className={`file-grid-name ${entry.kind}`}>
                                {entry.name}
                              </span>
                              {entry.kind === 'file' ? (
                                <span className="file-grid-meta">
                                  {formatBytes(entry.size)}
                                </span>
                              ) : null}
                            </div>
                          )
                        })}
                      </div>
                    </>
                  ) : (
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
                          <th className="col-thumb" aria-label="Preview" />
                          <th>Name</th>
                          <th>Size</th>
                          <th>Shards</th>
                        </tr>
                      </thead>
                      <tbody>
                        {entries.map((entry) => {
                          const isSelected = selectedIds.includes(entry.id)
                          const isDrop =
                            entry.kind === 'dir' && dropTarget === entry.path
                          return (
                            <tr
                              key={entry.id}
                              className={[
                                isSelected ? 'selected' : '',
                                isDrop ? 'drop-target' : '',
                              ]
                                .filter(Boolean)
                                .join(' ') || undefined}
                              {...entryDragProps(entry)}
                              onClick={(e) => selectEntry(entry, e)}
                              onDoubleClick={() => openEntry(entry)}
                            >
                              <td
                                className="col-check"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <input
                                  type="checkbox"
                                  className="file-check"
                                  checked={isSelected}
                                  disabled={busy}
                                  onChange={(e) =>
                                    onEntryCheckChange(entry, e.target.checked)
                                  }
                                  aria-label={`Select ${entry.name}`}
                                />
                              </td>
                              <td className="col-thumb">
                                <FileThumb entry={entry} />
                              </td>
                              <td>
                                <span className={`file-name ${entry.kind}`}>
                                  {entry.name}
                                </span>
                              </td>
                              <td className="list-row-meta">
                                {formatBytes(entry.size)}
                              </td>
                              <td className="list-row-meta">
                                {entry.kind === 'file' ? entry.shard_count : '—'}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  )
                ) : (
                  <div className="empty-hint">
                    Empty folder. Upload here or create a subfolder.
                  </div>
                )}
              </div>
            </div>
          </Panel>

          <Separator className="separator" style={{ width: 4 }} disabled={rightCollapsed} />

          <Panel
            id="inspect"
            panelRef={rightPanelRef}
            defaultSize="24"
            minSize="16"
            collapsible
            collapsedSize={0}
            onResize={syncRightCollapsed}
          >
            <div className="panel">
              <div className="panel-header">
                <div className="panel-title">Inspect</div>
                <div className="panel-header-actions">
                  <button
                    type="button"
                    className="btn-micro btn-icon"
                    onClick={toggleRightPanel}
                    title="Collapse inspect panel"
                    aria-label="Collapse inspect panel"
                  >
                    <PanelChevron dir="right" />
                  </button>
                </div>
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
                ) : detailQuery.data && inspectTarget ? (
                  <div className="inspect">
                    <div className="inspect-preview">
                      <FileThumb
                        entry={{
                          ...inspectTarget,
                          thumb_status:
                            detailQuery.data.thumb_status ??
                            inspectTarget.thumb_status,
                        }}
                        size="wide"
                      />
                    </div>
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

      {mkdirOpen ? (
        <div className="modal-backdrop" onClick={() => setMkdirOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">New folder</div>
            <div className="modal-body">
              <div>
                <div className="field-label">Name</div>
                <input
                  className="input"
                  type="text"
                  value={mkdirName}
                  autoFocus
                  onChange={(e) => setMkdirName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && mkdirName.trim()) mkdirMut.mutate()
                  }}
                />
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                Created in {path}
              </div>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => setMkdirOpen(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={!mkdirName.trim() || mkdirMut.isPending}
                onClick={() => mkdirMut.mutate()}
              >
                {mkdirMut.isPending ? 'Creating…' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {renameOpen ? (
        <div className="modal-backdrop" onClick={() => setRenameOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">Rename</div>
            <div className="modal-body">
              <div>
                <div className="field-label">New name</div>
                <input
                  className="input"
                  type="text"
                  value={renameValue}
                  autoFocus
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && renameValue.trim()) renameMut.mutate()
                  }}
                />
              </div>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => setRenameOpen(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={!renameValue.trim() || renameMut.isPending}
                onClick={() => renameMut.mutate()}
              >
                {renameMut.isPending ? 'Renaming…' : 'Rename'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {filePreview ? (
        <FilePreviewDialog
          entry={filePreview.entry}
          kind={filePreview.kind}
          imagePeers={previewImagePeers}
          maxBytes={previewMaxBytes}
          onClose={() => setFilePreview(null)}
          onDownload={(e) => downloadMut.mutate([e])}
          onChangeEntry={(e) => setFilePreview({ entry: e, kind: 'image' })}
        />
      ) : null}

      {uploadConflict ? (
        <div
          className="modal-backdrop"
          onClick={() => {
            if (!uploadMut.isPending) setUploadConflict(null)
          }}
        >
          <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">Upload conflicts</div>
            <div className="modal-body">
              <div className="conflict-summary">
                <span className="data">{uploadConflict.scan.conflicts.length}</span>{' '}
                {uploadConflict.scan.conflicts.length === 1
                  ? 'name already exists'
                  : 'names already exist'}
                . Choose replace, skip, or auto-rename for each.
              </div>
              <div className="conflict-apply-all">
                <span className="field-label">Apply to all</span>
                <div className="conflict-apply-btns">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => applyUploadConflictAction('replace')}
                  >
                    Replace
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => applyUploadConflictAction('skip')}
                  >
                    Skip
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => applyUploadConflictAction('rename')}
                  >
                    Rename
                  </button>
                </div>
              </div>
              <div className="conflict-scroll">
                {uploadConflict.scan.conflicts.map((c) => {
                  const action = uploadConflict.actions[c.relativePath] ?? 'rename'
                  return (
                    <div key={c.relativePath} className="conflict-row">
                      <div className="conflict-path" title={c.relativePath}>
                        {c.relativePath}
                      </div>
                      <div className="conflict-meta">
                        {c.existingId
                          ? 'Exists in vault'
                          : 'Duplicate in this upload'}
                        {action === 'rename' ? (
                          <>
                            {' → '}
                            <span className="data">{c.suggestedName}</span>
                            {' (auto)'}
                          </>
                        ) : null}
                      </div>
                      <select
                        className="input conflict-action"
                        value={action}
                        onChange={(e) => {
                          const next = e.target.value as ConflictAction
                          setUploadConflict((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  actions: {
                                    ...prev.actions,
                                    [c.relativePath]: next,
                                  },
                                }
                              : prev,
                          )
                        }}
                      >
                        <option value="replace">Replace</option>
                        <option value="skip">Skip</option>
                        <option value="rename">Rename</option>
                      </select>
                    </div>
                  )
                })}
              </div>
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="btn"
                onClick={() => setUploadConflict(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={() => confirmUploadConflicts()}
              >
                Continue upload
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {pruneOpen ? (
        <div
          className="modal-backdrop"
          onClick={() => {
            if (!pruneAnalyzing && !pruneApplyMut.isPending) {
              setPruneOpen(false)
              setPruneReport(null)
            }
          }}
        >
          <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">Prune accounts</div>
            <div className="modal-body">
              {pruneAnalyzing || !pruneReport ? (
                <div className="prune-empty">Analyzing provider vaults…</div>
              ) : (
                <>
                  <div className="prune-summary">
                    Scanned{' '}
                    <span className="data">{pruneReport.account_count}</span> account
                    {pruneReport.account_count === 1 ? '' : 's'}:{' '}
                    <span className="data">{pruneReport.total_orphans}</span> orphan
                    {pruneReport.total_orphans === 1 ? '' : 's'},{' '}
                    <span className="data">{pruneReport.total_stale_sessions}</span> stale
                    resume session
                    {pruneReport.total_stale_sessions === 1 ? '' : 's'}
                    {pruneReport.errors > 0 ? (
                      <>
                        {' '}
                        · <span className="prune-error">{pruneReport.errors} error(s)</span>
                      </>
                    ) : null}
                  </div>
                  <div className="prune-scroll">
                    {pruneReport.accounts.map((row) => {
                      const dirty =
                        row.orphans.length > 0 || row.stale_sessions.length > 0 || !!row.error
                      if (!dirty) {
                        return (
                          <div key={row.account_id} className="prune-account-block">
                            <div className="prune-account-title">
                              <span className={`provider-swatch ${row.provider}`} />
                              <span>{row.label}</span>
                              <span className="prune-account-meta">
                                {row.scanned} scanned · clean
                              </span>
                            </div>
                          </div>
                        )
                      }
                      return (
                        <div key={row.account_id} className="prune-account-block">
                          <div className="prune-account-title">
                            <span className={`provider-swatch ${row.provider}`} />
                            <span>{row.label}</span>
                            <span className="prune-account-meta">
                              {row.orphans.length} orphan
                              {row.orphans.length === 1 ? '' : 's'} ·{' '}
                              {row.stale_sessions.length} stale
                            </span>
                          </div>
                          {row.error ? (
                            <div className="prune-error" title={row.error}>
                              {row.error}
                            </div>
                          ) : null}
                          {row.orphans.length > 0 ? (
                            <ul className="prune-list">
                              {row.orphans.map((o) => (
                                <li key={o.id} title={`${o.kind}: ${o.name}`}>
                                  {o.kind}:{' '}
                                  {o.path ? (
                                    <>
                                      <span className="data">{o.path}</span>
                                      <span className="prune-account-meta">
                                        {' '}
                                        ({o.name})
                                      </span>
                                    </>
                                  ) : (
                                    o.name
                                  )}
                                </li>
                              ))}
                            </ul>
                          ) : null}
                          {row.stale_sessions.length > 0 ? (
                            <ul className="prune-list">
                              {row.stale_sessions.map((s) => (
                                <li key={s.id}>
                                  session ({s.status}): {s.filename}
                                </li>
                              ))}
                            </ul>
                          ) : null}
                        </div>
                      )
                    })}
                  </div>
                  <div className="prune-empty">
                    Cleaning deletes orphan remotes under the vault folder and clears
                    abandoned or failed resume sessions (partial uploads with no live job).
                    Indexed vault files are not modified.
                  </div>
                </>
              )}
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="btn"
                disabled={pruneAnalyzing || pruneApplyMut.isPending}
                onClick={() => {
                  setPruneOpen(false)
                  setPruneReport(null)
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={
                  pruneAnalyzing ||
                  pruneApplyMut.isPending ||
                  !pruneReport ||
                  (pruneReport.total_orphans === 0 &&
                    pruneReport.total_stale_sessions === 0)
                }
                onClick={() => pruneApplyMut.mutate()}
              >
                {pruneApplyMut.isPending
                  ? 'Cleaning…'
                  : pruneReport &&
                      pruneReport.total_orphans === 0 &&
                      pruneReport.total_stale_sessions === 0
                    ? 'Nothing to clean'
                    : 'Clean orphans'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {healthOpen ? (
        <div
          className="modal-backdrop"
          onClick={() => {
            if (!healthRunning) {
              setHealthOpen(false)
              setHealthReport(null)
            }
          }}
        >
          <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">Shard health</div>
            <div className="modal-body">
              {healthRunning || !healthReport ? (
                <div className="prune-empty">Checking indexed shards on providers…</div>
              ) : (
                <>
                  <div className="prune-summary">
                    {healthReport.healthy ? (
                      <>
                        All{' '}
                        <span className="data">{healthReport.ok}</span> remote object
                        {healthReport.ok === 1 ? '' : 's'} present (
                        <span className="data">{healthReport.file_count}</span> file
                        {healthReport.file_count === 1 ? '' : 's'}).
                      </>
                    ) : (
                      <>
                        Checked{' '}
                        <span className="data">{healthReport.checked}</span> /{' '}
                        <span className="data">{healthReport.object_count}</span>
                        : <span className="prune-error">{healthReport.issue_count} issue(s)</span>
                        {' · '}
                        <span className="data">{healthReport.ok}</span> ok
                      </>
                    )}
                  </div>
                  {healthReport.account_errors.length > 0 ? (
                    <div className="prune-scroll">
                      {healthReport.account_errors.map((row) => (
                        <div key={row.account_id} className="prune-error" title={row.error}>
                          {row.account_id}: {row.error}
                        </div>
                      ))}
                    </div>
                  ) : null}
                  {!healthReport.healthy ? (
                    <div className="prune-scroll">
                      {healthReport.files.map((file) => (
                        <div key={file.file_id} className="prune-account-block">
                          <div className="prune-account-title">
                            <span className="data" title={file.path}>
                              {file.path}
                            </span>
                            <span className="prune-account-meta">
                              {file.issues.length} issue
                              {file.issues.length === 1 ? '' : 's'}
                            </span>
                          </div>
                          <ul className="prune-list">
                            {file.issues.map((issue, idx) => (
                              <li
                                key={`${issue.remote_id}-${idx}`}
                                title={issue.error ?? issue.remote_id}
                              >
                                {issue.kind === 'thumb'
                                  ? 'thumbnail'
                                  : `shard ${issue.i}`}
                                {' · '}
                                {issue.provider || 'account'} {issue.label}
                                {' · '}
                                {issue.reason}
                                {issue.error ? `: ${issue.error}` : ''}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ))}
                    </div>
                  ) : null}
                  <div className="prune-empty">
                    Health only reports missing remotes. It does not repair or delete
                    anything.
                  </div>
                </>
              )}
            </div>
            <div className="modal-actions">
              {healthRunning ? (
                <button
                  type="button"
                  className="btn-danger"
                  onClick={() => jobAbortRef.current?.abort()}
                >
                  Cancel
                </button>
              ) : null}
              <button
                type="button"
                className="btn-primary"
                disabled={healthRunning}
                onClick={() => {
                  setHealthOpen(false)
                  setHealthReport(null)
                }}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {evacuateOpen && evacuateDetail ? (
        <div
          className="modal-backdrop"
          onClick={() => {
            if (!evacuateMut.isPending) {
              setEvacuateOpen(false)
              setEvacuateDetail(null)
            }
          }}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">Account still holds data</div>
            <div className="modal-body">
              <div className="prune-summary">
                <span className="list-row-label" title={evacuateDetail.label ?? undefined}>
                  {evacuateDetail.label || evacuateDetail.account_id}
                </span>
                {' holds '}
                {evacuateDetail.shard_count > 0 ? (
                  <>
                    <span className="data">{evacuateDetail.shard_count}</span> shard
                    {evacuateDetail.shard_count === 1 ? '' : 's'} in{' '}
                    <span className="data">{evacuateDetail.file_count}</span> file
                    {evacuateDetail.file_count === 1 ? '' : 's'}
                  </>
                ) : null}
                {evacuateDetail.shard_count > 0 && evacuateDetail.thumb_count > 0
                  ? ' and '
                  : null}
                {evacuateDetail.thumb_count > 0 ? (
                  <>
                    <span className="data">{evacuateDetail.thumb_count}</span> thumbnail
                    {evacuateDetail.thumb_count === 1 ? '' : 's'}
                  </>
                ) : null}
                .
              </div>
              {evacuateDetail.sample_paths.length > 0 ? (
                <div className="prune-empty" title={evacuateDetail.sample_paths.join(', ')}>
                  Examples: {evacuateDetail.sample_paths.slice(0, 3).join(', ')}
                </div>
              ) : null}
              <div className="prune-empty">
                Move this data onto your other enabled accounts, then disconnect.
              </div>
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="btn"
                disabled={evacuateMut.isPending}
                onClick={() => {
                  setEvacuateOpen(false)
                  setEvacuateDetail(null)
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={evacuateMut.isPending}
                onClick={() => evacuateMut.mutate(evacuateDetail.account_id)}
              >
                {evacuateMut.isPending ? 'Moving…' : 'Move data & disconnect'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

    </div>
  )
}
