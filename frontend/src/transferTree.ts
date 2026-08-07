import { normalizeRel } from './collectUploadItems'

export type TransferFileStatus = 'queued' | 'current' | 'done' | 'error'

export type TransferTreeNode = {
  name: string
  kind: 'file' | 'dir'
  path: string
  children?: TransferTreeNode[]
}

type MutableNode = {
  name: string
  kind: 'file' | 'dir'
  path: string
  children: Map<string, MutableNode>
}

function ensureDir(
  root: Map<string, MutableNode>,
  parts: string[],
): Map<string, MutableNode> {
  let map = root
  let path = ''
  for (const part of parts) {
    path = path ? `${path}/${part}` : part
    let next = map.get(part)
    if (!next) {
      next = { name: part, kind: 'dir', path, children: new Map() }
      map.set(part, next)
    } else if (next.kind === 'file') {
      // Prefer directory when both exist (rare).
      next = { name: part, kind: 'dir', path, children: new Map() }
      map.set(part, next)
    }
    map = next.children
  }
  return map
}

function toNodes(map: Map<string, MutableNode>): TransferTreeNode[] {
  return [...map.values()]
    .sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
    })
    .map((n) => ({
      name: n.name,
      kind: n.kind,
      path: n.path,
      children: n.kind === 'dir' ? toNodes(n.children) : undefined,
    }))
}

/** Build a folder/file tree from relative file paths and empty directories. */
export function buildTransferTree(
  filePaths: string[],
  emptyDirs: string[] = [],
): TransferTreeNode[] {
  const root = new Map<string, MutableNode>()

  for (const dir of emptyDirs) {
    const parts = normalizeRel(dir).split('/').filter(Boolean)
    if (parts.length) ensureDir(root, parts)
  }

  for (const filePath of filePaths) {
    const norm = normalizeRel(filePath)
    const parts = norm.split('/').filter(Boolean)
    if (parts.length === 0) continue
    const name = parts[parts.length - 1]
    const parentMap = parts.length > 1 ? ensureDir(root, parts.slice(0, -1)) : root
    parentMap.set(name, {
      name,
      kind: 'file',
      path: norm,
      children: new Map(),
    })
  }

  return toNodes(root)
}

export function initialFileStatuses(filePaths: string[]): Record<string, TransferFileStatus> {
  const out: Record<string, TransferFileStatus> = {}
  for (const p of filePaths) {
    out[normalizeRel(p)] = 'queued'
  }
  return out
}
