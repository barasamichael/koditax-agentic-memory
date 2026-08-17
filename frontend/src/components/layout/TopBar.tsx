import { useState, useEffect, useRef } from 'react'
import { PanelLeft, PanelRight, LogOut, Settings, User } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useUIStore } from '@/stores/uiStore'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useLogout } from '@/hooks/useAuth'
import { useToast } from '@/components/shared/Toast'
import { getProfile } from '@/api/auth.api'
import { ChatToolbar } from '@/components/chat/ChatToolbar'
import { cn } from '@/lib/utils'

const routeLabels: Record<string, string> = {
  '/chat': 'Chat',
  '/documents': 'Documents',
  '/account': 'Account',
  '/internal/knowledge': 'Knowledge Admin',
}

export function TopBar() {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const toggleRail = useUIStore((s) => s.toggleRail)
  const toggleChatHistory = useUIStore((s) => s.toggleChatHistory)
  const railOpen = useUIStore((s) => s.railOpen)
  const userId = useAuthStore((s) => s.userId)
  const activeConversationTitle = useChatStore((s) => {
    if (pathname !== '/chat' || !s.conversationId || !s.activeUserId) return null
    return (
      s.userStates[s.activeUserId]?.conversations.find(
        (conversation) => conversation.conversationId === s.conversationId
      )?.title ?? null
    )
  })

  const activeConversation = useChatStore((s) => {
    if (pathname !== '/chat' || !s.conversationId || !s.activeUserId) return null
    return (
      s.userStates[s.activeUserId]?.conversations.find(
        (conversation) => conversation.conversationId === s.conversationId
      ) ?? null
    )
  })
  const toast = useToast()
  const logout = useLogout()

  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  const session = useAuthStore((s) => s.session)
  const profileQuery = useQuery({
    queryKey: ['auth-profile'],
    queryFn: getProfile,
    enabled: !!session,
    staleTime: 5 * 60 * 1000,
  })

  const pageTitle = routeLabels[pathname] ?? 'Kodi'

  useEffect(() => {
    if (!menuOpen) return
    const handleMouseDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [menuOpen])

  const handleLogout = () => {
    setMenuOpen(false)
    logout.mutate(undefined, {
      onError: () => toast.error('Sign out failed. Try again.'),
    })
  }

  return (
    <header className="flex h-13 shrink-0 items-center justify-between border-b border-gray-100 bg-white px-4 sm:px-5">
      <div className="flex min-w-0 items-center gap-3">
        {pathname === '/chat' && (
          <button
            onClick={toggleChatHistory}
            className="flex items-center justify-center rounded-lg p-1.5 text-gray-400 transition-all hover:bg-gray-50 hover:text-gray-600 md:hidden"
            aria-label="Open previous chats"
          >
            <PanelLeft className="h-4 w-4" />
          </button>
        )}
        <div className="min-w-0">
          <span className="block truncate text-sm font-semibold text-gray-900">
            {pageTitle}
          </span>
          {pathname === '/chat' && activeConversationTitle && (
            <span className="block truncate text-[11px] text-gray-400">
              {activeConversationTitle}
            </span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1.5">
        {pathname === '/chat' && <ChatToolbar conversation={activeConversation} />}

        <button
          onClick={toggleRail}
          className={cn(
            'flex items-center justify-center rounded-lg p-1.5 transition-all',
            railOpen
              ? 'bg-navy-50 text-navy-700'
              : 'text-gray-400 hover:bg-gray-50 hover:text-gray-600'
          )}
          aria-label="Toggle context rail"
        >
          <PanelRight className="h-4 w-4" />
        </button>

        <div className="relative ml-1" ref={menuRef}>
          <button
            onClick={() => setMenuOpen((o) => !o)}
            className={cn(
              'flex h-7 w-7 items-center justify-center rounded-full transition-all overflow-hidden',
              'bg-navy-900 text-white hover:ring-2 hover:ring-navy-400 hover:ring-offset-1',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500 focus-visible:ring-offset-1'
            )}
            aria-label="User menu"
            aria-expanded={menuOpen}
          >
            {profileQuery.data?.gravatar_url ? (
              <img src={profileQuery.data.gravatar_url} alt="Avatar" className="h-7 w-7 object-cover" />
            ) : (
              <User className="h-3.5 w-3.5" />
            )}
          </button>

          {menuOpen && (
            <div className="absolute right-0 top-9 z-40 w-56 overflow-hidden rounded-xl border border-gray-100 bg-white shadow-xl">
              <div className="flex items-center gap-2.5 border-b border-gray-50 px-3 py-2.5">
                {profileQuery.data?.gravatar_url ? (
                  <img
                    src={profileQuery.data.gravatar_url}
                    alt="Avatar"
                    className="h-8 w-8 shrink-0 rounded-full"
                  />
                ) : (
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-navy-900">
                    <User className="h-4 w-4 text-white" />
                  </div>
                )}
                <div className="min-w-0">
                  <p className="truncate text-xs font-medium text-gray-800">
                    {profileQuery.data?.email ?? userId ?? ''}
                  </p>
                  <p className="text-[10px] text-gray-400">{profileQuery.data?.role ?? ''}</p>
                </div>
              </div>
              <div className="py-1">
                <button
                  onClick={() => { setMenuOpen(false); navigate('/account') }}
                  className="flex w-full items-center gap-2.5 px-3 py-2 text-sm text-gray-700 transition-colors hover:bg-gray-50"
                >
                  <Settings className="h-3.5 w-3.5 text-gray-400" />
                  Account settings
                </button>
                <div className="mx-3 my-1 border-t border-gray-100" />
                <button
                  onClick={handleLogout}
                  className="flex w-full items-center gap-2.5 px-3 py-2 text-sm text-red-600 transition-colors hover:bg-red-50"
                >
                  <LogOut className="h-3.5 w-3.5" />
                  Sign out
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
