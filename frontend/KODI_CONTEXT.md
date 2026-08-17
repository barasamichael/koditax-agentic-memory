# KODI Solutions — Master Context Document
> Load this file at the start of every Claude Code session. Do not ask for clarification on anything defined here.

---

## 1. Project Overview

**Product**: Kodi Solutions — AI-powered KRA tax assistance platform for Kenya.
**Architecture**: Chat-first workspace. All tax computation, document processing, and form generation are outcomes surfaced *through chat*, not separate apps.
**Users**: Individual taxpayers, tax consultants, accountants, administrators.
**Model**: Claude.ai UX pattern — left sidebar, center chat timeline, right context rail.

---

## 2. Tech Stack (fixed — no alternatives)

| Layer | Choice | Version |
|---|---|---|
| Framework | React | 18.3 |
| Language | TypeScript | 5.4 (strict mode) |
| Build | Vite | 5.x |
| Routing | React Router | 6.x (data router) |
| State — server | TanStack Query | 5.x |
| State — client | Zustand | 4.x |
| Forms | React Hook Form + Zod | 7.x / 3.x |
| Styling | Tailwind CSS | 3.x (JIT) |
| Components | shadcn/ui | (copy-paste, not installed as pkg) |
| Icons | Lucide React | latest |
| HTTP client | Axios | 1.x |
| Date handling | date-fns | 3.x |
| Animations | Framer Motion | 11.x |
| Formatting | Intl API (native) | — |
| Testing | Vitest + React Testing Library | — |

**No Redux. No MUI. No Chakra. No Ant Design.**

---

## 3. Design System

### 3.1 Color Tokens (`src/styles/tokens.ts`)

```ts
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
  primary:    '#2B5C96',
  primaryDark:'#1C3A5C',
  accent:     '#1D9E75',   // teal — secondary CTAs, success states

  // Status chips (matches FRS states)
  status: {
    draft:              { bg: '#E6F1FB', text: '#0C447C' },
    pending_verification:{ bg: '#FAEEDA', text: '#633806' },
    ready:              { bg: '#EAF3DE', text: '#27500A' },
    blocked:            { bg: '#FCEBEB', text: '#791F1F' },
    submitted:          { bg: '#E1F5EE', text: '#085041' },
    processing:         { bg: '#F1EFE8', text: '#5F5E5A' },
  },

  // Tailwind-mapped semantic colors (reference in tw classes)
  // success → green-600, warning → amber-600, error → red-600, info → blue-600
} as const
```

### 3.2 Tailwind Config (`tailwind.config.ts`)

```ts
export default {
  content: ['./src/**/*.{ts,tsx}'],
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
        }
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
      }
    }
  }
}
```

### 3.3 Typography Rules
- Headings: `font-size: 22px, weight: 500` — class: `text-display`
- Body: `16px, weight: 400, line-height: 1.7` — class: `text-body`
- Secondary/muted: `13px` — class: `text-small text-gray-500`
- Labels/caps: `11px, weight: 500, tracking-wide, uppercase` — class: `text-label`
- Two weights only in UI: `400` (regular) and `500` (medium). Never `600` or `700`.
- Sentence case everywhere. No Title Case. No ALL CAPS except label class.

### 3.4 Spacing Scale
```
4px  → gap-1, p-1
8px  → gap-2, p-2
12px → gap-3, p-3
16px → gap-4, p-4 (component internal default)
20px → gap-5, p-5
24px → gap-6, p-6 (section padding)
32px → gap-8, p-8
48px → gap-12
```

### 3.5 Component Aesthetic Rules
- Cards: `bg-white border border-gray-100 rounded-card shadow-card p-5`
- Inputs: `border border-gray-200 rounded-input h-10 px-3 text-sm focus:ring-2 focus:ring-navy-500 focus:border-transparent`
- Primary button: `bg-navy-900 text-white rounded-input px-4 py-2 text-sm font-medium hover:bg-navy-700 active:scale-[0.98] transition-all`
- Ghost button: `border border-gray-200 text-gray-600 rounded-input px-4 py-2 text-sm hover:bg-gray-50`
- No drop shadows on buttons. No gradients anywhere.
- Border style: `0.5px` or `1px solid` — use Tailwind `border border-gray-100` (≈0.5px visual weight at scale)

