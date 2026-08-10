/* @refresh reload */
import type { ReactNode } from 'react'

export function TitleIcon({ children }: { children: ReactNode }) {
  return (
    <svg
      width={12}
      height={12}
      viewBox="0 0 14 14"
      aria-hidden
      className="btn-icon-svg title-icon"
    >
      {children}
    </svg>
  )
}

export function TitleWithIcon({
  icon,
  children,
  className,
}: {
  icon: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div className={className}>
      {icon}
      {children}
    </div>
  )
}

export function AccountsIcon() {
  return (
    <TitleIcon>
      <circle cx="7" cy="4.5" r="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M2.5 11.5c.6-2.2 2.2-3.3 4.5-3.3s3.9 1.1 4.5 3.3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </TitleIcon>
  )
}

export function VaultIcon() {
  return (
    <TitleIcon>
      <rect
        x="2.5"
        y="3"
        width="9"
        height="8"
        rx="1"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <path
        d="M5.5 3V2.8a1.5 1.5 0 0 1 3 0V3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
      <circle cx="7" cy="7.5" r="1" fill="currentColor" />
    </TitleIcon>
  )
}

export function InspectIcon() {
  return (
    <TitleIcon>
      <circle cx="6" cy="6" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M8.5 8.5L11.5 11.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </TitleIcon>
  )
}

export function InsightsIcon() {
  return (
    <TitleIcon>
      <path
        d="M2.5 10.5V7.5M5.5 10.5V4.5M8.5 10.5V6M11.5 10.5V3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </TitleIcon>
  )
}

export function ActivityIcon() {
  return (
    <TitleIcon>
      <path
        d="M2 7h2.2l1.4-3.2L8.2 11l1.6-4H12"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function FiltersIcon() {
  return (
    <TitleIcon>
      <path
        d="M2.5 3.5h9M4 7h6M5.5 10.5h3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </TitleIcon>
  )
}

export function TransfersIcon() {
  return (
    <TitleIcon>
      <path
        d="M4 3.5v7M4 3.5L2.5 5M4 3.5L5.5 5M10 10.5v-7M10 10.5L8.5 9M10 10.5L11.5 9"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function ActionsIcon() {
  return (
    <TitleIcon>
      <rect x="2.5" y="2.5" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="7.5" y="2.5" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="2.5" y="7.5" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="7.5" y="7.5" width="4" height="4" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </TitleIcon>
  )
}

export function SignInIcon() {
  return (
    <TitleIcon>
      <rect
        x="2.5"
        y="2.5"
        width="6"
        height="9"
        rx="1"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <path
        d="M7 7h4.5M9.5 5L11.5 7L9.5 9"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function MegaIcon() {
  return (
    <TitleIcon>
      <path
        d="M2.5 10.5L4.5 3.5h2L8 8l1.5-4.5h2l2 7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function FolderIcon() {
  return (
    <TitleIcon>
      <path
        d="M2.5 4.5h3l1 1.2H11.5v5.8H2.5V4.5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function RenameIcon() {
  return (
    <TitleIcon>
      <path
        d="M8.5 3.5l2 2L5 11H3v-2L8.5 3.5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path
        d="M7.2 4.8l2 2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </TitleIcon>
  )
}

export function ConflictIcon() {
  return (
    <TitleIcon>
      <path
        d="M7 2.5L12.2 11.5H1.8L7 2.5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path
        d="M7 6v2.2M7 10.2h.01"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </TitleIcon>
  )
}

export function PruneIcon() {
  return (
    <TitleIcon>
      <path
        d="M4 5.5h6M5 5.5V11h4V5.5M5.5 5.5V3.5h3v2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function HealthIcon() {
  return (
    <TitleIcon>
      <path
        d="M7 12s-4.5-2.8-4.5-6A2.5 2.5 0 0 1 7 4.2 2.5 2.5 0 0 1 11.5 6C11.5 9.2 7 12 7 12z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function EvacuateIcon() {
  return (
    <TitleIcon>
      <path
        d="M3 3.5h5.5v7H3M8.5 7H12M10.2 5.2L12 7l-1.8 1.8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function DownloadIcon() {
  return (
    <TitleIcon>
      <path
        d="M7 2.5v7M4.5 7L7 9.5 9.5 7M2.5 11.5h9"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </TitleIcon>
  )
}

export function PreviewIcon() {
  return (
    <TitleIcon>
      <path
        d="M1.8 7s2-3.5 5.2-3.5S12.2 7 12.2 7s-2 3.5-5.2 3.5S1.8 7 1.8 7z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <circle cx="7" cy="7" r="1.4" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </TitleIcon>
  )
}
