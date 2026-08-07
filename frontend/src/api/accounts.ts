import { apiUrl } from './base'
import { waitForJob, type ProgressUpdate, type TransferJob } from './vault'

export type Account = {
  id: string
  provider: 'gdrive' | 'mega'
  label: string
  /** When false, account is skipped for new upload placement. */
  enabled?: boolean
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
  full: boolean
  error: string | null
}

export type StorageStats = {
  accounts: AccountStorage[]
  full_ratio: number
  combined: {
    used: number | null
    total: number | null
    available: number | null
    placeable: number | null
    complete: boolean
    account_count: number
  }
}

export type PruneOrphan = {
  id: string
  name: string
  kind: string
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

async function readError(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: string }
    if (typeof data.detail === 'string') return data.detail
  } catch {
    /* ignore */
  }
  return res.statusText || 'Request failed'
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
  if (!res.ok) throw new Error(await readError(res))
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
): Promise<HealthCheckResult> {
  const res = await fetch(apiUrl('/api/accounts/health'), {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  const { job_id: jobId } = (await res.json()) as { job_id: string }
  const job: TransferJob = await waitForJob(jobId, onProgress)
  const result = job.result as HealthCheckResult | null | undefined
  if (!result) throw new Error('Health check returned no result')
  return result
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

export async function connectMega(
  email: string,
  password: string,
  mfa?: string,
): Promise<Account> {
  const res = await fetch(apiUrl('/api/auth/mega'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email,
      password,
      ...(mfa?.trim() ? { mfa: mfa.trim() } : {}),
    }),
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<Account>
}

export function startGoogleOAuth(): void {
  window.location.href = apiUrl('/api/auth/google/start')
}
