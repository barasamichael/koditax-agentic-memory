import { Spinner } from '@/components/shared/Spinner'

export function DocumentsLoadingState() {
  return (
    <div
      className={[
        'flex flex-col items-center justify-center rounded-3xl border',
        'border-dashed border-gray-200',
        'bg-white px-6 py-12 text-center',
      ].join(' ')}
      role="status"
      aria-live="polite"
      aria-label="Loading documents"
    >
      <Spinner className="h-8 w-8 text-navy-700" />
      <p className="mt-4 text-sm font-medium text-gray-700">Loading documents…</p>
      <p className="mt-1 text-sm text-gray-500">Please wait while we load your saved files.</p>
    </div>
  )
}
