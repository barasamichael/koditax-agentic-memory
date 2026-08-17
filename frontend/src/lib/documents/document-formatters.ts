import { formatDate } from '@/lib/utils'

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const

export const formatDocumentDate = (date: Date | string | null | undefined): string | null => {
  if (!date) return null
  return formatDate(date)
}

export const formatFileSize = (bytes: number | null | undefined): string | null => {
  if (bytes == null || Number.isNaN(bytes)) return null
  if (bytes < 1024) return `${bytes} B`

  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < SIZE_UNITS.length - 1) {
    value /= 1024
    unitIndex += 1
  }

  const fractionDigits = unitIndex === 1 ? 1 : Number.isInteger(value) ? 0 : 1
  return `${value.toFixed(fractionDigits)} ${SIZE_UNITS[unitIndex]}`
}

export const getFileExtension = (name: string | null | undefined): string | null => {
  if (!name) return null
  const trimmed = name.trim()
  const lastDot = trimmed.lastIndexOf('.')
  if (lastDot <= 0 || lastDot === trimmed.length - 1) return null
  return trimmed.slice(lastDot + 1).toLowerCase()
}

export const formatDocumentName = (displayName: string | null | undefined): string => {
  const value = displayName?.trim()
  return value && value.length > 0 ? value : 'Untitled document'
}

export const formatFileTypeLabel = (fileExtension: string | null | undefined): string => {
  const value = fileExtension?.trim().toLowerCase()
  if (!value) return 'File'
  if (value === 'pdf') return 'PDF'
  return value.toUpperCase()
}
