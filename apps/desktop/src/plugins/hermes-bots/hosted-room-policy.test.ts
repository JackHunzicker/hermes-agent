import { beforeEach, expect, it, vi } from 'vitest'

const { request, route, rooms, capabilities } = vi.hoisted(() => ({
  request: vi.fn(),
  route: vi.fn(),
  rooms: { get: vi.fn() },
  capabilities: { get: vi.fn() }
}))

vi.mock('./group-chat', () => ({
  $groupChats: rooms,
  groupChatHostedGateway: (room: { hosted: string }) => room.hosted
}))
vi.mock('./hosted-room-runtime', () => ({ hostedRouteForRoom: route }))
vi.mock('./hosted-room-transport', () => ({ requestHostedConnection: request }))
vi.mock('./hosted-room-capability-state', () => ({ $hostedRoomCapabilities: capabilities }))
import { loadHostedPolicy, saveHostedPolicy } from './hosted-room-policy'
import type { ResponderPolicy } from './hosted-room-policy'

const policy: ResponderPolicy = {
  mode: 'event_driven',
  default_responder: 'leader',
  leader_member_id: 'stable-a',
  max_turns_per_window: 4,
  window_seconds: 120
}

const state = () => ({
  room: {
    room_id: 'room',
    authority_gateway_id: 'authority',
    authority_epoch: 3,
    revision: 7,
    responder_policy: { ...policy },
    members: [{ member_id: 'stable-a', handle: 'renamed', display_name: 'Renamed', target: { kind: 'local' } }]
  }
})

beforeEach(() => {
  request.mockReset().mockImplementation(async () => state())
  route.mockReset().mockResolvedValue({ connectionId: 'home', profile: 'default' })
  rooms.get.mockReturnValue({
    Board: { roomId: 'room', hosted: 'authority', hostedConnectionId: 'home', hostedEpoch: 3 }
  })
  capabilities.get.mockReturnValue({
    home: {
      authorityId: 'authority',
      methods: ['groups.state', 'groups.policy.update'],
      features: ['responder_policy_v1']
    }
  })
})
it('fences stale bindings and rejects unsupported, malformed or unverifiable policy without broadening scope', async () => {
  const snapshot = await loadHostedPolicy('Board', 'room')
  rooms.get.mockReturnValue({ Board: { roomId: 'replacement', hosted: 'authority', hostedConnectionId: 'home' } })
  request.mockClear()
  await expect(saveHostedPolicy(snapshot, snapshot.policy, 'stale')).rejects.toMatchObject({ code: 'roomChanged' })
  expect(request).not.toHaveBeenCalled()
  rooms.get.mockReturnValue({
    Board: { roomId: 'room', hosted: 'authority', hostedConnectionId: 'home', hostedEpoch: 3 }
  })
  await expect(
    saveHostedPolicy(snapshot, { ...snapshot.policy, leader_member_id: 'renamed' }, 'alias')
  ).rejects.toMatchObject({ code: 'invalidPolicy' })
  await expect(
    saveHostedPolicy(snapshot, { ...snapshot.policy, window_seconds: 0 }, 'invalid')
  ).rejects.toMatchObject({ code: 'invalidPolicy' })
  expect(request).not.toHaveBeenCalled()
  request.mockResolvedValueOnce(state()).mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('offline'))
  await expect(saveHostedPolicy(snapshot, snapshot.policy, 'accepted')).rejects.toMatchObject({
    code: 'verificationFailed'
  })
  request.mockResolvedValueOnce({ room: { ...state().room, room_id: 'wrong' } })
  await expect(loadHostedPolicy('Board', 'room')).rejects.toMatchObject({ code: 'invalidResponse' })
  request.mockResolvedValueOnce({ room: { ...state().room, responder_policy: {} } })
  expect((await loadHostedPolicy('Board', 'room')).policy).toEqual({
    mode: 'legacy_bounded',
    default_responder: 'all',
    leader_member_id: null,
    max_turns_per_window: 10,
    window_seconds: 60
  })
  let resolve!: (value: unknown) => void
  route.mockReturnValueOnce(
    new Promise(done => {
      resolve = done
    })
  )
  const pending = loadHostedPolicy('Board', 'room')
  rooms.get.mockReturnValue({ Board: { roomId: 'room', hosted: 'other-authority', hostedConnectionId: 'home' } })
  resolve({ connectionId: 'home' })
  await expect(pending).rejects.toMatchObject({ code: 'roomChanged' })
})

