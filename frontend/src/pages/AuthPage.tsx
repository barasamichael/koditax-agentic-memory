import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { AnimatePresence, motion } from 'framer-motion'
import { Eye, EyeOff } from 'lucide-react'
import { AuthBanner } from '@/components/auth/AuthBanner'
import { PasswordStrengthBar } from '@/components/auth/PasswordStrengthBar'
import { OtpInput } from '@/components/auth/OtpInput'
import { Spinner } from '@/components/shared/Spinner'
import {
  useRegister,
  useLogin,
  useLoginStepUp,
  useEmailOtpLogin,
  useVerifyOtp,
  useIssueOtpChallenge,
  usePasswordReset,
  isLoginSuccess,
  isLoginStepUp,
} from '@/hooks/useAuth'
import { useMutation } from '@tanstack/react-query'
import { updateRegistrationPhone } from '@/api/auth.api'
import { type AuthStatusReason, useAuthStore } from '@/stores/authStore'
import { normalizeError } from '@/lib/errorNormalizer'
import { normalizeKenyanPhone } from '@/lib/utils'
import { cn } from '@/lib/utils'

type Panel =
  | 'login'
  | 'register'
  | 'otp_registration_verify'
  | 'otp_login_step_up'
  | 'otp_recovery'
  | 'password_reset_initiate'

type BannerTone = 'error' | 'warning' | 'success' | 'info'

const AUTH_ERROR_MESSAGES: Record<string, string> = {
  registration_invalid_email: 'Please enter a valid email address.',
  registration_invalid_phone: 'Please enter a valid Kenyan phone number.',
  registration_invalid_kra_pin:
    'KRA PIN format invalid. Expected: one uppercase letter, 9 digits, one uppercase letter.',
  registration_weak_password:
    'Password does not meet requirements: 12+ chars, uppercase, lowercase, digit, symbol.',
  registration_invalid_role: 'Invalid account role.',
  registration_duplicate_email: 'An account with this email already exists.',
  registration_duplicate_phone: 'An account with this phone already exists.',
  registration_duplicate_email_or_phone: 'An account with these details already exists.',
  login_identifier_unsupported_type: 'Please enter a Kenyan phone number.',
  login_identifier_invalid_format: 'Phone number format invalid. Use +254 followed by 9 digits.',
  login_invalid_credentials: 'Incorrect phone number or password.',
  password_hash_verification_failed: 'Incorrect phone number or password.',
  login_lockout_active: 'Account temporarily locked. Try again later.',
  login_lockout_threshold_exceeded: 'Too many failed attempts. Account locked.',
  login_account_locked: 'Your account is locked. Please contact support.',
  login_account_not_active: 'Account not active. Please verify your phone number first.',
  login_step_up_challenge_expired: 'Verification code expired. Please sign in again.',
  login_step_up_otp_invalid: 'Incorrect verification code.',
  login_step_up_otp_attempt_limit_exceeded:
    'Too many incorrect codes. Please restart the sign-in flow.',
  otp_cooldown_active: 'Please wait before requesting another code.',
  otp_resend_limit_reached: 'Maximum resend attempts reached.',
  otp_expired: 'Code expired. Please request a new one.',
  otp_invalid: 'Incorrect code. Please try again.',
  otp_attempt_limit_exceeded: 'Too many incorrect attempts. Request a new code.',
  otp_challenge_context_mismatch: 'Verification session mismatch. Please start again.',
  password_reset_token_expired: 'Reset code expired. Please request a new one.',
  password_reset_token_already_used: 'This reset code has already been used.',
  password_reset_attempt_limit_exceeded: 'Too many reset attempts.',
  password_policy_violation: 'New password does not meet security requirements.',
  password_reuse_not_allowed: 'You cannot reuse a recent password.',
  refresh_token_reused: 'Session security error. Please sign in again.',
  refresh_token_expired: 'Session expired. Please sign in again.',
  phone_change_target_phone_invalid: 'Enter a valid Kenyan phone number.',
  phone_change_target_phone_already_registered:
    'That phone number is already registered on another account.',
  phone_change_step_up_invalid: 'The phone-change verification challenge is no longer valid.',
  phone_change_step_up_expired: 'The phone-change verification code has expired.',
}

const REASON_BANNERS: Record<
  Exclude<AuthStatusReason, null>,
  { tone: BannerTone; message: string }
> = {
  signed_out: {
    tone: 'success',
    message: 'You have been signed out successfully.',
  },
  session_required: {
    tone: 'warning',
    message: 'Sign in to continue to the page you requested.',
  },
  session_expired: {
    tone: 'warning',
    message: 'Your session is no longer active. Sign in again to continue safely.',
  },
  registration_verified: {
    tone: 'success',
    message: 'Your phone number has been verified. You can sign in now.',
  },
  password_reset_complete: {
    tone: 'success',
    message: 'Your password has been updated. Sign in with your new password.',
  },
  phone_change_confirmed: {
    tone: 'success',
    message: 'Your phone number was updated. Sign in again with the new number.',
  },
}

