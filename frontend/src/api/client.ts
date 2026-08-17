import axios, { type InternalAxiosRequestConfig } from 'axios'
import { getFlowCorrelationId, getRequestCorrelationId } from '@/lib/correlation'
import { PUBLIC_FRONTEND_BASE_URLS } from '@/lib/constants'
import { useAuthStore } from '@/stores/authStore'

type AuthStrategy = 'none' | 'bearer' | 'document-bearer' | 'x-auth-context'

const PUBLIC_NO_AUTH_PATH_PREFIXES = [
  '/v1/auth/register',
  '/v1/auth/login',
  '/v1/auth/refresh',
  '/v1/auth/password-reset/',
  '/v1/auth/oauth/',
  '/v1/auth/otp/',
] as const

// Auth protected routes parse semicolon-delimited bearer content rather than
// the structured JSON X-Auth-Context envelope.
const AUTH_BEARER_PATH_PREFIXES = [
  '/v1/auth/logout',
  '/v1/auth/sessions/',
  '/v1/auth/phone-change/',
  '/v1/auth/account-deletion/',
  '/v1/auth/profile',
] as const

// Document AI supports X-Auth-Context, but the documented bearer fallback is
// the safer browser path because it survives more proxy setups cleanly.
const DOCUMENT_AUTHORIZATION_PATH_PREFIXES = ['/v1/documents'] as const

const resolveAuthStrategy = (path: string): AuthStrategy => {
  if (PUBLIC_NO_AUTH_PATH_PREFIXES.some(prefix => path.startsWith(prefix))) {
    return 'none'
  }
  if (AUTH_BEARER_PATH_PREFIXES.some(prefix => path.startsWith(prefix))) {
    return 'bearer'
  }
  if (DOCUMENT_AUTHORIZATION_PATH_PREFIXES.some(prefix => path.startsWith(prefix))) {
    return 'document-bearer'
  }
  // Remaining routes are internal or admin-only boundaries and must be treated
  // explicitly rather than as normal user-facing browser APIs.
  return 'x-auth-context'
}

const getAuthState = () => {
  try {
    return useAuthStore.getState()
  } catch {
    return null
  }
}

export const getTrustedAuthHeaders = (path: string): Record<string, string> => {
  const strategy = resolveAuthStrategy(path)
  const state = getAuthState()
  if (!state?.session) return {}
  const session = state.session
  if (strategy === 'bearer') {
    return {
      Authorization:
        `Bearer user_id=${session.user_id};tenant_id=${session.tenant_id};role=${session.role}`,
    }
  }
  if (strategy === 'document-bearer') {
    return { Authorization: `Bearer ${session.user_id}:${session.role}` }
  }
  if (strategy === 'x-auth-context') {
    return {
      'X-Auth-Context': JSON.stringify({
        schema_version: '1.0.0',
        user_id: session.user_id,
        tenant_id: session.tenant_id,
        role: session.role,
        session_id: session.session_id,
        delegation_context: session.delegation_context,
      }),
    }
  }
  return {}
}

const clearAuthSession = () => {
  try {
    useAuthStore.getState().clearAuth()
  } catch {
    // Ignore store access failures outside the browser runtime.
  }
}

export const createServiceClient = (baseURL: string) => {
  const instance = axios.create({
    baseURL,
    headers: { 'Content-Type': 'application/json' },
  })

  instance.interceptors.request.use((config: InternalAxiosRequestConfig) => {
    if (!config.headers['X-Correlation-ID']) {
      config.headers['X-Correlation-ID'] = getRequestCorrelationId()
    }

    const path = (config.url ?? '').split('?')[0]
    const strategy = resolveAuthStrategy(path)
    if (strategy === 'document-bearer') {
      delete config.headers['X-Auth-Context']
    }
    Object.assign(config.headers, getTrustedAuthHeaders(path))

    return config
  })

  instance.interceptors.response.use(
    response => response,
    error => {
      if (axios.isAxiosError(error) && error.response?.status === 401) {
        clearAuthSession()
      }
      return Promise.reject(error)
    }
  )

  return instance
}

// Approved public clients for normal end-user frontend flows.
export const authClient = createServiceClient(PUBLIC_FRONTEND_BASE_URLS.auth)
export const orchestrationClient = createServiceClient(PUBLIC_FRONTEND_BASE_URLS.orchestration)
export const documentClient = createServiceClient(PUBLIC_FRONTEND_BASE_URLS.documents)
export const knowledgeServiceClient = createServiceClient(
  import.meta.env.VITE_KNOWLEDGE_URL ?? import.meta.env.VITE_API_BASE_URL ?? ''
)

// Quarantined internal client for legacy or explicitly internal/admin-only
// adapters. Do not use this client for standard end-user product flows.
export const internalOnlyServiceClient = createServiceClient(
  import.meta.env.VITE_API_BASE_URL ?? ''
)

export const withFlowCorrelation = (flowKey: string): Record<string, string> => ({
  'X-Correlation-ID': getFlowCorrelationId(flowKey),
})
