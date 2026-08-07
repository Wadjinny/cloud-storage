import {
  ensureDownloadFolder,
  supportsDownloadFolder,
  writeToDownloadFolder,
} from '../downloadFolder'
import { apiUrl } from './base'

export type VaultStatus = {
  unlocked: boolean
  username: string | null
  initialized: boolean
  file_count: number
  account_count: number
  /** True while a user-level job/transfer locks provider account management. */
  accounts_locked?: boolean
  /** Max parallel shard uploads; also the batch file-upload worker count. */
  shard_upload_concurrency?: number
}

export type ThumbStatus = 'none' | 'ready' | 'failed' | 'unsupported' | string

export type VaultEntry = {
  id: string
  kind: 'file' | 'dir'
  name: string
  path: string
  size: number | null
  mtime: number | null
  shard_count: number
  thumb_status?: ThumbStatus
}

export type VaultFileDetail = {
  id: string
  path: string
  name: string
  size: number | null
  mtime: number | null
  chunk_size: number | null
  alg: string | null
  thumb_status?: ThumbStatus
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

export type UploadResumeInfo = {
  session_id: string
  path: string
  filename: string
  size: number | null
  chunk_size: number
  resume_from_byte: number
  done_shards: number
}

export type UploadAccountStats = {
  account_id: string
  provider: string
  label: string
  shards: number
  /** Shards currently in flight on this account (upload or download). */
  active: number
  bytes: number
  estimated_shards: number
}

export type UploadJobStats = {
  done_shards: number
  expected_shards: number | null
  done_bytes: number
  total_bytes: number | null
  chunk_size: number
  active_shards?: number
  by_account: UploadAccountStats[]
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
  result?: {
    id?: string
    path?: string
    size?: number
    shard_count?: number
    resume?: UploadResumeInfo
    [key: string]: unknown
  } | null
  filename?: string | null
  stats?: UploadJobStats | null
  cancel_requested?: boolean
}

export class TransferJobError extends Error {
  job: TransferJob

  constructor(job: TransferJob) {
    super(job.error || 'Transfer failed')
    this.name = 'TransferJobError'
    this.job = job
  }
}

export type ProgressUpdate = {
  percent: number
  message: string
  phase: string
  stats?: UploadJobStats | null
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
  const res = await fetch(apiUrl('/api/vault/status'), { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultStatus>
}

export async function lockVault(): Promise<void> {
  const res = await fetch(apiUrl('/api/vault/lock'), {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
}

export async function mkdirVault(parent: string, name: string): Promise<VaultEntry> {
  const res = await fetch(apiUrl('/api/vault/mkdir'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parent, name }),
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultEntry>
}

export async function moveVaultEntry(
  srcPath: string,
  destPath: string,
  kind?: 'file' | 'dir',
): Promise<VaultEntry> {
  const res = await fetch(apiUrl('/api/vault/move'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ src_path: srcPath, dest_path: destPath, kind }),
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultEntry>
}

export async function deleteVaultDir(
  path: string,
  onProgress?: (update: ProgressUpdate) => void,
): Promise<void> {
  onProgress?.({ percent: 0, message: 'Starting folder delete…', phase: 'queued' })
  const res = await fetch(apiUrl('/api/vault/dirs/delete'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  await waitForJob(jobId, onProgress)
}

export async function listVaultFiles(path: string): Promise<VaultEntry[]> {
  const qs = new URLSearchParams({ path })
  const res = await fetch(apiUrl(`/api/vault/files?${qs}`), { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultEntry[]>
}

export async function getVaultFile(id: string): Promise<VaultFileDetail> {
  const res = await fetch(apiUrl(`/api/vault/files/${encodeURIComponent(id)}`), {
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<VaultFileDetail>
}

/** Fetch decrypted thumbnail bytes (credentialed; for blob URL display). */
export async function fetchVaultThumbnail(id: string): Promise<Blob> {
  const res = await fetch(
    apiUrl(`/api/vault/files/${encodeURIComponent(id)}/thumbnail`),
    { credentials: 'include' },
  )
  if (!res.ok) throw new Error(await readError(res))
  return res.blob()
}

export async function waitForJob(
  jobId: string,
  onProgress?: (update: ProgressUpdate) => void,
  signal?: AbortSignal,
): Promise<TransferJob> {
  const res = await fetch(apiUrl(`/api/vault/jobs/${encodeURIComponent(jobId)}/events`), {
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
          stats: job.stats ?? null,
        })
        if (job.status === 'done') return job
        if (job.status === 'error') throw new TransferJobError(job)
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
  if (last?.status === 'error') throw new TransferJobError(last)
  throw new Error('Job event stream ended early')
}

export async function cancelUploadJob(jobId: string): Promise<void> {
  try {
    await fetch(apiUrl(`/api/vault/upload/${encodeURIComponent(jobId)}/cancel`), {
      method: 'POST',
      credentials: 'include',
    })
  } catch {
    /* ignore */
  }
}

export async function cancelDownloadJob(jobId: string): Promise<void> {
  try {
    await fetch(apiUrl(`/api/vault/download/${encodeURIComponent(jobId)}/cancel`), {
      method: 'POST',
      credentials: 'include',
    })
  } catch {
    /* ignore */
  }
}

async function putUploadBody(
  jobId: string,
  body: Blob,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(apiUrl(`/api/vault/upload/${encodeURIComponent(jobId)}`), {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/octet-stream' },
    body,
    signal,
  })
}

async function runUploadAttempt(
  jobId: string,
  body: Blob,
  onProgress?: (update: ProgressUpdate) => void,
  signal?: AbortSignal,
): Promise<void> {
  const onAbort = () => {
    void cancelUploadJob(jobId)
  }
  signal?.addEventListener('abort', onAbort)
  const events = waitForJob(jobId, onProgress, signal)
  try {
    const putRes = await putUploadBody(jobId, body, signal)
    if (!putRes.ok) {
      let jobErr: unknown
      try {
        await events
      } catch (e) {
        jobErr = e
      }
      // Prefer job error (includes resume metadata) over bare HTTP detail.
      if (jobErr) throw jobErr
      throw new Error(await readError(putRes))
    }
    if (signal?.aborted) throw new Error('Upload cancelled')
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

export async function uploadVaultFile(
  path: string,
  file: File,
  onProgress?: (update: ProgressUpdate) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const signal = opts?.signal
  onProgress?.({ percent: 0, message: 'Starting upload…', phase: 'queued' })
  const qs = new URLSearchParams({ path })
  const startRes = await fetch(apiUrl(`/api/vault/upload/start?${qs}`), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name, size: file.size }),
    signal,
  })
  if (!startRes.ok) throw new Error(await readError(startRes))
  const started = (await startRes.json()) as { job_id: string; chunk_size: number }
  if (signal?.aborted) throw new Error('Upload cancelled')

  try {
    await runUploadAttempt(started.job_id, file, onProgress, signal)
    return
  } catch (err) {
    if (!(err instanceof TransferJobError)) throw err
    const resume = err.job.result?.resume
    if (!resume || signal?.aborted) throw err

    onProgress?.({
      percent: err.job.percent,
      message: `Retrying from ${resume.resume_from_byte} bytes…`,
      phase: 'resume',
    })
    const resumeRes = await fetch(apiUrl('/api/vault/upload/resume'), {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: resume.session_id }),
      signal,
    })
    if (!resumeRes.ok) throw new Error(await readError(resumeRes))
    const resumed = (await resumeRes.json()) as {
      job_id: string
      resume_from_byte: number
    }
    const offset = Math.max(0, resumed.resume_from_byte || 0)
    const slice = offset > 0 ? file.slice(offset) : file
    await runUploadAttempt(resumed.job_id, slice, onProgress, signal)
  }
}

async function runDownloadStreamJob(
  jobId: string,
  saveName: string,
  onProgress?: (update: ProgressUpdate) => void,
  opts?: { signal?: AbortSignal; directory?: FileSystemDirectoryHandle | null },
): Promise<void> {
  const signal = opts?.signal
  let dir: FileSystemDirectoryHandle | null = opts?.directory ?? null
  if (dir == null && supportsDownloadFolder()) {
    dir = await ensureDownloadFolder()
    if (!dir) throw new Error('Download cancelled')
  }
  if (signal?.aborted) throw new Error('Download cancelled')

  const streamUrl = apiUrl(`/api/vault/download/${encodeURIComponent(jobId)}/stream`)
  const onAbort = () => {
    void cancelDownloadJob(jobId)
  }
  signal?.addEventListener('abort', onAbort)
  const events = waitForJob(jobId, onProgress, signal)

  try {
    if (dir) {
      try {
        const streamRes = await fetch(streamUrl, { credentials: 'include', signal })
        if (!streamRes.ok) throw new Error(await readError(streamRes))
        if (!streamRes.body) throw new Error('Download stream unavailable')
        await writeToDownloadFolder(dir, saveName, streamRes.body, signal)
      } catch (err) {
        await cancelDownloadJob(jobId)
        if (
          (err instanceof DOMException && err.name === 'AbortError') ||
          (err instanceof Error && err.message === 'Download cancelled') ||
          signal?.aborted
        ) {
          throw new Error('Download cancelled')
        }
        try {
          await events
        } catch {
          /* prefer stream error */
        }
        throw err
      }
      await events
      return
    }

    // Fallback: native navigation download (browser-owned stream). Cancel still
    // stops the server job; the browser may keep a partial local file.
    if (signal?.aborted) throw new Error('Download cancelled')
    const a = document.createElement('a')
    a.href = streamUrl
    a.download = saveName
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
    try {
      await events
    } catch (err) {
      if (signal?.aborted || (err instanceof TransferJobError && err.job.cancel_requested)) {
        throw new Error('Download cancelled')
      }
      throw err
    }
  } catch (err) {
    if (signal?.aborted || (err instanceof Error && err.message === 'Download cancelled')) {
      await cancelDownloadJob(jobId)
      throw new Error('Download cancelled')
    }
    throw err
  } finally {
    signal?.removeEventListener('abort', onAbort)
  }
}

export async function downloadVaultFile(
  id: string,
  filename: string,
  onProgress?: (update: ProgressUpdate) => void,
  opts?: { signal?: AbortSignal; directory?: FileSystemDirectoryHandle | null },
): Promise<void> {
  const signal = opts?.signal
  onProgress?.({ percent: 0, message: 'Starting download…', phase: 'queued' })

  // Resolve save location before starting the server job (pick once, reuse).
  let directory: FileSystemDirectoryHandle | null | undefined = opts?.directory
  if (directory === undefined && supportsDownloadFolder()) {
    directory = await ensureDownloadFolder()
    if (!directory) throw new Error('Download cancelled')
  }
  if (signal?.aborted) throw new Error('Download cancelled')

  const qs = new URLSearchParams({ id })
  const res = await fetch(apiUrl(`/api/vault/download?${qs}`), {
    method: 'POST',
    credentials: 'include',
    signal,
  })
  if (!res.ok) throw new Error(await readError(res))
  const started = (await res.json()) as {
    job_id: string
    filename?: string
    size?: number | null
  }
  await runDownloadStreamJob(started.job_id, started.filename || filename, onProgress, {
    signal,
    directory: directory ?? null,
  })
}

export type ZipDownloadItem = {
  kind: 'file' | 'dir'
  path: string
  id?: string
}

export async function downloadVaultZip(
  items: ZipDownloadItem[],
  onProgress?: (update: ProgressUpdate) => void,
  opts?: { signal?: AbortSignal; directory?: FileSystemDirectoryHandle | null },
): Promise<void> {
  const signal = opts?.signal
  onProgress?.({ percent: 0, message: 'Starting zip download…', phase: 'queued' })

  let directory: FileSystemDirectoryHandle | null | undefined = opts?.directory
  if (directory === undefined && supportsDownloadFolder()) {
    directory = await ensureDownloadFolder()
    if (!directory) throw new Error('Download cancelled')
  }
  if (signal?.aborted) throw new Error('Download cancelled')

  const res = await fetch(apiUrl('/api/vault/download/zip'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
    signal,
  })
  if (!res.ok) throw new Error(await readError(res))
  const started = (await res.json()) as {
    job_id: string
    filename?: string
    file_count?: number
  }
  await runDownloadStreamJob(started.job_id, started.filename || 'vault.zip', onProgress, {
    signal,
    directory: directory ?? null,
  })
}

export async function deleteVaultFile(
  id: string,
  onProgress?: (update: ProgressUpdate) => void,
): Promise<void> {
  onProgress?.({ percent: 0, message: 'Starting delete…', phase: 'queued' })
  const res = await fetch(apiUrl(`/api/vault/files/${encodeURIComponent(id)}`), {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  await waitForJob(jobId, onProgress)
}
