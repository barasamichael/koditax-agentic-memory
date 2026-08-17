import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuthStore } from '@/stores/authStore'
import { documentQueryKeys, type DocumentListQueryParams } from '@/lib/documents/document-query-keys'
import { normalizeDocumentRecord } from '@/lib/documents/document-lifecycle'
import {
  computeFileSha256,
  createUploadSession,
  putFileToStorage,
  completeUpload,
  listDocuments,
  getDocument,
  issueDownloadCapability,
  validateDownloadCapability,
  renameDocument,
  trashDocument,
  restoreDocument,
  markEligibleForPurge,
  purgeDocument,
  purgeDryRun,
  requestComplianceOverride,
  approveComplianceOverride,
  rejectComplianceOverride,
} from '@/api/document.api'
import type {
  ComplianceOverrideAction,
  DocumentRecordEnvelope,
} from '@/types/document'
import { resolveDocumentStorageObjectKey } from '@/api/document.api'
import { documentContentType } from '@/api/document.api'

export type DocumentUploadStatus =
  | 'idle'
  | 'computing_checksum'
  | 'creating_session'
  | 'uploading'
  | 'completing'

export interface DocumentUploadProgress {
  loadedBytes: number
  totalBytes: number
  percent: number
}

// ─── Upload ───────────────────────────────────────────────────────────────────
// Three-step upload sequence:
//   1. Compute SHA-256 client-side (required on session creation request)
//   2. POST /v1/documents/upload-sessions → presigned PUT URL + document_id
//   3. PUT file to storage with the issued capability
//   4. POST /v1/documents/{document_id}/upload-completion → document record

export const useUploadDocument = () => {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<DocumentUploadStatus>('idle')
  const [progress, setProgress] = useState<DocumentUploadProgress | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  const cachedChecksumRef = useRef<string | null>(null)
  const cachedSessionRef = useRef<Awaited<ReturnType<typeof createUploadSession>> | null>(null)

  const resetUploadState = useCallback(() => {
    abortControllerRef.current?.abort()
    abortControllerRef.current = null
    cachedChecksumRef.current = null
    cachedSessionRef.current = null
    setStatus('idle')
    setProgress(null)
  }, [])

  useEffect(() => () => {
    abortControllerRef.current?.abort()
  }, [])

  const cancelUpload = useCallback(() => {
    abortControllerRef.current?.abort()
  }, [])

  const uploadDocument = useCallback(async (file: File) => {
    if (status !== 'idle') {
      return null
    }

    const contentType = documentContentType(file)
    if (!contentType) throw new Error('This file type is not supported.')

    const tenantId = useAuthStore.getState().tenantId

    try {
      setStatus('computing_checksum')
      const checksum = await computeFileSha256(file)
      cachedChecksumRef.current = checksum

      setStatus('creating_session')
      const session =
        cachedSessionRef.current &&
        cachedChecksumRef.current === checksum &&
        cachedSessionRef.current.document_id
          ? cachedSessionRef.current
          : await createUploadSession({
              file_name: file.name,
              content_type: contentType,
              expected_size_bytes: file.size,
              checksum_sha256: checksum,
            })
      cachedSessionRef.current = session

      setStatus('uploading')
      setProgress({ loadedBytes: 0, totalBytes: file.size, percent: 0 })
      abortControllerRef.current = new AbortController()
      await putFileToStorage(session.upload_url, file, {
        signal: abortControllerRef.current.signal,
        onProgress: setProgress,
      })

      setStatus('completing')
      const result = await completeUpload({
        document_id: session.document_id,
        upload_session_id: session.upload_session_id,
        object_key: resolveDocumentStorageObjectKey(tenantId, session.document_id),
        checksum_sha256: checksum,
        size_bytes: file.size,
        content_type: contentType,
      })

      queryClient.invalidateQueries({ queryKey: documentQueryKeys.all })
      resetUploadState()
      return result
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        resetUploadState()
        throw error
      }
      setStatus('idle')
      throw error
    } finally {
      abortControllerRef.current = null
    }
  }, [queryClient, status, resetUploadState])

  return {
    status,
    progress,
    uploadDocument,
    cancelUpload,
    resetUploadState,
  }
}

