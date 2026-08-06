import { isRouteErrorResponse, useRouteError } from 'react-router'

export default function ErrorPage() {
  const error = useRouteError()
  let message = 'Something went wrong.'
  if (isRouteErrorResponse(error)) {
    message = `${error.status} ${error.statusText}`
  } else if (error instanceof Error) {
    message = error.message
  }

  return (
    <div className="app-shell">
      <div className="empty-hint">{message}</div>
    </div>
  )
}