---

## 4. Project File Structure

```
src/
├── api/                    # Axios instances + per-service adapters
│   ├── client.ts           # Base axios instance with interceptors
│   ├── auth.api.ts
│   ├── orchestration.api.ts
│   ├── document.api.ts
│   ├── taxCore.api.ts
│   ├── forms.api.ts
│   ├── reports.api.ts
│   ├── storage.api.ts
│   ├── knowledge.api.ts
│   └── types/              # Per-service request/response DTOs
├── components/
│   ├── ui/                 # shadcn/ui primitives (Button, Input, Dialog, etc.)
│   ├── layout/             # AppShell, Sidebar, ContextRail, TopBar
│   ├── auth/               # AuthForm, OtpInput, PasswordStrength
│   ├── chat/               # ChatTimeline, PromptComposer, MessageBubble,
│   │                       # ActionApprovalCard, FinalOutcomeCard, TraceDrawer
│   ├── documents/          # UploadZone, DocumentCard, ExtractionReview,
│   │                       # ConfidenceIndicator, EvidenceLinkPanel
│   ├── computations/       # ComputationPanel, BracketBreakdown, AuditTrail
│   ├── forms/              # FormWorkspace, FormVersionList, BatchSelector
│   └── shared/             # StatusChip, CorrelationBadge, EmptyState, Spinner
├── hooks/                  # useAuth, useChat, useDocuments, useComputation,
│                           # useIdempotency, useCorrelation
├── pages/
│   ├── AuthPage.tsx        # P01
│   ├── ChatPage.tsx        # P02 (primary surface)
│   ├── DocumentsPage.tsx   # P03
│   ├── ComputationsPage.tsx# P04
│   ├── FormsPage.tsx       # P05
│   └── AccountPage.tsx     # P06
├── stores/
│   ├── authStore.ts        # Zustand — user, session, role
│   ├── chatStore.ts        # Zustand — messages, conversationId, context
│   └── uiStore.ts          # Zustand — sidebar open, rail open, active panel
├── styles/
│   └── tokens.ts
├── types/
│   ├── api.ts              # Canonical error envelope, CorrelationID
│   ├── auth.ts
│   ├── chat.ts
│   ├── document.ts
│   ├── computation.ts
│   └── forms.ts
└── lib/
    ├── idempotency.ts      # Deterministic key generation
    ├── correlation.ts      # Correlation ID management
    ├── errorNormalizer.ts  # Maps backend envelopes → UI errors
    └── utils.ts            # cn(), formatKES(), formatDate()
```

---

## 5. State Management

### 5.1 Zustand Stores

**authStore** — persisted to sessionStorage
```ts
interface AuthState {
  user: User | null
  sessionId: string | null
  role: 'IndividualTaxpayer' | 'TaxAgent' | 'Accountant' | 'Administrator'
  isAuthenticated: boolean
  // actions
  setSession: (user: User, sessionId: string) => void
  clearSession: () => void
}
```

**chatStore** — in-memory only
```ts
interface ChatState {
  messages: ChatMessage[]
  conversationId: string | null
  pendingAction: PendingAction | null   // ActionApprovalCard state
  contextDocuments: DocumentRef[]       // right rail
  activeComputationId: string | null
  // actions
  appendMessage: (msg: ChatMessage) => void
  setPendingAction: (action: PendingAction | null) => void
}
```

**uiStore**
```ts
interface UIState {
  sidebarCollapsed: boolean
  railOpen: boolean
  activePage: string
  slidePanel: 'computation' | 'document' | 'form' | null
}
```

### 5.2 TanStack Query Keys
```ts
// Convention: [service, resource, id?]
['auth', 'session', sessionId]
['documents', 'list']
['documents', 'detail', documentId]
['documents', 'extraction', documentId]
['computations', 'detail', computationId]
['forms', 'list']
['reports', 'metadata', reportId]
['knowledge', 'search', queryHash]
```

