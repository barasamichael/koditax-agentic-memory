import { cn } from '@/lib/utils'

interface SpinnerProps {
  size?: 'sm' | 'md'
  className?: string
}

export function Spinner({ size = 'md', className }: SpinnerProps) {
  return (
    <span
      className={cn(
        'inline-block animate-spin rounded-full border-2 border-current border-t-transparent',
        size === 'sm' ? 'w-4 h-4' : 'w-8 h-8',
        className
      )}
      aria-label="Loading"
      role="status"
    />
  )
}
