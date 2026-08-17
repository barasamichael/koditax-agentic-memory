import { useState } from 'react'

interface KnowledgeMetadataCorrectionPanelProps {
  ingestionJobId: string
  ingestionState: string
  onCorrect: (params: { note: string; updates: Record<string, unknown> }) => Promise<void>
  busy: boolean
}

const CORRECTABLE_STATES = ['uploaded', 'review_pending', 'approved_for_publication']

const EDITABLE_FIELDS = [
  { key: 'title', label: 'Title', type: 'text' },
  { key: 'tax_domain', label: 'Tax domain', type: 'text' },
  { key: 'issuing_authority', label: 'Issuing authority', type: 'text' },
  { key: 'effective_from', label: 'Effective from (YYYY-MM-DD)', type: 'date' },
  { key: 'effective_to', label: 'Effective to (YYYY-MM-DD, optional)', type: 'date' },
] as const

export function KnowledgeMetadataCorrectionPanel({
  ingestionJobId: _ingestionJobId,
  ingestionState,
  onCorrect,
  busy,
}: KnowledgeMetadataCorrectionPanelProps) {
  const [note, setNote] = useState('')
  const [fields, setFields] = useState<Record<string, string>>({})
  const [submitted, setSubmitted] = useState(false)

  const correctable = CORRECTABLE_STATES.includes(ingestionState)

  if (!correctable) {
    return (
      <div className="rounded-3xl border border-gray-100 bg-gray-50 p-5 text-sm text-gray-500">
        <p className="font-medium text-gray-700">Metadata correction unavailable</p>
        <p className="mt-1">
          Metadata correction is only available before the item is published or rejected.
        </p>
      </div>
    )
  }

  const populatedFields = Object.entries(fields).filter(([, v]) => v.trim() !== '')
  const hasUpdates = populatedFields.length > 0
  const canSubmit = hasUpdates && note.trim() !== '' && !busy

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSubmitted(true)
    if (!canSubmit) return
    const updates: Record<string, unknown> = {}
    for (const [key, value] of populatedFields) {
      updates[key] = value.trim()
    }
    await onCorrect({ note: note.trim(), updates })
    setNote('')
    setFields({})
    setSubmitted(false)
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="space-y-4 rounded-3xl border border-amber-100 bg-amber-50 p-5"
    >
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-amber-700">
          Correct metadata
        </p>
        <p className="mt-1 text-sm text-amber-900">
          Fill in only the fields that need correcting. Leave others blank to keep them unchanged.
          Corrections require a note explaining the reason.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {EDITABLE_FIELDS.map(({ key, label, type }) => (
          <div key={key}>
            <label
              htmlFor={`correction-${key}`}
              className="block text-xs font-medium text-amber-800"
            >
              {label}
            </label>
            <input
              id={`correction-${key}`}
              type={type}
              value={fields[key] ?? ''}
              onChange={(event) =>
                setFields((current) => ({ ...current, [key]: event.target.value }))
              }
              placeholder={type === 'date' ? 'YYYY-MM-DD' : ''}
              className="mt-1 w-full rounded-xl border border-amber-200 bg-white px-3 py-1.5 text-sm text-gray-800 placeholder-gray-400 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
              disabled={busy}
            />
          </div>
        ))}
      </div>

      <div>
        <label
          htmlFor="correction-note"
          className="block text-xs font-medium text-amber-800"
        >
          Reason for correction
        </label>
        <textarea
          id="correction-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          rows={3}
          placeholder="Explain what is being corrected and why..."
          className="mt-1 w-full rounded-xl border border-amber-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-400 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
          disabled={busy}
        />
      </div>

      {submitted && !hasUpdates ? (
        <p className="text-xs text-red-600">Fill in at least one field to apply a correction.</p>
      ) : null}
      {submitted && !note.trim() ? (
        <p className="text-xs text-red-600">A reason note is required before applying corrections.</p>
      ) : null}

      <button
        type="submit"
        disabled={!canSubmit}
        className="rounded-xl border border-amber-300 px-4 py-2 text-sm font-medium text-amber-800 transition-colors hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? 'Applying correction...' : 'Apply correction'}
      </button>
    </form>
  )
}
