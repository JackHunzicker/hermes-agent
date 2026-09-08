import { Button, ConfirmDialog, SearchField } from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'

import { groupChatHostedGateway } from './group-chat'
import { markHostedRead, searchHostedHistory, stopHostedScope } from './hosted-room-actions'
import type { HostedRoomCapability } from './hosted-room-client'
import type { HostedHistory } from './hosted-room-history'
import { HostedPolicyControl } from './hosted-room-policy-control'
import type { GroupChat } from './types'

type Capability = Pick<HostedRoomCapability, 'methods' | 'features'> | undefined
const supports = (capability: Capability, method: string, feature: string) => capability?.methods?.includes(method) && capability.features?.includes(feature)

export function HostedHistoryToolbar({ group, room, capability, onResults }: {
  group: string; room: GroupChat; capability: Capability; onResults: (results: HostedHistory | null) => void
}) {
  const [query, setQuery] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const generation = useRef(0)
  const canSearch = supports(capability, 'groups.history.search', 'message_history_search_v1')
  const canRead = supports(capability, 'groups.read.mark', 'room_read_cursors_v1')
  const policyRoom = room.roomId && groupChatHostedGateway(room)

  if (!canSearch && !canRead && !policyRoom) {return null}

  return <div className="flex flex-wrap items-center gap-2 px-2.5 py-1 text-xs">
    {policyRoom ? <HostedPolicyControl group={group} key={`${room.hostedConnectionId}:${policyRoom}:${room.hostedEpoch}`} roomId={room.roomId!} /> : null}
    {canSearch ? <form className="flex min-w-0 flex-wrap items-center gap-2" onSubmit={event => {
      event.preventDefault()
      const selected = ++generation.current
      setPending(true); setError('')
      void searchHostedHistory(group, query).then(result => { if (generation.current === selected) {onResults(result)} }).catch(err => {
        if (generation.current === selected) {setError(String(err.message || err))}
      }).finally(() => { if (generation.current === selected) {setPending(false)} })
    }}>
      <SearchField loading={pending} onChange={value => {
        generation.current++; setQuery(value); setPending(false); onResults(null)
      }} placeholder="Search room history" value={query} />
      <Button disabled={pending || !query.trim()} size="xs" type="submit" variant="text">Search</Button>
      {query ? <Button onClick={() => { generation.current++; setQuery(''); setPending(false); onResults(null) }} size="xs" type="button" variant="text">Clear search</Button> : null}
    </form> : null}
    {canRead ? <Button disabled={pending || !room.hostedHistory} onClick={() => {
      const selected = ++generation.current
      setPending(true); setError('')
      void markHostedRead(group, room.hostedHistory!.snapshotSeq).catch(err => { if (generation.current === selected) {setError(String(err.message || err))} }).finally(() => { if (generation.current === selected) {setPending(false)} })
    }} size="xs" variant="text">Mark room read</Button> : null}
    {room.hostedRead ? <span>{room.hostedRead.unread_count} unread</span> : null}
    {error ? <span className="text-destructive" role="alert">{error}</span> : null}
  </div>
}

export function HostedThreadActions({ group, roomId, thread, throughSeq, capability }: {
  group: string; roomId?: string; thread: string; throughSeq: number; capability: Capability
}) {
  const [cancelId, setCancelId] = useState<string | null>(null)
  const [error, setError] = useState('')

  return <div className="flex flex-wrap gap-2 text-xs">
    {roomId ? <HostedPolicyControl group={group} roomId={roomId} threadId={thread} /> : null}
    {supports(capability, 'groups.stop_scope', 'scoped_stop_v1') ? <Button onClick={() => setCancelId(crypto.randomUUID())} size="inline" variant="text">Stop this thread</Button> : null}
    {supports(capability, 'groups.read.mark', 'room_read_cursors_v1') ? <Button onClick={() => void markHostedRead(group, throughSeq, thread).catch(err => setError(String(err.message || err)))} size="inline" variant="text">Mark thread read</Button> : null}
    {error ? <span className="text-destructive" role="alert">{error}</span> : null}
    <ConfirmDialog confirmLabel="Confirm thread stop" description="Only work in this thread will be stopped. Other room threads will continue." onClose={() => setCancelId(null)} onConfirm={async () => {
      if (!cancelId) {return}
      await stopHostedScope(group, { kind: 'thread', thread_id: thread }, cancelId)
      setCancelId(null)
    }} open={Boolean(cancelId)} title="Stop this thread?" />
  </div>
}
