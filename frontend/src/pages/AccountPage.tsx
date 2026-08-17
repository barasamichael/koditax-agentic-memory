import { useEffect, useMemo, useRef, useState, type ElementType } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ChevronRight, Shield, Phone, Trash2, Clock, Activity, RefreshCw, Eye, EyeOff } from 'lucide-react'
import { AppShell } from '@/components/layout/AppShell'
import { OtpInput } from '@/components/auth/OtpInput'
import { PasswordStrengthBar } from '@/components/auth/PasswordStrengthBar'
import { ConfirmModal } from '@/components/shared/ConfirmModal'
import { Spinner } from '@/components/shared/Spinner'
import { StatusChip } from '@/components/shared/StatusChip'
import { useToast } from '@/components/shared/Toast'
import { useSession } from '@/hooks/useAuth'
import { useAuthStore } from '@/stores/authStore'
import {
  confirmPasswordReset,
  confirmPhoneChange,
  getProfile,
  initiatePasswordReset,
  requestAccountDeletion,
  requestPhoneChange,
} from '@/api/auth.api'
import { normalizeError } from '@/lib/errorNormalizer'
import { formatDate, normalizeKenyanPhone, cn } from '@/lib/utils'

// ─── Pure formatters (logic untouched) ───────────────────────────────────────

type SessionStatus = 'active' | 'warning' | 'invalidated' | 'expired'

interface AccountDeletionResponse {
  status: string
  request_id: string
  deletion_state: string
  requested_at: string
  blockers: string[]
}

function formatSessionDate(iso: string): string {
  const date = new Date(iso)
  return `${formatDate(date)} at ${date.toLocaleTimeString('en-KE', {
    hour: '2-digit',
    minute: '2-digit',
  })}`
}

