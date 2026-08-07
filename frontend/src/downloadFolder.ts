/** Persist a download directory handle (Chromium File System Access API). */

const DB_NAME = 'cloud-storage'
const DB_VERSION = 1
const STORE = 'handles'
const KEY = 'downloadDir'

type DirHandle = FileSystemDirectoryHandle

type DirPerms = DirHandle & {
  queryPermission: (opts: { mode?: 'read' | 'readwrite' }) => Promise<PermissionState>
  requestPermission: (opts: { mode?: 'read' | 'readwrite' }) => Promise<PermissionState>
}

function showDirectoryPicker(opts?: {
  mode?: 'read' | 'readwrite'
}): Promise<DirHandle> {
  const fn = (
    window as unknown as {
      showDirectoryPicker: (o?: { mode?: 'read' | 'readwrite' }) => Promise<DirHandle>
    }
  ).showDirectoryPicker
  return fn(opts)
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onerror = () => reject(req.error ?? new Error('IndexedDB open failed'))
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE)
      }
    }
    req.onsuccess = () => resolve(req.result)
  })
}

async function idbGet(): Promise<DirHandle | null> {
  const db = await openDb()
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly')
      const req = tx.objectStore(STORE).get(KEY)
      req.onerror = () => reject(req.error ?? new Error('IndexedDB get failed'))
      req.onsuccess = () => resolve((req.result as DirHandle | undefined) ?? null)
    })
  } finally {
    db.close()
  }
}

async function idbSet(handle: DirHandle): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite')
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB set failed'))
      tx.objectStore(STORE).put(handle, KEY)
    })
  } finally {
    db.close()
  }
}

async function idbClear(): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite')
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB clear failed'))
      tx.objectStore(STORE).delete(KEY)
    })
  } finally {
    db.close()
  }
}

export function supportsDownloadFolder(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker ===
      'function'
  )
}

async function permissionGranted(handle: DirHandle): Promise<boolean> {
  const h = handle as DirPerms
  const opts = { mode: 'readwrite' as const }
  if ((await h.queryPermission(opts)) === 'granted') return true
  return (await h.requestPermission(opts)) === 'granted'
}

/** Return a usable saved folder, or null if unsupported / user cancelled. */
export async function ensureDownloadFolder(opts?: {
  forcePick?: boolean
}): Promise<DirHandle | null> {
  if (!supportsDownloadFolder()) return null
  const forcePick = opts?.forcePick === true

  if (!forcePick) {
    const saved = await idbGet()
    if (saved) {
      try {
        if (await permissionGranted(saved)) return saved
      } catch {
        await idbClear()
      }
    }
  }

  try {
    const picked = await showDirectoryPicker({ mode: 'readwrite' })
    await idbSet(picked)
    return picked
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') return null
    throw err
  }
}

export async function clearDownloadFolder(): Promise<void> {
  await idbClear()
}

export async function getDownloadFolderName(): Promise<string | null> {
  if (!supportsDownloadFolder()) return null
  const saved = await idbGet()
  return saved?.name ?? null
}

export async function writeToDownloadFolder(
  dir: DirHandle,
  filename: string,
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): Promise<void> {
  const fileHandle = await dir.getFileHandle(filename, { create: true })
  const writable = await fileHandle.createWritable()
  const reader = body.getReader()
  try {
    for (;;) {
      if (signal?.aborted) throw new Error('Download cancelled')
      const { done, value } = await reader.read()
      if (done) break
      if (value) await writable.write(value as BlobPart)
    }
    await writable.close()
  } catch (err) {
    try {
      await reader.cancel()
    } catch {
      /* ignore */
    }
    try {
      await writable.abort()
    } catch {
      /* ignore */
    }
    throw err
  }
}
