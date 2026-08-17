import type { DocumentState } from '@/types/document'

export interface DocumentListQueryParams {
  state?: DocumentState
  uploaded_from?: string
  uploaded_to?: string
  computation_id?: string
}

const DOCUMENTS_ROOT = ['documents'] as const

const buildDocumentListKey = (params?: DocumentListQueryParams) =>
  [
    ...DOCUMENTS_ROOT,
    'list',
    params?.state ?? null,
    params?.uploaded_from ?? null,
    params?.uploaded_to ?? null,
    params?.computation_id ?? null,
  ] as const

export const documentQueryKeys = {
  all: DOCUMENTS_ROOT,
  list: buildDocumentListKey,
  trashList: () => buildDocumentListKey({ state: 'trashed' }),
  detail: (documentId: string) => [...DOCUMENTS_ROOT, 'detail', documentId] as const,
  openCapability: (documentId: string) => [...DOCUMENTS_ROOT, 'open-capability', documentId] as const,
  downloadCapability: (documentId: string) => [...DOCUMENTS_ROOT, 'download-capability', documentId] as const,
  purgeDryRun: (documentId: string) => [...DOCUMENTS_ROOT, 'purge-dry-run', documentId] as const,
}
