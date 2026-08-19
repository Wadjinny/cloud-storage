import { apiUrl } from './base'
import {
  cancelJob,
  waitForJob,
  TransferJobError,
  type ProgressUpdate,
  type TransferJob,
} from './vault'

/** 'expired': the provider rejected the stored grant; only a reconnect fixes it. */
export type AuthState = 'ok' | 'expired'

export type Account = {
  id: string
  provider: 'gdrive' | 'mega'
  label: string
  /** When false, account is skipped for new upload placement. */
  enabled?: boolean
  auth_state?: AuthState
  auth_error?: string | null
  /** Unix seconds of the last successful token renewal / connect. */
  auth_renewed_at?: number | null
}

export type AccountStorage = {
  id: string
  provider: 'gdrive' | 'mega' | string
  label: string
  enabled?: boolean
  used: number | null
  total: number | null
  available: number | null
  placeable: number | null
  /** Estimated vault ciphertext on this account (shards + thumbs). */
  vault_used?: number
  full: boolean
  error: string | null
  auth_state?: AuthState
  auth_error?: string | null
}

export type StorageStats = {
  accounts: AccountStorage[]
  full_ratio: number
  combined: {
    used: number | null
    total: number | null
    available: number | null
    placeable: number | null
    vault_used?: number
    complete: boolean
    account_count: number
  }
}

export type PruneOrphan = {
  id: string
  name: string
  kind: string
  /** Vault filesystem path when the opaque remote name still maps to an index row. */
  path?: string
}

export type PruneStaleSession = {
  id: string
  status: string
  filename: string
}

export type PruneAccountReport = {
  account_id: string
  label: string
  provider: string
  scanned: number
  kept: number
  orphans: PruneOrphan[]
  stale_sessions: PruneStaleSession[]
  error: string | null
}

export type PruneAnalyzeResult = {
  accounts: PruneAccountReport[]
  total_orphans: number
  total_stale_sessions: number
  account_count: number
  errors: number
}

export type PruneApplyAccountResult = {
  account_id: string
  label: string
  provider: string
  deleted: number
  kept: number
  errors: number
  discarded_sessions: number
  error: string | null
}

export type PruneApplyResult = {
  accounts: PruneApplyAccountResult[]
  deleted: number
  discarded_sessions: number
  errors: number
  account_count: number
}

export type HealthIssue = {
  kind: 'shard' | 'thumb' | string
  file_id: string
  path: string
  i: number | null
  account_id: string
  remote_id: string
  label: string
  provider: string
  reason: 'missing' | 'no_account' | 'account_error' | 'check_error' | string
  error: string | null
}

export type HealthFileReport = {
  file_id: string
  path: string
  issues: HealthIssue[]
}

export type HealthCheckResult = {
  file_count: number
  object_count: number
  checked: number
  ok: number
  issue_count: number
  healthy: boolean
  missing: HealthIssue[]
  files: HealthFileReport[]
  account_errors: Array<{ account_id: string; error: string }>
}

export type AccountInUseDetail = {
  code: 'account_in_use'
  message: string
  account_id: string
  label?: string | null
  provider?: string | null
  shard_count: number
  file_count: number
  thumb_count: number
  sample_paths: string[]
}

export class AccountInUseError extends Error {
  detail: AccountInUseDetail

  constructor(detail: AccountInUseDetail) {
    super(detail.message)
    this.name = 'AccountInUseError'
    this.detail = detail
  }
}

async function readError(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as {
      detail?: string | AccountInUseDetail
      error?: { message?: unknown }
    }
    if (typeof data.error?.message === 'string' && data.error.message) {
      return data.error.message
    }
    if (typeof data.detail === 'string') return data.detail
    if (
      data.detail &&
      typeof data.detail === 'object' &&
      typeof data.detail.message === 'string'
    ) {
      return data.detail.message
    }
  } catch {
    /* ignore */
  }
  return res.statusText || 'Request failed'
}

async function readDeleteAccountError(res: Response): Promise<Error> {
  try {
    const data = (await res.json()) as {
      detail?: string | AccountInUseDetail
      error?: { code?: unknown; message?: unknown } & Partial<AccountInUseDetail>
    }
    if (data.error && data.error.code === 'account_in_use') {
      return new AccountInUseError({
        code: 'account_in_use',
        message: String(data.error.message ?? data.detail ?? 'Account in use'),
        account_id: String(data.error.account_id ?? ''),
        label: data.error.label,
        provider: data.error.provider,
        shard_count: Number(data.error.shard_count ?? 0),
        file_count: Number(data.error.file_count ?? 0),
        thumb_count: Number(data.error.thumb_count ?? 0),
        sample_paths: data.error.sample_paths ?? [],
      })
    }
    if (
      data.detail &&
      typeof data.detail === 'object' &&
      data.detail.code === 'account_in_use'
    ) {
      return new AccountInUseError(data.detail)
    }
    if (typeof data.detail === 'string') return new Error(data.detail)
  } catch {
    /* ignore */
  }
  return new Error(res.statusText || 'Request failed')
}

