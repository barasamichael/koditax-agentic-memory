export const colors = {
  // Brand
  navy: {
    900: '#1C3A5C',
    700: '#2B5C96',
    500: '#378ADD',
    300: '#85B7EB',
    50:  '#E6F1FB',
  },
  // Semantic aliases
  primary:     '#2B5C96',
  primaryDark: '#1C3A5C',
  accent:      '#1D9E75',

  // Status chips
  status: {
    draft:               { bg: '#E6F1FB', text: '#0C447C' },
    pending_verification:{ bg: '#FAEEDA', text: '#633806' },
    ready:               { bg: '#EAF3DE', text: '#27500A' },
    blocked:             { bg: '#FCEBEB', text: '#791F1F' },
    submitted:           { bg: '#E1F5EE', text: '#085041' },
    processing:          { bg: '#F1EFE8', text: '#5F5E5A' },
  },
} as const
