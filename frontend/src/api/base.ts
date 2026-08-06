/**
 * Backend origin for API calls.
 * Empty = same origin (prod behind Caddy).
 * In dev, rewrite the host to match the page so session cookies stay same-site
 * whether you open localhost or 127.0.0.1.
 */
function resolveApiBase(): string {
  const raw = String(import.meta.env.VITE_API_BASE ?? '').trim()
  if (!raw) return ''
  try {
    const url = new URL(raw)
    if (typeof window !== 'undefined' && window.location.hostname) {
      url.hostname = window.location.hostname
    }
    return url.origin
  } catch {
    return raw.replace(/\/$/, '')
  }
}

const API_BASE = resolveApiBase()

export function apiUrl(path: string): string {
  const p = path.startsWith('/') ? path : `/${path}`
  return `${API_BASE}${p}`
}
