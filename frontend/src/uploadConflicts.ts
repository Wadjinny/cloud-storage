import { listVaultFiles, type VaultEntry } from './api/vault'
import {
  joinVaultDir,
  splitRelativePath,
  type UploadItem,
} from './collectUploadItems'

export type ConflictAction = 'replace' | 'skip' | 'rename'

export type UploadConflict = {
  /** Original relative path within the upload batch. */
  relativePath: string
  /** Vault parent directory. */
  parent: string
  /** Conflicting basename. */
  name: string
  /** Existing vault file id when colliding with the index; null for batch-only dupes. */
  existingId: string | null
  /** Suggested auto-rename (unique against vault + batch at detection time). */
  suggestedName: string
}

export type ResolvedUploadPlan = {
  files: UploadItem[]
  /** Existing vault files to delete before upload (replace). */
  replacements: Array<{ id: string; label: string }>
}

/** `photo.jpg` → `photo (1).jpg`, skipping names already in ``taken``. */
export function nextAvailableName(name: string, taken: Set<string>): string {
  if (!taken.has(name)) return name
  const dot = name.lastIndexOf('.')
  const hasExt = dot > 0
  const stem = hasExt ? name.slice(0, dot) : name
  const ext = hasExt ? name.slice(dot) : ''
  let i = 1
  for (;;) {
    const candidate = `${stem} (${i})${ext}`
    if (!taken.has(candidate)) return candidate
    i += 1
  }
}

function takenNamesInDir(entries: VaultEntry[]): Set<string> {
  const taken = new Set<string>()
  for (const e of entries) {
    if (e.kind === 'file') taken.add(e.name)
  }
  return taken
}

export type ConflictScan = {
  conflicts: UploadConflict[]
  /** Vault file basenames already present per parent dir. */
  occupiedByParent: Record<string, string[]>
}

/**
 * Find name collisions against the vault (and duplicates within the batch).
 * First occurrence of a path wins as non-conflict; later dupes conflict.
 */
export async function detectUploadConflicts(
  destDir: string,
  files: UploadItem[],
): Promise<ConflictScan> {
  const byParent = new Map<string, UploadItem[]>()
  for (const item of files) {
    const { dir } = splitRelativePath(item.relativePath)
    const parent = joinVaultDir(destDir, dir)
    const list = byParent.get(parent)
    if (list) list.push(item)
    else byParent.set(parent, [item])
  }

  const conflicts: UploadConflict[] = []
  const occupiedByParent: Record<string, string[]> = {}
  for (const [parent, items] of byParent) {
    let vaultEntries: VaultEntry[] = []
    try {
      vaultEntries = await listVaultFiles(parent)
    } catch {
      // Parent may not exist yet (new folder upload); treat as empty.
      vaultEntries = []
    }
    const idByName = new Map<string, string>()
    for (const e of vaultEntries) {
      if (e.kind === 'file') idByName.set(e.name, e.id)
    }
    const taken = takenNamesInDir(vaultEntries)
    occupiedByParent[parent] = [...taken]
    const claimed = new Set<string>()

    for (const item of items) {
      const { name } = splitRelativePath(item.relativePath)
      if (!name) continue
      const existingId = idByName.get(name) ?? null
      const batchDup = claimed.has(name)
      if (existingId != null || batchDup) {
        const suggestedName = nextAvailableName(name, new Set([...taken, ...claimed]))
        conflicts.push({
          relativePath: item.relativePath,
          parent,
          name,
          existingId: batchDup ? null : existingId,
          suggestedName,
        })
        continue
      }
      claimed.add(name)
    }
  }
  return { conflicts, occupiedByParent }
}

/**
 * Build the upload plan from per-conflict actions.
 * Rename targets stay unique against vault names and other kept batch names.
 */
export function buildUploadPlan(
  destDir: string,
  files: UploadItem[],
  conflicts: UploadConflict[],
  actions: Record<string, ConflictAction>,
  occupiedByParent: Record<string, string[]> = {},
): ResolvedUploadPlan {
  const conflictByRel = new Map(conflicts.map((c) => [c.relativePath, c]))
  const replacements: ResolvedUploadPlan['replacements'] = []

  const takenByParent = new Map<string, Set<string>>()

  function taken(parent: string): Set<string> {
    let set = takenByParent.get(parent)
    if (!set) {
      set = new Set(occupiedByParent[parent] ?? [])
      takenByParent.set(parent, set)
    }
    return set
  }

  type Keep = { item: UploadItem; finalRel: string; parent: string; name: string }
  const keeps: Keep[] = []

  function dropKeptNamed(parent: string, name: string) {
    for (let i = keeps.length - 1; i >= 0; i--) {
      if (keeps[i].parent === parent && keeps[i].name === name) {
        keeps.splice(i, 1)
      }
    }
  }

  for (const item of files) {
    const c = conflictByRel.get(item.relativePath)
    if (!c) {
      const { dir, name } = splitRelativePath(item.relativePath)
      const parent = joinVaultDir(destDir, dir)
      taken(parent).add(name)
      keeps.push({ item, finalRel: item.relativePath, parent, name })
      continue
    }
    const action = actions[item.relativePath] ?? 'rename'
    if (action === 'skip') continue
    if (action === 'replace') {
      if (c.existingId) {
        replacements.push({ id: c.existingId, label: item.relativePath })
      }
      // Drop an earlier batch file that claimed this name (batch-only duplicate).
      dropKeptNamed(c.parent, c.name)
      taken(c.parent).add(c.name)
      keeps.push({
        item,
        finalRel: item.relativePath,
        parent: c.parent,
        name: c.name,
      })
      continue
    }
    // rename
    const newName = nextAvailableName(c.name, taken(c.parent))
    taken(c.parent).add(newName)
    const { dir } = splitRelativePath(item.relativePath)
    const finalRel = dir ? `${dir}/${newName}` : newName
    keeps.push({ item, finalRel, parent: c.parent, name: newName })
  }

  const outFiles: UploadItem[] = keeps.map(({ item, finalRel }) => {
    const { name } = splitRelativePath(finalRel)
    const file =
      item.file.name === name
        ? item.file
        : new File([item.file], name, {
            type: item.file.type,
            lastModified: item.file.lastModified,
          })
    return { file, relativePath: finalRel }
  })

  return { files: outFiles, replacements }
}
