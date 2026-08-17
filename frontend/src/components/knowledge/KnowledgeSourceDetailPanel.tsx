import type {
  KnowledgeSourceDetail,
  KnowledgeSourceVersionLifecycle,
  KnowledgeSourceVersionSummary,
} from '@/types/knowledge'
import {
  labelForAuthorityLevel,
  labelForPublicationState,
  labelForSourceClass,
  labelForSourceVersionForm,
} from '@/lib/knowledgeStateLabels'

interface KnowledgeSourceDetailPanelProps {
  mode: 'sourceVersion' | 'source'
  sourceVersionSummary?: KnowledgeSourceVersionSummary | null
  sourceVersionDetail?: KnowledgeSourceVersionLifecycle | null
  sourceDetail?: KnowledgeSourceDetail | null
  loading: boolean
  error: string | null
}

export function KnowledgeSourceDetailPanel({
  mode,
  sourceVersionSummary,
  sourceVersionDetail,
  sourceDetail,
  loading,
  error,
}: KnowledgeSourceDetailPanelProps) {
  if (loading) {
    return (
      <div className="rounded-3xl border border-gray-200 bg-white p-6 text-sm text-gray-500">
        Loading detail...
      </div>
    )
  }

  if (error) {
    return (
      <div className="rounded-3xl border border-red-200 bg-red-50 p-6 text-sm text-red-700">
        {error}
      </div>
    )
  }

  if (mode === 'sourceVersion') {
    if (!sourceVersionSummary) {
      return (
        <div className="rounded-3xl border border-gray-200 bg-white p-6 text-sm text-gray-500">
          Select a published source from the list to inspect its lifecycle state and archive readiness.
        </div>
      )
    }

    const lifecycle = sourceVersionDetail ?? sourceVersionSummary

    return (
      <div className="space-y-5 rounded-3xl border border-gray-200 bg-white p-6 shadow-card">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Published source detail
          </p>
          <h2 className="mt-1 text-lg font-semibold text-navy-900">{sourceVersionSummary.title}</h2>
          <div className="mt-2 flex flex-wrap gap-2 text-xs text-gray-600">
            <span className="rounded-full bg-gray-100 px-2 py-1 font-medium">
              {labelForPublicationState(sourceVersionSummary.publication_state)}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-1">
              {labelForSourceVersionForm(sourceVersionSummary.source_version_form)}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-1">
              {labelForSourceClass(sourceVersionSummary.source_class)}
            </span>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Tax domain</p>
            <p className="mt-1 text-sm text-gray-700">{sourceVersionSummary.tax_domain}</p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
              Authority level
            </p>
            <p className="mt-1 text-sm text-gray-700">
              {labelForAuthorityLevel(sourceVersionSummary.authority_level)}
            </p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
              Effective window
            </p>
            <p className="mt-1 text-sm text-gray-700">
              {lifecycle.effective_from}
              {lifecycle.effective_to ? ` to ${lifecycle.effective_to}` : ' onward'}
            </p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Tax year</p>
            <p className="mt-1 text-sm text-gray-700">{lifecycle.tax_year ?? 'Open scope'}</p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Lineage</p>
            <p className="mt-1 text-sm text-gray-700">
              {lifecycle.supersedes_source_version_id
                ? 'Supersedes a prior version in this family'
                : 'No recorded predecessor'}
            </p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
              Successor
            </p>
            <p className="mt-1 text-sm text-gray-700">
              {lifecycle.superseded_by_source_version_id
                ? 'Superseded by a newer version in this family'
                : 'No recorded successor'}
            </p>
          </div>
        </div>
      </div>
    )
  }

  if (!sourceDetail) {
    return (
      <div className="rounded-3xl border border-gray-200 bg-white p-6 text-sm text-gray-500">
        Select a source to inspect its version history, coverage, and retention summary.
      </div>
    )
  }

  return (
    <div className="space-y-5 rounded-3xl border border-gray-200 bg-white p-6 shadow-card">
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Source detail</p>
        <h2 className="mt-1 text-lg font-semibold text-navy-900">{sourceDetail.title}</h2>
        <div className="mt-2 flex flex-wrap gap-2 text-xs text-gray-600">
          <span className="rounded-full bg-gray-100 px-2 py-1 font-medium">
            {labelForSourceClass(sourceDetail.source_class)}
          </span>
          <span className="rounded-full bg-gray-100 px-2 py-1">{sourceDetail.tax_domain}</span>
          <span className="rounded-full bg-gray-100 px-2 py-1">
            {labelForAuthorityLevel(sourceDetail.authority_level)}
          </span>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Source id</p>
          <p className="mt-1 break-all text-sm text-gray-700">{sourceDetail.source_id}</p>
        </div>
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Source family id
          </p>
          <p className="mt-1 break-all text-sm text-gray-700">{sourceDetail.source_family_id}</p>
        </div>
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Canonical URL
          </p>
          <a
            href={sourceDetail.canonical_url}
            target="_blank"
            rel="noreferrer"
            className="mt-1 block break-all text-sm text-navy-700 underline decoration-dotted underline-offset-2"
          >
            {sourceDetail.canonical_url}
          </a>
        </div>
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Issuing authority
          </p>
          <p className="mt-1 text-sm text-gray-700">{sourceDetail.issuing_authority}</p>
        </div>
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Coverage</p>
          <p className="mt-1 text-sm text-gray-700">
            {sourceDetail.version_count} version{sourceDetail.version_count === 1 ? '' : 's'} ·{' '}
            {sourceDetail.anchor_count} anchor{sourceDetail.anchor_count === 1 ? '' : 's'} ·{' '}
            {sourceDetail.chunk_count} chunk{sourceDetail.chunk_count === 1 ? '' : 's'}
          </p>
        </div>
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Retired at
          </p>
          <p className="mt-1 text-sm text-gray-700">{sourceDetail.retired_at ?? 'Active'}</p>
        </div>
      </div>

      <section className="rounded-2xl bg-gray-50 p-4">
        <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
          Retention summary
        </p>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <p className="text-sm text-gray-700">
            Lineage preserved: {sourceDetail.retention_summary.lineage_preserved ? 'yes' : 'no'}
          </p>
          <p className="text-sm text-gray-700">
            Document lineage: {sourceDetail.retention_summary.has_document_lineage ? 'yes' : 'no'}
          </p>
          <p className="text-sm text-gray-700">
            Purged lineage: {sourceDetail.retention_summary.has_purged_document_lineage ? 'yes' : 'no'}
          </p>
          <p className="text-sm text-gray-700">
            Purge supported: {sourceDetail.retention_summary.purge_supported ? 'yes' : 'no'}
          </p>
        </div>
        <p className="mt-3 text-sm text-gray-700">
          Policy: {sourceDetail.retention_summary.retention_policy_code}
        </p>
      </section>

      <section>
        <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
          Version history
        </p>
        {sourceDetail.versions.length === 0 ? (
          <p className="mt-3 text-sm text-gray-500">No version records are available for this source.</p>
        ) : (
          <div className="mt-3 space-y-2">
            {sourceDetail.versions.map((version) => {
              const stateLabel = labelForPublicationState(version.publication_state)
              const isPublished = version.publication_state === 'published'
              const isArchived = version.publication_state === 'archived'
              return (
                <div key={version.source_version_id} className="rounded-2xl border border-gray-200 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-sm font-medium text-gray-900">{version.title}</p>
                    <span
                      className={`rounded-full px-2 py-1 text-[11px] font-medium ${
                        isPublished
                          ? 'bg-emerald-100 text-emerald-700'
                          : isArchived
                            ? 'bg-gray-100 text-gray-500'
                            : 'bg-amber-100 text-amber-700'
                      }`}
                    >
                      {stateLabel}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-gray-500">
                    {version.effective_from}
                    {version.effective_to ? ` to ${version.effective_to}` : ' onward'} ·{' '}
                    {labelForSourceVersionForm(version.source_version_form)}
                  </p>
                </div>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}
