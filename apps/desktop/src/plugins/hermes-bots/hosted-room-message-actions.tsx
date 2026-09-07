import { Button, ConfirmDialog, Textarea } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { mutateHostedMessage } from './hosted-room-actions'
import type { HostedRoomCapability } from './hosted-room-client'
import type { HostedHistoryMessage } from './hosted-room-history'

interface Props {
  group: string
  message: HostedHistoryMessage
  capability?: Pick<HostedRoomCapability, 'methods' | 'features'>
}

/** Actions sit beside the existing transcript; source messages remain immutable. */
export function HostedMessageActions({ group, message, capability }: Props) {
  const [edit, setEdit] = useState<null | { text: string; revision: number; commandId: string }>(null)
  const [deletion, setDeletion] = useState<null | { revision: number; commandId: string }>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const supports = (operation: string) => capability?.features?.includes('message_mutations_v1') && capability.methods?.includes(`groups.message.${operation}`)
  const own = message.actor.kind === 'user' && message.actor.id === 'desktop'
  const liked = message.reactions.some(reaction => reaction.reaction === '👍' && reaction.actors.some(actor => actor.kind === 'user' && actor.id === 'desktop'))

  const run = async (action: () => Promise<unknown>) => {
    setPending(true)
    setError('')

    try { await action() } catch (err) { setError(String((err as Error).message || err)) } finally { setPending(false) }
  }

  if (message.deleted) {return null}

  return <div className="flex flex-wrap items-center gap-2 text-xs">
    {own && supports('edit') ? <Button aria-label="Edit message" disabled={pending} onClick={() => setEdit({ text: message.text || '', revision: message.revision, commandId: crypto.randomUUID() })} size="inline" variant="text">Edit</Button> : null}
    {own && supports('delete') ? <Button aria-label="Delete message" disabled={pending} onClick={() => setDeletion({ revision: message.revision, commandId: crypto.randomUUID() })} size="inline" variant="text">Delete</Button> : null}
    {supports('react') ? <Button aria-label={liked ? 'Remove thumbs up' : 'Add thumbs up'} disabled={pending} onClick={() => void run(() => mutateHostedMessage(group, 'react', { event_id: crypto.randomUUID(), target_event_id: message.event_id, reaction: '👍', present: !liked }))} size="inline" variant="text">{liked ? 'Unlike' : '👍'}</Button> : null}
    {edit ? <div className="grid w-full gap-2">
      <Textarea aria-label="Edit message" disabled={pending} onChange={event => setEdit({ ...edit, text: event.target.value, commandId: crypto.randomUUID() })} value={edit.text} />
      <div className="flex gap-2"><Button disabled={pending || !edit.text.trim()} onClick={() => void run(async () => {
        await mutateHostedMessage(group, 'edit', { event_id: edit.commandId, target_event_id: message.event_id, expected_revision: edit.revision, text: edit.text })
        setEdit(null)
      })} size="xs">Save edit</Button><Button disabled={pending} onClick={() => setEdit(null)} size="xs" variant="text">Cancel</Button></div>
    </div> : null}
    {error ? <span className="text-destructive" role="alert">{error}</span> : null}
    <ConfirmDialog confirmLabel="Delete message" description="The current message will be deleted. Its original remains in the room audit history." onClose={() => setDeletion(null)} onConfirm={async () => {
      if (!deletion) {return}
      await mutateHostedMessage(group, 'delete', { event_id: deletion.commandId, target_event_id: message.event_id, expected_revision: deletion.revision })
      setDeletion(null)
    }} open={Boolean(deletion)} title="Delete message?" />
  </div>
}
