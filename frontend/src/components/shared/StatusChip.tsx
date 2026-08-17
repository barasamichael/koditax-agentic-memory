import { cn } from '@/lib/utils'

const STATUS_CONFIG: Record<string, { label: string; bg: string; text: string }> = {
  checking_file: { label: 'Checking file', bg: 'bg-amber-50', text: 'text-amber-900' },
  getting_ready: { label: 'Getting ready', bg: 'bg-gray-100', text: 'text-gray-700' },
  ready: { label: 'Ready', bg: 'bg-green-50', text: 'text-green-800' },
  ready_with_limitations: {
    label: 'Ready with limitations',
    bg: 'bg-sky-50',
    text: 'text-sky-900',
  },
  needs_attention: { label: 'Needs attention', bg: 'bg-orange-50', text: 'text-orange-900' },
  updating: { label: 'Updating', bg: 'bg-indigo-50', text: 'text-indigo-900' },
  in_trash: { label: 'In trash', bg: 'bg-slate-100', text: 'text-slate-700' },
  deleting: { label: 'Deleting', bg: 'bg-red-50', text: 'text-red-800' },
  draft: { label: 'Draft', bg: 'bg-navy-50', text: 'text-navy-900' },
  pending_verification: { label: 'Pending verification', bg: 'bg-amber-50', text: 'text-amber-900' },
  blocked: { label: 'Blocked', bg: 'bg-red-50', text: 'text-red-800' },
  submitted: { label: 'Submitted', bg: 'bg-kodi-accent/10', text: 'text-green-900' },
  processing: { label: 'Processing', bg: 'bg-gray-100', text: 'text-gray-600' },
}

interface StatusChipProps {
  status: string
  label?: string
  className?: string
}

export function StatusChip({ status, label, className }: StatusChipProps) {
  const cfg = STATUS_CONFIG[status] ?? { label: status, bg: 'bg-gray-100', text: 'text-gray-700' }
  return (
    <span
      className={cn(
        'inline-flex items-center px-2.5 py-0.5 rounded-chip text-xs font-medium',
        cfg.bg,
        cfg.text,
        className
      )}
    >
      {label ?? cfg.label}
    </span>
  )
}
