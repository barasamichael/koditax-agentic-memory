import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { StorageValue } from 'zustand/middleware'
import type { AuthSession, Role } from '@/types/auth'
import { clampLoginSessionExpiresAt } from '@/lib/authSession'

export type AuthStatusReason =
  | 'signed_out'
  | 'session_required'
  | 'session_expired'
  | 'registration_verified'
  | 'password_reset_complete'
  | 'phone_change_confirmed'
  | null

interface AuthState {
  session: AuthSession | null
  accessToken: string | null
  refreshToken: string | null
  tokenExpiresAt: string | null
  isAuthenticated: boolean
  userId: string | null
  role: Role | null
  tenantId: string
  authStatusReason: AuthStatusReason
  setAuth: (params: {
    session: AuthSession
    accessToken: string
    refreshToken: string
    expiresAt: string
  }) => void
  clearAuth: (options?: { reason?: AuthStatusReason }) => void
  rotateTokens: (params: {
    accessToken: string
    refreshToken: string
    expiresAt: string
    session: AuthSession
  }) => void
  setAuthStatusReason: (reason: AuthStatusReason) => void
  clearAuthStatusReason: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      session: null,
      accessToken: null,
      refreshToken: null,
      tokenExpiresAt: null,
      isAuthenticated: false,
      userId: null,
      role: null,
      tenantId: 'default_tenant',
      authStatusReason: null,

      setAuth: ({ session, accessToken, refreshToken, expiresAt }) =>
        set({
          session,
          accessToken,
          refreshToken,
          tokenExpiresAt: clampLoginSessionExpiresAt(expiresAt),
          isAuthenticated: true,
          userId: session.user_id,
          role: session.role,
          tenantId: session.tenant_id,
          authStatusReason: null,
        }),

      clearAuth: (options) =>
        set({
          session: null,
          accessToken: null,
          refreshToken: null,
          tokenExpiresAt: null,
          isAuthenticated: false,
          userId: null,
          role: null,
          tenantId: 'default_tenant',
          authStatusReason: options?.reason ?? null,
        }),

      rotateTokens: ({ accessToken, refreshToken, expiresAt, session }) =>
        set({
          accessToken,
          refreshToken,
          tokenExpiresAt: clampLoginSessionExpiresAt(expiresAt),
          session,
          userId: session.user_id,
          role: session.role,
          tenantId: session.tenant_id,
          authStatusReason: null,
        }),

      setAuthStatusReason: (reason) =>
        set({
          authStatusReason: reason,
        }),

      clearAuthStatusReason: () =>
        set({
          authStatusReason: null,
        }),
    }),
    {
      name: 'kodi-auth',
      storage: {
        getItem: (key) => {
          try {
            const item = sessionStorage.getItem(key)
            if (!item) return null
            const parsed = JSON.parse(item) as Partial<StorageValue<Partial<AuthState>>>
            if (!parsed.state) return null
            if (!parsed.state.tokenExpiresAt) return parsed as StorageValue<Partial<AuthState>>
            return {
              ...parsed,
              state: {
                ...parsed.state,
                tokenExpiresAt: clampLoginSessionExpiresAt(parsed.state.tokenExpiresAt),
              },
            } as StorageValue<Partial<AuthState>>
          } catch {
            return null
          }
        },
        setItem: (key, value) => {
          const persisted = value as StorageValue<Partial<AuthState>>
          const normalized = {
            ...persisted,
            state: {
              ...persisted.state,
              tokenExpiresAt:
                typeof persisted.state.tokenExpiresAt === 'string'
                  ? clampLoginSessionExpiresAt(persisted.state.tokenExpiresAt)
                  : persisted.state.tokenExpiresAt ?? null,
            },
          }
          sessionStorage.setItem(key, JSON.stringify(normalized))
        },
        removeItem: (key) => sessionStorage.removeItem(key),
      },
      partialize: (state) => ({
        session: state.session,
        accessToken: state.accessToken,
        refreshToken: state.refreshToken,
        tokenExpiresAt: state.tokenExpiresAt,
        isAuthenticated: state.isAuthenticated,
        userId: state.userId,
        role: state.role,
        tenantId: state.tenantId,
        authStatusReason: state.authStatusReason,
      }) as AuthState,
    }
  )
)
