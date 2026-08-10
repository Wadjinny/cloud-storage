import { createBrowserRouter } from 'react-router'
import AppShell from './routes/app-shell'
import ErrorPage from './routes/error-page'
import HistoryPage from './routes/history'
import Workspace from './routes/workspace'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    errorElement: <ErrorPage />,
    children: [
      { index: true, element: <Workspace /> },
      { path: 'history', element: <HistoryPage /> },
    ],
  },
])
