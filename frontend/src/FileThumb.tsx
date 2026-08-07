import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { fetchVaultThumbnail, type VaultEntry } from './api/vault'
import { iconKindForEntry } from './fileIconKinds'
import { FileTypeIcon } from './fileIcons'

const THUMB_STALE_MS = 24 * 60 * 60 * 1000

export type FileThumbSize = 'sm' | 'lg' | 'wide'

const ICON_PX: Record<FileThumbSize, number> = {
  sm: 16,
  lg: 36,
  wide: 48,
}

const BADGE_PX: Record<FileThumbSize, number> = {
  sm: 9,
  lg: 12,
  wide: 14,
}

export function FileThumb({
  entry,
  size = 'sm',
}: {
  entry: VaultEntry
  size?: FileThumbSize
}) {
  const kind = iconKindForEntry(entry)
  const ready = entry.kind === 'file' && entry.thumb_status === 'ready'
  const iconPx = ICON_PX[size]

  const query = useQuery({
    queryKey: ['vault', 'thumbnail', entry.id],
    queryFn: () => fetchVaultThumbnail(entry.id),
    enabled: ready,
    staleTime: THUMB_STALE_MS,
    gcTime: THUMB_STALE_MS,
    retry: 1,
  })

  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!query.data) {
      setObjectUrl(null)
      return
    }
    const url = URL.createObjectURL(query.data)
    setObjectUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [query.data])

  if (ready && objectUrl) {
    return (
      <span className={`file-thumb file-thumb-${size} file-thumb-has-img`}>
        <img src={objectUrl} alt="" className="file-thumb-img" />
        <span className={`file-thumb-badge kind-${kind}`} title={kind}>
          <FileTypeIcon kind={kind} size={BADGE_PX[size]} />
        </span>
      </span>
    )
  }

  return (
    <span
      className={`file-thumb file-thumb-${size} file-thumb-icon kind-${kind}`}
      title={kind}
    >
      <FileTypeIcon kind={kind} size={iconPx} />
    </span>
  )
}