export async function listAccounts(): Promise<Account[]> {
  const res = await fetch(apiUrl('/api/accounts'), { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<Account[]>
}

export async function getStorageStats(): Promise<StorageStats> {
  const res = await fetch(apiUrl('/api/accounts/storage'), { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<StorageStats>
}

export async function deleteAccount(id: string): Promise<void> {
  const res = await fetch(apiUrl(`/api/accounts/${encodeURIComponent(id)}`), {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!res.ok) throw await readDeleteAccountError(res)
}

export type EvacuateResult = {
  account_id: string
  moved_shards: number
  moved_thumbs: number
  moved_bytes?: number
  deleted: boolean
  label?: string | null
  provider?: string | null
}

export async function evacuateAccount(
  id: string,
  onProgress?: (update: ProgressUpdate) => void,
  opts?: { signal?: AbortSignal },
): Promise<EvacuateResult> {
  const signal = opts?.signal
  const res = await fetch(apiUrl(`/api/accounts/${encodeURIComponent(id)}/evacuate`), {
    method: 'POST',
    credentials: 'include',
    signal,
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  if (signal?.aborted) throw new Error('Evacuate cancelled')

  const onAbort = () => {
    void cancelJob(jobId)
  }
  signal?.addEventListener('abort', onAbort)
  try {
    const job: TransferJob = await waitForJob(jobId, onProgress, signal)
    const result = job.result as EvacuateResult | null | undefined
    if (!result) throw new Error('Evacuate returned no result')
    return result
  } catch (err) {
    if (
      signal?.aborted ||
      (err instanceof TransferJobError && err.job.cancel_requested)
    ) {
      await cancelJob(jobId)
      const detail =
        err instanceof TransferJobError && err.job.error
          ? err.job.error
          : 'Evacuate cancelled'
      throw new Error(detail)
    }
    throw err
  } finally {
    signal?.removeEventListener('abort', onAbort)
  }
}

export async function setAccountEnabled(id: string, enabled: boolean): Promise<Account> {
  const res = await fetch(apiUrl(`/api/accounts/${encodeURIComponent(id)}`), {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<Account>
}

export async function runHealthCheck(
  onProgress?: (update: ProgressUpdate) => void,
  opts?: { signal?: AbortSignal },
): Promise<HealthCheckResult> {
  const signal = opts?.signal
  const res = await fetch(apiUrl('/api/accounts/health'), {
    method: 'POST',
    credentials: 'include',
    signal,
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  if (signal?.aborted) throw new Error('Health check cancelled')

  const onAbort = () => {
    void cancelJob(jobId)
  }
  signal?.addEventListener('abort', onAbort)
  try {
    const job: TransferJob = await waitForJob(jobId, onProgress, signal)
    const result = job.result as HealthCheckResult | null | undefined
    if (!result) throw new Error('Health check returned no result')
    return result
  } catch (err) {
    if (
      signal?.aborted ||
      (err instanceof TransferJobError && err.job.cancel_requested)
    ) {
      await cancelJob(jobId)
      throw new Error('Health check cancelled')
    }
    throw err
  } finally {
    signal?.removeEventListener('abort', onAbort)
  }
}

export async function analyzePrune(
  onProgress?: (update: ProgressUpdate) => void,
): Promise<PruneAnalyzeResult> {
  const res = await fetch(apiUrl('/api/accounts/prune/analyze'), {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  const job: TransferJob = await waitForJob(jobId, onProgress)
  const result = job.result as PruneAnalyzeResult | null | undefined
  if (!result) throw new Error('Prune analysis returned no result')
  return result
}

export async function applyPrune(
  onProgress?: (update: ProgressUpdate) => void,
): Promise<PruneApplyResult> {
  const res = await fetch(apiUrl('/api/accounts/prune'), {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  const job: TransferJob = await waitForJob(jobId, onProgress)
  const result = job.result as PruneApplyResult | null | undefined
  if (!result) throw new Error('Prune returned no result')
  return result
}

export async function connectMega(email: string, password: string): Promise<Account> {
  const res = await fetch(apiUrl('/api/auth/mega'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<Account>
}

export type RenewAccountRow = {
  account_id: string
  label: string | null
  provider: string
  /** 'renewed' | 'fresh' | 'expired' | 'error' */
  state: string
  renewed: boolean
  error: string | null
}

export type RenewResult = {
  accounts: RenewAccountRow[]
  renewed: number
  expired: number
}

/** Refresh provider OAuth tokens now (the backend also sweeps on a timer). */
export async function renewAccounts(): Promise<RenewResult> {
  const res = await fetch(apiUrl('/api/accounts/renew'), {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<RenewResult>
}

/** Pass an account id to re-consent that account instead of connecting a new one. */
export function startGoogleOAuth(accountId?: string): void {
  const path = accountId
    ? `/api/auth/google/start?account_id=${encodeURIComponent(accountId)}`
    : '/api/auth/google/start'
  window.location.href = apiUrl(path)
}
