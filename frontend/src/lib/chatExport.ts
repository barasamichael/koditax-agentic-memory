import type { ChatConversation } from '@/types/chat'

export type ExportFormat = 'pdf' | 'docx' | 'markdown'

function safeFilename(title: string): string {
  return title.replace(/[^a-z0-9\s-]/gi, '').trim().replace(/\s+/g, '_') || 'kodi_chat'
}

// ── Markdown ──────────────────────────────────────────────────────────────────

function buildMarkdown(conversation: ChatConversation): string {
  const lines: string[] = [
    `# ${conversation.title}`,
    '',
    `*Exported from Kodi · ${new Date().toLocaleDateString('en-KE', { dateStyle: 'long' })}*`,
    '',
    '---',
    '',
  ]

  for (const msg of conversation.messages) {
    if (msg.role === 'user') {
      lines.push(`**You**`, '', msg.content, '')
    } else if (msg.type === 'outcome' && msg.content.trim()) {
      lines.push(`**Kodi**`, '', msg.content, '')
    }
    lines.push('---', '')
  }

  return lines.join('\n')
}

export function downloadMarkdown(conversation: ChatConversation): void {
  const text = buildMarkdown(conversation)
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${safeFilename(conversation.title)}.md`
  a.click()
  URL.revokeObjectURL(url)
}

// ── DOCX ──────────────────────────────────────────────────────────────────────

export async function downloadDocx(conversation: ChatConversation): Promise<void> {
  const { Document, Paragraph, TextRun, HeadingLevel, Packer } = await import('docx')
  const { saveAs } = await import('file-saver')

  const children: InstanceType<typeof Paragraph>[] = []

  // Cover heading
  children.push(
    new Paragraph({
      text: conversation.title,
      heading: HeadingLevel.HEADING_1,
      spacing: { after: 200 },
    }),
    new Paragraph({
      children: [
        new TextRun({
          text: `Exported from Kodi · ${new Date().toLocaleDateString('en-KE', { dateStyle: 'long' })}`,
          italics: true,
          color: '888888',
          size: 20,
        }),
      ],
      spacing: { after: 400 },
    })
  )

  for (const msg of conversation.messages) {
    if (msg.role === 'user') {
      children.push(
        new Paragraph({
          children: [new TextRun({ text: 'You', bold: true, size: 22 })],
          spacing: { before: 300, after: 80 },
        }),
        new Paragraph({
          children: [new TextRun({ text: msg.content, size: 22 })],
          spacing: { after: 240 },
        })
      )
    } else if (msg.type === 'outcome' && msg.content.trim()) {
      children.push(
        new Paragraph({
          children: [new TextRun({ text: 'Kodi', bold: true, color: '1B3A5C', size: 22 })],
          spacing: { before: 300, after: 80 },
        })
      )
      // Split on lines to preserve paragraph breaks
      for (const line of msg.content.split(/\n\n+/)) {
        const trimmed = line.trim()
        if (trimmed) {
          children.push(
            new Paragraph({
              children: [new TextRun({ text: trimmed, size: 22 })],
              spacing: { after: 160 },
            })
          )
        }
      }
      children.push(new Paragraph({ text: '', spacing: { after: 160 } }))
    }
  }

  const doc = new Document({
    creator: 'Kodi',
    title: conversation.title,
    sections: [
      {
        properties: {},
        children,
      },
    ],
  })

  const buffer = await Packer.toBlob(doc)
  saveAs(buffer, `${safeFilename(conversation.title)}.docx`)
}

// ── PDF via hidden print iframe ────────────────────────────────────────────────

function renderMessageHtml(
  role: 'user' | 'assistant',
  content: string,
  timestamp: string
): string {
  const dateStr = new Date(timestamp).toLocaleDateString('en-KE', { dateStyle: 'medium' })

  if (role === 'user') {
    return `
      <div class="msg user">
        <div class="label">You</div>
        <div class="bubble user-bubble">${escapeHtml(content)}</div>
        <div class="timestamp">${dateStr}</div>
      </div>`
  }

  // Convert simple markdown to HTML for the PDF answer block
  const htmlContent = markdownToHtml(content)
  return `
    <div class="msg assistant">
      <div class="label kodi-label">Kodi</div>
      <div class="bubble answer-bubble">${htmlContent}</div>
      <div class="timestamp">${dateStr}</div>
    </div>`
}

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/\n/g, '<br>')
}

function markdownToHtml(md: string): string {
  return md
    // Fenced code blocks
    .replace(/```[\w]*\n([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
    // Inline code
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // Italic
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // H3
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    // H2
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    // H1
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    // Unordered list items
    .replace(/^[\-\*] (.+)$/gm, '<li>$1</li>')
    // Ordered list items
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    // Wrap consecutive <li> in <ul>
    .replace(/(<li>[\s\S]+?<\/li>)(\n(?!<li>)|$)/g, '<ul>$1</ul>')
    // Paragraphs — double newlines
    .replace(/\n\n+/g, '</p><p>')
    // Wrap everything in a paragraph if not already a block element
    .replace(/^(?!<[hup]|<pre|<li)(.+)$/gm, '<p>$1</p>')
    // Clean up empty paragraphs
    .replace(/<p>\s*<\/p>/g, '')
    // Horizontal rules
    .replace(/^---$/gm, '<hr>')
}

function buildPdfHtml(conversation: ChatConversation): string {
  const dateStr = new Date().toLocaleDateString('en-KE', { dateStyle: 'long' })
  const messagesHtml = conversation.messages
    .filter((m) => (m.role === 'user' || m.type === 'outcome') && m.content.trim())
    .map((m) => renderMessageHtml(m.role, m.content, m.timestamp))
    .join('')

  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>${conversation.title}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    font-size: 11pt;
    color: #1a1a1a;
    background: #fff;
    padding: 0;
  }

  /* ── Cover page ── */
  .cover {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: flex-start;
    padding: 72pt 64pt;
    background: linear-gradient(145deg, #0f2340 0%, #1b3a5c 60%, #234e78 100%);
    color: #fff;
    page-break-after: always;
  }
  .cover-brand {
    font-size: 10pt;
    font-weight: 600;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: rgba(255,255,255,0.55);
    margin-bottom: 48pt;
  }
  .cover-title {
    font-size: 26pt;
    font-weight: 700;
    line-height: 1.25;
    color: #fff;
    max-width: 480pt;
    margin-bottom: 20pt;
  }
  .cover-date {
    font-size: 10pt;
    color: rgba(255,255,255,0.5);
  }
  .cover-accent {
    width: 40pt;
    height: 3pt;
    background: #4ade80;
    border-radius: 2pt;
    margin-bottom: 24pt;
  }

  /* ── Chat area ── */
  .chat-area {
    padding: 40pt 48pt;
    max-width: 600pt;
    margin: 0 auto;
  }

  .section-header {
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #9ca3af;
    margin-bottom: 28pt;
    padding-bottom: 8pt;
    border-bottom: 1pt solid #e5e7eb;
  }

  .msg {
    margin-bottom: 28pt;
    page-break-inside: avoid;
  }

  .label {
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #6b7280;
    margin-bottom: 6pt;
  }
  .kodi-label { color: #1b3a5c; }

  .bubble {
    padding: 12pt 16pt;
    border-radius: 12pt;
    font-size: 10.5pt;
    line-height: 1.65;
  }

  .user-bubble {
    background: #1b3a5c;
    color: #fff;
    border-radius: 14pt 14pt 4pt 14pt;
  }

  .answer-bubble {
    background: #f0fdf4;
    border: 1pt solid #bbf7d0;
    border-radius: 4pt 14pt 14pt 14pt;
    color: #1a1a1a;
  }

  .timestamp {
    font-size: 8pt;
    color: #9ca3af;
    margin-top: 5pt;
    text-align: right;
  }
  .msg.assistant .timestamp { text-align: left; }

  /* ── Markdown elements inside answer bubbles ── */
  .answer-bubble h1 { font-size: 13pt; font-weight: 700; margin: 10pt 0 6pt; color: #111827; }
  .answer-bubble h2 { font-size: 11pt; font-weight: 700; margin: 8pt 0 5pt; color: #1f2937; }
  .answer-bubble h3 { font-size: 10.5pt; font-weight: 600; margin: 7pt 0 4pt; color: #374151; }
  .answer-bubble p { margin-bottom: 7pt; }
  .answer-bubble ul, .answer-bubble ol { padding-left: 16pt; margin-bottom: 7pt; }
  .answer-bubble li { margin-bottom: 3pt; }
  .answer-bubble strong { font-weight: 700; color: #111827; }
  .answer-bubble em { font-style: italic; }
  .answer-bubble code {
    background: #f1f5f9;
    border: 0.5pt solid #e2e8f0;
    border-radius: 3pt;
    padding: 1pt 4pt;
    font-family: 'Courier New', monospace;
    font-size: 9pt;
  }
  .answer-bubble pre {
    background: #f8fafc;
    border: 0.5pt solid #e2e8f0;
    border-radius: 6pt;
    padding: 10pt;
    margin: 8pt 0;
    overflow: hidden;
  }
  .answer-bubble pre code { background: none; border: none; padding: 0; font-size: 9pt; }
  .answer-bubble hr { border: none; border-top: 1pt solid #e5e7eb; margin: 10pt 0; }
  .answer-bubble table { width: 100%; border-collapse: collapse; margin: 8pt 0; font-size: 9.5pt; }
  .answer-bubble th {
    background: #f1f5f9;
    font-weight: 700;
    padding: 6pt 8pt;
    border: 0.5pt solid #cbd5e1;
    text-align: left;
  }
  .answer-bubble td { padding: 5pt 8pt; border: 0.5pt solid #e2e8f0; }
  .answer-bubble tr:nth-child(even) td { background: #f8fafc; }

  /* ── Footer ── */
  .footer {
    margin-top: 40pt;
    padding-top: 12pt;
    border-top: 1pt solid #e5e7eb;
    font-size: 8pt;
    color: #9ca3af;
    display: flex;
    justify-content: space-between;
  }

  @media print {
    body { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    .cover { min-height: 100vh; }
    .msg { page-break-inside: avoid; }
  }
</style>
</head>
<body>

<div class="cover">
  <div class="cover-brand">Kodi · Tax Intelligence</div>
  <div class="cover-accent"></div>
  <div class="cover-title">${escapeHtml(conversation.title)}</div>
  <div class="cover-date">Exported on ${dateStr}</div>
</div>

<div class="chat-area">
  <div class="section-header">Conversation</div>
  ${messagesHtml}
  <div class="footer">
    <span>Kodi · Kenyan Tax Intelligence</span>
    <span>${dateStr}</span>
  </div>
</div>

</body>
</html>`
}

export function downloadPdf(conversation: ChatConversation): void {
  const html = buildPdfHtml(conversation)
  const printWindow = window.open('', '_blank', 'width=900,height=700')
  if (!printWindow) return

  printWindow.document.open()
  printWindow.document.write(html)
  printWindow.document.close()

  // Wait for resources to load then trigger print dialog
  printWindow.onload = () => {
    setTimeout(() => {
      printWindow.focus()
      printWindow.print()
      // Don't close automatically — let the user dismiss after saving
    }, 400)
  }
}
