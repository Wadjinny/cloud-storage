import { apiUrl } from './base'

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

async function readError(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: string }
    if (typeof data.detail === 'string') return data.detail
  } catch {
    /* ignore */
  }
  return res.statusText || 'Request failed'
}

export async function listActivity(opts?: {
  limit?: number
  before?: number
  action?: string
  status?: string
}): Promise<ActivityListResponse> {
  const params = new URLSearchParams()
  if (opts?.limit != null) params.set('limit', String(opts.limit))
  if (opts?.before != null) params.set('before', String(opts.before))
  if (opts?.action) params.set('action', opts.action)
  if (opts?.status) params.set('status', opts.status)
  const q = params.toString()
  const res = await fetch(apiUrl(`/api/activity${q ? `?${q}` : ''}`), {
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
  return (await res.json()) as ActivityListResponse
}