it('re-resolves authority immediately before a write and refuses a retargeted connection', async () => {
  const snapshot = await loadHostedPolicy('Board', 'room')

  request.mockClear()
  capabilities.get.mockReturnValue({
    home: {
      authorityId: 'different-authority',
      methods: ['groups.state', 'groups.policy.update'],
      features: ['responder_policy_v1']
    }
  })

  await expect(saveHostedPolicy(snapshot, snapshot.policy, 'retargeted')).rejects.toMatchObject({
    code: 'roomChanged'
  })
  expect(request).not.toHaveBeenCalled()
})

it('refuses a connection retarget while the preflight state request is pending', async () => {
  const snapshot = await loadHostedPolicy('Board', 'room')
  request.mockClear()
  request.mockImplementationOnce(async () => {
    capabilities.get.mockReturnValue({
      home: {
        authorityId: 'different-authority',
        methods: ['groups.state', 'groups.policy.update'],
        features: ['responder_policy_v1']
      }
    })

    return state()
  })
  await expect(saveHostedPolicy(snapshot, snapshot.policy, 'retarget-during-read')).rejects.toBeDefined()
  expect(request.mock.calls.map(call => call[1])).toEqual(['groups.state'])
})

it('refuses a write after the local authority epoch changes', async () => {
  const snapshot = await loadHostedPolicy('Board', 'room')

  request.mockClear()
  rooms.get.mockReturnValue({
    Board: { roomId: 'room', hosted: 'authority', hostedConnectionId: 'home', hostedEpoch: 4 }
  })

  await expect(saveHostedPolicy(snapshot, snapshot.policy, 'new-epoch')).rejects.toMatchObject({ code: 'roomChanged' })
  expect(request).not.toHaveBeenCalled()
})

it('does not report verification success when a newer canonical policy differs from the submitted policy', async () => {
  const snapshot = await loadHostedPolicy('Board', 'room')
  const different = state()

  different.room.revision = 8
  different.room.responder_policy.default_responder = 'mentions_only'
  different.room.responder_policy.leader_member_id = null
  request.mockClear()
  request.mockResolvedValueOnce(state()).mockResolvedValueOnce({ room: { revision: 8 } }).mockResolvedValueOnce(different)

  await expect(
    saveHostedPolicy(snapshot, { ...snapshot.policy, max_turns_per_window: 5 }, 'concurrent-policy')
  ).rejects.toMatchObject({ code: 'verificationFailed' })
  expect(request.mock.calls.map(call => call[1])).toEqual(['groups.state', 'groups.policy.update', 'groups.state'])
})

it('loads canonical policy and writes the captured revision and stable member ID, then reads back', async () => {
  const snapshot = await loadHostedPolicy('Board', 'room')
  expect(snapshot.policy).toEqual(policy)
  const saved = state()
  saved.room.revision = 8
  saved.room.responder_policy.max_turns_per_window = 5
  request.mockResolvedValueOnce(state()).mockResolvedValueOnce({ room: { revision: 8 } }).mockResolvedValueOnce(saved)
  const result = await saveHostedPolicy(snapshot, { ...snapshot.policy, max_turns_per_window: 5 }, 'edit-id')
  expect(request.mock.calls.map(call => call[1])).toEqual([
    'groups.state',
    'groups.state',
    'groups.policy.update',
    'groups.state'
  ])
  expect(request.mock.calls[2][2]).toEqual({
    room_id: 'room',
    expected_revision: 7,
    event_id: 'edit-id',
    policy: { ...policy, max_turns_per_window: 5 }
  })
  expect(result.revision).toBe(8)
  expect(result.policy.max_turns_per_window).toBe(5)
})
