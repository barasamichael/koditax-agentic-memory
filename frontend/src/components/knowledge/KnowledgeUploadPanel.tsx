import { useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { labelForBulkStatus } from '@/lib/knowledgeStateLabels'
import type { KnowledgeBulkIngestionResult, KnowledgeSourceClass } from '@/types/knowledge'

const SOURCE_CLASS_OPTIONS: Array<{ value: KnowledgeSourceClass; label: string }> = [
  { value: 'tax_law', label: 'Tax Law' },
  { value: 'regulation', label: 'Regulation' },
  { value: 'guidance', label: 'Guidance' },
  { value: 'commentary', label: 'Commentary' },
]

const ACCEPTED_FILE_TYPES = [
  '.pdf',
  '.html',
  '.txt',
  '.md',
  '.docx',
  '.xml',
  'application/pdf',
  'text/html',
  'text/plain',
  'text/markdown',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/xml',
].join(',')

interface KnowledgeUploadPanelProps {
  busy: boolean
  result: KnowledgeBulkIngestionResult | null
  onUploadFile: (file: File, sourceClass: KnowledgeSourceClass) => Promise<void>
  onBulkUploadFiles: (files: File[], sourceClass: KnowledgeSourceClass) => Promise<void>
  onUploadUrl: (url: string, sourceClass: KnowledgeSourceClass) => Promise<void>
  onBulkUploadUrls: (urls: string[], sourceClass: KnowledgeSourceClass) => Promise<void>
}

export function KnowledgeUploadPanel({
  busy,
  result,
  onUploadFile,
  onBulkUploadFiles,
  onUploadUrl,
  onBulkUploadUrls,
}: KnowledgeUploadPanelProps) {
  const [mode, setMode] = useState<'file' | 'url'>('file')
  const [file, setFile] = useState<File | null>(null)
  const [files, setFiles] = useState<File[]>([])
  const [url, setUrl] = useState('')
  const [urlsText, setUrlsText] = useState('')
  const [sourceClass, setSourceClass] = useState<KnowledgeSourceClass>('tax_law')
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const bulkFileInputRef = useRef<HTMLInputElement | null>(null)

  const parsedUrls = urlsText
    .split('\n')
    .map((value) => value.trim())
    .filter(Boolean)

  const resultStatusColor =
    result?.bulk_status === 'full_success'
      ? 'border-green-100 bg-green-50'
      : result?.bulk_status === 'partial_failure'
        ? 'border-amber-100 bg-amber-50'
        : result?.bulk_status === 'full_rejection'
          ? 'border-red-100 bg-red-50'
          : 'border-gray-100 bg-gray-50'

  return (
    <div className="space-y-4 rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-widest text-gray-400">
          New upload
        </p>
        <p className="mt-1 text-sm text-gray-500">
          Add a file or official source URL to create a new knowledge ingestion job.
        </p>
      </div>

      <div className="flex gap-2 rounded-lg bg-gray-100 p-1">
        <button
          type="button"
          onClick={() => setMode('file')}
          className={cn(
            'flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors',
            mode === 'file'
              ? 'bg-white text-navy-900 shadow-sm'
              : 'text-gray-500 hover:text-gray-700'
          )}
        >
          File upload
        </button>
        <button
          type="button"
          onClick={() => setMode('url')}
          className={cn(
            'flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors',
            mode === 'url'
              ? 'bg-white text-navy-900 shadow-sm'
              : 'text-gray-500 hover:text-gray-700'
          )}
        >
          URL import
        </button>
      </div>

      <div>
        <label className="block text-[11px] font-semibold uppercase tracking-widest text-gray-400">
          Source type
        </label>
        <select
          value={sourceClass}
          onChange={(event) => setSourceClass(event.target.value as KnowledgeSourceClass)}
          disabled={busy}
          className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30 disabled:opacity-50"
        >
          {SOURCE_CLASS_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      {mode === 'file' ? (
        <div className="space-y-3">
          <div className="grid gap-3 xl:grid-cols-2">
            <div className="space-y-3 rounded-xl border border-gray-100 p-4">
              <label className="block text-[11px] font-semibold uppercase tracking-widest text-gray-400">
                Single file
              </label>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy}
                className="w-full rounded-xl border border-dashed border-gray-300 bg-gray-50 px-4 py-6 text-left transition-colors hover:border-gray-400 hover:bg-gray-50/80 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <p className="text-sm font-medium text-gray-700">
                  {file ? file.name : 'Choose a supported file'}
                </p>
                <p className="mt-1 text-xs text-gray-400">
                  PDF, HTML, TXT, Markdown, DOCX, or XML
                </p>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPTED_FILE_TYPES}
                className="hidden"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
              <button
                type="button"
                onClick={async () => {
                  if (!file) return
                  try {
                    await onUploadFile(file, sourceClass)
                    setFile(null)
                    if (fileInputRef.current) fileInputRef.current.value = ''
                  } catch {}
                }}
                disabled={!file || busy}
                className="w-full rounded-lg bg-navy-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-navy-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? 'Uploading…' : 'Create upload job'}
              </button>
            </div>

            <div className="space-y-3 rounded-xl border border-gray-100 p-4">
              <label className="block text-[11px] font-semibold uppercase tracking-widest text-gray-400">
                Bulk files
              </label>
              <button
                type="button"
                onClick={() => bulkFileInputRef.current?.click()}
                disabled={busy}
                className="w-full rounded-xl border border-dashed border-gray-300 bg-gray-50 px-4 py-6 text-left transition-colors hover:border-gray-400 hover:bg-gray-50/80 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <p className="text-sm font-medium text-gray-700">
                  {files.length > 0
                    ? `${files.length} file${files.length === 1 ? '' : 's'} selected`
                    : 'Choose multiple supported files'}
                </p>
                <p className="mt-1 text-xs text-gray-400">
                  Each file becomes its own ingestion job.
                </p>
              </button>
              <input
                ref={bulkFileInputRef}
                type="file"
                multiple
                accept={ACCEPTED_FILE_TYPES}
                className="hidden"
                onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
              />
              {files.length > 0 ? (
                <div className="max-h-32 space-y-1 overflow-y-auto rounded-lg bg-gray-50 px-3 py-2">
                  {files.map((selectedFile) => (
                    <p key={`${selectedFile.name}-${selectedFile.size}`} className="truncate text-[12px] text-gray-500">
                      {selectedFile.name}
                    </p>
                  ))}
                </div>
              ) : null}
              <button
                type="button"
                onClick={async () => {
                  if (files.length === 0) return
                  try {
                    await onBulkUploadFiles(files, sourceClass)
                    setFiles([])
                    if (bulkFileInputRef.current) bulkFileInputRef.current.value = ''
                  } catch {}
                }}
                disabled={files.length === 0 || busy}
                className="w-full rounded-lg border border-navy-200 px-4 py-2.5 text-sm font-medium text-navy-800 transition-colors hover:bg-navy-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? 'Uploading…' : `Create ${files.length || ''} bulk job${files.length === 1 ? '' : 's'}`}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-3 xl:grid-cols-2">
            <div className="space-y-3 rounded-xl border border-gray-100 p-4">
              <div>
                <label className="block text-[11px] font-semibold uppercase tracking-widest text-gray-400">
                  Single URL
                </label>
                <input
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  disabled={busy}
                  placeholder="https://..."
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 placeholder-gray-400 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30 disabled:opacity-50"
                />
              </div>
              <button
                type="button"
                onClick={async () => {
                  if (!url.trim()) return
                  try {
                    await onUploadUrl(url.trim(), sourceClass)
                    setUrl('')
                  } catch {}
                }}
                disabled={!url.trim() || busy}
                className="w-full rounded-lg bg-navy-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-navy-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? 'Submitting…' : 'Create URL job'}
              </button>
            </div>

            <div className="space-y-3 rounded-xl border border-gray-100 p-4">
              <div>
                <label className="block text-[11px] font-semibold uppercase tracking-widest text-gray-400">
                  Bulk URLs
                </label>
                <textarea
                  value={urlsText}
                  onChange={(event) => setUrlsText(event.target.value)}
                  disabled={busy}
                  rows={5}
                  placeholder={'https://example.com/one\nhttps://example.com/two'}
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 placeholder-gray-400 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30 disabled:opacity-50"
                />
                <p className="mt-1 text-[11px] text-gray-400">
                  Enter one URL per line.
                </p>
              </div>
              <button
                type="button"
                onClick={async () => {
                  if (parsedUrls.length === 0) return
                  try {
                    await onBulkUploadUrls(parsedUrls, sourceClass)
                    setUrlsText('')
                  } catch {}
                }}
                disabled={parsedUrls.length === 0 || busy}
                className="w-full rounded-lg border border-navy-200 px-4 py-2.5 text-sm font-medium text-navy-800 transition-colors hover:bg-navy-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? 'Submitting…' : `Create ${parsedUrls.length || ''} bulk URL job${parsedUrls.length === 1 ? '' : 's'}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {result ? (
        <div className={cn('space-y-3 rounded-xl border p-4', resultStatusColor)}>
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-widest text-gray-400">
              Last ingestion result
            </p>
            <p className="mt-1 text-sm font-medium text-gray-700">
              {labelForBulkStatus(result.bulk_status)} — {result.total}{' '}
              {result.total === 1 ? 'item' : 'items'} processed
            </p>
          </div>
          <div className="space-y-1.5">
            {result.items.map((item) => (
              <div
                key={`${item.index}-${item.idempotency_key}`}
                className={cn(
                  'rounded-lg border bg-white p-3',
                  item.status === 'ok' ? 'border-green-100' : 'border-red-100'
                )}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-[12px] font-medium text-gray-700">
                    Item {item.index + 1}
                  </p>
                  <span
                    className={cn(
                      'rounded-md px-2 py-0.5 text-[11px] font-semibold',
                      item.status === 'ok'
                        ? 'bg-green-50 text-green-700 ring-1 ring-green-200'
                        : 'bg-red-50 text-red-600 ring-1 ring-red-200'
                    )}
                  >
                    {item.status === 'ok' ? 'Succeeded' : 'Failed'}
                  </span>
                </div>
                <p className="mt-1 text-[12px] text-gray-500">{item.outcome}</p>
                {item.ingestion_job_id ? (
                  <p className="mt-0.5 break-all text-[11px] font-mono text-gray-400">
                    {item.ingestion_job_id}
                  </p>
                ) : null}
                {item.reason ? (
                  <p className="mt-0.5 text-[12px] text-gray-500">{item.reason}</p>
                ) : null}
                {item.error_code ? (
                  <p className="mt-0.5 text-[12px] font-mono text-red-600">{item.error_code}</p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  )
}
