/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  readonly VITE_AUTH_SERVICE_URL: string
  readonly VITE_ORCHESTRATION_URL: string
  readonly VITE_DOCUMENTS_URL: string
  readonly VITE_TAX_CORE_URL: string
  readonly VITE_FORMS_URL: string
  readonly VITE_REPORTS_URL: string
  readonly VITE_KNOWLEDGE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
