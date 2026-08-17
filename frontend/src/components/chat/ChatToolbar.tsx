import { useState, useCallback, useRef, useEffect } from 'react'
import { Download, Share2, Check, ChevronDown, FileText, File, Code, Trash2, PencilLine } from 'lucide-react'
import type { ChatConversation } from '@/types/chat'
import { cn } from '@/lib/utils'
import { createShare } from '@/lib/shareStore'
import { downloadPdf, downloadMarkdown, downloadDocx } from '@/lib/chatExport'
import type { ExportFormat } from '@/lib/chatExport'
import {
  deleteConversation as deleteConversationApi,
  renameConversation as renameConversationApi,
} from '@/api/orchestration.api'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useToast } from '@/components/shared/Toast'
import { ConfirmModal } from '@/components/shared/ConfirmModal'
import { ConversationRenameModal } from '@/components/chat/ConversationRenameModal'
import { normalizeError } from '@/lib/errorNormalizer'

interface ChatToolbarProps {
  conversation: ChatConversation | null
}

const FORMAT_OPTIONS: { id: ExportFormat; label: string; description: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: 'pdf', label: 'PDF', description: 'Best for printing or sharing', icon: FileText },
  { id: 'docx', label: 'Word document', description: 'Editable in Microsoft Word', icon: File },
  { id: 'markdown', label: 'Markdown', description: 'Plain text with formatting', icon: Code },
]