function sessionChipStatus(status: SessionStatus): 'ready' | 'pending_verification' | 'blocked' {
  if (status === 'active') return 'ready'
  if (status === 'warning') return 'pending_verification'
  return 'blocked'
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function AccountPage() {
  const navigate   = useNavigate()
  const location   = useLocation()
  const toast      = useToast()

  // ── Store (logic unchanged) ──────────────────────────────────────────────
  const session   = useAuthStore((state) => state.session)
  const role      = useAuthStore((state) => state.role)
  const clearAuth = useAuthStore((state) => state.clearAuth)

  const sessionQuery = useSession(session?.session_id ?? null)

  const profileQuery = useQuery({
    queryKey: ['auth-profile'],
    queryFn: getProfile,
    enabled: !!session,
    staleTime: 5 * 60 * 1000,
  })

  // ── Phone-change state (logic unchanged) ────────────────────────────────
  const [phoneChangeRequestId,   setPhoneChangeRequestId]   = useState<string | null>(null)
  const [phoneChangeChallengeId, setPhoneChangeChallengeId] = useState<string | null>(null)
  const [phoneChangeOtp,         setPhoneChangeOtp]         = useState('')
  const [newPhoneNumber,         setNewPhoneNumber]         = useState('')
  const [currentPassword,        setCurrentPassword]        = useState('')

  // ── Password reset state ─────────────────────────────────────────────────
  type PasswordResetState = 'idle' | 'sending' | 'sent' | 'confirming' | 'done' | 'error'
  const [passwordResetState,    setPasswordResetState]    = useState<PasswordResetState>('idle')
  const [passwordResetError,    setPasswordResetError]    = useState<string | null>(null)
  const [passwordResetCooldown, setPasswordResetCooldown] = useState(0)
  const [passwordResetChallengeId, setPasswordResetChallengeId] = useState<string | null>(null)
  const [passwordResetOtp,      setPasswordResetOtp]      = useState('')
  const [newPassword,           setNewPassword]           = useState('')
  const [confirmPassword,       setConfirmPassword]       = useState('')
  const [showNewPassword,       setShowNewPassword]       = useState(false)
  const [showConfirmPassword,   setShowConfirmPassword]   = useState(false)
  const cooldownRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── Deletion state (logic unchanged) ────────────────────────────────────
  const [deletionReason,      setDeletionReason]      = useState('')
  const [deletionConfirmOpen, setDeletionConfirmOpen] = useState(false)
  const [deletionResult,      setDeletionResult]      = useState<AccountDeletionResponse | null>(null)

  // ── Mutations (logic unchanged) ──────────────────────────────────────────
  const requestPhoneChangeMutation = useMutation({
    mutationFn: () =>
      requestPhoneChange({
        new_phone_number: normalizeKenyanPhone(newPhoneNumber),
        current_password: currentPassword,
      }),
    onSuccess: (data) => {
      setPhoneChangeRequestId(data.request_id)
      setPhoneChangeChallengeId(data.step_up_challenge_id)
      setPhoneChangeOtp('')
      toast.success('Phone change challenge created. Enter the OTP to confirm the new number.')
    },
    onError: (err) => {
      toast.error(normalizeError(err).message)
    },
  })

  const confirmPhoneChangeMutation = useMutation({
    mutationFn: () => {
      if (!phoneChangeRequestId || !phoneChangeChallengeId) {
        throw new Error('Missing phone change confirmation context.')
      }
      return confirmPhoneChange({
        request_id: phoneChangeRequestId,
        step_up_challenge_id: phoneChangeChallengeId,
        step_up_otp_code: phoneChangeOtp,
      })
    },
    onSuccess: () => {
      clearAuth({ reason: 'phone_change_confirmed' })
      navigate('/?reason=phone_change_confirmed', { replace: true })
      toast.success('Phone number updated. Sign in again with the new number.')
    },
    onError: (err) => {
      toast.error(normalizeError(err).message)
    },
  })

  const startCooldown = () => {
    setPasswordResetCooldown(60)
    if (cooldownRef.current) clearInterval(cooldownRef.current)
    cooldownRef.current = setInterval(() => {
      setPasswordResetCooldown((prev) => {
        if (prev <= 1) {
          clearInterval(cooldownRef.current!)
          cooldownRef.current = null
          return 0
        }
        return prev - 1
      })
    }, 1000)
  }

  useEffect(() => () => { if (cooldownRef.current) clearInterval(cooldownRef.current) }, [])

  const passwordResetMutation = useMutation({
    mutationFn: () => {
      const email = profileQuery.data?.email
      if (!email) throw new Error('Profile email not loaded. Refresh the page and try again.')
      return initiatePasswordReset({ purpose: 'password_reset', email })
    },
    onMutate: () => {
      setPasswordResetState('sending')
      setPasswordResetError(null)
    },
    onSuccess: (data) => {
      setPasswordResetChallengeId(data.challenge_id)
      setPasswordResetOtp('')
      setNewPassword('')
      setConfirmPassword('')
      setPasswordResetState('sent')
      startCooldown()
    },
    onError: (err) => {
      const norm = normalizeError(err)
      const errorMessages: Record<string, string> = {
        auth_rate_limit_exceeded: 'Too many reset attempts. Wait a minute before trying again.',
        otp_resend_throttled: 'A reset email was sent recently. Wait before requesting another.',
        account_not_found: 'No account found for this email address.',
        account_locked: 'Your account is locked. Contact support.',
      }
      setPasswordResetError(errorMessages[norm.error_code] ?? norm.message)
      setPasswordResetState('error')
    },
  })

  const confirmPasswordResetMutation = useMutation({
    mutationFn: () => {
      if (!passwordResetChallengeId) throw new Error('No reset challenge active.')
      return confirmPasswordReset({
        challenge_id: passwordResetChallengeId,
        reset_code: passwordResetOtp,
        new_password: newPassword,
      })
    },
    onMutate: () => {
      setPasswordResetState('confirming')
      setPasswordResetError(null)
    },
    onSuccess: () => {
      setPasswordResetState('done')
      if (cooldownRef.current) clearInterval(cooldownRef.current)
    },
    onError: (err) => {
      const norm = normalizeError(err)
      const errorMessages: Record<string, string> = {
        otp_invalid: 'Incorrect reset code. Check your email and try again.',
        otp_expired: 'The reset code has expired. Request a new one.',
        otp_max_attempts_exceeded: 'Too many incorrect attempts. Request a new reset code.',
        password_policy_violation: 'Password does not meet the requirements.',
      }
      setPasswordResetError(errorMessages[norm.error_code] ?? norm.message)
      setPasswordResetState('sent')
    },
  })

  const deletionMutation = useMutation({
    mutationFn: () => requestAccountDeletion({ request_reason: deletionReason }),
    onSuccess: (data) => {
      const result = data as AccountDeletionResponse
      setDeletionResult(result)
      setDeletionConfirmOpen(false)
      if (result.blockers.length > 0) {
        toast.warning('Deletion request submitted but there are blockers.')
      } else {
        toast.success('Deletion request submitted. Check your email for next steps.')
      }
    },
    onError: (err) => {
      toast.error(normalizeError(err).message)
      setDeletionConfirmOpen(false)
    },
  })

  // ── Derived (logic unchanged) ────────────────────────────────────────────
  const highlightPhoneChange = location.pathname === '/account/phone-change'
  const highlightDeletion    = location.pathname === '/account/deletion'
  const sessionData          = sessionQuery.data

  const sessionBanner = useMemo(() => {
    if (!sessionData) return null
    if (sessionData.status === 'warning' && sessionData.extension_allowed) {
      return {
        tone: 'warning' as const,
        message: 'Your session is nearing expiry. Save any work you need and be ready to sign in again.',
      }
    }
    if (sessionData.status === 'invalidated' || sessionData.status === 'expired') {
      return {
        tone: 'danger' as const,
        message: 'This session is no longer active. Re-authenticate to continue safely.',
      }
    }
    return null
  }, [sessionData])

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <AppShell>
      {/* Full-width scroll container — no arbitrary max-width cap */}
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">

          {/* Page header */}
          <div className="mb-8">
            <p className="mb-1 text-xs font-semibold uppercase tracking-widest text-gray-400">
              Settings
            </p>
            <h1 className="text-2xl font-bold text-gray-900">Account</h1>
            <p className="mt-1 text-sm text-gray-500">
              Manage your profile, session, and account security.
            </p>
          </div>

          {/* ── Top grid: Profile + Session ─────────────────────────────── */}
          <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-5">

            {/* Profile card — 2 of 5 columns on desktop */}
            <div className="lg:col-span-2">
              <Card>
                <div className="flex flex-col items-center pb-6 pt-2 text-center">
                  {/* Avatar */}
                  <div className="relative mb-4">
                    {profileQuery.data?.gravatar_url ? (
                      <img
                        src={profileQuery.data.gravatar_url}
                        alt="Profile avatar"
                        className="h-20 w-20 rounded-full ring-4 ring-white shadow-md"
                      />
                    ) : (
                      <div className="h-20 w-20 rounded-full bg-navy-50 ring-4 ring-white shadow-md" />
                    )}
                    <span className="absolute -bottom-1 -right-1 flex h-5 w-5 items-center justify-center rounded-full bg-green-500 ring-2 ring-white">
                      <span className="h-2 w-2 rounded-full bg-white" />
                    </span>
                  </div>

                  {/* Identity */}
                  {profileQuery.isLoading ? (
                    <div className="flex flex-col items-center gap-2">
                      <div className="h-4 w-40 animate-pulse rounded bg-gray-100" />
                      <div className="h-3 w-24 animate-pulse rounded bg-gray-100" />
                    </div>
                  ) : (
                    <>
                      <p className="max-w-full truncate text-base font-semibold text-gray-900 px-4">
                        {profileQuery.data?.email ?? 'Not available'}
                      </p>
                      <span className="mt-1.5 inline-flex items-center rounded-full bg-navy-50 px-2.5 py-0.5 text-xs font-medium text-navy-700">
                        {profileQuery.data?.role ?? role ?? ''}
                      </span>
                    </>
                  )}
                </div>

                {/* Divider */}
                <div className="border-t border-gray-100" />

                {/* Profile fields */}
                <div className="space-y-0 divide-y divide-gray-50">
                  <MetaRow label="Phone">
                    {profileQuery.isLoading
                      ? <SkeletonText />
                      : profileQuery.data?.phone_number ?? '—'}
                  </MetaRow>
                  <MetaRow label="Plan">
                    {profileQuery.isLoading
                      ? <SkeletonText />
                      : <span className="capitalize">{profileQuery.data?.subscription_tier ?? '—'}</span>}
                  </MetaRow>
                  <MetaRow label="Member since">
                    {profileQuery.isLoading
                      ? <SkeletonText />
                      : profileQuery.data?.member_since ?? '—'}
                  </MetaRow>
                  <MetaRow label="Status">
                    {profileQuery.isLoading
                      ? <SkeletonText />
                      : <AccountStateChip state={profileQuery.data?.account_state} />}
                  </MetaRow>
                </div>
              </Card>
            </div>

            {/* Session card — 3 of 5 columns on desktop */}
            <div className="lg:col-span-3">
              <Card className="h-full">
                <div className="mb-5 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Activity className="h-4 w-4 text-gray-400" />
                    <h2 className="text-sm font-semibold text-gray-900">Active session</h2>
                  </div>
                  {sessionData && (
                    <StatusChip status={sessionChipStatus(sessionData.status)} />
                  )}
                </div>

                {/* Loading */}
                {sessionQuery.isLoading && (
                  <div className="flex items-center gap-2.5 text-sm text-gray-400">
                    <Spinner size="sm" />
                    Loading session details…
                  </div>
                )}

                {/* Error */}
                {sessionQuery.isError && (
                  <div className="rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-sm text-red-700">
                    Could not load live session information right now.
                  </div>
                )}

                {/* Banner */}
                {sessionBanner && (
                  <div
                    className={cn(
                      'mb-5 rounded-xl border px-4 py-3 text-sm',
                      sessionBanner.tone === 'warning'
                        ? 'border-amber-200 bg-amber-50 text-amber-800'
                        : 'border-red-200 bg-red-50 text-red-700'
                    )}
                  >
                    {sessionBanner.message}
                  </div>
                )}

                {/* Session data grid */}
                {sessionData && (
                  <div className="space-y-0">
                    <div className="grid grid-cols-2 gap-4 sm:grid-cols-2">
                      <SessionCell
                        icon={Clock}
                        label="Issued"
                        value={formatSessionDate(sessionData.issued_at)}
                      />
                      <SessionCell
                        icon={Clock}
                        label="Expires"
                        value={formatSessionDate(sessionData.expires_at)}
                      />
                      <SessionCell
                        icon={Activity}
                        label="Last activity"
                        value={
                          sessionData.last_activity_at
                            ? formatSessionDate(sessionData.last_activity_at)
                            : 'No recent activity'
                        }
                      />
                      <SessionCell
                        icon={RefreshCw}
                        label="Inactivity limit"
                        value={formatSessionDate(sessionData.inactivity_expires_at)}
                      />
                    </div>

                    {(sessionData.status === 'invalidated' || sessionData.status === 'expired') && (
                      <div className="mt-5 border-t border-gray-100 pt-5">
                        <button
                          onClick={() => {
                            clearAuth({ reason: 'session_expired' })
                            navigate('/?reason=session_expired&redirect=/account', { replace: true })
                          }}
                          className="inline-flex items-center gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-2 text-sm font-medium text-red-700 transition-colors hover:bg-red-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400 focus-visible:ring-offset-2"
                        >
                          Sign in again
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </Card>
            </div>
          </div>

          {/* ── Security section ─────────────────────────────────────────── */}
          <div className="mb-6">
            <Card
              className={cn(
                'transition-shadow duration-200',
                highlightPhoneChange && 'ring-2 ring-navy-400 ring-offset-2'
              )}
            >
              <div className="mb-5 flex items-center gap-2">
                <Shield className="h-4 w-4 text-gray-400" />
                <h2 className="text-sm font-semibold text-gray-900">Security and recovery</h2>
              </div>

              {/* Action links */}
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <ActionCard
                  icon={Phone}
                  label="Change phone"
                  description="Update your verified phone number"
                  onClick={() => navigate('/account/phone-change')}
                />
                <ActionCard
                  icon={Shield}
                  label="Reset password"
                  description="Send a reset code to your email"
                  onClick={() => {
                    if (passwordResetState === 'idle') {
                      passwordResetMutation.mutate()
                    } else if (passwordResetState === 'sent') {
                      setPasswordResetState('idle')
                      setPasswordResetError(null)
                      setPasswordResetChallengeId(null)
                    }
                  }}
                  active={passwordResetState !== 'idle'}
                />
                <ActionCard
                  icon={Trash2}
                  label="Delete account"
                  description="Begin the account removal process"
                  onClick={() => navigate('/account/deletion')}
                  danger
                />
              </div>

              {/* Password reset inline panel — shown when not idle */}
              {passwordResetState !== 'idle' && (
                <div className="mt-6 rounded-2xl border border-navy-100 bg-navy-50/60 p-5">
                  <div className="mb-4">
                    <p className="text-sm font-semibold text-navy-900">Reset your password</p>
                    <p className="mt-1 text-sm text-navy-700/80 leading-relaxed">
                      Enter the code sent to your email along with your new password.
                    </p>
                  </div>

                  {/* Sending state */}
                  {passwordResetState === 'sending' && (
                    <div className="flex items-center gap-2.5 text-sm text-navy-700">
                      <Spinner size="sm" />
                      Sending reset code…
                    </div>
                  )}

                  {/* Sent / confirming states — OTP + new password form */}
                  {(passwordResetState === 'sent' || passwordResetState === 'confirming') && (
                    <div className="space-y-5">
                      {/* Inline error from a failed confirm attempt */}
                      {passwordResetError && (
                        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                          {passwordResetError}
                        </div>
                      )}

                      {/* OTP entry */}
                      <div>
                        <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                          Reset code (from email)
                        </label>
                        <OtpInput value={passwordResetOtp} onChange={setPasswordResetOtp} />
                      </div>

                      {/* New password fields */}
                      <div className="space-y-4">
                        <div>
                          <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                            New password
                          </label>
                          <div className="relative">
                            <input
                              type={showNewPassword ? 'text' : 'password'}
                              value={newPassword}
                              onChange={(e) => setNewPassword(e.target.value)}
                              autoComplete="new-password"
                              placeholder="Min. 12 characters"
                              className="h-11 w-full rounded-xl border border-gray-200 bg-white px-3.5 pr-10 text-sm text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-transparent focus:ring-2 focus:ring-navy-500"
                            />
                            <button
                              type="button"
                              onClick={() => setShowNewPassword((v) => !v)}
                              aria-label={showNewPassword ? 'Hide password' : 'Show password'}
                              className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 focus-visible:outline-none"
                            >
                              {showNewPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                            </button>
                          </div>
                          <PasswordStrengthBar password={newPassword} />
                        </div>

                        <div>
                          <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                            Confirm password
                          </label>
                          <div className="relative">
                            <input
                              type={showConfirmPassword ? 'text' : 'password'}
                              value={confirmPassword}
                              onChange={(e) => setConfirmPassword(e.target.value)}
                              autoComplete="new-password"
                              placeholder="Repeat new password"
                              className={cn(
                                'h-11 w-full rounded-xl border bg-white px-3.5 pr-10 text-sm text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-transparent focus:ring-2 focus:ring-navy-500',
                                confirmPassword.length > 0 && confirmPassword !== newPassword
                                  ? 'border-red-300 focus:ring-red-400'
                                  : 'border-gray-200'
                              )}
                            />
                            <button
                              type="button"
                              onClick={() => setShowConfirmPassword((v) => !v)}
                              aria-label={showConfirmPassword ? 'Hide password' : 'Show password'}
                              className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 focus-visible:outline-none"
                            >
                              {showConfirmPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                            </button>
                          </div>
                          {confirmPassword.length > 0 && confirmPassword !== newPassword && (
                            <p className="mt-1 text-xs text-red-500">Passwords do not match.</p>
                          )}
                        </div>
                      </div>

                      {/* Submit */}
                      <div className="flex items-center gap-4">
                        <button
                          onClick={() => confirmPasswordResetMutation.mutate()}
                          disabled={
                            confirmPasswordResetMutation.isPending ||
                            passwordResetState === 'confirming' ||
                            passwordResetOtp.length < 4 ||
                            newPassword.length < 12 ||
                            newPassword !== confirmPassword
                          }
                          className="inline-flex items-center gap-2 rounded-xl bg-navy-900 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-navy-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-navy-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                          {passwordResetState === 'confirming' && <Spinner size="sm" />}
                          Set new password
                        </button>

                        <span className="text-sm text-gray-400">
                          {passwordResetCooldown > 0 ? (
                            `Resend code in ${passwordResetCooldown}s`
                          ) : (
                            <button
                              onClick={() => passwordResetMutation.mutate()}
                              className="font-medium text-navy-700 underline underline-offset-2 hover:text-navy-900 focus-visible:outline-none"
                            >
                              Resend code
                            </button>
                          )}
                        </span>
                      </div>
                    </div>
                  )}

                  {/* Done state */}
                  {passwordResetState === 'done' && (
                    <div className="space-y-3">
                      <div className="rounded-xl border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800">
                        Password updated successfully. Your other sessions have been signed out.
                      </div>
                      <button
                        onClick={() => {
                          clearAuth({ reason: 'password_reset_complete' })
                          navigate('/?reason=password_reset_complete', { replace: true })
                        }}
                        className="inline-flex items-center gap-2 rounded-xl border border-navy-200 bg-navy-50 px-4 py-2 text-sm font-medium text-navy-700 transition-colors hover:bg-navy-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-navy-500 focus-visible:ring-offset-2"
                      >
                        Sign in with new password
                      </button>
                    </div>
                  )}

                  {/* Error state (initiation failure — no challenge yet) */}
                  {passwordResetState === 'error' && (
                    <div className="space-y-3">
                      <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                        {passwordResetError ?? 'Something went wrong. Please try again.'}
                      </div>
                      <button
                        onClick={() => {
                          setPasswordResetState('idle')
                          setPasswordResetError(null)
                        }}
                        className="text-sm font-medium text-navy-700 underline underline-offset-2 hover:text-navy-900 focus-visible:outline-none"
                      >
                        Try again
                      </button>
                    </div>
                  )}
                </div>
              )}

              {/* Phone change inline form — shown when route matches */}
              {highlightPhoneChange && (
                <div className="mt-6 rounded-2xl border border-navy-100 bg-navy-50/60 p-5">
                  <div className="mb-5">
                    <p className="text-sm font-semibold text-navy-900">Change phone number</p>
                    <p className="mt-1 text-sm text-navy-700/80 leading-relaxed">
                      Re-authenticate with your current password, then confirm the OTP sent to the new
                      number. Completing this signs you out of the current session.
                    </p>
                  </div>

                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                        New phone number
                      </label>
                      <input
                        value={newPhoneNumber}
                        onChange={(event) => setNewPhoneNumber(event.target.value)}
                        onBlur={(event) => setNewPhoneNumber(normalizeKenyanPhone(event.target.value))}
                        placeholder="+254 712 345 678"
                        className="h-11 w-full rounded-xl border border-gray-200 bg-white px-3.5 text-sm text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-transparent focus:ring-2 focus:ring-navy-500"
                      />
                    </div>

                    <div>
                      <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                        Current password
                      </label>
                      <input
                        type="password"
                        value={currentPassword}
                        onChange={(event) => setCurrentPassword(event.target.value)}
                        autoComplete="current-password"
                        className="h-11 w-full rounded-xl border border-gray-200 bg-white px-3.5 text-sm text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-transparent focus:ring-2 focus:ring-navy-500"
                      />
                    </div>
                  </div>

                  <div className="mt-4">
                    <button
                      onClick={() => requestPhoneChangeMutation.mutate()}
                      disabled={
                        requestPhoneChangeMutation.isPending ||
                        newPhoneNumber.trim().length === 0 ||
                        currentPassword.trim().length === 0
                      }
                      className="inline-flex items-center gap-2 rounded-xl bg-navy-900 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-navy-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-navy-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {requestPhoneChangeMutation.isPending && <Spinner size="sm" />}
                      Send OTP
                    </button>
                  </div>

                  {/* OTP confirmation step */}
                  {phoneChangeRequestId && phoneChangeChallengeId && (
                    <div className="mt-5 rounded-2xl border border-navy-200 bg-white p-5 shadow-sm">
                      <p className="text-sm font-semibold text-gray-900">Confirm new number</p>
                      <p className="mt-1 mb-5 text-sm text-gray-500">
                        Enter the OTP sent to the new phone number to complete the change.
                      </p>

                      <OtpInput value={phoneChangeOtp} onChange={setPhoneChangeOtp} />

                      <div className="mt-5">
                        <button
                          onClick={() => confirmPhoneChangeMutation.mutate()}
                          disabled={confirmPhoneChangeMutation.isPending || phoneChangeOtp.length < 4}
                          className="inline-flex items-center gap-2 rounded-xl border border-navy-200 bg-navy-50 px-5 py-2.5 text-sm font-semibold text-navy-700 transition-colors hover:bg-navy-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-navy-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                          {confirmPhoneChangeMutation.isPending && <Spinner size="sm" />}
                          Confirm phone change
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </Card>
          </div>

          {/* ── Danger zone ──────────────────────────────────────────────── */}
          <div className="mb-8">
            <Card
              className={cn(
                'border-l-4 border-l-red-400 transition-shadow duration-200',
                highlightDeletion && 'ring-2 ring-red-200 ring-offset-2'
              )}
            >
              <div className="mb-1 flex items-center gap-2">
                <Trash2 className="h-4 w-4 text-red-500" />
                <h2 className="text-sm font-semibold text-red-600">Danger zone</h2>
              </div>

              <p className="mb-5 text-sm text-gray-500 leading-relaxed">
                Requesting account deletion begins a governed multi-step process. Compliance or retention
                blockers will be clearly reported — the request is always recorded.
              </p>

              <div className="mb-4">
                <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                  Reason for deletion <span className="text-red-500">*</span>
                </label>
                <textarea
                  value={deletionReason}
                  onChange={(event) => setDeletionReason(event.target.value)}
                  rows={3}
                  placeholder="Please describe why you want to delete your account…"
                  className="w-full resize-none rounded-xl border border-gray-200 bg-white px-3.5 py-3 text-sm text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-red-300 focus:ring-2 focus:ring-red-200"
                />
              </div>

              <button
                onClick={() => setDeletionConfirmOpen(true)}
                disabled={deletionReason.trim().length === 0 || deletionMutation.isPending}
                className="inline-flex items-center gap-2 rounded-xl border border-red-200 bg-red-50 px-5 py-2.5 text-sm font-semibold text-red-600 transition-colors hover:bg-red-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {deletionMutation.isPending && <Spinner size="sm" className="h-3.5 w-3.5" />}
                Request account deletion
              </button>

              {/* Deletion result feedback */}
              {deletionResult && (
                <div className="mt-5 space-y-2">
                  {deletionResult.blockers.length > 0 ? (
                    deletionResult.blockers.map((blocker) => (
                      <div
                        key={blocker}
                        className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"
                      >
                        <span className="font-semibold shrink-0">Blocked:</span>
                        {blocker.replace(/_/g, ' ')}
                      </div>
                    ))
                  ) : (
                    <div className="rounded-xl border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800">
                      Deletion request submitted. Check your email for next steps.
                    </div>
                  )}
                </div>
              )}
            </Card>
          </div>
        </div>
      </div>

      {/* Confirm modal — props completely unchanged */}
      <ConfirmModal
        open={deletionConfirmOpen}
        onOpenChange={setDeletionConfirmOpen}
        title="Request account deletion"
        description="This begins the governed account deletion process. You may still be blocked by policy or retention checks."
        confirmLabel="Request deletion"
        variant="danger"
        loading={deletionMutation.isPending}
        onConfirm={() => deletionMutation.mutate()}
      />
    </AppShell>
  )
}

// ─── Shared presentational primitives ────────────────────────────────────────

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('rounded-2xl border border-gray-100 bg-white p-5 shadow-sm', className)}>
      {children}
    </div>
  )
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-3 text-sm">
      <span className="text-gray-500">{label}</span>
      <span className="text-right font-medium text-gray-900">{children}</span>
    </div>
  )
}

function SessionCell({
  icon: Icon,
  label,
  value,
}: {
  icon: ElementType
  label: string
  value: string
}) {
  return (
    <div className="rounded-xl bg-gray-50 px-3.5 py-3">
      <div className="mb-1 flex items-center gap-1.5">
        <Icon className="h-3 w-3 text-gray-400" />
        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">{label}</span>
      </div>
      <p className="text-xs font-medium text-gray-800 leading-snug">{value}</p>
    </div>
  )
}

function ActionCard({
  icon: Icon,
  label,
  description,
  onClick,
  danger = false,
  active = false,
}: {
  icon: ElementType
  label: string
  description: string
  onClick: () => void
  danger?: boolean
  active?: boolean
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'group w-full rounded-xl border p-4 text-left transition-all',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2',
        danger
          ? 'border-red-100 hover:border-red-200 hover:bg-red-50 focus-visible:ring-red-400'
          : active
            ? 'border-navy-200 bg-navy-50/60 focus-visible:ring-navy-400'
            : 'border-gray-100 hover:border-navy-200 hover:bg-navy-50/60 focus-visible:ring-navy-400'
      )}
    >
      <div className="mb-2 flex items-center justify-between">
        <div
          className={cn(
            'flex h-7 w-7 items-center justify-center rounded-lg',
            danger ? 'bg-red-100' : 'bg-navy-50'
          )}
        >
          <Icon className={cn('h-3.5 w-3.5', danger ? 'text-red-500' : 'text-navy-600')} />
        </div>
        <ChevronRight
          className={cn(
            'h-3.5 w-3.5 opacity-0 transition-opacity group-hover:opacity-100',
            danger ? 'text-red-400' : 'text-navy-400'
          )}
        />
      </div>
      <p className={cn('text-sm font-semibold', danger ? 'text-red-600' : 'text-gray-900')}>
        {label}
      </p>
      <p className="mt-0.5 text-xs text-gray-500 leading-snug">{description}</p>
    </button>
  )
}

function AccountStateChip({ state }: { state: string | undefined }) {
  if (!state) return <span className="text-gray-400">—</span>

  const styles: Record<string, string> = {
    active:               'bg-green-50 text-green-700',
    pending_verification: 'bg-amber-50 text-amber-700',
    disabled:             'bg-gray-100 text-gray-500',
    locked:               'bg-red-50 text-red-600',
  }

  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold capitalize',
        styles[state] ?? 'bg-gray-100 text-gray-600'
      )}
    >
      {state.replace(/_/g, ' ')}
    </span>
  )
}

function SkeletonText() {
  return <span className="inline-block h-3.5 w-28 animate-pulse rounded bg-gray-100" />
}
