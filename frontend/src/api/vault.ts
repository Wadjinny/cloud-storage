export type VaultStatus = {
  unlocked: boolean
  initialized: boolean
  file_count: number
  account_count: number
}

export type VaultEntry = {
  id: string
  name: string
  path: string
  size: number | null
  mtime: number | null
  kind: 'file' | 'dir'
  shard_count: number
}

export type VaultFileDetail = {
  id: string
  path: string
  name: string
  size: number
  mtime: number | null
  chunk_size: number | null
  alg: string | null
  shard_count: number
  shards_by_account: Array<{
    account_id: string
    provider: string
    label: string
    count: number
    shard_indices: number[]
  }>
  shards: Array<{
    i: number
    account_id: string
    provider: string
    label: string
    remote_id: string
    sha256: string
  }>
}

export type TransferJob = {
  id: string
  kind: 'upload' | 'download' | 'delete' | string
  status: 'running' | 'done' | 'error' | string
  phase: string
  message: string
  current: number
  total: number
  percent: number
  error: string | null
  result?: { id: string; path: string; size: number; shard_count: number } | null
  filename?: string | null
  cancel_requested?: boolean
}

export type ProgressUpdate = {
  percent: number
  message: string
  phase: string
}

async function readError(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: string }
    if (typeof data.detail === 'string') return data.detail
  } catch {
    /* ignore */
  }
  return res.statusText || 'Request failed'
}

export async function getVaultStatus(): Promise<VaultStatus> {
  const res = await fetch('/api/vault/status', { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultStatus>
}

export async function unlockVault(passphrase: string): Promise<void> {
  const res = await fetch('/api/vault/unlock', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ passphrase }),
  })
  if (!res.ok) throw new Error(await readError(res))
}

