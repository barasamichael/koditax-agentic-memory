import { useState } from 'react'
import { cn } from '@/lib/utils'
import { labelForAuthorityLevel, labelForPublicationState, labelForSourceClass } from '@/lib/knowledgeStateLabels'
import * as knowledgeApi from '@/api/knowledge.api'
import type {
  KnowledgeAnchorDetail,
  KnowledgeSearchResultItem,
  KnowledgeTimelineResultItem,
} from '@/types/knowledge'

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[11px] font-semibold uppercase tracking-widest text-gray-400">
      {children}
    </p>
  )
}

function ResultCard({
  title,
  metadata,
  onViewAnchor,
}: {
  title: string
  metadata: React.ReactNode
  onViewAnchor: () => void
}) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-gray-900">{title}</p>
          <div className="mt-1 space-y-1 text-[12px] text-gray-500">{metadata}</div>
        </div>
        <button
          type="button"
          onClick={onViewAnchor}
          className="rounded-lg border border-gray-200 px-3 py-1.5 text-[12px] font-medium text-gray-700 transition-colors hover:bg-gray-50"
        >
          View anchor
        </button>
      </div>
    </div>
  )
}

export function KnowledgeExplorerPanel() {
  const [mode, setMode] = useState<'search' | 'retrieve' | 'timeline'>('search')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<Array<KnowledgeSearchResultItem | KnowledgeTimelineResultItem>>([])
  const [anchorBusy, setAnchorBusy] = useState(false)
  const [anchorError, setAnchorError] = useState<string | null>(null)
  const [anchorDetail, setAnchorDetail] = useState<KnowledgeAnchorDetail | null>(null)
  const [anchorLookupId, setAnchorLookupId] = useState('')
  const [query, setQuery] = useState('')
  const [sourceType, setSourceType] = useState('')
  const [taxDomain, setTaxDomain] = useState('')
  const [effectiveDate, setEffectiveDate] = useState('')
  const [sourceIds, setSourceIds] = useState('')
  const [anchorIds, setAnchorIds] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  const runAnchorLookup = async (anchorId: string) => {
    if (!anchorId.trim()) return
    setAnchorBusy(true)
    setAnchorError(null)
    try {
      const result = await knowledgeApi.getKnowledgeAnchor(anchorId.trim())
      setAnchorDetail(result)
    } catch {
      setAnchorDetail(null)
      setAnchorError('Could not load anchor details.')
    } finally {
      setAnchorBusy(false)
    }
  }

  const runExplorer = async () => {
    setBusy(true)
    setError(null)
    setResults([])
    try {
      if (mode === 'search') {
        const result = await knowledgeApi.searchKnowledge({
          query,
          source_type: sourceType || undefined,
          tax_domain: taxDomain || undefined,
          effective_date: effectiveDate || undefined,
        })
        setResults(result.items)
      } else if (mode === 'retrieve') {
        const result = await knowledgeApi.retrieveKnowledge({
          source_ids: sourceIds.split('\n').map((value) => value.trim()).filter(Boolean),
          anchor_ids: anchorIds.split('\n').map((value) => value.trim()).filter(Boolean),
        })
        setResults(result.items)
      } else {
        const result = await knowledgeApi.timelineSearchKnowledge({
          query,
          tax_domain: taxDomain,
          source_type: sourceType || undefined,
          start_date: startDate,
          end_date: endDate,
        })
        setResults(result.items)
      }
    } catch {
      setError('Knowledge query failed. Please check the request inputs and try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(320px,0.9fr)]">
      <div className="space-y-6">
        <div className="space-y-4 rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
          <div>
            <SectionLabel>Explorer</SectionLabel>
            <p className="mt-1 text-sm text-gray-500">
              Run search, retrieve, and timeline queries against the knowledge service.
            </p>
          </div>

          <div className="flex gap-2 rounded-lg bg-gray-100 p-1">
            {[
              ['search', 'Search'],
              ['retrieve', 'Retrieve'],
              ['timeline', 'Timeline'],
            ].map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setMode(value as 'search' | 'retrieve' | 'timeline')}
                className={cn(
                  'flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  mode === value
                    ? 'bg-white text-navy-900 shadow-sm'
                    : 'text-gray-500 hover:text-gray-700'
                )}
              >
                {label}
              </button>
            ))}
          </div>

          {mode === 'retrieve' ? (
            <div className="grid gap-4">
              <div>
                <SectionLabel>Source IDs</SectionLabel>
                <textarea
                  value={sourceIds}
                  onChange={(event) => setSourceIds(event.target.value)}
                  rows={4}
                  placeholder="one source id per line"
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                />
              </div>
              <div>
                <SectionLabel>Anchor IDs</SectionLabel>
                <textarea
                  value={anchorIds}
                  onChange={(event) => setAnchorIds(event.target.value)}
                  rows={4}
                  placeholder="one anchor id per line"
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                />
              </div>
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              <div className={mode === 'timeline' ? 'sm:col-span-2' : ''}>
                <SectionLabel>Query</SectionLabel>
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                />
              </div>
              <div>
                <SectionLabel>Source type</SectionLabel>
                <input
                  value={sourceType}
                  onChange={(event) => setSourceType(event.target.value)}
                  placeholder="tax_law, regulation..."
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                />
              </div>
              <div>
                <SectionLabel>Tax area</SectionLabel>
                <input
                  value={taxDomain}
                  onChange={(event) => setTaxDomain(event.target.value)}
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                />
              </div>
              {mode === 'search' ? (
                <div>
                  <SectionLabel>Effective date</SectionLabel>
                  <input
                    type="date"
                    value={effectiveDate}
                    onChange={(event) => setEffectiveDate(event.target.value)}
                    className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                  />
                </div>
              ) : (
                <>
                  <div>
                    <SectionLabel>Start date</SectionLabel>
                    <input
                      type="date"
                      value={startDate}
                      onChange={(event) => setStartDate(event.target.value)}
                      className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                    />
                  </div>
                  <div>
                    <SectionLabel>End date</SectionLabel>
                    <input
                      type="date"
                      value={endDate}
                      onChange={(event) => setEndDate(event.target.value)}
                      className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
                    />
                  </div>
                </>
              )}
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => void runExplorer()}
              disabled={busy}
              className="rounded-lg bg-navy-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-navy-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? 'Running…' : 'Run query'}
            </button>
            <p className="text-[12px] text-gray-400">
              {results.length} result{results.length === 1 ? '' : 's'}
            </p>
          </div>

          {error ? <p className="text-sm text-red-600">{error}</p> : null}

          <div className="space-y-3">
            {results.length === 0 ? (
              <div className="rounded-xl border border-gray-100 bg-gray-50 p-4 text-sm text-gray-500">
                Run a query to inspect governed knowledge results.
              </div>
            ) : (
              results.map((item) => (
                <ResultCard
                  key={`${item.source_id}-${item.anchor_id}-${'timeline_position' in item ? item.timeline_position : item.effective_from}`}
                  title={item.title}
                  onViewAnchor={() => void runAnchorLookup(item.anchor_id)}
                  metadata={
                    <>
                      <p>
                        {labelForSourceClass(item.source_type)} · {item.tax_domain}
                      </p>
                      <p>
                        {labelForAuthorityLevel(item.authority_level)} · {item.effective_from}
                        {item.effective_to ? ` – ${item.effective_to}` : ' onward'}
                      </p>
                      <p className="break-all">Anchor: {item.anchor_id}</p>
                      {'publication_state' in item ? (
                        <p>
                          Status: {labelForPublicationState(item.publication_state)} · Timeline #
                          {item.timeline_position}
                        </p>
                      ) : (
                        <a
                          href={item.url}
                          target="_blank"
                          rel="noreferrer"
                          className="break-all text-navy-700 underline decoration-dotted underline-offset-2 hover:text-navy-900"
                        >
                          {item.url}
                        </a>
                      )}
                    </>
                  }
                />
              ))
            )}
          </div>
        </div>
      </div>

      <div className="space-y-6">
        <div className="space-y-4 rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
          <div>
            <SectionLabel>Anchor Detail</SectionLabel>
            <p className="mt-1 text-sm text-gray-500">
              Open anchor detail directly from query results or look up an anchor ID manually.
            </p>
          </div>

          <div className="flex gap-2">
            <input
              value={anchorLookupId}
              onChange={(event) => setAnchorLookupId(event.target.value)}
              placeholder="Enter anchor ID"
              className="flex-1 rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800 transition-colors focus:border-navy-300 focus:bg-white focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500/30"
            />
            <button
              type="button"
              onClick={() => void runAnchorLookup(anchorLookupId)}
              disabled={anchorBusy || !anchorLookupId.trim()}
              className="rounded-lg border border-gray-200 px-4 py-2.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {anchorBusy ? 'Loading…' : 'Lookup'}
            </button>
          </div>

          {anchorError ? <p className="text-sm text-red-600">{anchorError}</p> : null}

          {!anchorDetail ? (
            <div className="rounded-xl border border-gray-100 bg-gray-50 p-4 text-sm text-gray-500">
              No anchor selected.
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <p className="text-sm font-medium text-gray-900">{anchorDetail.anchor_title}</p>
                <p className="mt-1 text-[12px] text-gray-500">{anchorDetail.source_title}</p>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <SectionLabel>Status</SectionLabel>
                  <p className="mt-1 text-sm text-gray-700">
                    {labelForPublicationState(anchorDetail.publication_state)}
                  </p>
                </div>
                <div>
                  <SectionLabel>Authority</SectionLabel>
                  <p className="mt-1 text-sm text-gray-700">
                    {labelForAuthorityLevel(anchorDetail.authority_level)}
                  </p>
                </div>
                <div>
                  <SectionLabel>Source type</SectionLabel>
                  <p className="mt-1 text-sm text-gray-700">
                    {labelForSourceClass(anchorDetail.source_type)}
                  </p>
                </div>
                <div>
                  <SectionLabel>Tax area</SectionLabel>
                  <p className="mt-1 text-sm text-gray-700">{anchorDetail.tax_domain}</p>
                </div>
                <div className="sm:col-span-2">
                  <SectionLabel>Anchor path</SectionLabel>
                  <p className="mt-1 break-all text-[12px] font-mono text-gray-600">
                    {anchorDetail.anchor_path}
                  </p>
                </div>
                <div>
                  <SectionLabel>Effective window</SectionLabel>
                  <p className="mt-1 text-sm text-gray-700">
                    {anchorDetail.temporal_scope_from}
                    {anchorDetail.temporal_scope_to ? ` – ${anchorDetail.temporal_scope_to}` : ' onward'}
                  </p>
                </div>
                <div>
                  <SectionLabel>Chunks</SectionLabel>
                  <p className="mt-1 text-sm text-gray-700">{anchorDetail.chunk_count}</p>
                </div>
              </div>

              <div>
                <SectionLabel>Chunk Summary</SectionLabel>
                <div className="mt-2 space-y-1.5">
                  {anchorDetail.chunks.map((chunk) => (
                    <div key={chunk.chunk_id} className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-[12px] font-mono text-gray-600">{chunk.chunk_id}</p>
                        <span className="text-[11px] text-gray-400">Chunk {chunk.chunk_index}</span>
                      </div>
                      <p className="mt-1 text-[12px] text-gray-500">
                        {chunk.has_embedding ? 'Embedding available' : 'No embedding stored'}
                      </p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
