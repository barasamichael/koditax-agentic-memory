import { create } from 'zustand'

export interface Toast {
  id: string
  message: string
  type: 'success' | 'error' | 'warning' | 'info'
  duration?: number
}

interface UIState {
  sidebarCollapsed: boolean
  railOpen: boolean
  chatHistoryOpen: boolean
  slidePanel: 'computation' | 'document' | 'form' | null
  toasts: Toast[]
  toggleSidebar: () => void
  toggleRail: () => void
  toggleChatHistory: () => void
  setChatHistoryOpen: (open: boolean) => void
  setSlidePanel: (panel: UIState['slidePanel']) => void
  addToast: (toast: Omit<Toast, 'id'>) => void
  removeToast: (id: string) => void
}

export const useUIStore = create<UIState>()((set) => ({
  sidebarCollapsed: false,
  railOpen: false,
  chatHistoryOpen: false,
  slidePanel: null,
  toasts: [],
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
  toggleRail: () => set((state) => ({ railOpen: !state.railOpen })),
  toggleChatHistory: () => set((state) => ({ chatHistoryOpen: !state.chatHistoryOpen })),
  setChatHistoryOpen: (open) => set({ chatHistoryOpen: open }),
  setSlidePanel: (panel) => set({ slidePanel: panel }),
  addToast: (toast) =>
    set((state) => ({
      toasts: [
        ...state.toasts,
        { ...toast, id: `${Date.now()}-${Math.random().toString(36).slice(2)}` },
      ],
    })),
  removeToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}))
