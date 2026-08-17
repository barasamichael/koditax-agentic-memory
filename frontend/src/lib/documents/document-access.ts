import { issueDownloadCapability } from '@/api/document.api'
import { formatDocumentName } from './document-formatters'
import { getDocumentErrorMessage } from './document-errors'
import { normalizeError } from '@/lib/errorNormalizer'
import type { SignedDownloadCapability } from '@/types/document'

const DOCUMENT_ACCESS_UNAVAILABLE_MESSAGE =
  'The document is temporarily unavailable. Please try again.'

const DOCUMENT_PREVIEW_BLOCKED_MESSAGE =
  'Your browser blocked the document preview. Allow pop-ups and try again.'

type DocumentAccessRequest = {
  documentId: string
  displayName?: string | null
}

const fetchDocumentBinary = async (
  capability: SignedDownloadCapability,
): Promise<Blob> => {
  const response = await fetch(capability.download_url, {
    method: capability.method,
    headers: capability.headers,
    mode: 'cors',
    credentials: 'omit',
    referrerPolicy: 'no-referrer',
  })

  if (!response.ok) {
    throw new Error(DOCUMENT_ACCESS_UNAVAILABLE_MESSAGE)
  }

  return response.blob()
}

const createPreviewWindow = (): Window | null => {
  const previewWindow = window.open('', '_blank', 'noopener,noreferrer')
  if (previewWindow === null) return null
  return previewWindow
}

const createDownloadAnchor = (url: string, fileName: string): HTMLAnchorElement => {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.rel = 'noopener noreferrer'
  anchor.style.display = 'none'
  return anchor
}

const buildDownloadName = (displayName?: string | null): string => {
  const normalized = formatDocumentName(displayName)
  return normalized === 'Untitled document' ? 'document.pdf' : normalized
}

const issueDocumentCapability = async (documentId: string) =>
  issueDownloadCapability(documentId)

export const openDocumentPreview = async ({
  documentId,
  displayName,
}: DocumentAccessRequest): Promise<void> => {
  try {
    const issuedCapability = await issueDocumentCapability(documentId)
    const blob = await fetchDocumentBinary(issuedCapability.capability)
    const objectUrl = URL.createObjectURL(blob)
    const previewWindow = createPreviewWindow()

    if (previewWindow === null) {
      URL.revokeObjectURL(objectUrl)
      throw new Error(DOCUMENT_PREVIEW_BLOCKED_MESSAGE)
    }

    previewWindow.location.href = objectUrl
    previewWindow.document.title = formatDocumentName(displayName)
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
  } catch (error) {
    const normalized = normalizeError(error)
    throw new Error(
      getDocumentErrorMessage(error) ||
        normalized.message ||
        DOCUMENT_ACCESS_UNAVAILABLE_MESSAGE,
    )
  }
}

export const downloadDocumentOriginal = async ({
  documentId,
  displayName,
}: DocumentAccessRequest): Promise<void> => {
  try {
    const issuedCapability = await issueDocumentCapability(documentId)
    const blob = await fetchDocumentBinary(issuedCapability.capability)
    const objectUrl = URL.createObjectURL(blob)
    const anchor = createDownloadAnchor(objectUrl, buildDownloadName(displayName))

    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
  } catch (error) {
    const normalized = normalizeError(error)
    throw new Error(
      getDocumentErrorMessage(error) ||
        normalized.message ||
        DOCUMENT_ACCESS_UNAVAILABLE_MESSAGE,
    )
  }
}
