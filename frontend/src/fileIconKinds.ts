import type { VaultEntry } from './api/vault'

export type IconKind =
  | 'dir'
  | 'image'
  | 'pdf'
  | 'video'
  | 'office'
  | 'archive'
  | 'code'
  | 'audio'
  | 'generic'

const IMAGE_EXT = new Set([
  '.jpg',
  '.jpeg',
  '.png',
  '.gif',
  '.webp',
  '.bmp',
  '.tif',
  '.tiff',
  '.heic',
  '.heif',
])
const PDF_EXT = new Set(['.pdf'])
const VIDEO_EXT = new Set(['.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.mpeg', '.mpg'])
const OFFICE_EXT = new Set([
  '.doc',
  '.docx',
  '.odt',
  '.rtf',
  '.ppt',
  '.pptx',
  '.odp',
  '.xls',
  '.xlsx',
  '.ods',
])
const ARCHIVE_EXT = new Set(['.zip', '.tar', '.gz', '.tgz', '.rar', '.7z', '.bz2'])
const CODE_EXT = new Set([
  '.py',
  '.js',
  '.ts',
  '.tsx',
  '.jsx',
  '.json',
  '.toml',
  '.yaml',
  '.yml',
  '.md',
  '.txt',
  '.css',
  '.html',
  '.rs',
  '.go',
  '.java',
  '.c',
  '.h',
  '.cpp',
  '.sh',
])
const AUDIO_EXT = new Set(['.mp3', '.wav', '.flac', '.ogg', '.aac', '.m4a'])

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

export function iconKindForEntry(entry: Pick<VaultEntry, 'kind' | 'name'>): IconKind {
  if (entry.kind === 'dir') return 'dir'
  const ext = extOf(entry.name)
  if (IMAGE_EXT.has(ext)) return 'image'
  if (PDF_EXT.has(ext)) return 'pdf'
  if (VIDEO_EXT.has(ext)) return 'video'
  if (OFFICE_EXT.has(ext)) return 'office'
  if (ARCHIVE_EXT.has(ext)) return 'archive'
  if (CODE_EXT.has(ext)) return 'code'
  if (AUDIO_EXT.has(ext)) return 'audio'
  return 'generic'
}
