import { authClient } from './client'
import { generateUniqueIdempotencyKey } from '@/lib/idempotency'
import type {
  RegisterResponse,
  LoginResponse,
  LoginSuccessResponse,
  OtpChallengeResponse,
  OtpVerifyResponse,
  RefreshResponse,
  LogoutResponse,
  SessionIntrospectionResponse,
  PasswordResetConfirmResponse,
  PhoneChangeRequestResponse,
  PhoneChangeConfirmResponse,
  RegistrationPhoneUpdateResponse,
  UserProfile,
  OtpPurpose,
} from '@/types/auth'

// Approved public frontend adapter: auth is a normal user-facing service boundary.

// Registration creates a new account in pending_verification state.
// The account cannot log in until the phone OTP is verified.
export const register = (params: {
  email: string
  phone_number: string
  kra_pin: string
  password: string
}): Promise<RegisterResponse> =>
  authClient.post<RegisterResponse>('/v1/auth/register', {
    ...params,
    role: 'IndividualTaxpayer',
  }).then(r => r.data)

// Login validates credentials and triggers an OTP step-up challenge.
// Returns either a full session (rare, policy-dependent) or a step_up_required
// response containing the challenge_id needed to complete login via loginStepUp().
export const login = (params: {
  login_id: string
  password: string
  device_fingerprint?: string
}): Promise<LoginResponse> =>
  authClient.post<LoginResponse>('/v1/auth/login', params).then(r => r.data)

// Passwordless email-OTP login: first call issues OTP, second call verifies it.
// Step 1: pass { email } → backend sends OTP to that email, returns step_up_challenge_id.
// Step 2: pass { email, step_up_challenge_id, step_up_otp_code } → returns session.
export const loginWithEmailOtp = (params: {
  email: string
  device_fingerprint?: string
  step_up_challenge_id?: string
  step_up_otp_code?: string
}): Promise<LoginResponse> =>
  authClient.post<LoginResponse>('/v1/auth/login/email-otp', params).then(r => r.data)

// Completes a login that required OTP step-up.
// Credentials must be resent alongside the challenge fields —
// the backend re-validates the full credential pair on this call.
export const loginStepUp = (params: {
  login_id: string
  password: string
  step_up_challenge_id: string
  step_up_otp_code: string
  device_fingerprint?: string
}): Promise<LoginSuccessResponse> =>
  authClient.post<LoginSuccessResponse>('/v1/auth/login', params).then(r => r.data)

// Issues an OTP challenge for the given purpose and delivery channel.
// Returns a challenge_id that must be passed to verifyOtp() or loginStepUp().
export const issueOtpChallenge = (params: {
  purpose: OtpPurpose
  channel: 'email' | 'sms'
  email?: string
  phone_number?: string
  fallback_channel?: 'email'
}): Promise<OtpChallengeResponse> =>
  authClient.post<OtpChallengeResponse>('/v1/auth/otp/challenges', params, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('otp-challenge'),
    },
  }).then(r => r.data)

// Verifies a registration_verify OTP challenge and activates the account.
// Only handles registration_verify — login step-up OTP is verified via loginStepUp().
export const verifyOtp = (params: {
  challenge_id: string
  otp_code: string
}): Promise<OtpVerifyResponse> =>
  authClient.post<OtpVerifyResponse>('/v1/auth/otp/verify', params).then(r => r.data)

// Rotates both access and refresh tokens. The old refresh token is invalidated
// immediately — reusing it will return 409 and force a full logout.
export const refreshTokens = (refreshToken: string): Promise<RefreshResponse> =>
  authClient.post<RefreshResponse>('/v1/auth/refresh', {
    refresh_token: refreshToken,
  }).then(r => r.data)

// Revokes the specified session(s). Use revoke_scope 'all_sessions' to sign
// out everywhere, or 'single_session' with a target_session_id for one device.
export const logout = (params: {
  revoke_scope: 'single_session' | 'all_sessions'
  target_session_id?: string
}): Promise<LogoutResponse> =>
  authClient.post<LogoutResponse>('/v1/auth/logout', params).then(r => r.data)

// Returns live session state including expiry timestamps and whether
// the session is within the inactivity warning window.
export const getSession = (sessionId: string): Promise<SessionIntrospectionResponse> =>
  authClient.get<SessionIntrospectionResponse>(`/v1/auth/sessions/${sessionId}`).then(r => r.data)

// Sends a password reset OTP to the given email address.
// The response shape is identical whether the address exists or not
// to prevent account enumeration.
export const initiatePasswordReset = (params: {
  purpose: 'password_reset' | 'password_setup'
  email: string
}): Promise<OtpChallengeResponse> =>
  authClient.post<OtpChallengeResponse>('/v1/auth/password-reset/initiate', {
    ...params,
    channel: 'email',
  }, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('password-reset-initiate'),
    },
  }).then(r => r.data)

// Applies the new password after the reset OTP is verified.
// All active sessions are revoked on success.
export const confirmPasswordReset = (params: {
  challenge_id: string
  reset_code: string
  new_password: string
}): Promise<PasswordResetConfirmResponse> =>
  authClient.post<PasswordResetConfirmResponse>(
    '/v1/auth/password-reset/confirm',
    params,
  ).then(r => r.data)

