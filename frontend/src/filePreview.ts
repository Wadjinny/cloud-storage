import type { VaultEntry } from './api/vault'
import { iconKindForEntry } from './fileIconKinds'

/** Fallback when status has not loaded yet (matches backend default). */
export const DEFAULT_PREVIEW_MAX_BYTES = 5 * 1024 * 1024

const BROWSER_IMAGE_EXT = new Set([
  '.jpg',
  '.jpeg',
  '.png',
  '.gif',
  '.webp',
  '.bmp',
])

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

export function isBrowserImageName(name: string): boolean {
  return BROWSER_IMAGE_EXT.has(extOf(name))
}

export function isPdfName(name: string): boolean {
  return extOf(name) === '.pdf'
}

export function mimeForFilename(name: string): string {
  const ext = extOf(name)
  switch (ext) {
    case '.jpg':
    case '.jpeg':
      return 'image/jpeg'
    case '.png':
      return 'image/png'
    case '.gif':
      return 'image/gif'
    case '.webp':
      return 'image/webp'
    case '.bmp':
      return 'image/bmp'
    case '.pdf':
      return 'application/pdf'
    default:
      return 'application/octet-stream'
  }
}

export type PreviewOpenKind = 'image' | 'pdf' | 'download'

/** Decide how double-click should open this entry. */
export function previewOpenKind(
  entry: VaultEntry,
  maxBytes: number = DEFAULT_PREVIEW_MAX_BYTES,
): PreviewOpenKind | null {
  if (entry.kind !== 'file') return null
  const size = entry.size
  const kind = iconKindForEntry(entry)
  if (kind === 'image' && isBrowserImageName(entry.name)) {
    if (size != null && size <= maxBytes) return 'image'
    return 'download'
  }
  if (kind === 'pdf' || isPdfName(entry.name)) {
    if (size != null && size <= maxBytes) return 'pdf'
    return 'download'
  }
  return 'download'
}

/** Small browser-displayable images in the current folder (for next/prev). */
export function previewableImagesInFolder(
  entries: VaultEntry[],
  maxBytes: number = DEFAULT_PREVIEW_MAX_BYTES,
): VaultEntry[] {
  return entries.filter(
    (e) =>
      e.kind === 'file' &&
      isBrowserImageName(e.name) &&
      e.size != null &&
      e.size <= maxBytes,
  )
}

export function formatPreviewLimit(maxBytes: number = DEFAULT_PREVIEW_MAX_BYTES): string {
  if (maxBytes >= 1024 * 1024 && maxBytes % (1024 * 1024) === 0) {
    return `${maxBytes / (1024 * 1024)} MiB`
  }
  if (maxBytes >= 1024 && maxBytes % 1024 === 0) {
    return `${maxBytes / 1024} KiB`
  }
  return `${maxBytes} B`
}
