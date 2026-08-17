import { cn } from '@/lib/utils'

interface PasswordStrengthBarProps {
  password: string
}

const criteria = [
  { label: '12+ characters', check: (password: string) => password.length >= 12 },
  { label: 'Uppercase', check: (password: string) => /[A-Z]/.test(password) },
  { label: 'Lowercase', check: (password: string) => /[a-z]/.test(password) },
  { label: 'Number', check: (password: string) => /\d/.test(password) },
  { label: 'Special char (!@#$%^&*)', check: (password: string) => /[!@#$%^&*]/.test(password) },
]

const strengthLabel = ['Too short', 'Weak', 'Fair', 'Good', 'Strong', 'Very strong']
const strengthColor = [
  'bg-gray-200',
  'bg-red-500',
  'bg-amber-500',
  'bg-yellow-400',
  'bg-emerald-500',
  'bg-kodi-accent',
]

export function PasswordStrengthBar({ password }: PasswordStrengthBarProps) {
  const score = criteria.filter(({ check }) => check(password)).length
  const activeColor = strengthColor[score]

  return (
    <div className="mt-2 space-y-2">
      <div className="flex gap-1">
        {criteria.map((_, index) => (
          <div
            key={index}
            className={cn(
              'h-1 flex-1 rounded-full transition-all duration-200',
              index < score ? activeColor : 'bg-gray-200'
            )}
          />
        ))}
      </div>

      {password.length > 0 ? (
        <div className="space-y-1">
          <p className="text-small text-gray-500">{strengthLabel[score]}</p>
          <div className="flex flex-wrap gap-2">
            {criteria.map(({ label, check }) => (
              <span
                key={label}
                className={cn(
                  'rounded-full px-2 py-1 text-[11px] font-medium',
                  check(password)
                    ? 'bg-emerald-50 text-emerald-700'
                    : 'bg-gray-100 text-gray-500'
                )}
              >
                {label}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  )
}
