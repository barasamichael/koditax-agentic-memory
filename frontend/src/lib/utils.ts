import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs))

export const formatKES = (amount: number) =>
  new Intl.NumberFormat('en-KE', { style: 'currency', currency: 'KES', minimumFractionDigits: 0 }).format(amount)

export const formatDate = (date: Date | string) =>
  new Intl.DateTimeFormat('en-KE', { dateStyle: 'medium' }).format(new Date(date))

export const normalizeKenyanPhone = (phone: string): string => {
  const digits = phone.replace(/\D/g, '')
  if (digits.startsWith('254')) return `+${digits}`
  if (digits.startsWith('0')) return `+254${digits.slice(1)}`
  return `+254${digits}`
}