export function ChatToolbar({ conversation }: ChatToolbarProps) {
  const [shareCopied, setShareCopied] = useState(false)
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const [downloading, setDownloading] = useState<ExportFormat | null>(null)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [renameDialogOpen, setRenameDialogOpen] = useState(false)
  const [renameDialogBusy, setRenameDialogBusy] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const userId = useAuthStore((state) => state.userId ?? '')
  const deleteConversation = useChatStore((state) => state.deleteConversation)
  const renameConversation = useChatStore((state) => state.renameConversation)
  const toast = useToast()

  useEffect(() => {
    if (!dropdownOpen) return
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [dropdownOpen])

  const handleDownload = useCallback(async (format: ExportFormat) => {
    if (!conversation) return
    setDownloading(format)
    setDropdownOpen(false)
    try {
      if (format === 'pdf') downloadPdf(conversation)
      else if (format === 'docx') await downloadDocx(conversation)
      else downloadMarkdown(conversation)
    } finally {
      setDownloading(null)
    }
  }, [conversation])

  const handleShare = useCallback(() => {
    if (!conversation) return
    const id = createShare(conversation)
    const url = `${window.location.origin}/chat/shared/${id}`
    navigator.clipboard.writeText(url).then(() => {
      setShareCopied(true)
      setTimeout(() => setShareCopied(false), 2500)
    })
  }, [conversation])

  const handleDelete = useCallback(() => {
    if (!conversation || !userId) return
    setDeleteDialogOpen(true)
  }, [conversation, userId])

  const handleRename = useCallback(() => {
    if (!conversation || !userId) return
    setRenameDialogOpen(true)
  }, [conversation, userId])

  const handleConfirmDelete = useCallback(() => {
    if (!conversation || !userId) return
    void (async () => {
      try {
        await deleteConversationApi(conversation.conversationId)
        deleteConversation(userId, conversation.conversationId)
        toast.success('Chat deleted.')
        setDeleteDialogOpen(false)
      } catch (error) {
        const canonical = normalizeError(error)
        console.error('[chat] delete current conversation failed', {
          error,
          canonical,
        })
        toast.error(
          canonical.error_code === 'UNKNOWN'
            ? 'Could not delete this chat. Please try again.'
            : canonical.message
        )
      }
    })()
  }, [conversation, deleteConversation, toast, userId])

  const handleConfirmRename = useCallback(
    (title: string) => {
      if (!conversation || !userId) return
      setRenameDialogBusy(true)
      void (async () => {
        try {
          const response = await renameConversationApi(conversation.conversationId, title)
          renameConversation(userId, conversation.conversationId, response.conversation_title)
          toast.success('Chat renamed.')
          setRenameDialogOpen(false)
        } catch (error) {
          const canonical = normalizeError(error)
          console.error('[chat] rename current conversation failed', {
            error,
            canonical,
          })
          toast.error(
            canonical.error_code === 'UNKNOWN'
            ? 'Could not rename this chat. Please try again.'
            : canonical.message
          )
        } finally {
          setRenameDialogBusy(false)
        }
      })()
    },
    [conversation, renameConversation, toast, userId]
  )

  if (!conversation) return null

  const hasMessages = conversation.messages.length > 0

  return (
    <div className="flex items-center gap-1">
      {hasMessages && (
        <div className="relative" ref={dropdownRef}>
          <div className="flex items-center">
            <button
              onClick={() => handleDownload('pdf')}
              disabled={!!downloading}
              title="Download as PDF"
              className={cn(
                'flex items-center gap-1.5 rounded-l-lg border-r border-gray-200 px-2.5 py-1.5 text-xs font-medium transition-colors',
                'text-gray-500 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-50'
              )}
            >
              <Download className={cn('h-3.5 w-3.5', downloading === 'pdf' && 'animate-bounce')} />
              <span className="hidden sm:inline">Download</span>
            </button>
            <button
              onClick={() => setDropdownOpen((o) => !o)}
              title="Choose format"
              className={cn(
                'flex items-center rounded-r-lg px-1.5 py-1.5 text-xs font-medium transition-colors',
                dropdownOpen
                  ? 'bg-gray-100 text-gray-700'
                  : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600'
              )}
            >
              <ChevronDown className={cn('h-3 w-3 transition-transform', dropdownOpen && 'rotate-180')} />
            </button>
          </div>

          {dropdownOpen && (
            <div className="absolute right-0 top-10 z-50 w-56 overflow-hidden rounded-xl border border-gray-100 bg-white shadow-xl">
              <p className="border-b border-gray-50 px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                Download as
              </p>
              {FORMAT_OPTIONS.map((opt) => {
                const Icon = opt.icon
                const isLoading = downloading === opt.id
                return (
                  <button
                    key={opt.id}
                    onClick={() => handleDownload(opt.id)}
                    disabled={!!downloading}
                    className="flex w-full items-start gap-3 px-3 py-2.5 text-left transition-colors hover:bg-gray-50 disabled:opacity-50"
                  >
                    <Icon className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" />
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-gray-800">
                        {opt.label}
                        {opt.id === 'pdf' && (
                          <span className="ml-1.5 rounded bg-navy-50 px-1.5 py-0.5 text-[10px] font-semibold text-navy-700">
                            Default
                          </span>
                        )}
                      </p>
                      <p className="text-[11px] text-gray-400">{isLoading ? 'Preparing…' : opt.description}</p>
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}

      {hasMessages && (
        <button
          onClick={handleShare}
          title="Copy share link"
          className={cn(
            'flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors',
            shareCopied
              ? 'bg-emerald-50 text-emerald-600'
              : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700'
          )}
        >
          {shareCopied ? (
            <>
              <Check className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Link copied</span>
            </>
          ) : (
            <>
              <Share2 className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Share</span>
            </>
          )}
        </button>
      )}

      <button
        onClick={handleRename}
        title="Rename chat"
        className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700"
      >
        <PencilLine className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">Rename</span>
      </button>

      <button
        onClick={handleDelete}
        title="Delete chat"
        className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-gray-500 transition-colors hover:bg-red-50 hover:text-red-600"
      >
        <Trash2 className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">Delete</span>
      </button>

      <ConfirmModal
        open={deleteDialogOpen}
        onOpenChange={setDeleteDialogOpen}
        title="Delete conversation?"
        description={`Delete "${conversation.title}" from the backend and remove it from your sidebar? This cannot be undone from the chat history.`}
        confirmLabel="Delete chat"
        cancelLabel="Cancel"
        variant="danger"
        onConfirm={handleConfirmDelete}
      />

      <ConversationRenameModal
        open={renameDialogOpen}
        onOpenChange={setRenameDialogOpen}
        currentTitle={conversation.title}
        loading={renameDialogBusy}
        onRename={handleConfirmRename}
      />
    </div>
  )
}
