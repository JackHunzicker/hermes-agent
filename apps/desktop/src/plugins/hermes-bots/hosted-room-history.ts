import type { HostedRoomCapability } from './hosted-room-client'

export interface HostedHistoryMessage {
  event_id: string
  seq: number
  thread_id: string
  parent_event_id?: string | null
  actor: { kind: string; id: string; display_name?: string }
  original_text: string
  text: string | null
  deleted: boolean
  revision: number
  attachments: unknown[]
  reactions: Array<{ reaction: string; actors: Array<{ kind: string; id: string }> }>
}

export interface HostedHistory {
  messages: Record<string, HostedHistoryMessage>
  snapshotSeq: number
}

export interface HostedReadCursor {
  room_id: string
  thread_id: string | null
  reader: { kind: string; id: string }
  through_seq: number
  latest_seq: number
  unread_count: number
}

interface HistoryPage {
  messages: HostedHistoryMessage[]
  cursor: number
  snapshot_seq: number
  has_more: boolean
}

export type HostedHistoryRequest = (method: string, params: Record<string, unknown>) => Promise<unknown>

export function supportsHostedMethod(capability: HostedRoomCapability | undefined, method: string, feature: string) {
  return Boolean(capability?.methods?.includes(method) && capability.features?.includes(feature))
}

/** Always re-project from zero: later mutations affect earlier message sequences.
 * Pages are one pinned snapshot, bounded to avoid an unending catch-up loop. */
export async function readHostedHistory(request: HostedHistoryRequest, roomId: string, query?: string): Promise<HostedHistory> {
  const messages: HostedHistory['messages'] = {}
  let cursor = 0
  let snapshot: number | undefined

  for (let page = 0; page < 100; page++) {
    const result = await request(query === undefined ? 'groups.history' : 'groups.history.search', {
      room_id: roomId, after_seq: cursor, limit: 100,
      ...(snapshot === undefined ? {} : { snapshot_seq: snapshot }),
      ...(query === undefined ? {} : { query })
    }) as HistoryPage

    if (!Array.isArray(result?.messages) || !Number.isSafeInteger(result.snapshot_seq) ||
      !Number.isSafeInteger(result.cursor) || result.cursor < cursor ||
      (snapshot !== undefined && result.snapshot_seq !== snapshot) ||
      (result.has_more && result.cursor <= cursor)) {throw new Error('Invalid room history page')}

    snapshot = result.snapshot_seq

    for (const message of result.messages) {messages[message.event_id] = message}
    cursor = result.cursor

    if (!result.has_more) {return { messages, snapshotSeq: snapshot }}
  }

  throw new Error('Room history page limit reached; retry to reload')
}
