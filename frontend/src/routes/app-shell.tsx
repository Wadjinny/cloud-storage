import { Outlet } from 'react-router'
import { TransferProvider } from '../TransferContext'

/** Shared chrome that stays mounted across Vault / History (transfer UI). */
export default function AppShell() {
  return (
    <TransferProvider>
      <Outlet />
    </TransferProvider>
  )
}
