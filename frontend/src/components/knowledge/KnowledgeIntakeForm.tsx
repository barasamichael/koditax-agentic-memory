import { useState } from 'react'

const SOURCE_CLASS_OPTIONS = [
  { value: '', label: 'Not specified (classify later)' },
  { value: 'tax_law', label: 'Tax Law' },
  { value: 'regulation', label: 'Regulation' },
  { value: 'guidance', label: 'Guidance' },
  { value: 'commentary', label: 'Commentary' },
] as const

interface KnowledgeIntakeFormProps {
  onSubmit: (params: { url: string; sourceClass: string }) => Promise<void>
  busy: boolean
}

export function KnowledgeIntakeForm({ onSubmit, busy }: KnowledgeIntakeFormProps) {
  const [url, setUrl] = useState('')
  const [sourceClass, setSourceClass] = useState('')
  const [touched, setTouched] = useState(false)

  const urlValid = url.trim().startsWith('http://') || url.trim().startsWith('https://')
  const canSubmit = urlValid && !busy

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setTouched(true)
    if (!canSubmit) return
    await onSubmit({ url: url.trim(), sourceClass })
    setUrl('')
    setSourceClass('')
    setTouched(false)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 rounded-3xl border border-gray-200 bg-white p-6 shadow-card">
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
          Register a URL source
        </p>
        <p className="mt-1 text-sm text-gray-600">
          Submit an official-source URL to start a new intake job. The item will appear in
          Incoming Items once registered and can then be reviewed and approved by an administrator.
        </p>
      </div>

      <div>
        <label htmlFor="intake-url" className="block text-xs font-medium uppercase tracking-wide text-gray-500">
          Official source URL
        </label>
        <input
          id="intake-url"
          type="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          onBlur={() => setTouched(true)}
          placeholder="https://www.kra.go.ke/..."
          className="mt-2 w-full rounded-xl border border-gray-200 px-3 py-2 text-sm text-gray-800 placeholder-gray-400 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500"
          disabled={busy}
          required
        />
        {touched && !urlValid && url.length > 0 ? (
          <p className="mt-1 text-xs text-red-600">Enter a valid URL starting with https://</p>
        ) : null}
      </div>

      <div>
        <label htmlFor="intake-class" className="block text-xs font-medium uppercase tracking-wide text-gray-500">
          Source type
        </label>
        <select
          id="intake-class"
          value={sourceClass}
          onChange={(event) => setSourceClass(event.target.value)}
          className="mt-2 w-full rounded-xl border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500"
          disabled={busy}
        >
          {SOURCE_CLASS_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <p className="mt-1 text-[11px] text-gray-400">
          You can set or correct the source type during review if needed.
        </p>
      </div>

      <button
        type="submit"
        disabled={!canSubmit}
        className="rounded-xl bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-navy-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? 'Registering...' : 'Register source'}
      </button>
    </form>
  )
}
