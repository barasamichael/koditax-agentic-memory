// Role values from README "POST /v1/auth/register" → Validation section
export type Role = 'IndividualTaxpayer' | 'TaxAgent' | 'Accountant' | 'Administrator'

// Subscription tier from DB schema "users" → subscription_tier column
export type SubscriptionTier = 'standard'

// delegation_context shape from README "POST /v1/auth/login" → session fields
export interface DelegationContext {
  is_delegated: boolean
  principal_user_id: string | null
  delegate_user_id: string | null
  delegation_id: string | null
  granted_at: string | null
  revoked_at: string | null
}

// session object returned inside login, refresh responses
export interface AuthSession {
  user_id: string
  tenant_id: string          // assigned at sign-in and used for protected requests
  role: Role
  session_id: string
  delegation_context: DelegationContext
}

// Full authenticated login response (status: 'authenticated')
export interface LoginSuccessResponse {
  status: 'authenticated'
  access_token: string
  refresh_token: string
  expires_at: string
  session: AuthSession
}

// Step-up pending response — README lists both 'login_status' and 'status' fields
export interface LoginStepUpResponse {
  login_status: string
  status: string
  step_up_required: boolean
  step_up_purpose: string
  step_up_channel: string              // current runtime always resolves to 'sms'
  step_up_challenge_id: string
  step_up_expires_at: string
}

export type LoginResponse = LoginSuccessResponse | LoginStepUpResponse

// OTP challenge response — shared by /v1/auth/otp/challenges and /v1/auth/password-reset/initiate
export interface OtpChallengeResponse {
  status: string
  challenge_id: string
  expires_at: string
}

// OTP verify response — README: runtime ONLY returns status + verification_status
export interface OtpVerifyResponse {
  status: string
  verification_status: string
}

// Registration response — from README "POST /v1/auth/register" → Response fields
export interface RegisterResponse {
  user_id: string
  registration_status: 'pending_verification'
  created_at: string
}

// Refresh response — from README "POST /v1/auth/refresh" → Response fields
export interface RefreshResponse {
  status: string
  access_token: string
  refresh_token: string
  expires_at: string
  session: AuthSession
}

// Logout response — from README "POST /v1/auth/logout" → Response fields
export interface LogoutResponse {
  status: string
  revoke_scope: string
  revoked_session_count: number
  traceability: { trace_id: string; correlation_id: string }
}

// Session introspection — from README "GET /v1/auth/sessions/{session_id}"
export interface SessionIntrospectionResponse {
  status: 'active' | 'warning' | 'invalidated' | 'expired'
  session: AuthSession
  issued_at: string
  expires_at: string
  inactivity_expires_at: string
  absolute_expires_at: string
  last_activity_at: string
  warning_window_started_at: string | null
  extension_allowed: boolean
  is_invalidated: boolean
  traceability: { trace_id: string; correlation_id: string }
}

// Phone change request response — POST /v1/auth/phone-change/requests
export interface PhoneChangeRequestResponse {
  status: string
  request_id: string
  phone_change_state: string
  step_up_challenge_id: string
  step_up_expires_at: string
}

// Phone change confirm response — POST /v1/auth/phone-change/confirm
export interface PhoneChangeConfirmResponse {
  status: string
  request_id: string
  phone_change_state: string
  updated_phone_number: string
  updated_at: string
}

// Password reset confirm response — from README "POST /v1/auth/password-reset/confirm"
export interface PasswordResetConfirmResponse {
  status: string
  updated_at: string
}

// Canonical error shape — from README "Error Handling" section
export interface AuthError {
  error_code: string
  message: string
  reason: string
  reason_code?: string
  correlation_id: string
  trace_id: string
  details: Record<string, unknown>
  // Promoted top-level fields (present on relevant errors):
  current_state?: string
  requested_state?: string
  lockout_expires_at?: string
  lockout_remaining_seconds?: number
  account_deletion_state?: string
  audit_reference_id?: string
  incident_code?: string
}

// User profile response — from GET /v1/auth/profile
export interface UserProfile {
  user_id: string
  role: string
  phone_number: string
  email: string
  account_state: string
  subscription_tier: string
  member_since: string
  gravatar_url: string
}

// Registration phone update response — PATCH /v1/auth/phone-verification/update-phone
export interface RegistrationPhoneUpdateResponse {
  status: string
  challenge_id: string
  expires_at: string
  updated_phone_number: string
  attempts_remaining: number
}

// OTP purpose values
export type OtpPurpose =
  | 'registration_verify'
  | 'login_step_up'
  | 'recovery'
  | 'account_deletion_confirm'
  | 'phone_change_confirm'
