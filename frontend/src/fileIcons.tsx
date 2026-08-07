import type { IconKind } from './fileIconKinds'

/** Compact glyph icons (no emoji); SVG strokes use currentColor. */
export function FileTypeIcon({ kind, size = 16 }: { kind: IconKind; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.4,
    'aria-hidden': true as const,
  }
  switch (kind) {
    case 'dir':
      return (
        <svg {...common}>
          <path d="M2 4.5h4l1 1.5H14v6.5H2z" />
        </svg>
      )
    case 'image':
      return (
        <svg {...common}>
          <rect x="2" y="3" width="12" height="10" rx="1" />
          <circle cx="5.5" cy="6.5" r="1" />
          <path d="M2 11l3.5-3.5L9 11l2-2 3 3" />
        </svg>
      )
    case 'pdf':
      return (
        <svg {...common}>
          <path d="M4 2h6l3 3v9H4z" />
          <path d="M10 2v3h3" />
          <path d="M5.5 10.5h5M5.5 8h5" />
        </svg>
      )
    case 'video':
      return (
        <svg {...common}>
          <rect x="2" y="4" width="9" height="8" rx="1" />
          <path d="M11 6.5l3-1.5v6l-3-1.5z" />
        </svg>
      )
    case 'office':
      return (
        <svg {...common}>
          <path d="M4 2h6l3 3v9H4z" />
          <path d="M10 2v3h3" />
          <path d="M6 8h4M6 10.5h4" />
        </svg>
      )
    case 'archive':
      return (
        <svg {...common}>
          <path d="M3 3h10v10H3z" />
          <path d="M8 3v10M5 6h6M5 9h6" />
        </svg>
      )
    case 'code':
      return (
        <svg {...common}>
          <path d="M5 5L2.5 8 5 11M11 5l2.5 3L11 11M9 4.5l-2 7" />
        </svg>
      )
    case 'audio':
      return (
        <svg {...common}>
          <path d="M5 11a2 2 0 1 0 0-4 2 2 0 0 0 0 4z" />
          <path d="M7 9V4l6-1v5" />
          <path d="M13 8a2 2 0 1 0 0 4" />
        </svg>
      )
    default:
      return (
        <svg {...common}>
          <path d="M4 2h6l3 3v9H4z" />
          <path d="M10 2v3h3" />
        </svg>
      )
  }
}
