import { apiUrl } from './base'

async function readError(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: string }
    if (typeof data.detail === 'string') return data.detail
  } catch {
    /* ignore */
  }
  return res.statusText || 'Request failed'
}

export async function login(username: string, passphrase: string): Promise<void> {
  const res = await fetch(apiUrl('/api/auth/login'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, passphrase }),
  })
  if (!res.ok) throw new Error(await readError(res))
}

export async function logout(): Promise<void> {
  const res = await fetch(apiUrl('/api/auth/logout'), {
    method: 'POST',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
}