// ─── List documents ───────────────────────────────────────────────────────────

export const useDocuments = (params?: DocumentListQueryParams) =>
  useQuery({
    queryKey: documentQueryKeys.list(params),
    queryFn: () => listDocuments(params),
  })

export const useDocumentViewModels = (params?: DocumentListQueryParams) => {
  const query = useDocuments(params)

  return {
    ...query,
    documentViews: query.data?.documents.map(normalizeDocumentRecord) ?? [],
    documents: query.data?.documents ?? [],
  }
}

// ─── Get single document ──────────────────────────────────────────────────────

export const useDocument = (
  documentId: string,
  options?: {
    placeholderData?: DocumentRecordEnvelope | (() => DocumentRecordEnvelope | undefined)
  },
) =>
  useQuery({
    queryKey: documentQueryKeys.detail(documentId),
    queryFn: () => getDocument(documentId),
    enabled: !!documentId,
    placeholderData: options?.placeholderData,
  })

// ─── Download capability ──────────────────────────────────────────────────────

export const useIssueDownloadCapability = (documentId: string) =>
  useMutation({
    mutationFn: () => issueDownloadCapability(documentId),
  })

export const useValidateDownloadCapability = (documentId: string) =>
  useMutation({
    mutationFn: (capabilityToken: string) =>
      validateDownloadCapability(documentId, capabilityToken),
  })

// ─── Lifecycle actions ────────────────────────────────────────────────────────

export const useTrashDocument = (documentId: string) => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (complianceOverrideId?: string) =>
      trashDocument(documentId, complianceOverrideId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentQueryKeys.all })
    },
  })
}

export const useRestoreDocument = (documentId: string) => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (complianceOverrideId?: string) =>
      restoreDocument(documentId, complianceOverrideId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentQueryKeys.all })
    },
  })
}

export const useMarkEligibleForPurge = (documentId: string) => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      purgeEligibleAt,
      complianceOverrideId,
    }: {
      purgeEligibleAt: string
      complianceOverrideId?: string
    }) => markEligibleForPurge(documentId, purgeEligibleAt, complianceOverrideId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentQueryKeys.all })
    },
  })
}

export const usePurgeDryRun = (documentId: string, enabled = true) =>
  useQuery({
    queryKey: documentQueryKeys.purgeDryRun(documentId),
    queryFn: () => purgeDryRun(documentId),
    enabled: !!documentId && enabled,
  })

export const usePurgeDocument = (documentId: string) => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      purgedAt,
      complianceOverrideId,
    }: {
      purgedAt?: string
      complianceOverrideId?: string
    }) => purgeDocument(documentId, purgedAt, complianceOverrideId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentQueryKeys.all })
    },
  })
}

// ─── Compliance overrides ─────────────────────────────────────────────────────

export const useRenameDocument = (documentId: string) => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (params: { display_name: string; expected_revision: number }) =>
      renameDocument(documentId, params),
    onSuccess: (data) => {
      queryClient.setQueryData(documentQueryKeys.detail(documentId), data)
      queryClient.invalidateQueries({ queryKey: documentQueryKeys.list() })
    },
  })
}

export const useRequestComplianceOverride = (documentId: string) =>
  useMutation({
    mutationFn: (params: {
      requested_action: ComplianceOverrideAction
      justification: string
    }) => requestComplianceOverride(documentId, params),
  })

export const useApproveComplianceOverride = (documentId: string) =>
  useMutation({
    mutationFn: ({
      overrideId,
      ...params
    }: {
      overrideId: string
      requested_action: ComplianceOverrideAction
      justification: string
    }) => approveComplianceOverride(documentId, overrideId, params),
  })

export const useRejectComplianceOverride = (documentId: string) =>
  useMutation({
    mutationFn: ({
      overrideId,
      ...params
    }: {
      overrideId: string
      requested_action: ComplianceOverrideAction
      justification: string
    }) => rejectComplianceOverride(documentId, overrideId, params),
  })
