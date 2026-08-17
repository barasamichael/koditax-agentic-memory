import { useState } from 'react'
import { Download, ExternalLink } from 'lucide-react'
import { Spinner } from '@/components/shared/Spinner'
import { useToast } from '@/components/shared/Toast'
import { getDocumentErrorMessage } from '@/lib/documents/document-errors'
import { downloadDocumentOriginal, openDocumentPreview } from '@/lib/documents/document-access'
import { cn } from '@/lib/utils'

interface DocumentAccessActionsProps {
  documentId: string
  displayName: string
  canOpen: boolean
  canDownload: boolean
  availabilityMessage?: string
  className?: string
}

export function DocumentAccessActions({
  documentId,
  displayName,
  canOpen,
  canDownload,
  availabilityMessage,
  className,
}: DocumentAccessActionsProps) {
  const toast = useToast()
  const [isOpening, setIsOpening] = useState(false)
  const [isDownloading, setIsDownloading] = useState(false)

  const handleOpen = async () => {
    if (!canOpen || isOpening || isDownloading) return
    setIsOpening(true)
    try {
      await openDocumentPreview({ documentId, displayName })
      toast.success('Document opened in a new tab.')
    } catch (error) {
      toast.error(getDocumentErrorMessage(error))
    } finally {
      setIsOpening(false)
    }
  }

  const handleDownload = async () => {
    if (!canDownload || isOpening || isDownloading) return
    setIsDownloading(true)
    try {
      await downloadDocumentOriginal({ documentId, displayName })
      toast.success('Download started.')
    } catch (error) {
      toast.error(getDocumentErrorMessage(error))
    } finally {
      setIsDownloading(false)
    }
  }

  const buttonClassName = [
    'inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-all',
    'disabled:cursor-not-allowed disabled:opacity-60',
  ].join(' ')

  return (
    <div className={cn('flex flex-wrap items-center gap-2', className)}>
      <button
        type="button"
        onClick={() => void handleOpen()}
        disabled={!canOpen || isOpening || isDownloading}
        className={cn(
          buttonClassName,
          canOpen ? 'border-navy-200 bg-navy-50 text-navy-800 hover:bg-navy-100' : 'border-gray-200 bg-gray-50 text-gray-400',
        )}
        aria-label={`Open ${displayName}`}
      >
        {isOpening ? <Spinner size="sm" className="h-4 w-4" /> : <ExternalLink className="h-4 w-4" aria-hidden="true" />}
        Open
      </button>
      <button
        type="button"
        onClick={() => void handleDownload()}
        disabled={!canDownload || isOpening || isDownloading}
        className={cn(
          buttonClassName,
          canDownload ? 'border-gray-200 bg-white text-gray-700 hover:bg-gray-50' : 'border-gray-200 bg-gray-50 text-gray-400',
        )}
        aria-label={`Download ${displayName}`}
      >
        {isDownloading ? <Spinner size="sm" className="h-4 w-4" /> : <Download className="h-4 w-4" aria-hidden="true" />}
        Download
      </button>
      {!canOpen && !canDownload ? (
        <span className="text-xs text-gray-500">
          {availabilityMessage ?? 'Open and download are unavailable for this document right now.'}
        </span>
      ) : null}
    </div>
  )
}
