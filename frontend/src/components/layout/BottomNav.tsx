import { NavLink } from 'react-router-dom'
import { MessageSquare, FolderOpen, User } from 'lucide-react'
import { cn } from '@/lib/utils'

const tabs = [
  { icon: MessageSquare, label: 'Chat', to: '/chat' },
  { icon: FolderOpen, label: 'Docs', to: '/documents' },
  { icon: User, label: 'Account', to: '/account' },
]

export function BottomNav() {
  return (
    <nav className="fixed bottom-0 left-0 right-0 z-20 flex border-t border-gray-100 bg-white/95 pb-safe backdrop-blur-md md:hidden">
      {tabs.map(({ icon: Icon, label, to }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              'flex flex-1 flex-col items-center justify-center gap-1 py-3 text-xs font-medium transition-all',
              isActive ? 'text-navy-900' : 'text-gray-400 hover:text-gray-600'
            )
          }
          aria-label={label}
        >
          {({ isActive }) => (
            <>
              <div className={cn(
                'flex h-6 w-6 items-center justify-center rounded-lg transition-all',
                isActive ? 'bg-navy-900' : 'bg-transparent'
              )}>
                <Icon className={cn('h-4 w-4', isActive ? 'text-white' : 'text-gray-400')} />
              </div>
              <span className={isActive ? 'text-navy-900' : 'text-gray-400'}>{label}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  )
}
