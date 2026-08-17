import { type ReactNode } from 'react'
import { cn } from '@/lib/utils'

interface EmptyStateProps {
  title: string
  description: string
  icon?: ReactNode
  action?: { label: string; onClick: () => void }
  className?: string
}

export function EmptyState({ title, description, icon, action, className }: EmptyStateProps) {
  return (
    <div className={cn('flex justify-center py-8 sm:py-12', className)}>
      <div className="flex w-full max-w-xl flex-col items-center justify-center gap-4 rounded-3xl border border-gray-200 bg-white px-6 py-10 text-center shadow-card">
        {icon && (
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gray-50 text-gray-400">
            {icon}
          </div>
        )}
        <div>
          <p className="text-body font-medium text-gray-700">{title}</p>
          <p className="mt-1 text-small text-gray-500">{description}</p>
        </div>
        {action && (
          <button
            onClick={action.onClick}
            className="rounded-input border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 transition-all hover:bg-gray-50"
          >
            {action.label}
          </button>
        )}
      </div>
    </div>
  )
}
