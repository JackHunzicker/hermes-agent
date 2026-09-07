import { beforeEach, describe, expect, it, vi } from 'vitest'

import { pluginSdkMock } from './group-test-utils'
const { request, route, rooms, capabilities } = vi.hoisted(() => ({ request: vi.fn(), route: vi.fn(), rooms: { get: vi.fn(), set: vi.fn() }, capabilities: { get: vi.fn() } }))
vi.mock('@hermes/plugin-sdk', async () => pluginSdkMock({}))
vi.mock('./group-chat', () => ({ $groupChats: rooms, groupChatHostedGateway: (room: { hosted: string }) => room.hosted, updateGroupChat: vi.fn() }))
vi.mock('./hosted-room-runtime', () => ({ hostedRouteForRoom: route, refreshHostedRooms: vi.fn() }))
vi.mock('./hosted-room-transport', () => ({ requestHostedConnection: request }))
vi.mock('./hosted-room-capability-state', () => ({ $hostedRoomCapabilities: capabilities }))
import { markHostedRead, mutateHostedMessage, stopHostedScope } from './hosted-room-actions'

beforeEach(() => {
  request.mockReset().mockResolvedValue({})
  route.mockResolvedValue({ connectionId: 'home' })
  rooms.get.mockReturnValue({ Board: { roomId: 'room', hosted: 'authority', hostedConnectionId: 'home' } })
  capabilities.get.mockReturnValue({ home: { methods: ['groups.message.edit', 'groups.read.mark', 'groups.stop_scope'], features: ['message_mutations_v1', 'room_read_cursors_v1', 'scoped_stop_v1'] } })
})

describe('canonical room actions', () => {
  it('answers the exact hosted input request without using member-session clarify.respond', async () => {
    const { respondHostedInput } = await import('./hosted-room-actions')
    capabilities.get.mockReturnValue({ home: { methods: ['groups.input.respond'], features: ['scoped_input_v1'] } })
    await respondHostedInput('Board', { roomId: 'room', memberId: 'member', threadId: 'thread', taskId: 'task', executionGeneration: 3 }, 'request', ' answer\n', 'command', 'question')
    expect(request).toHaveBeenCalledExactlyOnceWith({ connectionId: 'home' }, 'groups.input.respond', { room_id: 'room', member_id: 'member', thread_id: 'thread', task_id: 'task', execution_generation: 3, request_id: 'request', answer: ' answer\n', command_id: 'command', question_id: 'question' })
  })

  it('sends revision-checked edits with exact text and canonical read bounds', async () => {
    await mutateHostedMessage('Board', 'edit', { event_id: 'command', target_event_id: 'source', expected_revision: 9, text: '  new\n' })
    expect(request).toHaveBeenCalledWith({ connectionId: 'home' }, 'groups.message.edit', { room_id: 'room', event_id: 'command', target_event_id: 'source', expected_revision: 9, text: '  new\n' })
    await markHostedRead('Board', 11, 'thread')
    expect(request).toHaveBeenLastCalledWith({ connectionId: 'home' }, 'groups.read.mark', { room_id: 'room', through_seq: 11, thread_id: 'thread' })
  })
  it('never broadens a task stop when unsupported or rejected', async () => {
    const scope = { kind: 'task' as const, thread_id: 'thread', task_id: 'task', execution_generation: 2, cancel_generation: 0 }
    request.mockRejectedValueOnce(new Error('task_attempt_changed'))
    await expect(stopHostedScope('Board', scope, 'cancel')).rejects.toThrow('task_attempt_changed')
    expect(request).toHaveBeenCalledExactlyOnceWith({ connectionId: 'home' }, 'groups.stop_scope', { room_id: 'room', cancel_id: 'cancel', scope })
    request.mockClear()
    capabilities.get.mockReturnValue({ home: { methods: ['groups.stop'], features: [] } })
    await expect(stopHostedScope('Board', scope, 'cancel')).rejects.toThrow('update')
    expect(request).not.toHaveBeenCalled()
  })
})
