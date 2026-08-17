export const DEFAULT_TENANT_ID = 'pilot_tenant_alpha'

// Approved frontend-visible base URLs for normal user flows.
// Root README policy:
// - auth: public
// - gateway: public ingress for authenticated orchestration
// - document_ai: conditional public
// Internal services must not become standard browser integrations.
export const PUBLIC_FRONTEND_BASE_URLS = {
  auth: import.meta.env.VITE_AUTH_SERVICE_URL ?? import.meta.env.VITE_API_BASE_URL ?? '',
  orchestration:
    import.meta.env.VITE_ORCHESTRATION_URL ??
    import.meta.env.VITE_GATEWAY_URL ?? import.meta.env.VITE_API_BASE_URL ?? '',
  documents: import.meta.env.VITE_DOCUMENTS_URL ?? import.meta.env.VITE_API_BASE_URL ?? '',
} as const

// Internal services remain quarantined from standard end-user frontend flows.
export const QUARANTINED_INTERNAL_FRONTEND_SURFACES = [
  'tax_core',
  'forms',
  'reports',
  'validation',
  'storage',
  'event_store',
  'knowledge',
] as const
