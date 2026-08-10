import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { VaultEntry } from './api/vault'

export type FileContextMenuState = {
  x: number
  y: number
  targets: VaultEntry[]
}

type Item =
  | {
      kind: 'action'
      id: string
      label: string
      disabled?: boolean
      danger?: boolean
      onSelect: () => void
    }
  | { kind: 'sep' }

export function FileContextMenu({
  menu,
  currentPath,
  busy,
  canPaste,
  onClose,
  onOpen,
  onDownload,
  onCopy,
  onPaste,
  onRename,
  onDelete,
}: {
  menu: FileContextMenuState
  currentPath: string
  busy: boolean
  canPaste: boolean
  onClose: () => void
  onOpen: (entry: VaultEntry) => void
  onDownload: (entries: VaultEntry[]) => void
  onCopy: (entries: VaultEntry[]) => void
  onPaste: (destDir: string) => void
  onRename: (entry: VaultEntry) => void
  onDelete: (entries: VaultEntry[]) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: menu.x, top: menu.y })
  const targets = menu.targets
  const single = targets.length === 1 ? targets[0] : null
  const pasteDest = single?.kind === 'dir' ? single.path : currentPath

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const pad = 6
    let left = menu.x
    let top = menu.y
    if (left + rect.width > window.innerWidth - pad) {
      left = Math.max(pad, window.innerWidth - rect.width - pad)
    }
    if (top + rect.height > window.innerHeight - pad) {
      top = Math.max(pad, window.innerHeight - rect.height - pad)
    }
    setPos({ left, top })
  }, [menu.x, menu.y, targets.length, canPaste, busy])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onScroll = () => onClose()
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onDown, true)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onDown, true)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [onClose])

  const items: Item[] = [
    {
      kind: 'action',
      id: 'open',
      label: single?.kind === 'dir' ? 'Open' : 'Open / Preview',
      disabled: busy || !single,
      onSelect: () => {
        if (single) onOpen(single)
        onClose()
      },
    },
    {
      kind: 'action',
      id: 'download',
      label:
        targets.length > 1
          ? `Download (${targets.length})`
          : single?.kind === 'dir'
            ? 'Download folder'
            : 'Download',
      disabled: busy || targets.length === 0,
      onSelect: () => {
        onDownload(targets)
        onClose()
      },
    },
    { kind: 'sep' },
    {
      kind: 'action',
      id: 'copy',
      label: targets.length > 1 ? `Copy (${targets.length})` : 'Copy',
      disabled: busy || targets.length === 0,
      onSelect: () => {
        onCopy(targets)
        onClose()
      },
    },
    {
      kind: 'action',
      id: 'paste',
      label: 'Paste',
      disabled: busy || !canPaste,
      onSelect: () => {
        onPaste(pasteDest)
        onClose()
      },
    },
    { kind: 'sep' },
    {
      kind: 'action',
      id: 'rename',
      label: 'Rename',
      disabled: busy || !single,
      onSelect: () => {
        if (single) onRename(single)
        onClose()
      },
    },
    {
      kind: 'action',
      id: 'delete',
      label: targets.length > 1 ? `Delete (${targets.length})` : 'Delete',
      disabled: busy || targets.length === 0,
      danger: true,
      onSelect: () => {
        onDelete(targets)
        onClose()
      },
    },
  ]

  return (
    <div
      ref={ref}
      className="ctx-menu"
      style={{ left: pos.left, top: pos.top }}
      role="menu"
      aria-label="File actions"
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((item, i) =>
        item.kind === 'sep' ? (
          <div key={`sep-${i}`} className="ctx-menu-sep" role="separator" />
        ) : (
          <button
            key={item.id}
            type="button"
            role="menuitem"
            className={`ctx-menu-item${item.danger ? ' danger' : ''}`}
            disabled={item.disabled}
            onClick={item.onSelect}
          >
            {item.label}
          </button>
        ),
      )}
    </div>
  )
}
