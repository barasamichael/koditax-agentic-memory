import { useEffect } from 'react'
import { createBrowserRouter, Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'
import AuthPage from '@/pages/AuthPage'
import ChatPage from '@/pages/ChatPage'
import SharedChatPage from '@/pages/SharedChatPage'
import DocumentsPage from '@/pages/DocumentsPage'
import AccountPage from '@/pages/AccountPage'
import KnowledgeAdminPage from '@/pages/KnowledgeAdminPage'

const buildAuthRedirect = (path: string, reason: string) =>
  `/?reason=${encodeURIComponent(reason)}&redirect=${encodeURIComponent(path)}`

function getCurrentPath(pathname: string, search: string, hash: string): string {
  return `${pathname}${search}${hash}` || '/chat'
}

function ProtectedRoute() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const tokenExpiresAt = useAuthStore((s) => s.tokenExpiresAt)
  const clearAuth = useAuthStore((s) => s.clearAuth)
  const location = useLocation()

  const currentPath = getCurrentPath(location.pathname, location.search, location.hash)
  const isExpired = Boolean(tokenExpiresAt && new Date(tokenExpiresAt).getTime() <= Date.now())

  useEffect(() => {
    if (isAuthenticated && isExpired) {
      clearAuth({ reason: 'session_expired' })
    }
  }, [clearAuth, isAuthenticated, isExpired])

  if (!isAuthenticated || isExpired) {
    return <Navigate to={buildAuthRedirect(currentPath, isExpired ? 'session_expired' : 'session_required')} replace />
  }

  return <Outlet />
}

function AdminRoute() {
  const role = useAuthStore((s) => s.role)
  if (role !== 'Administrator') {
    return <Navigate to="/chat" replace />
  }
  return <Outlet />
}

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AuthPage />,
  },
  {
    element: <ProtectedRoute />,
    children: [
      { path: '/chat', element: <ChatPage /> },
      { path: '/chat/shared/:shareId', element: <SharedChatPage /> },
      { path: '/documents', element: <DocumentsPage /> },
      { path: '/documents/trash', element: <DocumentsPage /> },
      { path: '/documents/:documentId', element: <DocumentsPage /> },
      { path: '/documents/*', element: <DocumentsPage /> },
      { path: '/account', element: <AccountPage /> },
      { path: '/account/phone-change', element: <AccountPage /> },
      { path: '/account/deletion', element: <AccountPage /> },
      {
        element: <AdminRoute />,
        children: [{ path: '/internal/knowledge', element: <KnowledgeAdminPage /> }],
      },
    ],
  },
])
