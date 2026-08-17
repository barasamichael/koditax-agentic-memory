import { useEffect, useRef, useState } from 'react'
import { CheckCircle2, FileText, Upload, X } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { useToast } from '@/components/shared/Toast'
import { Spinner } from '@/components/shared/Spinner'
import { cn } from '@/lib/utils'
import { formatFileSize } from '@/lib/documents/document-formatters'
import { documentQueryKeys } from '@/lib/documents/document-query-keys'
import { getDocumentErrorMessage } from '@/lib/documents/document-errors'
import { useUploadDocument } from '@/hooks/useDocuments'

const MAX_SIZE_BYTES = 200 * 1024 * 1024
const ACCEPTED_EXTENSIONS = '.pdf,.docx,.odt,.rtf,.xlsx,.ods,.csv,.tsv,.pptx,.odp,.txt,.md,.json,.xml,.png,.jpg,.jpeg,.webp,.tif,.tiff'
const ACCEPTED_EXTENSION_SET = new Set(ACCEPTED_EXTENSIONS.split(',').map(value => value.slice(1)))
const REQUIRED_HEADERS = [
  'PDFs, Word documents, spreadsheets, presentations, text files, and images',
  'maximum 200 MB',
  'saved directly to your document library',
] as const

interface UploadZoneProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

function formatProgress(progressBytes: { loadedBytes: number; totalBytes: number } | null) {
  if (!progressBytes) return null
  const loaded = formatFileSize(progressBytes.loadedBytes) ?? `${progressBytes.loadedBytes} B`
  const total = formatFileSize(progressBytes.totalBytes) ?? `${progressBytes.totalBytes} B`
  return `${loaded} of ${total}`
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === 'AbortError'
}