---

## 6. API Integration

### 6.1 Axios Base Client (`src/api/client.ts`)

```ts
// Single instance per service — not one global axios
// Headers on every request:
//   Content-Type: application/json
//   X-Correlation-ID: [from correlationStore, generated per user flow]
//   Idempotency-Key: [from idempotencyManager, on write endpoints only]
//   Authorization: [endpoint-specific — see AuthContextAdapter]

// Response interceptor: normalize all non-2xx to CanonicalError
// Read: detail.error_code, detail.message, detail.reason, detail.reason_code, detail.context
```

### 6.2 Auth Header Strategy (per API contract)
- Public endpoints (register, login, OTP): no auth header
- Protected endpoints: `X-Auth-Context: <token>` (most service boundaries)
- Session-sensitive (account-deletion, refresh): bearer-style, endpoint-specific
- **Never hardcode one global auth header shape** — use `AuthContextAdapter` that maps endpoint → header strategy

### 6.3 Idempotency
- All endpoints with `"idempotency": true` in manifest MUST include `Idempotency-Key` header
- Key format: `SHA-256(userId + endpoint + requestBodyHash + timestamp-bucket)`
- Keys stored in sessionStorage, keyed by `${endpoint}:${resourceId}`
- On replay (same key, same body) → backend returns cached response

### 6.4 Canonical Error Type
```ts
interface CanonicalError {
  error_code: string
  message: string
  reason: string
  reason_code?: string
  context?: Record<string, unknown>
}
// All API errors normalized to this shape before reaching UI
```

### 6.5 Endpoint Reference (from manifest — do not deviate)
See `frontend-endpoint-manifest.json`. Key surfaces:
- Auth: `/v1/auth/*` — no base path prefix
- Orchestration: `/v1/orchestration/prompt/{ingest|decide|execute}`
- Documents: `/v1/documents/*`
- Tax core: `/computations/*` (no v1 prefix)
- Forms: `/v1/forms/income-tax/*`
- Reports: `/v1/reports/income-tax/*`
- Knowledge: `/knowledge/{search|retrieve}` (no v1 prefix)
- Validation: `/validate/return` (no v1 prefix)

---

## 7. Component Patterns

### 7.1 Component Template
```tsx
// Named export, no default export except pages
// Props interface always: ComponentNameProps
// No inline styles — Tailwind only
// cn() utility for conditional classes (from lib/utils.ts)
// Framer Motion for any entering/exiting animation

interface StatusChipProps {
  status: keyof typeof STATUS_CONFIG
  className?: string
}

const STATUS_CONFIG = {
  draft:               { label: 'Draft',                bg: 'bg-navy-50',   text: 'text-navy-900' },
  pending_verification:{ label: 'Pending verification', bg: 'bg-amber-50',  text: 'text-amber-900' },
  ready:               { label: 'Ready',                bg: 'bg-green-50',  text: 'text-green-800' },
  blocked:             { label: 'Blocked',              bg: 'bg-red-50',    text: 'text-red-800' },
  submitted:           { label: 'Submitted',            bg: 'bg-kodi-accent/10', text: 'text-green-900' },
  processing:          { label: 'Processing',           bg: 'bg-gray-100',  text: 'text-gray-600' },
} as const

export function StatusChip({ status, className }: StatusChipProps) {
  const cfg = STATUS_CONFIG[status]
  return (
    <span className={cn(
      'inline-flex items-center px-2.5 py-0.5 rounded-chip text-xs font-medium',
      cfg.bg, cfg.text, className
    )}>
      {cfg.label}
    </span>
  )
}
```

### 7.2 Page Template
```tsx
// Pages are thin orchestrators — no business logic
// All data fetching via TanStack Query hooks
// All mutations via custom hooks (useDocuments, useComputation, etc.)
// Pages own layout; components own appearance

export default function DocumentsPage() {
  return (
    <AppShell>
      <PageHeader title="Documents" />
      <div className="flex-1 overflow-auto p-6">
        {/* content */}
      </div>
    </AppShell>
  )
}
```

