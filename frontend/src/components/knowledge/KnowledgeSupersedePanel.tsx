import { useState } from 'react'
import type { KnowledgeSourceVersionSummary } from '@/types/knowledge'
import {
  labelForPublicationState,
  labelForSourceVersionForm,
} from '@/lib/knowledgeStateLabels'

interface KnowledgeSupersedePanelProps {
  currentVersionId: string
  currentFamilyId: string
  currentPublicationState: string
  publishedVersions: KnowledgeSourceVersionSummary[]
  busy: boolean
  onSupersede: (successorId: string) => Promise<void>
}

export function KnowledgeSupersedePanel({
  currentVersionId,
  currentFamilyId,
  currentPublicationState,
  publishedVersions,
  busy,
  onSupersede,
}: KnowledgeSupersedePanelProps) {
  const [selectedSuccessorId, setSelectedSuccessorId] = useState<string>('')
  const [confirmed, setConfirmed] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  // Only published versions from the same family (excluding the current one) are valid successors.
  const eligibleSuccessors = publishedVersions.filter(
    (v) =>
      v.source_family_id === currentFamilyId &&
      v.source_version_id !== currentVersionId &&
      v.publication_state === 'published'
  )

  const canSupersede =
    currentPublicationState === 'published' &&
    selectedSuccessorId !== '' &&
    confirmed &&
    !busy

  if (currentPublicationState !== 'published') {
    return (
      <div className="rounded-3xl border border-gray-100 bg-gray-50 p-5 text-sm text-gray-500">
        <p className="font-medium text-gray-700">Supersede unavailable</p>
        <p className="mt-1">
          Supersede is only available for currently published source versions.
        </p>
      </div>
    )
  }

  if (eligibleSuccessors.length === 0) {
    return (
      <div className="rounded-3xl border border-amber-100 bg-amber-50 p-5 text-sm text-amber-800">
        <p className="font-medium text-amber-900">No eligible successors found</p>
        <p className="mt-1">
          Supersede requires a different published version from the same source family. Publish a
          successor version before superseding this one.
        </p>
      </div>
    )
  }

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSubmitted(true)
    if (!canSupersede) return
    await onSupersede(selectedSuccessorId)
    setSelectedSuccessorId('')
    setConfirmed(false)
    setSubmitted(false)
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="space-y-4 rounded-3xl border border-orange-100 bg-orange-50 p-5"
    >
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-orange-700">
          Supersede this version
        </p>
        <p className="mt-1 text-sm text-orange-900">
          Superseding marks this version as replaced by a newer successor. The superseded version
          remains visible for audit purposes but will no longer be served as active knowledge.
          This action cannot be undone.
        </p>
      </div>

      <div>
        <label
          htmlFor="supersede-successor"
          className="block text-xs font-medium text-orange-800"
        >
          Select the successor version
        </label>
        <select
          id="supersede-successor"
          value={selectedSuccessorId}
          onChange={(event) => {
            setSelectedSuccessorId(event.target.value)
            setConfirmed(false)
          }}
          disabled={busy}
          className="mt-1 w-full rounded-xl border border-orange-200 bg-white px-3 py-2 text-sm text-gray-800 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-orange-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <option value="">— Select a published successor —</option>
          {eligibleSuccessors.map((v) => (
            <option key={v.source_version_id} value={v.source_version_id}>
              {v.title} · {labelForPublicationState(v.publication_state)} ·{' '}
              {labelForSourceVersionForm(v.source_version_form)} · from {v.effective_from}
            </option>
          ))}
        </select>
      </div>

      {selectedSuccessorId ? (
        <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-orange-200 bg-white p-3 text-sm text-orange-900">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            disabled={busy}
            className="mt-0.5 h-4 w-4 rounded border-orange-300 accent-orange-600"
          />
          <span>
            I confirm that the selected version is the correct successor and that this supersession
            is authorised. This action is permanent.
          </span>
        </label>
      ) : null}

      {submitted && !selectedSuccessorId ? (
        <p className="text-xs text-red-600">Select a successor version before proceeding.</p>
      ) : null}
      {submitted && selectedSuccessorId && !confirmed ? (
        <p className="text-xs text-red-600">Confirm the supersession before proceeding.</p>
      ) : null}

      <button
        type="submit"
        disabled={!canSupersede}
        className="rounded-xl border border-orange-300 px-4 py-2 text-sm font-medium text-orange-800 transition-colors hover:bg-orange-100 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? 'Superseding...' : 'Supersede this version'}
      </button>
    </form>
  )
}
