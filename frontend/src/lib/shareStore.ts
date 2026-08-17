import type { ChatConversation } from '@/types/chat'

const SHARE_PREFIX = 'kodi-share-'
const SHARE_TTL_MS = 7 * 24 * 60 * 60 * 1000 // 7 days

interface ShareEntry {
  id: string
  title: string
  createdAt: string
  expiresAt: string
  messages: {
    role: 'user' | 'assistant'
    content: string
    type: string
    timestamp: string
  }[]
}

function randomId(): string {
  const bytes = new Uint8Array(6)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

export function createShare(conversation: ChatConversation): string {
  const id = randomId()
  const entry: ShareEntry = {
    id,
    title: conversation.title,
    createdAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + SHARE_TTL_MS).toISOString(),
    messages: conversation.messages
      .filter((m) => (m.type === 'text' || m.type === 'outcome') && m.content.trim())
      .map((m) => ({
        role: m.role,
        content: m.content,
        type: m.type,
        timestamp: m.timestamp,
      })),
  }
  try {
    localStorage.setItem(SHARE_PREFIX + id, JSON.stringify(entry))
  } catch {
    // localStorage quota exceeded — prune expired entries and retry once
    pruneExpiredShares()
    try {
      localStorage.setItem(SHARE_PREFIX + id, JSON.stringify(entry))
    } catch {
      // still failing — storage unavailable
    }
  }
  return id
}

export function resolveShare(id: string): ShareEntry | null {
  try {
    const raw = localStorage.getItem(SHARE_PREFIX + id)
    if (!raw) return null
    const entry = JSON.parse(raw) as ShareEntry
    if (new Date(entry.expiresAt).getTime() < Date.now()) {
      localStorage.removeItem(SHARE_PREFIX + id)
      return null
    }
    return entry
  } catch {
    return null
  }
}

function pruneExpiredShares(): void {
  const now = Date.now()
  for (const key of Object.keys(localStorage)) {
    if (!key.startsWith(SHARE_PREFIX)) continue
    try {
      const entry = JSON.parse(localStorage.getItem(key) ?? '{}') as Partial<ShareEntry>
      if (entry.expiresAt && new Date(entry.expiresAt).getTime() < now) {
        localStorage.removeItem(key)
      }
    } catch {
      localStorage.removeItem(key)
    }
  }
}
