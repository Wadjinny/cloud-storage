import { createBrowserRouter } from 'react-router'
import ErrorPage from './routes/error-page'
import Workspace from './routes/workspace'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <Workspace />,
    errorElement: <ErrorPage />,
  },
])