export function UploadZone({ open, onOpenChange }: UploadZoneProps) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const dialogRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [validationError, setValidationError] = useState<string | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const {
    status,
    progress,
    uploadDocument,
    cancelUpload,
    resetUploadState,
  } = useUploadDocument()

  const isPending = status !== 'idle'
  const hasFile = selectedFile !== null
  const isUploading = status === 'uploading'
  const isPreparing = status === 'computing_checksum' || status === 'creating_session'
  const isCompleting = status === 'completing'

  useEffect(() => {
    if (!open) return
    const timeout = window.setTimeout(() => {
      fileInputRef.current?.focus()
    }, 0)
    return () => window.clearTimeout(timeout)
  }, [open])

  useEffect(() => {
    if (!open) {
      setSelectedFile(null)
      setValidationError(null)
      setUploadError(null)
      resetUploadState()
    }
  }, [open, resetUploadState])

  useEffect(() => {
    if (!isPending) return
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [isPending])

  useEffect(() => {
    if (!open) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      handleClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isPending])

  const validateFile = (file: File): string | null => {
    const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
    if (!ACCEPTED_EXTENSION_SET.has(extension)) {
      return 'This file type is not supported.'
    }
    if (file.size > MAX_SIZE_BYTES) {
      return 'This file is larger than the 200 MB limit.'
    }
    return null
  }

  const setFile = (file: File | null) => {
    if (!file) {
      setSelectedFile(null)
      setValidationError(null)
      setUploadError(null)
      resetUploadState()
      return
    }

    const error = validateFile(file)
    if (error) {
      setSelectedFile(null)
      setValidationError(error)
      setUploadError(null)
      resetUploadState()
      return
    }

    setSelectedFile(file)
    setValidationError(null)
    setUploadError(null)
    resetUploadState()
  }

  const closeAndReset = () => {
    if (isPending) {
      cancelUpload()
    }
    setSelectedFile(null)
    setValidationError(null)
    setUploadError(null)
    resetUploadState()
    onOpenChange(false)
  }

  const handleClose = () => {
    closeAndReset()
  }

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    if (isPending) return
    const file = event.dataTransfer.files[0]
    if (file) setFile(file)
  }

  const handleInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null
    setFile(file)
    event.target.value = ''
  }

  const handleUpload = async () => {
    if (!selectedFile || isPending) return

    try {
      setUploadError(null)
      await uploadDocument(selectedFile)
      await queryClient.invalidateQueries({ queryKey: documentQueryKeys.all })
      toast.success('Document added to your library.')
      closeAndReset()
    } catch (error) {
      if (isAbortError(error)) {
        return
      }
      setUploadError(getDocumentErrorMessage(error))
    }
  }

  if (!open) return null

  const progressLabel = formatProgress(progress)
  const uploadStatusLabel = isPreparing
    ? 'Checking file...'
    : isUploading
      ? 'Uploading file...'
      : isCompleting
        ? 'Saving to your library...'
        : 'Add document'

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-document-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onMouseDown={(event) => {
        if (event.target === dialogRef.current) {
          handleClose()
        }
      }}
    >
      <div className="mx-4 w-full max-w-xl overflow-hidden rounded-2xl bg-white shadow-2xl">
        <div className="flex items-start justify-between border-b border-gray-100 px-6 py-4">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
              Document library
            </p>
            <h2 id="add-document-title" className="mt-1 text-base font-semibold text-gray-900">
              Add document
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-gray-500">
              Add a file from your device and save it directly to your document library.
            </p>
          </div>
          <button
            type="button"
            onClick={handleClose}
            className="text-gray-400 transition-colors hover:text-gray-600 disabled:opacity-50"
            aria-label={isPending ? 'Cancel upload' : 'Close dialog'}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-5 px-6 py-5">
          <div className="rounded-2xl border border-gray-100 bg-gray-50/70 p-4">
            <div className="flex items-center gap-2">
              <Upload className="h-4 w-4 text-navy-700" aria-hidden="true" />
              <p className="text-sm font-medium text-gray-800">Before you choose a file</p>
            </div>
            <ul className="mt-3 space-y-2 text-sm text-gray-600">
              {REQUIRED_HEADERS.map((item) => (
                <li key={item} className="flex items-center gap-2">
                  <CheckCircle2 className="h-3.5 w-3.5 text-green-600" aria-hidden="true" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </div>

          <div
            className={cn(
              'cursor-pointer rounded-2xl border-2 border-dashed p-8 text-center transition-colors',
              isPending
                ? 'cursor-not-allowed border-gray-200 bg-gray-50'
                : 'border-gray-200 hover:border-navy-300 hover:bg-navy-50/40'
            )}
            onDragOver={(event) => {
              event.preventDefault()
            }}
            onDrop={handleDrop}
            onClick={() => {
              if (!isPending) fileInputRef.current?.click()
            }}
          >
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-white text-gray-400 shadow-sm">
              <FileText className="h-6 w-6" aria-hidden="true" />
            </div>
            <p className="mt-4 text-sm font-medium text-gray-700">
              {hasFile ? selectedFile?.name : 'Drag a document here or click to browse'}
            </p>
            <p className="mt-1 text-xs text-gray-500">Supported documents, maximum 200 MB.</p>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED_EXTENSIONS}
              className="hidden"
              onChange={handleInputChange}
              disabled={isPending}
            />
          </div>

          {selectedFile ? (
            <div className="flex items-center gap-3 rounded-2xl border border-gray-100 bg-white px-4 py-3">
              <FileText className="h-5 w-5 shrink-0 text-navy-700" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-gray-900">{selectedFile.name}</p>
                <p className="text-xs text-gray-500">{formatFileSize(selectedFile.size)}</p>
              </div>
              {!isPending ? (
                <button
                  type="button"
                  onClick={() => setFile(null)}
                  className="text-gray-400 transition-colors hover:text-gray-600"
                  aria-label="Remove selected file"
                >
                  <X className="h-4 w-4" />
                </button>
              ) : null}
            </div>
          ) : null}

          {validationError ? (
            <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {validationError}
            </div>
          ) : null}

          {uploadError ? (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              {uploadError}
            </div>
          ) : null}

          {isPending ? (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs font-medium text-gray-500">
                <span>{uploadStatusLabel}</span>
                {progressLabel ? <span>{progress?.percent ?? 0}%</span> : null}
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-gray-100">
                {isUploading && progress ? (
                  <div
                    className="h-full rounded-full bg-navy-700 transition-all"
                    style={{ width: `${progress.percent}%` }}
                  />
                ) : (
                  <div className="h-full w-full animate-pulse rounded-full bg-navy-700" />
                )}
              </div>
              {progressLabel ? (
                <p className="text-xs text-gray-500">{progressLabel}</p>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="flex items-center justify-end gap-3 border-t border-gray-100 px-6 py-4">
          <button
            type="button"
            onClick={handleClose}
            className="rounded-lg border border-gray-200 px-4 py-2 text-sm text-gray-700 transition-all hover:bg-gray-50"
          >
            {isPending ? 'Cancel upload' : 'Cancel'}
          </button>
          <button
            type="button"
            onClick={handleUpload}
            disabled={!selectedFile || isPending}
            className="inline-flex items-center gap-2 rounded-lg bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-navy-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isPending ? <Spinner size="sm" className="h-3.5 w-3.5" /> : null}
            {uploadStatusLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
