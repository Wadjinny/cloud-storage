import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
} from 'react'
import { cancelAllUploadJobs, type UploadAccountStats } from './api/vault'
import { TransferBatchPanel } from './TransferBatchPanel'
import type { TransferFileStatus, TransferTreeNode } from './transferTree'

export type TransferKind =
  | 'upload'
  | 'download'
  | 'delete'
  | 'prune'
  | 'health'
  | 'evacuate'
  | 'copy'

export const TRANSFER_LABELS: Record<TransferKind, string> = {
  upload: 'Upload',
  download: 'Download',
  delete: 'Delete',
  prune: 'Prune',
  health: 'Health',
  evacuate: 'Move data',
  copy: 'Copy',
}

export type AccountRate = { shardsPerSec: number; mbPerSec: number }

export type TransferState = {
  kind: TransferKind
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
  itemLabel?: string | null
  itemIndex?: number
  itemTotal?: number
}

export type BatchQueueState = {
  kind: 'upload' | 'download'
  tree: TransferTreeNode[]
  statuses: Record<string, TransferFileStatus>
  visible: boolean
}

type TransferContextValue = {
  transfer: TransferState | null
  setTransfer: Dispatch<SetStateAction<TransferState | null>>
  batchQueue: BatchQueueState | null
  setBatchQueue: Dispatch<SetStateAction<BatchQueueState | null>>
  /** True while an upload batch holds an AbortController (even if transfer UI shows another job). */
  uploadActive: boolean
  setUploadActive: Dispatch<SetStateAction<boolean>>
  uploadAbortRef: RefObject<AbortController | null>
  downloadAbortRef: RefObject<AbortController | null>
  /** Shared abort for delete / health (and other non upload-download jobs). */
  jobAbortRef: RefObject<AbortController | null>
  cancelUploads: () => Promise<void>
}

const TransferContext = createContext<TransferContextValue | null>(null)

export function TransferProvider({ children }: { children: ReactNode }) {
  const [transfer, setTransfer] = useState<TransferState | null>(null)
  const [batchQueue, setBatchQueue] = useState<BatchQueueState | null>(null)
  const [uploadActive, setUploadActive] = useState(false)
  const uploadAbortRef = useRef<AbortController | null>(null)
  const downloadAbortRef = useRef<AbortController | null>(null)
  const jobAbortRef = useRef<AbortController | null>(null)

  const cancelUploads = useCallback(async () => {
    // Server-side abort first so ClientDisconnect cannot finish() queued shards.
    await cancelAllUploadJobs()
    uploadAbortRef.current?.abort()
  }, [])

  const value = useMemo(
    () => ({
      transfer,
      setTransfer,
      batchQueue,
      setBatchQueue,
      uploadActive,
      setUploadActive,
      uploadAbortRef,
      downloadAbortRef,
      jobAbortRef,
      cancelUploads,
    }),
    [transfer, batchQueue, uploadActive, cancelUploads],
  )

  return (
    <TransferContext.Provider value={value}>
      <div className="app-root">
        {children}
        <TransferOverlay />
      </div>
    </TransferContext.Provider>
  )
}

