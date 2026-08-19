import { apiUrl, readError } from './base'

export type ActivityStatus = 'success' | 'error' | 'cancelled'

export type ActivityEvent = {
  id: string
  user_id: string | null
  action: string
  status: ActivityStatus | string
  path: string | null
  detail: string | null
  meta: Record<string, unknown> | null
  created_at: number
}

export type ActivityListResponse = {
  events: ActivityEvent[]
  retention_days: number
}

export type ActivityActionCount = {
  action: string
  count: number
}

export type ActivitySummary = {
  events: number
  by_status: Record<string, number>
  by_action: ActivityActionCount[]
  upload_bytes: number
  download_bytes: number
  evacuated_bytes: number
  upload_ok: number
  download_ok: number
  evacuate_ok: number
  since: number | null
  until: number | null
  retention_days: number
}

export type ActivityFilters = {
  limit?: number
  before?: number
  since?: number
  until?: number
  action?: string
  status?: string
}

function toParams(opts?: ActivityFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (opts?.limit != null) params.set('limit', String(opts.limit))
  if (opts?.before != null) params.set('before', String(opts.before))
  if (opts?.since != null) params.set('since', String(opts.since))
  if (opts?.until != null) params.set('until', String(opts.until))
  if (opts?.action) params.set('action', opts.action)
  if (opts?.status) params.set('status', opts.status)
  return params
}

export async function listActivity(opts?: ActivityFilters): Promise<ActivityListResponse> {
  const q = toParams(opts).toString()
  const res = await fetch(apiUrl(`/api/activity${q ? `?${q}` : ''}`), {
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  return (await res.json()) as ActivityListResponse
}

export async function getActivitySummary(
  opts?: Omit<ActivityFilters, 'limit' | 'before'>,
): Promise<ActivitySummary> {
  const q = toParams(opts).toString()
  const res = await fetch(apiUrl(`/api/activity/summary${q ? `?${q}` : ''}`), {
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  return (await res.json()) as ActivitySummary
}