const loginSchema = z.object({
  login_id: z.string().min(1, 'Phone number is required'),
  password: z.string().min(1, 'Password is required'),
})

const emailOtpLoginSchema = z.object({
  email: z.string().email('Enter a valid email address'),
})

const registerSchema = z.object({
  email: z.string().email('Enter a valid email address'),
  phone_number: z.string().min(9, 'Enter a valid phone number'),
  kra_pin: z
    .string()
    .regex(/^[A-Z]\d{9}[A-Z]$/, 'Format: one letter, 9 digits, one letter (e.g. A123456789B)'),
  password: z
    .string()
    .min(12, 'At least 12 characters')
    .regex(/[A-Z]/, 'Must include uppercase')
    .regex(/[a-z]/, 'Must include lowercase')
    .regex(/\d/, 'Must include a number')
    .regex(/[!@#$%^&*]/, 'Must include a special character'),
})

const resetInitSchema = z.object({
  email: z.string().email('Enter a valid email address'),
})

const resetConfirmSchema = z.object({
  new_password: z
    .string()
    .min(12, 'At least 12 characters')
    .regex(/[A-Z]/, 'Must include uppercase')
    .regex(/[a-z]/, 'Must include lowercase')
    .regex(/\d/, 'Must include a number')
    .regex(/[!@#$%^&*]/, 'Must include a special character'),
})

const panelVariants = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -8 },
}

function resolveAuthError(err: unknown): string {
  const canonical = normalizeError(err)
  return AUTH_ERROR_MESSAGES[canonical.error_code] ?? canonical.message
}

function safePanelFromSearch(value: string | null): Panel {
  if (value === 'password_reset_initiate') return 'password_reset_initiate'
  if (value === 'register') return 'register'
  return 'login'
}

function resolveSafeRedirect(rawRedirect: string | null, role: string | null): string {
  if (!rawRedirect || !rawRedirect.startsWith('/') || rawRedirect.startsWith('//')) {
    return '/chat'
  }

  if (rawRedirect === '/') return '/chat'
  if (rawRedirect.startsWith('/internal/knowledge') && role !== 'Administrator') {
    return '/chat'
  }

  return rawRedirect
}

function humanizeRedirectTarget(path: string): string | null {
  if (path.startsWith('/documents')) return 'Documents'
  if (path.startsWith('/account')) return 'Account'
  if (path.startsWith('/internal/knowledge')) return 'Knowledge Admin'
  if (path.startsWith('/chat')) return 'Chat'
  return null
}

function Banner({
  message,
  tone,
  onDismiss,
}: {
  message: string
  tone: BannerTone
  onDismiss: () => void
}) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 5000)
    return () => clearTimeout(t)
  }, [message, onDismiss])

  const toneClasses: Record<BannerTone, string> = {
    error: 'border-red-200 bg-red-50 text-red-800',
    warning: 'border-amber-200 bg-amber-50 text-amber-800',
    success: 'border-emerald-200 bg-emerald-50 text-emerald-800',
    info: 'border-blue-200 bg-blue-50 text-blue-800',
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      className={cn('mb-4 rounded-input border px-4 py-3 text-sm', toneClasses[tone])}
    >
      {message}
    </motion.div>
  )
}

function FieldError({ message }: { message?: string }) {
  if (!message) return null
  return <p className="mt-1 text-small text-red-600">{message}</p>
}