export function useTransfer(): TransferContextValue {
  const ctx = useContext(TransferContext)
  if (!ctx) throw new Error('useTransfer requires TransferProvider')
  return ctx
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

export function ShardBoxesBar({
  done,
  active = 0,
  total,
  variant = 'transfer',
}: {
  done: number
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

function TransferOverlay() {
  const {
    transfer,
    batchQueue,
    setBatchQueue,
    uploadActive,
    downloadAbortRef,
    jobAbortRef,
    cancelUploads,
  } = useTransfer()

  const showUploadCancel = uploadActive || transfer?.kind === 'upload'
  // When another job steals/clears the transfer slot, keep a cancel affordance
  // for background uploads that are still running.
  const displayTransfer: TransferState | null =
    transfer ??
    (uploadActive
      ? {
          kind: 'upload',
          percent: 0,
          message: 'Uploads still running…',
        }
      : null)

  return (
    <>
      {displayTransfer ? (
        <div className="transfer-bar">
          <div className="transfer-meta">
            <span className="transfer-kind">{TRANSFER_LABELS[displayTransfer.kind]}</span>
            <span className="transfer-msg">{displayTransfer.message}</span>
            {displayTransfer.itemTotal != null && displayTransfer.itemTotal > 0 ? (
              <span
                className="transfer-stats"
                title={
                  displayTransfer.kind === 'download' && displayTransfer.itemTotal > 1
                    ? 'Current file / files in archive'
                    : 'Files in this batch'
                }
              >
                {(displayTransfer.itemIndex ?? 0) + 1}/{displayTransfer.itemTotal}{' '}
                {displayTransfer.itemTotal === 1 ? 'file' : 'files'}
              </span>
            ) : null}
            <span className="transfer-pct">{displayTransfer.percent}%</span>
            {(displayTransfer.kind === 'upload' || displayTransfer.kind === 'download') &&
            displayTransfer.doneShards != null ? (
              <span className="transfer-stats" title="Shards done / expected · active">
                {displayTransfer.doneShards}
                {displayTransfer.expectedShards != null
                  ? `/${displayTransfer.expectedShards}`
                  : ''}{' '}
                sh
                {(displayTransfer.activeShards ?? 0) > 0 ? (
                  <span className="transfer-active">
                    {' '}
                    · {displayTransfer.activeShards} active
                  </span>
                ) : null}
              </span>
            ) : null}
            {(displayTransfer.kind === 'upload' ||
              displayTransfer.kind === 'download' ||
              displayTransfer.kind === 'evacuate') &&
            displayTransfer.doneBytes != null ? (
              <span className="transfer-stats" title="Transferred / total">
                {formatBytes(displayTransfer.doneBytes)}
                {displayTransfer.totalBytes != null
                  ? ` / ${formatBytes(displayTransfer.totalBytes)}`
                  : ''}
              </span>
            ) : null}
            {displayTransfer.kind === 'upload' || displayTransfer.kind === 'download' ? (
              <span className="transfer-stats" title="1 min average">
                {formatRate(displayTransfer.shardsPerSec, 'sh/s')} ·{' '}
                {formatRate(displayTransfer.mbPerSec, 'MB/s')}
              </span>
            ) : null}
            {displayTransfer.kind === 'evacuate' ? (
              <span className="transfer-stats" title="1 min average">
                {formatRate(displayTransfer.mbPerSec, 'MB/s')}
              </span>
            ) : null}
            {showUploadCancel ? (
              <button
                type="button"
                className="btn-micro btn-danger"
                title="Stop all in-flight uploads"
                onClick={() => {
                  void cancelUploads()
                }}
              >
                {displayTransfer.kind === 'upload' ? 'Cancel' : 'Cancel uploads'}
              </button>
            ) : null}
            {displayTransfer.kind === 'download' ? (
              <button
                type="button"
                className="btn-micro btn-danger"
                onClick={() => downloadAbortRef.current?.abort()}
              >
                Cancel
              </button>
            ) : null}
            {displayTransfer.kind === 'delete' || displayTransfer.kind === 'health' ? (
              <button
                type="button"
                className="btn-micro btn-danger"
                onClick={() => jobAbortRef.current?.abort()}
              >
                Cancel
              </button>
            ) : null}
            {displayTransfer.kind === 'evacuate' ? (
              <button
                type="button"
                className="btn-micro btn-danger"
                title="Stop after in-flight objects finish. Moved data is kept; run again to continue."
                onClick={() => jobAbortRef.current?.abort()}
              >
                Stop safely
              </button>
            ) : null}
          </div>
          {(displayTransfer.kind === 'upload' || displayTransfer.kind === 'download') &&
          (displayTransfer.expectedShards != null ||
            (displayTransfer.doneShards ?? 0) > 0) ? (
            <ShardBoxesBar
              done={displayTransfer.doneShards ?? 0}
              active={displayTransfer.activeShards ?? 0}
              total={Math.max(
                displayTransfer.expectedShards ?? 0,
                (displayTransfer.doneShards ?? 0) + (displayTransfer.activeShards ?? 0),
                1,
              )}
            />
          ) : (
            <div className="transfer-track">
              <div
                className="transfer-fill"
                style={{ width: `${displayTransfer.percent}%` }}
              />
            </div>
          )}
          {displayTransfer.itemLabel ? (
            <div className="transfer-queue">
              <div className="transfer-queue-row">
                <span className="transfer-queue-label">Current</span>
                <span className="transfer-file" title={displayTransfer.itemLabel}>
                  {displayTransfer.itemLabel}
                </span>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {batchQueue?.visible ? (
        <TransferBatchPanel
          kind={batchQueue.kind}
          tree={batchQueue.tree}
          statuses={batchQueue.statuses}
          onClose={() => setBatchQueue((q) => (q ? { ...q, visible: false } : q))}
        />
      ) : null}

      {batchQueue && !batchQueue.visible ? (
        <button
          type="button"
          className="transfer-batch-reopen"
          onClick={() => setBatchQueue((q) => (q ? { ...q, visible: true } : q))}
          title="Show transfer queue"
          aria-label="Show transfer queue"
        >
          Queue
        </button>
      ) : null}
    </>
  )
}