export async function lockVault(): Promise<void> {
  const res = await fetch('/api/vault/lock', {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
}

export async function listVaultFiles(path: string): Promise<VaultEntry[]> {
  const qs = new URLSearchParams({ path })
  const res = await fetch(`/api/vault/files?${qs}`, { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultEntry[]>
}

export async function getVaultFile(id: string): Promise<VaultFileDetail> {
  const res = await fetch(`/api/vault/files/${encodeURIComponent(id)}`, {
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultFileDetail>
}

async function waitForJob(
  jobId: string,
  onProgress?: (update: ProgressUpdate) => void,
  signal?: AbortSignal,
): Promise<TransferJob> {
  const res = await fetch(`/api/vault/jobs/${encodeURIComponent(jobId)}/events`, {
    credentials: 'include',
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  if (!res.ok) throw new Error(await readError(res))
  if (!res.body) throw new Error('Job event stream unavailable')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let last: TransferJob | null = null

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split('\n\n')
      buffer = parts.pop() ?? ''
      for (const part of parts) {
        const dataLine = part.split('\n').find((line) => line.startsWith('data: '))
        if (!dataLine) continue
        const job = JSON.parse(dataLine.slice(6)) as TransferJob
        last = job
        onProgress?.({
          percent: job.percent,
          message: job.message,
          phase: job.phase,
        })
        if (job.status === 'done') return job
        if (job.status === 'error') throw new Error(job.error || 'Transfer failed')
      }
    }
  } finally {
    try {
      reader.releaseLock()
    } catch {
      /* ignore */
    }
  }

  if (last?.status === 'done') return last
  if (last?.status === 'error') throw new Error(last.error || 'Transfer failed')
  throw new Error('Job event stream ended early')
}

export async function cancelUploadJob(jobId: string): Promise<void> {
  try {
    await fetch(`/api/vault/upload/${encodeURIComponent(jobId)}/cancel`, {
      method: 'POST',
      credentials: 'include',
    })
  } catch {
    /* ignore */
  }
}

export async function uploadVaultFile(
  path: string,
  file: File,
  onProgress?: (update: ProgressUpdate) => void,
  options?: { signal?: AbortSignal },
): Promise<void> {
  const signal = options?.signal
  onProgress?.({ percent: 0, message: 'Starting upload…', phase: 'queued' })
  const qs = new URLSearchParams({ path })
  const startRes = await fetch(`/api/vault/upload/start?${qs}`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name || 'upload.bin', size: file.size }),
    signal,
  })
  if (!startRes.ok) throw new Error(await readError(startRes))
  const { job_id: jobId } = (await startRes.json()) as { job_id: string }

  const onAbort = () => {
    void cancelUploadJob(jobId)
  }
  signal?.addEventListener('abort', onAbort)

  const events = waitForJob(jobId, onProgress)
  try {
    const putRes = await fetch(`/api/vault/upload/${encodeURIComponent(jobId)}`, {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
      signal,
    })
    if (!putRes.ok) {
      try {
        await events
      } catch {
        /* prefer PUT error below */
      }
      if (signal?.aborted) throw new Error('Upload cancelled')
      throw new Error(await readError(putRes))
    }
    await events
  } catch (err) {
    if (signal?.aborted) {
      await cancelUploadJob(jobId)
      throw new Error('Upload cancelled')
    }
    throw err
  } finally {
    signal?.removeEventListener('abort', onAbort)
  }
}

async function cancelDownloadJob(jobId: string): Promise<void> {
  try {
    await fetch(`/api/vault/download/${encodeURIComponent(jobId)}/cancel`, {
      method: 'POST',
      credentials: 'include',
    })
  } catch {
    /* ignore */
  }
}

export async function downloadVaultFile(
  id: string,
  filename: string,
  onProgress?: (update: ProgressUpdate) => void,
): Promise<void> {
  onProgress?.({ percent: 0, message: 'Starting download…', phase: 'queued' })
  const qs = new URLSearchParams({ id })
  const res = await fetch(`/api/vault/download?${qs}`, {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  const started = (await res.json()) as {
    job_id: string
    filename?: string
    size?: number | null
  }
  const jobId = started.job_id
  const saveName = started.filename || filename
  const streamUrl = `/api/vault/download/${encodeURIComponent(jobId)}/stream`

  const events = waitForJob(jobId, onProgress)

  type SaveWritable = {
    write: (chunk: Uint8Array) => Promise<void>
    close: () => Promise<void>
    abort: () => Promise<void>
  }
  const picker = (
    window as Window & {
      showSaveFilePicker?: (opts: { suggestedName?: string }) => Promise<{
        createWritable: () => Promise<SaveWritable>
      }>
    }
  ).showSaveFilePicker

  if (typeof picker === 'function') {
    let writable: SaveWritable | null = null
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
    try {
      const handle = await picker({ suggestedName: saveName })
      writable = await handle.createWritable()
      const streamRes = await fetch(streamUrl, { credentials: 'include' })
      if (!streamRes.ok) throw new Error(await readError(streamRes))
      if (!streamRes.body) throw new Error('Download stream unavailable')
      reader = streamRes.body.getReader()
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        if (value) await writable.write(value)
      }
      await writable.close()
      writable = null
      reader = null
    } catch (err) {
      if (reader) {
        try {
          await reader.cancel()
        } catch {
          /* ignore */
        }
      }
      if (writable) {
        try {
          await writable.abort()
        } catch {
          /* ignore */
        }
      }
      await cancelDownloadJob(jobId)
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new Error('Download cancelled')
      }
      try {
        await events
      } catch {
        /* prefer stream/picker error */
      }
      throw err
    }
    await events
    return
  }

  // Fallback: browser streams the response to disk (no JS full-file buffer).
  const a = document.createElement('a')
  a.href = streamUrl
  a.download = saveName
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  await events
}

export async function deleteVaultFile(
  id: string,
  onProgress?: (update: ProgressUpdate) => void,
): Promise<void> {
  onProgress?.({ percent: 0, message: 'Starting delete…', phase: 'queued' })
  const res = await fetch(`/api/vault/files/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  await waitForJob(jobId, onProgress)
}
