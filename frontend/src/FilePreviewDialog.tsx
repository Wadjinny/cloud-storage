import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { fetchVaultFileBlob, type VaultEntry } from './api/vault'
import {
  DEFAULT_PREVIEW_MAX_BYTES,
  formatPreviewLimit,
  mimeForFilename,
  type PreviewOpenKind,
} from './filePreview'

type CacheEntry = {
  promise: Promise<string>
  url?: string
}

function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

export function FilePreviewDialog({
  entry,
  kind,
  imagePeers,
  maxBytes = DEFAULT_PREVIEW_MAX_BYTES,
  onClose,
  onDownload,
  onChangeEntry,
}: {
  entry: VaultEntry
  kind: PreviewOpenKind
  imagePeers: VaultEntry[]
  maxBytes?: number
  onClose: () => void
  onDownload: (entry: VaultEntry) => void
  onChangeEntry: (entry: VaultEntry) => void
}) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [loading, setLoading] = useState(kind !== 'download')
  const [error, setError] = useState<string | null>(null)
  const cacheRef = useRef(new Map<string, CacheEntry>())
  const loadSeqRef = useRef(0)

  const imageIndex =
    kind === 'image' ? imagePeers.findIndex((e) => e.id === entry.id) : -1
  const hasPrev = kind === 'image' && imageIndex > 0
  const hasNext =
    kind === 'image' && imageIndex >= 0 && imageIndex < imagePeers.length - 1

  const ensureCached = useEffectEvent((file: VaultEntry) => {
    const cache = cacheRef.current
    const existing = cache.get(file.id)
    if (existing) return existing.promise

    const mime = mimeForFilename(file.name)
    const promise = fetchVaultFileBlob(file.id, { mime })
      .then(({ blob }) => {
        const url = URL.createObjectURL(blob)
        const cur = cache.get(file.id)
        if (cur) cur.url = url
        return url
      })
      .catch((err) => {
        cache.delete(file.id)
        throw err
      })
    cache.set(file.id, { promise })
    return promise
  })

  useEffect(() => {
    if (kind === 'download') {
      setLoading(false)
      setError(null)
      setObjectUrl(null)
      return
    }

    const seq = ++loadSeqRef.current
    setLoading(true)
    setError(null)
    setObjectUrl(null)

    void (async () => {
      try {
        const url = await ensureCached(entry)
        if (seq !== loadSeqRef.current) return
        setObjectUrl(url)
        setLoading(false)
      } catch (err) {
        if (seq !== loadSeqRef.current) return
        setError(err instanceof Error ? err.message : String(err))
        setLoading(false)
      }
    })()
  }, [entry, kind])

  // Speculative next/prev image fetch.
  useEffect(() => {
    if (kind !== 'image' || imageIndex < 0) return
    if (imageIndex > 0) void ensureCached(imagePeers[imageIndex - 1]).catch(() => undefined)
    if (imageIndex < imagePeers.length - 1) {
      void ensureCached(imagePeers[imageIndex + 1]).catch(() => undefined)
    }
  }, [kind, imageIndex, imagePeers])

  useEffect(() => {
    const cache = cacheRef.current
    return () => {
      loadSeqRef.current += 1
      for (const cached of cache.values()) {
        if (cached.url) URL.revokeObjectURL(cached.url)
      }
      cache.clear()
    }
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (kind !== 'image') return
      if (e.key === 'ArrowLeft' && hasPrev) {
        e.preventDefault()
        onChangeEntry(imagePeers[imageIndex - 1])
      } else if (e.key === 'ArrowRight' && hasNext) {
        e.preventDefault()
        onChangeEntry(imagePeers[imageIndex + 1])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [kind, hasPrev, hasNext, imageIndex, imagePeers, onChangeEntry, onClose])

  if (kind === 'download') {
    return (
      <div className="modal-backdrop" onClick={onClose}>
        <div className="modal" onClick={(e) => e.stopPropagation()}>
          <div className="modal-header">Download file</div>
          <div className="modal-body">
            <div className="preview-download-msg">
              <span className="data">{entry.name}</span> cannot be previewed
              {entry.size != null && entry.size > maxBytes
                ? ` (larger than ${formatPreviewLimit(maxBytes)})`
                : ''}
              .
            </div>
            <div className="preview-download-meta">
              Size <span className="data">{formatBytes(entry.size)}</span>
            </div>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                onDownload(entry)
                onClose()
              }}
            >
              Download
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="modal-backdrop preview-backdrop" onClick={onClose}>
      <div
        className="modal modal-preview"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={`Preview ${entry.name}`}
      >
        <div className="modal-header preview-header">
          <span className="preview-title data" title={entry.name}>
            {entry.name}
          </span>
          <span className="preview-meta">
            {formatBytes(entry.size)}
            {kind === 'image' && imagePeers.length > 1 ? (
              <>
                {' · '}
                <span className="data">
                  {imageIndex + 1}/{imagePeers.length}
                </span>
              </>
            ) : null}
          </span>
          <button
            type="button"
            className="btn-micro"
            onClick={onClose}
            title="Close (Esc)"
          >
            Close
          </button>
        </div>
        <div className="preview-stage">
          {kind === 'image' && hasPrev ? (
            <button
              type="button"
              className="preview-nav preview-nav-prev"
              onClick={() => onChangeEntry(imagePeers[imageIndex - 1])}
              title="Previous (←)"
              aria-label="Previous image"
            >
              ‹
            </button>
          ) : (
            <span className="preview-nav-spacer" />
          )}
          <div className="preview-content">
            {loading ? (
              <div className="preview-status">Loading…</div>
            ) : error ? (
              <div className="preview-status preview-error">{error}</div>
            ) : objectUrl && kind === 'image' ? (
              <img src={objectUrl} alt={entry.name} className="preview-img" />
            ) : objectUrl && kind === 'pdf' ? (
              <iframe title={entry.name} src={objectUrl} className="preview-pdf" />
            ) : null}
          </div>
          {kind === 'image' && hasNext ? (
            <button
              type="button"
              className="preview-nav preview-nav-next"
              onClick={() => onChangeEntry(imagePeers[imageIndex + 1])}
              title="Next (→)"
              aria-label="Next image"
            >
              ›
            </button>
          ) : (
            <span className="preview-nav-spacer" />
          )}
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => onDownload(entry)}>
            Download
          </button>
          <button type="button" className="btn-primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
