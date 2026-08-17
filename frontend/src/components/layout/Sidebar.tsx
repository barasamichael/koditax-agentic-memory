import { NavLink, useNavigate, useMatch } from 'react-router-dom'
import { MessageSquare, FolderOpen, User, ChevronLeft, ChevronRight, LogOut, ShieldCheck } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { useUIStore } from '@/stores/uiStore'
import { useAuthStore } from '@/stores/authStore'
import { useLogout } from '@/hooks/useAuth'
import { useToast } from '@/components/shared/Toast'
import { Spinner } from '@/components/shared/Spinner'
import { getProfile } from '@/api/auth.api'
import { cn } from '@/lib/utils'
import type { Role } from '@/types/auth'

const navItems = [
  { icon: MessageSquare, label: 'Chat', to: '/chat' },
  { icon: FolderOpen, label: 'Documents', to: '/documents' },
  { icon: User, label: 'Account', to: '/account' },
]

const ROLE_CONFIG: Record<Role, { label: string }> = {
  IndividualTaxpayer: { label: 'Individual' },
  TaxAgent:          { label: 'Tax Agent' },
  Accountant:        { label: 'Accountant' },
  Administrator:     { label: 'Admin' },
}

export function Sidebar() {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const session = useAuthStore((s) => s.session)
  const role = useAuthStore((s) => s.role)
  const navigate = useNavigate()
  const toast = useToast()
  const knowledgeAdminActive = useMatch('/internal/knowledge')
  const documentsRootActive = useMatch('/documents')
  const documentsActive = useMatch('/documents/*')

  const logout = useLogout()
  const roleConfig = role ? ROLE_CONFIG[role] : null

  const profileQuery = useQuery({
    queryKey: ['auth-profile'],
    queryFn: getProfile,
    enabled: !!session,
    staleTime: 5 * 60 * 1000,
  })

  const handleLogout = () => {
    logout.mutate(undefined, {
      onError: () => {
        toast.error('Sign out failed. Try again.')
      },
    })
  }

  return (
    <aside
      className={cn(
        'hidden shrink-0 flex-col bg-navy-900 transition-all duration-200 md:flex',
        collapsed ? 'w-[60px]' : 'w-56'
      )}
    >
      {/* Logo */}
      <div
        className="flex h-14 shrink-0 items-center gap-2.5 px-4 cursor-pointer select-none"
        onClick={() => navigate('/chat')}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && navigate('/chat')}
        aria-label="Go to chat"
      >
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-kodi-accent">
          <span className="text-xs font-bold text-white">K</span>
        </div>
        <AnimatePresence>
          {!collapsed && (
            <motion.span
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: 'auto' }}
              exit={{ opacity: 0, width: 0 }}
              className="overflow-hidden whitespace-nowrap text-sm font-semibold text-white"
            >
              Kodi
            </motion.span>
          )}
        </AnimatePresence>
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-0.5 px-2 py-3">
        {navItems.map(({ icon: Icon, label, to }) => {
          const isDocuments = to === '/documents' && (!!documentsRootActive || !!documentsActive)
          return (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm transition-all',
                  isActive || isDocuments
                    ? 'bg-white/10 font-medium text-white'
                    : 'text-white/50 hover:bg-white/5 hover:text-white/80'
                )
              }
              aria-label={label}
            >
              <Icon className="h-4 w-4 shrink-0" />
              <AnimatePresence>
                {!collapsed && (
                  <motion.span
                    initial={{ opacity: 0, width: 0 }}
                    animate={{ opacity: 1, width: 'auto' }}
                    exit={{ opacity: 0, width: 0 }}
                    className="overflow-hidden whitespace-nowrap"
                  >
                    {label}
                  </motion.span>
                )}
              </AnimatePresence>
            </NavLink>
          )
        })}

        {role === 'Administrator' && (
          <div className="mt-5 space-y-0.5">
            {!collapsed && (
              <p className="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-widest text-white/30">
                Knowledge Base
              </p>
            )}
            <NavLink
              to="/internal/knowledge"
              className={() =>
                cn(
                  'flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm transition-all',
                  knowledgeAdminActive
                    ? 'bg-red-500/20 font-medium text-red-300'
                    : 'text-white/50 hover:bg-white/5 hover:text-white/80'
                )
              }
              aria-label="Knowledge Base admin"
            >
              <ShieldCheck className="h-4 w-4 shrink-0" />
              <AnimatePresence>
                {!collapsed && (
                  <motion.span
                    initial={{ opacity: 0, width: 0 }}
                    animate={{ opacity: 1, width: 'auto' }}
                    exit={{ opacity: 0, width: 0 }}
                    className="overflow-hidden whitespace-nowrap"
                  >
                    Knowledge Base
                  </motion.span>
                )}
              </AnimatePresence>
            </NavLink>
          </div>
        )}
      </nav>

      {/* User + collapse */}
      <div className="space-y-1 border-t border-white/10 p-2">
        <div className="flex items-center gap-2 rounded-lg px-2 py-1.5">
          {profileQuery.data?.gravatar_url ? (
            <img
              src={profileQuery.data.gravatar_url}
              alt="Avatar"
              className="h-7 w-7 shrink-0 rounded-full ring-1 ring-white/20"
            />
          ) : (
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white/15">
              <User className="h-3.5 w-3.5 text-white/60" />
            </div>
          )}
          <AnimatePresence>
            {!collapsed && (
              <motion.div
                initial={{ opacity: 0, width: 0 }}
                animate={{ opacity: 1, width: 'auto' }}
                exit={{ opacity: 0, width: 0 }}
                className="min-w-0 flex-1 overflow-hidden whitespace-nowrap"
              >
                <p className="truncate text-xs font-medium text-white/80">
                  {profileQuery.data?.email ?? '…'}
                </p>
                {roleConfig && (
                  <span className="text-[10px] text-white/40">{roleConfig.label}</span>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <button
          onClick={handleLogout}
          disabled={logout.isPending}
          className={cn(
            'flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-sm transition-all',
            'text-white/40 hover:bg-white/5 hover:text-white/70',
            logout.isPending && 'cursor-not-allowed opacity-50'
          )}
          aria-label="Sign out"
        >
          {logout.isPending ? (
            <Spinner size="sm" className="h-4 w-4 shrink-0 text-white/40" />
          ) : (
            <LogOut className="h-4 w-4 shrink-0" />
          )}
          <AnimatePresence>
            {!collapsed && (
              <motion.span
                initial={{ opacity: 0, width: 0 }}
                animate={{ opacity: 1, width: 'auto' }}
                exit={{ opacity: 0, width: 0 }}
                className="overflow-hidden whitespace-nowrap"
              >
                Sign out
              </motion.span>
            )}
          </AnimatePresence>
        </button>

        <button
          onClick={toggleSidebar}
          className="flex w-full items-center justify-center rounded-lg p-1.5 text-white/30 transition-all hover:bg-white/5 hover:text-white/60"
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronLeft className="h-3.5 w-3.5" />}
        </button>
      </div>
    </aside>
  )
}
