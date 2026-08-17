import { Shield, MessageSquareText, FileText } from 'lucide-react'

const features = [
  { icon: Shield, text: 'Governed identity, OTP, and session controls' },
  { icon: MessageSquareText, text: 'Chat continuity that keeps the same thread alive' },
  { icon: FileText, text: 'Document and tax workflows behind approved service boundaries' },
]

export function AuthBanner() {
  return (
    <div
      className="hidden w-[40%] flex-col justify-center bg-navy-900 px-12 text-white md:flex"
    >
      <div className="mb-10">
        <div className="mb-2 flex items-baseline gap-1">
          <span className="text-display font-medium text-white">Kodi</span>
          <span className="text-display font-medium text-navy-300">Solutions</span>
        </div>
        <p className="max-w-sm text-sm text-navy-300">
          Kenya&apos;s AI tax workspace with secure access, grounded orchestration, and internal
          governance where it belongs.
        </p>
      </div>
      <ul className="space-y-5">
        {features.map(({ icon: Icon, text }) => (
          <li key={text} className="flex items-center gap-3">
            <Icon className="h-4 w-4 shrink-0 text-navy-300" />
            <span className="text-sm text-navy-300">{text}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