### 7.3 Form Pattern (React Hook Form + Zod)
```tsx
const schema = z.object({
  phoneNumber: z.string().regex(/^\+254\d{9}$/, 'Enter a valid Kenyan phone number'),
  kraPin: z.string().regex(/^[AP]\d{9}[A-Z]$/, 'Enter a valid KRA PIN'),
  password: z.string().min(12).regex(/[A-Z]/).regex(/[a-z]/).regex(/\d/).regex(/[!@#$%^&*]/),
})

const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema) })
```

### 7.4 Mutation Pattern
```tsx
// Always: loading state, error state, success state
// Always: idempotency key on writes
// Always: correlation ID on all requests
// On error: normalize via errorNormalizer, display reason_code-mapped message

const { mutate, isPending, error } = useMutation({
  mutationFn: (data) => documentsApi.createUploadSession(data, {
    idempotencyKey: generateIdempotencyKey('upload-session', userId),
  }),
  onSuccess: (res) => queryClient.invalidateQueries({ queryKey: ['documents', 'list'] }),
})
```

---

## 8. Chat Orchestration Flow

The three-step orchestration (ingest → decide → execute) is transparent to the user. Internally:

1. `POST /v1/orchestration/prompt/ingest` — submit raw user message
2. `POST /v1/orchestration/prompt/decide` — get intent + route decision
3. If decision requires confirmation → render `ActionApprovalCard` in timeline, pause
4. User confirms → `POST /v1/orchestration/prompt/execute` → render `FinalOutcomeCard`
5. If decision is informational only → skip confirmation, render `FinalOutcomeCard` directly

```ts
interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  type: 'text' | 'action_approval' | 'outcome' | 'error'
  metadata?: {
    actionType?: string
    correlationId?: string
    computationId?: string
    documentIds?: string[]
  }
}
```

---

## 9. Critical Constraints (never violate)

1. **No endpoint calls outside manifest** — `frontend-endpoint-manifest.json` is the only truth
2. **No untyped `any`** — TypeScript strict mode, all API responses fully typed
3. **Idempotency keys required** on every endpoint marked `"idempotency": true`
4. **Correlation ID on every request** — generated once per user flow, propagated
5. **All non-2xx responses** → `CanonicalError` → reason_code-mapped UI message
6. **No hidden mutation** in components — mutations only in hooks, confirmed in UI
7. **Auth headers are per-endpoint** via `AuthContextAdapter` — no global header
8. **Status chips** (`draft | pending_verification | ready | blocked | submitted | processing`) used consistently on all stateful objects
9. **Confirmation required** for all WRITE/SUBMIT actions — `ActionApprovalCard` must render before any side-effecting API call
10. **KRA phone format**: always store/display as `+254XXXXXXXXX`; accept `0XXXXXXXXX` and auto-convert

---

## 10. Accessibility & Quality Requirements

- All interactive elements keyboard-accessible (tab order, focus rings: `focus-visible:ring-2 focus-visible:ring-navy-500`)
- `aria-label` on icon-only buttons
- Error messages linked to inputs via `aria-describedby`
- Loading states: skeleton screens (not spinners) for list/page loads; inline spinner for button actions
- Empty states: always include an illustration-free message + CTA button
- Mobile-first: sidebar collapses to bottom nav on `< 768px`
- No `position: fixed` modals — use Dialog from shadcn/ui (portaled)

---

## 11. Environment & Configuration

```env
VITE_API_BASE_URL=https://api.kodi.co.ke
VITE_AUTH_SERVICE_URL=https://api.kodi.co.ke
VITE_ORCHESTRATION_URL=https://api.kodi.co.ke
VITE_DOCUMENTS_URL=https://api.kodi.co.ke
VITE_TAX_CORE_URL=https://api.kodi.co.ke
VITE_FORMS_URL=https://api.kodi.co.ke
VITE_REPORTS_URL=https://api.kodi.co.ke
VITE_KNOWLEDGE_URL=https://api.kodi.co.ke
```

All service URLs currently point to the same base — `client.ts` routes by path prefix. Keep them separate env vars for future service splits.