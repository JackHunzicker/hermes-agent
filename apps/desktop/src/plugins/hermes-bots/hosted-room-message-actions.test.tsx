import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { HostedMessageActions } from './hosted-room-message-actions'
const { mutate } = vi.hoisted(() => ({ mutate: vi.fn().mockResolvedValue({}) }))
vi.mock('./hosted-room-actions', () => ({ mutateHostedMessage: mutate }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('submits exact edited bytes against the revision selected, not a newer live revision', async () => {
  const message = { event_id: 'source', seq: 1, thread_id: 'thread', actor: { kind: 'user', id: 'desktop' }, original_text: 'original', text: 'current', deleted: false, revision: 3, attachments: [], reactions: [] }
  const capability = { methods: ['groups.message.edit'], features: ['message_mutations_v1'] }
  const view = render(<HostedMessageActions capability={capability} group="Board" message={message} />)
  fireEvent.click(screen.getByRole('button', { name: 'Edit message' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Edit message' }), { target: { value: '  edited\n' } })
  view.rerender(<HostedMessageActions capability={capability} group="Board" message={{ ...message, revision: 9 }} />)
  fireEvent.click(screen.getByRole('button', { name: 'Save edit' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledWith('Board', 'edit', expect.objectContaining({ target_event_id: 'source', expected_revision: 3, text: '  edited\n' })))
})

it('does not offer edit/delete for another human and sends explicit reaction presence', async () => {
  const message = { event_id: 'remote', seq: 1, thread_id: 'thread', actor: { kind: 'user', id: 'telegram:42' }, original_text: 'original', text: 'current', deleted: false, revision: 3, attachments: [], reactions: [{ reaction: '👍', actors: [{ kind: 'user', id: 'desktop' }] }] }
  render(<HostedMessageActions capability={{ methods: ['groups.message.edit', 'groups.message.delete', 'groups.message.react'], features: ['message_mutations_v1'] }} group="Board" message={message} />)
  expect(screen.queryByRole('button', { name: 'Edit message' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Delete message' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Remove thumbs up' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledWith('Board', 'react', expect.objectContaining({ target_event_id: 'remote', reaction: '👍', present: false })))
})
