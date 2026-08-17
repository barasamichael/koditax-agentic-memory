import type { Config } from 'tailwindcss'

export default {
  content: ['./src/**/*.{ts,tsx}', './index.html'],
  theme: {
    extend: {
      colors: {
        navy: {
          900: '#1C3A5C',
          700: '#2B5C96',
          500: '#378ADD',
          300: '#85B7EB',
          50:  '#E6F1FB',
        },
        kodi: {
          accent:  '#1D9E75',
          surface: '#F8F7F4',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      fontSize: {
        'display': ['22px', { lineHeight: '1.2', fontWeight: '500' }],
        'body':    ['16px', { lineHeight: '1.7', fontWeight: '400' }],
        'small':   ['13px', { lineHeight: '1.5', fontWeight: '400' }],
        'label':   ['11px', { lineHeight: '1.4', fontWeight: '500', letterSpacing: '0.06em' }],
      },
      borderRadius: {
        'card': '12px',
        'chip': '20px',
        'input': '8px',
      },
      boxShadow: {
        'card': '0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04)',
      },
    },
  },
  plugins: [],
} satisfies Config
