import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'
import * as authApi from '@/api/auth.api'
import { normalizeError } from '@/lib/errorNormalizer'
import type { LoginSuccessResponse, LoginStepUpResponse } from '@/types/auth'

export const isLoginSuccess = (res: unknown): res is LoginSuccessResponse =>
  typeof res === 'object' && res !== null && 'access_token' in res && 'session' in res

export const isLoginStepUp = (res: unknown): res is LoginStepUpResponse =>
  typeof res === 'object' && res !== null && 'step_up_challenge_id' in res

export const useRegister = () =>
  useMutation({
    mutationFn: authApi.register,
    onError: (err) => normalizeError(err),
  })

export const useLogin = () =>
  useMutation({
    mutationFn: (params: { login_id: string; password: string }) =>
      authApi.login({
        ...params,
        device_fingerprint: authApi.generateDeviceFingerprint(),
      }),
    onError: (err) => normalizeError(err),
  })

export const useLoginStepUp = () =>
  useMutation({
    mutationFn: (params: {
      login_id: string
      password: string
      step_up_challenge_id: string
      step_up_otp_code: string
    }) =>
      authApi.loginStepUp({
        ...params,
        device_fingerprint: authApi.generateDeviceFingerprint(),
      }),
    onError: (err) => normalizeError(err),
  })

export const useEmailOtpLogin = () =>
  useMutation({
    mutationFn: (params: {
      email: string
      step_up_challenge_id?: string
      step_up_otp_code?: string
    }) =>
      authApi.loginWithEmailOtp({
        ...params,
        device_fingerprint: authApi.generateDeviceFingerprint(),
      }),
    onError: (err) => normalizeError(err),
  })

export const useIssueOtpChallenge = () =>
  useMutation({
    mutationFn: authApi.issueOtpChallenge,
    onError: (err) => normalizeError(err),
  })

export const useVerifyOtp = () =>
  useMutation({
    mutationFn: authApi.verifyOtp,
    onError: (err) => normalizeError(err),
  })

export const useRefreshTokens = () => {
  const { rotateTokens, clearAuth } = useAuthStore()
  const navigate = useNavigate()

  return useMutation({
    mutationFn: () => authApi.refreshTokens(useAuthStore.getState().refreshToken!),
    onSuccess: (data) => {
      rotateTokens({
        accessToken: data.access_token,
        refreshToken: data.refresh_token,
        expiresAt: data.expires_at,
        session: data.session,
      })
    },
    onError: () => {
      clearAuth({ reason: 'session_expired' })
      navigate('/?reason=session_expired', { replace: true })
    },
  })
}

export const useLogout = () => {
  const { clearAuth } = useAuthStore()
  const navigate = useNavigate()

  return useMutation({
    mutationFn: () => authApi.logout({ revoke_scope: 'all_sessions' }),
    onSuccess: () => {
      clearAuth({ reason: 'signed_out' })
      navigate('/?reason=signed_out', { replace: true })
    },
    onError: () => {
      clearAuth({ reason: 'signed_out' })
      navigate('/?reason=signed_out', { replace: true })
    },
  })
}

export const useSession = (sessionId: string | null) =>
  useQuery({
    queryKey: ['auth', 'session', sessionId],
    queryFn: () => authApi.getSession(sessionId!),
    enabled: !!sessionId,
    staleTime: 30_000,
  })

export const usePasswordReset = () => ({
  initiate: useMutation({
    mutationFn: authApi.initiatePasswordReset,
    onError: (err) => normalizeError(err),
  }),
  confirm: useMutation({
    mutationFn: authApi.confirmPasswordReset,
    onError: (err) => normalizeError(err),
  }),
})

export { normalizeError }
export type { LoginSuccessResponse, LoginStepUpResponse }
