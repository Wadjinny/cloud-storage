export type Account = {
  id: string
  provider: 'gdrive' | 'mega'
  label: string
}

export type AccountStorage = {
  id: string
  provider: 'gdrive' | 'mega' | string
  label: string
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
  const res = await fetch('/api/accounts', { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<Account[]>
}

export async function getStorageStats(): Promise<StorageStats> {
  const res = await fetch('/api/accounts/storage', { credentials: 'include' })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<StorageStats>
}

export async function deleteAccount(id: string): Promise<void> {
  const res = await fetch(`/api/accounts/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await readError(res))
}

export async function connectMega(
  email: string,
  password: string,
  mfa?: string,
): Promise<Account> {
  const res = await fetch('/api/auth/mega', {
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
  window.location.href = '/api/auth/google/start'
}
