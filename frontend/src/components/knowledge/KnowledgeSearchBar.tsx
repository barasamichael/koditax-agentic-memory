interface KnowledgeSearchBarProps {
  label: string
  placeholder: string
  query: string
  onChange: (value: string) => void
  resultCount: number
  helperText?: string
}

export function KnowledgeSearchBar({
  label,
  placeholder,
  query,
  onChange,
  resultCount,
  helperText,
}: KnowledgeSearchBarProps) {
  return (
    <div className="border-b border-gray-100 p-4">
      <label className="block text-xs font-medium uppercase tracking-wide text-gray-500">
        {label}
      </label>
      <input
        value={query}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mt-2 w-full rounded-xl border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-400 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500"
      />
      <p className="mt-2 text-xs text-gray-500">
        Showing {resultCount} item{resultCount === 1 ? '' : 's'}
      </p>
      {helperText ? (
        <p className="mt-1 text-[11px] text-gray-400">{helperText}</p>
      ) : null}
    </div>
  )
}
