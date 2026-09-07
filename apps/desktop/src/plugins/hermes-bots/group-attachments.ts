/**
 * Group-chat attachments: turning picked, pasted, or dropped files into the
 * data-URL payloads a room's members are shown.
 *
 * A leaf module — it talks to the DOM and the toast host, nothing else in Bot
 * Mode.
 */

import { host } from '@hermes/plugin-sdk'

import type { Attachment, AttachmentKind } from './types'

// ── group-chat attachments: pick/paste/drop files the room's members see ────

/** Classify a picked file for the group-attachment pipeline. */
function groupAttachmentKind(file: File): AttachmentKind {
  if (/^image\//.test(file.type || '')) {
    return 'image'
  }

  if (file.type === 'application/pdf' || /\.pdf$/i.test(file.name || '')) {
    return 'pdf'
  }

  return 'file'
}

/** File objects → [{ name, data, kind }] (data URLs), oversized files skipped
 *  with a toast. Preserve original bytes for source-qualified downloads;
 *  display previews must not replace the uploaded file. Shared by the
 *  picker button, the composer paste handler, and room drag & drop. */
export async function filesToGroupAttachments(files: File[] | FileList | null | undefined): Promise<Attachment[]> {
  const picked: Attachment[] = []

  for (const file of [...(files || [])]) {
    if (!file) {
      continue
    }

    if (file.size > 15_000_000) {
      host.notify({
        kind: 'error',
        message: `${file.name || 'attachment'}: too large (max 15MB).`
      })

      continue
    }

    const data = await new Promise<null | string>(done => {
      const reader = new FileReader()
      reader.onload = () => done(typeof reader.result === 'string' ? reader.result : null)
      reader.onerror = () => done(null)
      reader.readAsDataURL(file)
    })

    if (!data) {
      continue
    }

    const kind = groupAttachmentKind(file)
    picked.push({
      name: file.name || (kind === 'image' ? 'pasted image' : 'attachment'),
      data,
      kind
    })
  }

  return picked
}

/** Multi-file picker for the group composer — any file type; kind decides
 *  the staging RPC. Resolves to [{ name, data, kind }]. */
export function pickGroupAttachments(): Promise<Attachment[]> {
  return new Promise(resolve => {
    const input = document.createElement('input')
    input.type = 'file'
    input.multiple = true
    input.onchange = () => resolve(filesToGroupAttachments(input.files))
    input.click()
  })
}
