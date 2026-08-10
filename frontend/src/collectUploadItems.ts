/** One file to upload, with a path relative to the drop/picker root. */
export type UploadItem = {
  file: File
  /** Posix path including filename, e.g. `Photos/img.jpg` or `readme.txt`. */
  relativePath: string
}

export type CollectedUploads = {
  files: UploadItem[]
  /** Relative directory paths with no files (empty folders). */
  emptyDirs: string[]
}

export function normalizeRel(path: string): string {
  return path.replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+$/, '')
}

function joinRel(prefix: string, name: string): string {
  return prefix ? `${prefix}/${name}` : name
}

/** Split `a/b/c.txt` into parent dir `a/b` and name `c.txt`. */
export function splitRelativePath(relativePath: string): { dir: string; name: string } {
  const norm = normalizeRel(relativePath)
  const i = norm.lastIndexOf('/')
  if (i < 0) return { dir: '', name: norm }
  return { dir: norm.slice(0, i), name: norm.slice(i + 1) }
}

/** Join vault base dir with a relative subdir. */
export function joinVaultDir(base: string, relDir: string): string {
  const rel = normalizeRel(relDir)
  if (!rel) return base === '/' || !base ? '/' : base.replace(/\/+$/, '') || '/'
  if (!base || base === '/') return `/${rel}`
  return `${base.replace(/\/+$/, '')}/${rel}`
}

export function collectFromFileList(list: FileList | File[]): CollectedUploads {
  const files: UploadItem[] = []
  for (const file of Array.from(list)) {
    const rel =
      typeof file.webkitRelativePath === 'string' && file.webkitRelativePath
        ? normalizeRel(file.webkitRelativePath)
        : file.name
    if (!rel || rel.endsWith('/')) continue
    files.push({ file, relativePath: rel })
  }
  return { files, emptyDirs: [] }
}

function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => {
    const all: FileSystemEntry[] = []
    const pump = () => {
      reader.readEntries(
        (batch) => {
          if (batch.length === 0) {
            resolve(all)
            return
          }
          all.push(...batch)
          pump()
        },
        reject,
      )
    }
    pump()
  })
}

function entryFile(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject))
}

async function walkEntry(
  entry: FileSystemEntry,
  prefix: string,
  files: UploadItem[],
  emptyDirs: string[],
): Promise<void> {
  if (entry.isFile) {
    const file = await entryFile(entry as FileSystemFileEntry)
    files.push({ file, relativePath: joinRel(prefix, entry.name) })
    return
  }
  if (!entry.isDirectory) return

  const dirPath = joinRel(prefix, entry.name)
  const children = await readAllEntries(
    (entry as FileSystemDirectoryEntry).createReader(),
  )
  if (children.length === 0) {
    emptyDirs.push(dirPath)
    return
  }
  for (const child of children) {
    await walkEntry(child, dirPath, files, emptyDirs)
  }
}

/** Walk DataTransfer items (files + folders). Falls back to FileList if entries unavailable. */
export async function collectFromDataTransfer(
  dt: DataTransfer,
): Promise<CollectedUploads> {
  const files: UploadItem[] = []
  const emptyDirs: string[] = []
  const items = dt.items ? Array.from(dt.items) : []

  // DataTransferItem entries are only valid synchronously during the drop event;
  // they are neutered once we await. Grab every entry up front, THEN walk them,
  // otherwise all but the first dropped item are lost.
  const entries: FileSystemEntry[] = []
  for (const item of items) {
    if (item.kind !== 'file') continue
    const entry =
      typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null
    if (entry) entries.push(entry)
  }

  if (entries.length > 0) {
    for (const entry of entries) {
      await walkEntry(entry, '', files, emptyDirs)
    }
    return { files, emptyDirs }
  }

  return collectFromFileList(dt.files)
}

export function dataTransferHasFiles(dt: DataTransfer | null): boolean {
  if (!dt) return false
  return Array.from(dt.types).includes('Files')
}

export function dataTransferIsVaultMove(dt: DataTransfer | null): boolean {
  if (!dt) return false
  return Array.from(dt.types).includes('application/x-vault-entry')
}