// Initiates a phone number change. Requires current password for re-authentication.
// Returns a step_up_challenge_id for OTP verification of the new number.
export const requestPhoneChange = (params: {
  new_phone_number: string
  current_password: string
}): Promise<PhoneChangeRequestResponse> =>
  authClient.post<PhoneChangeRequestResponse>('/v1/auth/phone-change/requests', params, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('phone-change-request'),
    },
  }).then(r => r.data)

// Confirms the phone change by verifying the OTP sent to the new number.
// All active sessions are revoked after the number is updated.
export const confirmPhoneChange = (params: {
  request_id: string
  step_up_challenge_id: string
  step_up_otp_code: string
}): Promise<PhoneChangeConfirmResponse> =>
  authClient.post<PhoneChangeConfirmResponse>('/v1/auth/phone-change/confirm', params, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('phone-change-confirm'),
    },
  }).then(r => r.data)

// Requests account deletion. Blocked accounts (compliance lock, legal hold, etc.)
// still receive a 201 response with a blockers array describing why deletion
// cannot proceed immediately.
export const requestAccountDeletion = (params: {
  request_reason: string
}): Promise<unknown> =>
  authClient.post('/v1/auth/account-deletion/requests', params, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('account-deletion-request'),
    },
  }).then(r => r.data)

// Confirms account deletion using two proofs: a re-auth proof and an OTP
// verification ID. A cooldown period begins after confirmation during which
// deletion can still be cancelled.
export const confirmAccountDeletion = (params: {
  request_id: string
  reauth_proof: string
  otp_verification_id: string
}): Promise<unknown> =>
  authClient.post('/v1/auth/account-deletion/confirm', params, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('account-deletion-confirm'),
    },
  }).then(r => r.data)

// Cancels a confirmed deletion request during the cooldown window.
// Cancellation is not possible after the cooldown expires.
export const cancelAccountDeletion = (params: {
  request_id: string
}): Promise<unknown> =>
  authClient.post('/v1/auth/account-deletion/cancel', params, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('account-deletion-cancel'),
    },
  }).then(r => r.data)

// Executes account deletion after the cooldown period has elapsed.
// Tombstones the account, invalidates all credentials, and revokes all sessions.
export const executeAccountDeletion = (params: {
  request_id: string
}): Promise<unknown> =>
  authClient.post('/v1/auth/account-deletion/execute', params, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey('account-deletion-execute'),
    },
  }).then(r => r.data)

// Updates the phone number for a pending-verification registration and re-sends OTP.
// Rate-limited to 3 changes per registration session with a 60s cooldown between changes.
export const updateRegistrationPhone = (params: {
  user_id: string
  new_phone_number: string
}): Promise<RegistrationPhoneUpdateResponse> =>
  authClient.patch<RegistrationPhoneUpdateResponse>(
    '/v1/auth/phone-verification/update-phone',
    params,
    {
      headers: {
        'Idempotency-Key': generateUniqueIdempotencyKey('reg-phone-update'),
      },
    },
  ).then(r => r.data)

// Starts an OAuth 2.1 Authorization Code + PKCE flow for the given provider.
// Returns an authorization_url to redirect the user to.
export const startOAuth = (params: {
  provider: string
  redirect_uri: string
}): Promise<unknown> =>
  authClient.post(`/v1/auth/oauth/${params.provider}/start`, {
    redirect_uri: params.redirect_uri,
  }).then(r => r.data)

// Handles the OAuth provider callback after the user authorises the app.
// The browser navigates to this URL directly — use this helper only when
// processing the callback programmatically after the redirect.
export const handleOAuthCallback = (params: {
  provider: string
  code: string
  state: string
}): Promise<unknown> =>
  authClient.get(`/v1/auth/oauth/${params.provider}/callback`, {
    params: { code: params.code, state: params.state },
  }).then(r => r.data)

// Returns the authenticated user's profile with masked sensitive fields.
// Phone and email are partially obscured — the backend masks them server-side.
export const getProfile = (): Promise<UserProfile> =>
  authClient.get<UserProfile>('/v1/auth/profile').then(r => r.data)

// Changes the role of a target user. Requires Administrator access
// with X-Auth-Context header set by the API client interceptor.
export const changeRole = (params: {
  target_user_id: string
  new_role: string
  reason?: string
}): Promise<unknown> =>
  authClient.post('/v1/auth/roles/change', params).then(r => r.data)

// Generates a stable browser fingerprint from non-PII signals.
// Used as an optional hint on login — never shown to the user.
export const generateDeviceFingerprint = (): string => {
  const raw = [
    navigator.userAgent,
    screen.width,
    screen.height,
    Intl.DateTimeFormat().resolvedOptions().timeZone,
    navigator.language,
  ].join('|')
  let h = 0
  for (let i = 0; i < raw.length; i++) {
    h = (Math.imul(31, h) + raw.charCodeAt(i)) | 0
  }
  return Math.abs(h).toString(16).padStart(8, '0')
}
