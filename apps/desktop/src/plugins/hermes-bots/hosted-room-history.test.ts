import { describe, expect, it, vi } from 'vitest'
import { readHostedHistory } from './hosted-room-history'

describe('canonical history', () => {
  it('pins the snapshot across pages and retains original text and actor identity', async () => {
    const original = { event_id: 'human', seq: 2, thread_id: 'thread', actor: { kind: 'user', id: 'telegram:42', display_name: 'Jordan' }, original_text: ' original\n', text: ' edited\n', deleted: false, revision: 6, attachments: [], reactions: [] }
    const request = vi.fn().mockResolvedValueOnce({ messages: [original], cursor: 2, snapshot_seq: 7, latest_seq: 7, has_more: true }).mockResolvedValueOnce({ messages: [], cursor: 7, snapshot_seq: 7, latest_seq: 8, has_more: false })
    const result = await readHostedHistory(request, 'room')
    expect(request.mock.calls).toEqual([
      ['groups.history', { room_id: 'room', after_seq: 0, limit: 100 }],
      ['groups.history', { room_id: 'room', after_seq: 2, limit: 100, snapshot_seq: 7 }]
    ])
    expect(result.messages.human).toEqual(original)
    expect(result.snapshotSeq).toBe(7)
  })
})
