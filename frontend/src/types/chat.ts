export interface ChatAttachment {
  id: string
  displayName: string
  fileExtension?: string
}

export interface ChatSourceLocation {
  location_kind: 'page' | 'slide' | 'sheet' | 'line' | 'section' | 'cell' | 'image' | 'unknown'
  location_label: string
  location_status: 'exact' | 'approximate' | 'partial' | 'unavailable'
  page_number?: number | null
  slide_number?: number | null
  sheet_name?: string | null
  line_start?: number | null
  line_end?: number | null
  cell_reference?: string | null
  section_name?: string | null
}

export interface ChatSourceReference {
  document_id: string
  document_label: string
  document_status: 'available' | 'partial' | 'unavailable' | string
  source_location: ChatSourceLocation
  openable: boolean
  accessibility_label?: string | null
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  type: 'text' | 'action_approval' | 'outcome' | 'error'
  metadata?: {
    assistantState?: 'pending' | 'running' | 'completed' | 'failed'
    progressLabel?: string
    actionType?: string
    computationId?: string
    documentIds?: string[]
    documents?: ChatAttachment[]
    sourceReferences?: ChatSourceReference[]
    retryPrompt?: string
    retryable?: boolean
    interrupted?: boolean
  }
}

export interface ChatConversation {
  conversationId: string
  title: string
  createdAt: string
  updatedAt: string
  status: 'draft' | 'active' | 'attention'
  messages: ChatMessage[]
}

export interface PendingAction {
  id: string
  conversationId: string
  label: string
  description: string
  consequence: string
  onConfirm: () => Promise<void>
}