export default function AuthPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const searchParams = useMemo(() => new URLSearchParams(location.search), [location.search])

  const { setAuth, isAuthenticated, role, authStatusReason, clearAuthStatusReason } = useAuthStore()

  const [panel, setPanel] = useState<Panel>(safePanelFromSearch(searchParams.get('panel')))
  const [error, setError] = useState<string | null>(null)
  const [infoBanner, setInfoBanner] = useState<{ tone: BannerTone; message: string } | null>(null)
  const [otpValue, setOtpValue] = useState('')
  const [challengeId, setChallengeId] = useState('')
  const [resendAt, setResendAt] = useState<Date | null>(null)
  const [countdown, setCountdown] = useState(0)
  const [showPassword, setShowPassword] = useState(false)
  const [showNewPassword, setShowNewPassword] = useState(false)
  const [registrationPhone, setRegistrationPhone] = useState<string | null>(null)
  const [registrationUserId, setRegistrationUserId] = useState<string | null>(null)
  const [phoneChangeOpen, setPhoneChangeOpen] = useState(false)
  const [newPhoneInput, setNewPhoneInput] = useState('')
  const [phoneChangeCooldown, setPhoneChangeCooldown] = useState(0)
  const [phoneChangeAttemptsRemaining, setPhoneChangeAttemptsRemaining] = useState(3)
  const [recoveryEmail, setRecoveryEmail] = useState<string | null>(null)
  const [loginCredentials, setLoginCredentials] = useState<{
    login_id: string
    password: string
  } | null>(null)
  const [loginMethod, setLoginMethod] = useState<'phone' | 'email'>('phone')
  const [emailOtpEmail, setEmailOtpEmail] = useState<string | null>(null)

  const registerMutation = useRegister()
  const loginMutation = useLogin()
  const loginStepUpMutation = useLoginStepUp()
  const emailOtpLoginMutation = useEmailOtpLogin()
  const verifyOtpMutation = useVerifyOtp()
  const issueOtpChallengeMutation = useIssueOtpChallenge()
  const { initiate: resetInitiateMutation, confirm: resetConfirmMutation } = usePasswordReset()

  const loginForm = useForm<z.infer<typeof loginSchema>>({
    resolver: zodResolver(loginSchema),
  })

  const emailOtpLoginForm = useForm<z.infer<typeof emailOtpLoginSchema>>({
    resolver: zodResolver(emailOtpLoginSchema),
  })

  const registerForm = useForm<z.infer<typeof registerSchema>>({
    resolver: zodResolver(registerSchema),
  })

  const resetInitForm = useForm<z.infer<typeof resetInitSchema>>({
    resolver: zodResolver(resetInitSchema),
  })

  const resetConfirmForm = useForm<z.infer<typeof resetConfirmSchema>>({
    resolver: zodResolver(resetConfirmSchema),
  })

  const redirectTarget = useMemo(
    () => resolveSafeRedirect(searchParams.get('redirect'), role),
    [role, searchParams]
  )

  const redirectTargetLabel = useMemo(
    () => humanizeRedirectTarget(redirectTarget),
    [redirectTarget]
  )

  useEffect(() => {
    setPanel(safePanelFromSearch(searchParams.get('panel')))
  }, [searchParams])

  useEffect(() => {
    const reason = searchParams.get('reason') as AuthStatusReason
    if (reason && REASON_BANNERS[reason]) {
      const baseBanner = REASON_BANNERS[reason]
      const message =
        reason === 'session_required' && redirectTargetLabel
          ? `${baseBanner.message} Continue to ${redirectTargetLabel} after sign-in.`
          : baseBanner.message
      setInfoBanner({ tone: baseBanner.tone, message })
      return
    }

    if (authStatusReason && REASON_BANNERS[authStatusReason]) {
      setInfoBanner(REASON_BANNERS[authStatusReason])
      clearAuthStatusReason()
    }
  }, [authStatusReason, clearAuthStatusReason, redirectTargetLabel, searchParams])

  useEffect(() => {
    if (isAuthenticated) {
      navigate(redirectTarget, { replace: true })
    }
  }, [isAuthenticated, navigate, redirectTarget])

  useEffect(() => {
    if (!resendAt) {
      setCountdown(0)
      return
    }

    const tick = () => {
      const remaining = Math.max(0, Math.floor((resendAt.getTime() - Date.now()) / 1000))
      setCountdown(remaining)
    }

    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [resendAt])

  const switchPanel = (next: Panel) => {
    setPanel(next)
    setError(null)
    setOtpValue('')
    setInfoBanner(null)
  }

  const completeAuthentication = (params: {
    session: Parameters<typeof setAuth>[0]['session']
    accessToken: string
    refreshToken: string
    expiresAt: string
  }) => {
    setAuth(params)
    navigate(resolveSafeRedirect(searchParams.get('redirect'), params.session.role), {
      replace: true,
    })
  }

  const handleLoginSubmit = loginForm.handleSubmit(async (data) => {
    setError(null)
    try {
      const normalizedPhone = normalizeKenyanPhone(data.login_id)
      const res = await loginMutation.mutateAsync({
        login_id: normalizedPhone,
        password: data.password,
      })

      if (isLoginSuccess(res)) {
        completeAuthentication({
          session: res.session,
          accessToken: res.access_token,
          refreshToken: res.refresh_token,
          expiresAt: res.expires_at,
        })
      } else if (isLoginStepUp(res)) {
        setLoginCredentials({ login_id: normalizedPhone, password: data.password })
        setChallengeId(res.step_up_challenge_id)
        setResendAt(new Date(res.step_up_expires_at))
        switchPanel('otp_login_step_up')
        setInfoBanner({
          tone: 'info',
          message: 'Enter the verification code to finish signing in.',
        })
      }
    } catch (err) {
      setError(resolveAuthError(err))
    }
  })

  const handleEmailOtpLoginSubmit = emailOtpLoginForm.handleSubmit(async (data) => {
    setError(null)
    try {
      const res = await emailOtpLoginMutation.mutateAsync({ email: data.email })
      if (isLoginSuccess(res)) {
        completeAuthentication({
          session: res.session,
          accessToken: res.access_token,
          refreshToken: res.refresh_token,
          expiresAt: res.expires_at,
        })
      } else if (isLoginStepUp(res)) {
        setEmailOtpEmail(data.email)
        setChallengeId(res.step_up_challenge_id)
        setResendAt(new Date(res.step_up_expires_at))
        switchPanel('otp_login_step_up')
        setInfoBanner({
          tone: 'info',
          message: `Enter the verification code sent to ${data.email}.`,
        })
      }
    } catch (err) {
      setError(resolveAuthError(err))
    }
  })

  const handleEmailOtpStepUpSubmit = async () => {
    if (otpValue.length < 4 || !emailOtpEmail) return
    setError(null)
    try {
      const res = await emailOtpLoginMutation.mutateAsync({
        email: emailOtpEmail,
        step_up_challenge_id: challengeId,
        step_up_otp_code: otpValue,
      })
      if (isLoginSuccess(res)) {
        completeAuthentication({
          session: res.session,
          accessToken: res.access_token,
          refreshToken: res.refresh_token,
          expiresAt: res.expires_at,
        })
      }
    } catch (err) {
      setError(resolveAuthError(err))
    }
  }

  const updatePhoneMutation = useMutation({
    mutationFn: (newPhone: string) => {
      if (!registrationUserId) throw new Error('No registration in progress.')
      return updateRegistrationPhone({
        user_id: registrationUserId,
        new_phone_number: newPhone,
      })
    },
    onSuccess: (data) => {
      setRegistrationPhone(data.updated_phone_number)
      setChallengeId(data.challenge_id)
      setResendAt(new Date(data.expires_at))
      setPhoneChangeAttemptsRemaining(data.attempts_remaining)
      setPhoneChangeOpen(false)
      setNewPhoneInput('')
      setOtpValue('')
      setError(null)
      startPhoneChangeCooldown()
    },
    onError: (err) => {
      const norm = normalizeError(err)
      const messages: Record<string, string> = {
        registration_phone_update_cooldown: norm.message,
        registration_phone_update_limit_exceeded: norm.message,
        registration_duplicate_phone: 'This phone number is already registered to another account.',
        registration_invalid_phone: 'Invalid phone number format.',
      }
      setError(messages[norm.error_code] ?? norm.message)
    },
  })

  const startPhoneChangeCooldown = () => {
    setPhoneChangeCooldown(60)
    const interval = setInterval(() => {
      setPhoneChangeCooldown((prev) => {
        if (prev <= 1) { clearInterval(interval); return 0 }
        return prev - 1
      })
    }, 1000)
  }

  const handleRegisterSubmit = registerForm.handleSubmit(async (data) => {
    setError(null)
    try {
      const phone = normalizeKenyanPhone(data.phone_number)
      const reg = await registerMutation.mutateAsync({
        email: data.email,
        phone_number: phone,
        password: data.password,
        kra_pin: data.kra_pin,
      })

      const challenge = await issueOtpChallengeMutation.mutateAsync({
        purpose: 'registration_verify',
        channel: 'sms',
        phone_number: phone,
      })

      setRegistrationPhone(phone)
      setRegistrationUserId(reg.user_id)
      setChallengeId(challenge.challenge_id)
      setResendAt(new Date(challenge.expires_at))
      switchPanel('otp_registration_verify')
      setInfoBanner({
        tone: 'success',
        message: 'Your account was created. Verify the phone number to activate sign-in.',
      })
    } catch (err) {
      setError(resolveAuthError(err))
    }
  })

  const handleRegistrationOtpSubmit = async () => {
    if (otpValue.length < 4) return
    setError(null)
    try {
      await verifyOtpMutation.mutateAsync({
        challenge_id: challengeId,
        otp_code: otpValue,
      })
      navigate('/?reason=registration_verified', { replace: true })
    } catch (err) {
      setError(resolveAuthError(err))
    }
  }

  const handleLoginStepUpSubmit = async () => {
    if (otpValue.length < 4 || !loginCredentials) return
    setError(null)
    try {
      const res = await loginStepUpMutation.mutateAsync({
        login_id: loginCredentials.login_id,
        password: loginCredentials.password,
        step_up_challenge_id: challengeId,
        step_up_otp_code: otpValue,
      })

      completeAuthentication({
        session: res.session,
        accessToken: res.access_token,
        refreshToken: res.refresh_token,
        expiresAt: res.expires_at,
      })
    } catch (err) {
      setError(resolveAuthError(err))
    }
  }

  const handleResetInitSubmit = resetInitForm.handleSubmit(async (data) => {
    setError(null)
    try {
      const res = await resetInitiateMutation.mutateAsync({
        purpose: 'password_reset',
        email: data.email,
      })
      setRecoveryEmail(data.email)
      setChallengeId(res.challenge_id)
      setResendAt(new Date(res.expires_at))
      switchPanel('otp_recovery')
      setInfoBanner({
        tone: 'info',
        message: 'A password reset code has been sent to your email. Enter it below to continue.',
      })
    } catch (err) {
      setError(resolveAuthError(err))
    }
  })

  const handleResetConfirmSubmit = resetConfirmForm.handleSubmit(async (data) => {
    if (otpValue.length < 4) return
    setError(null)
    try {
      await resetConfirmMutation.mutateAsync({
        challenge_id: challengeId,
        reset_code: otpValue,
        new_password: data.new_password,
      })
      navigate('/?reason=password_reset_complete', { replace: true })
    } catch (err) {
      setError(resolveAuthError(err))
    }
  })

  const handleResend = async () => {
    setError(null)
    try {
      if (panel === 'otp_registration_verify' && registrationPhone) {
        const challenge = await issueOtpChallengeMutation.mutateAsync({
          purpose: 'registration_verify',
          channel: 'sms',
          phone_number: registrationPhone,
        })
        setChallengeId(challenge.challenge_id)
        setResendAt(new Date(challenge.expires_at))
        return
      }

      if (panel === 'otp_login_step_up' && emailOtpEmail) {
        const res = await emailOtpLoginMutation.mutateAsync({ email: emailOtpEmail })
        if (isLoginStepUp(res)) {
          setChallengeId(res.step_up_challenge_id)
          setResendAt(new Date(res.step_up_expires_at))
        }
        return
      }

      if (panel === 'otp_login_step_up' && loginCredentials) {
        const challenge = await issueOtpChallengeMutation.mutateAsync({
          purpose: 'login_step_up',
          channel: 'sms',
          phone_number: loginCredentials.login_id,
        })
        setChallengeId(challenge.challenge_id)
        setResendAt(new Date(challenge.expires_at))
        return
      }

      if (panel === 'otp_recovery' && recoveryEmail) {
        const res = await resetInitiateMutation.mutateAsync({
          purpose: 'password_reset',
          email: recoveryEmail,
        })
        setChallengeId(res.challenge_id)
        setResendAt(new Date(res.expires_at))
      }
    } catch (err) {
      setError(resolveAuthError(err))
    }
  }

  const isPending =
    loginMutation.isPending ||
    emailOtpLoginMutation.isPending ||
    registerMutation.isPending ||
    verifyOtpMutation.isPending ||
    loginStepUpMutation.isPending ||
    issueOtpChallengeMutation.isPending ||
    resetInitiateMutation.isPending ||
    resetConfirmMutation.isPending

  const inputClass = (hasError: boolean) =>
    cn(
      'h-10 w-full rounded-input border px-3 text-sm transition-all focus:outline-none focus-visible:border-transparent focus-visible:ring-2 focus-visible:ring-navy-500',
      hasError ? 'border-red-400 bg-red-50' : 'border-gray-200 bg-white'
    )

  const canResend = countdown === 0

  return (
    <div className="flex min-h-screen overflow-hidden bg-white">
      <AuthBanner />

      <div className="flex flex-1 flex-col items-center justify-center overflow-auto bg-white p-6 sm:p-8">
        <div className="w-full max-w-sm">
          <AnimatePresence mode="wait">
            {error ? (
              <Banner key={`error-${error}`} message={error} tone="error" onDismiss={() => setError(null)} />
            ) : null}
          </AnimatePresence>

          <AnimatePresence mode="wait">
            {infoBanner ? (
              <Banner
                key={`info-${infoBanner.message}`}
                message={infoBanner.message}
                tone={infoBanner.tone}
                onDismiss={() => setInfoBanner(null)}
              />
            ) : null}
          </AnimatePresence>

          <AnimatePresence mode="wait">
            {panel === 'login' ? (
              <motion.div key="login" {...panelVariants} transition={{ duration: 0.15 }}>
                <h1 className="text-display mb-1">Welcome back</h1>
                <p className="mb-6 text-small text-gray-500">
                  Sign in to your Kodi account
                  {redirectTargetLabel ? ` and continue to ${redirectTargetLabel}.` : '.'}
                </p>

                <div className="mb-5 flex rounded-input border border-gray-200 p-0.5">
                  <button
                    type="button"
                    onClick={() => { setLoginMethod('phone'); setError(null) }}
                    className={cn(
                      'flex-1 rounded-[calc(var(--radius-input)-2px)] py-1.5 text-sm font-medium transition-all',
                      loginMethod === 'phone'
                        ? 'bg-navy-900 text-white shadow-sm'
                        : 'text-gray-500 hover:text-gray-700'
                    )}
                  >
                    Phone
                  </button>
                  <button
                    type="button"
                    onClick={() => { setLoginMethod('email'); setError(null) }}
                    className={cn(
                      'flex-1 rounded-[calc(var(--radius-input)-2px)] py-1.5 text-sm font-medium transition-all',
                      loginMethod === 'email'
                        ? 'bg-navy-900 text-white shadow-sm'
                        : 'text-gray-500 hover:text-gray-700'
                    )}
                  >
                    Email
                  </button>
                </div>

                {loginMethod === 'phone' ? (
                  <form onSubmit={handleLoginSubmit} noValidate className="space-y-4">
                    <div>
                      <label className="mb-1 block text-sm font-medium text-gray-700">Phone number</label>
                      <input
                        type="tel"
                        placeholder="+254 712 345 678"
                        autoComplete="tel"
                        className={inputClass(Boolean(loginForm.formState.errors.login_id))}
                        {...loginForm.register('login_id')}
                        onBlur={(event) => {
                          loginForm.setValue('login_id', normalizeKenyanPhone(event.target.value))
                          void loginForm.trigger('login_id')
                        }}
                      />
                      <FieldError message={loginForm.formState.errors.login_id?.message} />
                    </div>

                    <div>
                      <label className="mb-1 block text-sm font-medium text-gray-700">Password</label>
                      <div className="relative">
                        <input
                          type={showPassword ? 'text' : 'password'}
                          autoComplete="current-password"
                          className={cn(
                            inputClass(Boolean(loginForm.formState.errors.password)),
                            'pr-10'
                          )}
                          {...loginForm.register('password')}
                        />
                        <button
                          type="button"
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                          onClick={() => setShowPassword((value) => !value)}
                          aria-label={showPassword ? 'Hide password' : 'Show password'}
                        >
                          {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                        </button>
                      </div>
                      <FieldError message={loginForm.formState.errors.password?.message} />
                    </div>

                    <div className="flex justify-end">
                      <button
                        type="button"
                        className="text-small text-navy-700 hover:underline"
                        onClick={() => switchPanel('password_reset_initiate')}
                      >
                        Forgot password?
                      </button>
                    </div>

                    <button
                      type="submit"
                      disabled={isPending}
                      className="flex w-full items-center justify-center gap-2 rounded-input bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
                    >
                      {loginMutation.isPending && <Spinner size="sm" />}
                      Sign in
                    </button>

                    <div className="my-1 flex items-center gap-3">
                      <div className="h-px flex-1 bg-gray-200" />
                      <span className="text-small text-gray-400">or</span>
                      <div className="h-px flex-1 bg-gray-200" />
                    </div>

                    <button
                      type="button"
                      onClick={() => switchPanel('register')}
                      className="w-full rounded-input border border-gray-200 px-4 py-2 text-sm text-gray-600 transition-all hover:bg-gray-50"
                    >
                      Create account
                    </button>
                  </form>
                ) : (
                  <form onSubmit={handleEmailOtpLoginSubmit} noValidate className="space-y-4">
                    <div>
                      <label className="mb-1 block text-sm font-medium text-gray-700">Email address</label>
                      <input
                        type="email"
                        placeholder="you@example.com"
                        autoComplete="email"
                        className={inputClass(Boolean(emailOtpLoginForm.formState.errors.email))}
                        {...emailOtpLoginForm.register('email')}
                      />
                      <FieldError message={emailOtpLoginForm.formState.errors.email?.message} />
                    </div>

                    <p className="text-xs text-gray-400">
                      We'll send a one-time code to your email. No password needed.
                    </p>

                    <button
                      type="submit"
                      disabled={isPending}
                      className="flex w-full items-center justify-center gap-2 rounded-input bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
                    >
                      {emailOtpLoginMutation.isPending && <Spinner size="sm" />}
                      Send verification code
                    </button>

                    <div className="my-1 flex items-center gap-3">
                      <div className="h-px flex-1 bg-gray-200" />
                      <span className="text-small text-gray-400">or</span>
                      <div className="h-px flex-1 bg-gray-200" />
                    </div>

                    <button
                      type="button"
                      onClick={() => switchPanel('register')}
                      className="w-full rounded-input border border-gray-200 px-4 py-2 text-sm text-gray-600 transition-all hover:bg-gray-50"
                    >
                      Create account
                    </button>
                  </form>
                )}
              </motion.div>
            ) : null}

            {panel === 'register' ? (
              <motion.div key="register" {...panelVariants} transition={{ duration: 0.15 }}>
                <h1 className="text-display mb-1">Create account</h1>
                <p className="mb-6 text-small text-gray-500">
                  Create your Kodi account and verify your phone to unlock the workspace.
                </p>

                <form onSubmit={handleRegisterSubmit} noValidate className="space-y-4">
                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700">Email address</label>
                    <input
                      type="email"
                      placeholder="you@example.com"
                      autoComplete="email"
                      className={inputClass(Boolean(registerForm.formState.errors.email))}
                      {...registerForm.register('email')}
                    />
                    <FieldError message={registerForm.formState.errors.email?.message} />
                  </div>

                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700">Phone number</label>
                    <input
                      type="tel"
                      placeholder="+254 712 345 678"
                      autoComplete="tel"
                      className={inputClass(Boolean(registerForm.formState.errors.phone_number))}
                      {...registerForm.register('phone_number')}
                      onBlur={(event) => {
                        registerForm.setValue('phone_number', normalizeKenyanPhone(event.target.value))
                        void registerForm.trigger('phone_number')
                      }}
                    />
                    <FieldError message={registerForm.formState.errors.phone_number?.message} />
                  </div>

                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700">KRA PIN</label>
                    <input
                      type="text"
                      placeholder="A123456789B"
                      className={inputClass(Boolean(registerForm.formState.errors.kra_pin))}
                      {...registerForm.register('kra_pin')}
                    />
                    <p className="mt-1 text-small text-gray-400">
                      One letter, 9 digits, one letter, all uppercase.
                    </p>
                    <FieldError message={registerForm.formState.errors.kra_pin?.message} />
                  </div>

                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700">Password</label>
                    <div className="relative">
                      <input
                        type={showPassword ? 'text' : 'password'}
                        autoComplete="new-password"
                        className={cn(
                          inputClass(Boolean(registerForm.formState.errors.password)),
                          'pr-10'
                        )}
                        {...registerForm.register('password')}
                      />
                      <button
                        type="button"
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                        onClick={() => setShowPassword((value) => !value)}
                        aria-label={showPassword ? 'Hide password' : 'Show password'}
                      >
                        {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    </div>
                    <PasswordStrengthBar password={registerForm.watch('password') ?? ''} />
                    <FieldError message={registerForm.formState.errors.password?.message} />
                  </div>

                  <button
                    type="submit"
                    disabled={isPending}
                    className="flex w-full items-center justify-center gap-2 rounded-input bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
                  >
                    {registerMutation.isPending && <Spinner size="sm" />}
                    Create account
                  </button>

                  <p className="text-center text-small text-gray-500">
                    Already have an account?{' '}
                    <button
                      type="button"
                      className="text-navy-700 hover:underline"
                      onClick={() => switchPanel('login')}
                    >
                      Sign in
                    </button>
                  </p>
                </form>
              </motion.div>
            ) : null}

            {panel === 'otp_registration_verify' ? (
              <motion.div key="otp_registration_verify" {...panelVariants} transition={{ duration: 0.15 }}>
                <h1 className="text-display mb-1">Verify your phone</h1>
                <p className="mb-2 text-small text-gray-500">
                  Enter the verification code sent to{' '}
                  <span className="font-medium text-gray-700">{registrationPhone}</span>.
                </p>

                <div className="space-y-5">
                  <OtpInput
                    value={otpValue}
                    onChange={setOtpValue}
                    disabled={verifyOtpMutation.isPending}
                  />

                  <div className="flex items-center justify-between">
                    <button
                      type="button"
                      disabled={!canResend || issueOtpChallengeMutation.isPending}
                      className="text-small text-navy-700 hover:underline disabled:text-gray-400 disabled:no-underline"
                      onClick={handleResend}
                    >
                      {countdown > 0
                        ? `Resend in ${Math.floor(countdown / 60)}:${String(countdown % 60).padStart(2, '0')}`
                        : 'Resend code'}
                    </button>

                    {phoneChangeAttemptsRemaining > 0 && (
                      <button
                        type="button"
                        className="text-small text-gray-500 hover:text-gray-700 hover:underline"
                        onClick={() => { setPhoneChangeOpen((v) => !v); setError(null) }}
                      >
                        Wrong number?
                      </button>
                    )}
                  </div>

                  {/* ── Inline phone-change form ─────────────────────── */}
                  {phoneChangeOpen && (
                    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 space-y-3">
                      <p className="text-sm font-medium text-gray-800">Change phone number</p>
                      <p className="text-xs text-gray-500">
                        Current: <span className="font-medium">{registrationPhone}</span>
                        {phoneChangeAttemptsRemaining < 3 && (
                          <span className="ml-2 text-amber-600">
                            ({phoneChangeAttemptsRemaining} change{phoneChangeAttemptsRemaining !== 1 ? 's' : ''} remaining)
                          </span>
                        )}
                      </p>
                      <input
                        type="tel"
                        value={newPhoneInput}
                        onChange={(e) => setNewPhoneInput(e.target.value)}
                        onBlur={(e) => setNewPhoneInput(normalizeKenyanPhone(e.target.value))}
                        placeholder="+254 712 345 678"
                        className="h-10 w-full rounded-lg border border-gray-200 bg-white px-3 text-sm text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-transparent focus:ring-2 focus:ring-navy-500"
                      />
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() => updatePhoneMutation.mutate(normalizeKenyanPhone(newPhoneInput))}
                          disabled={
                            updatePhoneMutation.isPending ||
                            phoneChangeCooldown > 0 ||
                            newPhoneInput.trim().length === 0 ||
                            normalizeKenyanPhone(newPhoneInput) === registrationPhone
                          }
                          className="flex items-center gap-2 rounded-lg bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-navy-700 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                          {updatePhoneMutation.isPending && <Spinner size="sm" />}
                          {phoneChangeCooldown > 0 ? `Wait ${phoneChangeCooldown}s` : 'Update'}
                        </button>
                        <button
                          type="button"
                          onClick={() => { setPhoneChangeOpen(false); setNewPhoneInput(''); setError(null) }}
                          className="rounded-lg border border-gray-200 px-4 py-2 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}

                  <button
                    type="button"
                    disabled={otpValue.length < 4 || verifyOtpMutation.isPending}
                    onClick={handleRegistrationOtpSubmit}
                    className="flex w-full items-center justify-center gap-2 rounded-input bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
                  >
                    {verifyOtpMutation.isPending && <Spinner size="sm" />}
                    Verify
                  </button>

                  <button
                    type="button"
                    className="text-small text-gray-500 hover:underline"
                    onClick={() => switchPanel('register')}
                  >
                    Back
                  </button>
                </div>
              </motion.div>
            ) : null}

            {panel === 'otp_login_step_up' ? (
              <motion.div key="otp_login_step_up" {...panelVariants} transition={{ duration: 0.15 }}>
                <h1 className="text-display mb-1">Verify your identity</h1>
                <p className="mb-6 text-small text-gray-500">
                  {emailOtpEmail
                    ? `Enter the verification code sent to ${emailOtpEmail}.`
                    : 'Enter the verification code to finish signing in safely.'}
                </p>

                <div className="space-y-6">
                  <OtpInput
                    value={otpValue}
                    onChange={setOtpValue}
                    disabled={loginStepUpMutation.isPending || emailOtpLoginMutation.isPending}
                  />

                  <button
                    type="button"
                    disabled={!canResend || issueOtpChallengeMutation.isPending || emailOtpLoginMutation.isPending}
                    className="text-small text-navy-700 hover:underline disabled:text-gray-400 disabled:no-underline"
                    onClick={handleResend}
                  >
                    {countdown > 0
                      ? `Resend in ${Math.floor(countdown / 60)}:${String(countdown % 60).padStart(2, '0')}`
                      : 'Resend code'}
                  </button>

                  <button
                    type="button"
                    disabled={otpValue.length < 4 || loginStepUpMutation.isPending || emailOtpLoginMutation.isPending}
                    onClick={emailOtpEmail ? handleEmailOtpStepUpSubmit : handleLoginStepUpSubmit}
                    className="flex w-full items-center justify-center gap-2 rounded-input bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
                  >
                    {(loginStepUpMutation.isPending || emailOtpLoginMutation.isPending) && <Spinner size="sm" />}
                    Verify
                  </button>

                  <button
                    type="button"
                    className="text-small text-gray-500 hover:underline"
                    onClick={() => { setEmailOtpEmail(null); switchPanel('login') }}
                  >
                    Back to sign in
                  </button>
                </div>
              </motion.div>
            ) : null}

            {panel === 'password_reset_initiate' ? (
              <motion.div key="password_reset_initiate" {...panelVariants} transition={{ duration: 0.15 }}>
                <h1 className="text-display mb-1">Reset password</h1>
                <p className="mb-6 text-small text-gray-500">
                  Enter your email to receive a reset code.
                </p>

                <form onSubmit={handleResetInitSubmit} noValidate className="space-y-4">
                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700">Email address</label>
                    <input
                      type="email"
                      placeholder="you@example.com"
                      autoComplete="email"
                      className={inputClass(Boolean(resetInitForm.formState.errors.email))}
                      {...resetInitForm.register('email')}
                    />
                    <FieldError message={resetInitForm.formState.errors.email?.message} />
                  </div>

                  <button
                    type="submit"
                    disabled={isPending}
                    className="flex w-full items-center justify-center gap-2 rounded-input bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
                  >
                    {resetInitiateMutation.isPending && <Spinner size="sm" />}
                    Send reset code
                  </button>

                  <button
                    type="button"
                    className="text-small text-gray-500 hover:underline"
                    onClick={() => switchPanel('login')}
                  >
                    Back to sign in
                  </button>
                </form>
              </motion.div>
            ) : null}

            {panel === 'otp_recovery' ? (
              <motion.div key="otp_recovery" {...panelVariants} transition={{ duration: 0.15 }}>
                <h1 className="text-display mb-1">Set new password</h1>
                <p className="mb-6 text-small text-gray-500">
                  Enter the reset code from your email and choose a new password.
                </p>

                <form onSubmit={handleResetConfirmSubmit} noValidate className="space-y-4">
                  <div>
                    <label className="mb-2 block text-sm font-medium text-gray-700">Reset code</label>
                    <OtpInput
                      value={otpValue}
                      onChange={setOtpValue}
                      disabled={resetConfirmMutation.isPending}
                    />
                  </div>

                  <button
                    type="button"
                    disabled={!canResend || resetInitiateMutation.isPending}
                    className="text-small text-navy-700 hover:underline disabled:text-gray-400 disabled:no-underline"
                    onClick={handleResend}
                  >
                    {countdown > 0
                      ? `Resend in ${Math.floor(countdown / 60)}:${String(countdown % 60).padStart(2, '0')}`
                      : 'Resend code'}
                  </button>

                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700">New password</label>
                    <div className="relative">
                      <input
                        type={showNewPassword ? 'text' : 'password'}
                        autoComplete="new-password"
                        className={cn(
                          inputClass(Boolean(resetConfirmForm.formState.errors.new_password)),
                          'pr-10'
                        )}
                        {...resetConfirmForm.register('new_password')}
                      />
                      <button
                        type="button"
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                        onClick={() => setShowNewPassword((value) => !value)}
                        aria-label={showNewPassword ? 'Hide password' : 'Show password'}
                      >
                        {showNewPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    </div>
                    <PasswordStrengthBar password={resetConfirmForm.watch('new_password') ?? ''} />
                    <FieldError message={resetConfirmForm.formState.errors.new_password?.message} />
                  </div>

                  <button
                    type="submit"
                    disabled={otpValue.length < 4 || isPending}
                    className="flex w-full items-center justify-center gap-2 rounded-input bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
                  >
                    {resetConfirmMutation.isPending && <Spinner size="sm" />}
                    Set password
                  </button>

                  <button
                    type="button"
                    className="text-small text-gray-500 hover:underline"
                    onClick={() => switchPanel('password_reset_initiate')}
                  >
                    Back
                  </button>
                </form>
              </motion.div>
            ) : null}
          </AnimatePresence>
        </div>
      </div>
    </div>
  )
}
