import { type ReactNode } from 'react'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { ContextRail } from './ContextRail'
import { BottomNav } from './BottomNav'
import { ToastContainer } from '@/components/shared/Toast'
import { cn } from '@/lib/utils'

interface AppShellProps {
  children: ReactNode
  surfaceOwnership?: 'standard' | 'deferred_internal'
  surfaceNotice?: string
}

export function AppShell({
  children,
  surfaceOwnership = 'standard',
  surfaceNotice,
}: AppShellProps) {
  const showDeferredBanner = surfaceOwnership === 'deferred_internal' && surfaceNotice

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50 text-gray-900">
      <Sidebar />

      <div className="flex-1 flex flex-col overflow-hidden">
        <TopBar />

        <div className="flex-1 flex overflow-hidden">
          <main className="flex-1 flex flex-col overflow-hidden pb-16 md:pb-0">
            {showDeferredBanner && (
              <div
                className={cn(
                  'border-b px-5 py-3 text-sm',
                  'border-amber-200 bg-amber-50 text-amber-900'
                )}
              >
                <span className="font-medium">Deferred internal surface.</span>{' '}
                {surfaceNotice}
              </div>
            )}
            {children}
          </main>

          <ContextRail />
        </div>
      </div>

      <BottomNav />
      <ToastContainer />
    </div>
  )
}
