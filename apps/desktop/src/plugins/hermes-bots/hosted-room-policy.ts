import { $groupChats, groupChatHostedGateway } from './group-chat'
import { $hostedRoomCapabilities } from './hosted-room-capability-state'
import { hostedRouteForRoom } from './hosted-room-runtime'
import { requestHostedConnection } from './hosted-room-transport'
import type { ProfileRoute } from './types'

export interface ResponderPolicy {
  mode: 'legacy_bounded' | 'event_driven'
  default_responder: 'all' | 'leader' | 'mentions_only'
  leader_member_id: string | null
  max_turns_per_window: number
  window_seconds: number
}
export interface PolicyMember {
  member_id: string
  handle: string
  display_name?: string
  target: { kind: string }
}
export interface HostedPolicySnapshot {
  group: string
  roomId: string
  authorityId: string
  authorityEpoch: number
  connectionId: string
  route: ProfileRoute
  revision: number
  policy: ResponderPolicy
  members: PolicyMember[]
}
type PolicyBinding = Omit<HostedPolicySnapshot, 'revision' | 'policy' | 'members'>
export type HostedPolicyErrorCode =
  | 'roomChanged'
  | 'authorityUnavailable'
  | 'hostUpdateRequired'
  | 'invalidPolicy'
  | 'invalidResponse'
  | 'peerUnsupported'
  | 'readFailed'
  | 'saveFailed'
  | 'verificationFailed'

export class HostedPolicyError extends Error {
  readonly code: HostedPolicyErrorCode

  constructor(code: HostedPolicyErrorCode, cause?: unknown) {
    super(code, cause === undefined ? undefined : { cause })
    this.name = 'HostedPolicyError'
    this.code = code
  }
}

const failure = (code: HostedPolicyErrorCode, cause?: unknown) => new HostedPolicyError(code, cause)

// responder_policy_v1 represents an unset policy as {}. These are its documented
// execution defaults (gateway/hosted_room_responder_policy.py), not an offline fallback.
const defaults: ResponderPolicy = {
  mode: 'legacy_bounded',
  default_responder: 'all',
  leader_member_id: null,
  max_turns_per_window: 10,
  window_seconds: 60
}

function assertBinding(binding: Omit<PolicyBinding, 'route'>) {
  const current = $groupChats.get()[binding.group]

  if (
    !current ||
    current.roomId !== binding.roomId ||
    groupChatHostedGateway(current) !== binding.authorityId ||
    (current.hostedConnectionId || '') !== binding.connectionId ||
    current.hostedEpoch !== binding.authorityEpoch
  ) {
    throw failure('roomChanged')
  }
}

function assertSupported(route: ProfileRoute, authorityId: string) {
  const capability = $hostedRoomCapabilities.get()[route.connectionId]

  if (capability?.authorityId !== authorityId) {
    throw failure('roomChanged')
  }

  if (
    !['groups.state', 'groups.policy.update'].every(method => capability?.methods?.includes(method)) ||
    !capability?.features?.includes('responder_policy_v1')
  ) {
    throw failure('hostUpdateRequired')
  }
}

export function validateResponderPolicy(value: ResponderPolicy, members: PolicyMember[]): ResponderPolicy {
  if (!value || Object.keys(value).sort().join() !== Object.keys(defaults).sort().join()) {
    throw failure('invalidPolicy')
  }

  if (
    !['legacy_bounded', 'event_driven'].includes(value.mode) ||
    !['all', 'leader', 'mentions_only'].includes(value.default_responder)
  ) {
    throw failure('invalidPolicy')
  }

  for (const [key, maximum] of [
    ['max_turns_per_window', 32],
    ['window_seconds', 3600]
  ] as const) {
    if (!Number.isSafeInteger(value[key]) || value[key] < 1 || value[key] > maximum) {
      throw failure('invalidPolicy')
    }
  }

  if (
    (value.leader_member_id !== null && !members.some(member => member.member_id === value.leader_member_id)) ||
    (value.default_responder === 'leader' && value.leader_member_id === null)
  ) {
    throw failure('invalidPolicy')
  }

  return {
    mode: value.mode,
    default_responder: value.default_responder,
    leader_member_id: value.leader_member_id,
    max_turns_per_window: value.max_turns_per_window,
    window_seconds: value.window_seconds
  }
}

async function readPolicy(binding: PolicyBinding): Promise<HostedPolicySnapshot> {
  assertBinding(binding)

  let response: {
    room: {
      room_id: string
      authority_gateway_id: string
      authority_epoch: number
      revision: number
      responder_policy: ResponderPolicy
      members: PolicyMember[]
    }
  }

  try {
    response = await requestHostedConnection(binding.route, 'groups.state', { room_id: binding.roomId })
  } catch (error) {
    throw failure('readFailed', error)
  }

  assertBinding(binding)
  const { room } = response

  if (room?.authority_gateway_id !== binding.authorityId || room?.authority_epoch !== binding.authorityEpoch) {
    throw failure('roomChanged')
  }

  if (
    !room ||
    room.room_id !== binding.roomId ||
    !Number.isSafeInteger(room.revision) ||
    room.revision < 1 ||
    !Array.isArray(room.members) ||
    !room.members.every(
      member =>
        member &&
        typeof member.member_id === 'string' &&
        member.member_id &&
        typeof member.handle === 'string' &&
        typeof member.target?.kind === 'string'
    ) ||
    new Set(room.members.map(member => member.member_id)).size !== room.members.length ||
    !room.responder_policy ||
    typeof room.responder_policy !== 'object'
  ) {
    throw failure('invalidResponse')
  }

  const policy = validateResponderPolicy(
    Object.keys(room.responder_policy).length === 0 ? defaults : room.responder_policy,
    room.members
  )

  return { ...binding, revision: room.revision, policy, members: room.members }
}

export async function loadHostedPolicy(group: string, roomId: string): Promise<HostedPolicySnapshot> {
  const room = $groupChats.get()[group]

  if (!room || room.roomId !== roomId) {
    throw failure('roomChanged')
  }

  const authorityId = groupChatHostedGateway(room)

  if (!authorityId) {
    throw failure('authorityUnavailable')
  }

  const authorityEpoch = room.hostedEpoch

  if (!Number.isSafeInteger(authorityEpoch) || Number(authorityEpoch) < 1) {
    throw failure('authorityUnavailable')
  }

  const binding = {
    group,
    roomId,
    authorityId,
    authorityEpoch: Number(authorityEpoch),
    connectionId: room.hostedConnectionId || ''
  }

  let route: ProfileRoute | null

  try {
    route = await hostedRouteForRoom(room, 'control')
  } catch (error) {
    throw failure('authorityUnavailable', error)
  }

  assertBinding(binding)

  if (!route) {
    throw failure('authorityUnavailable')
  }

  assertSupported(route, authorityId)

  return readPolicy({ ...binding, route })
}

export async function saveHostedPolicy(snapshot: HostedPolicySnapshot, policy: ResponderPolicy, eventId: string) {
  assertBinding(snapshot)

  if (snapshot.members.some(member => member.target.kind === 'peer')) {
    throw failure('peerUnsupported')
  }

  const payload = validateResponderPolicy(policy, snapshot.members)
  const room = $groupChats.get()[snapshot.group]
  let route: ProfileRoute | null

  try {
    route = room ? await hostedRouteForRoom(room, 'control') : null
  } catch (error) {
    throw failure('authorityUnavailable', error)
  }

  assertBinding(snapshot)

  if (!route || route.connectionId !== snapshot.connectionId) {
    throw failure('roomChanged')
  }

  assertSupported(route, snapshot.authorityId)

  try {
    const before = await readPolicy({ ...snapshot, route })

    if (before.revision !== snapshot.revision) {
      throw failure('verificationFailed')
    }
  } catch (error) {
    throw failure('verificationFailed', error)
  }

  assertBinding(snapshot)
  assertSupported(route, snapshot.authorityId)

  try {
    await requestHostedConnection(route, 'groups.policy.update', {
      room_id: snapshot.roomId,
      expected_revision: snapshot.revision,
      event_id: eventId,
      policy: payload
    })
  } catch (error) {
    throw failure('saveFailed', error)
  }

  try {
    const current = await readPolicy({ ...snapshot, route })

    if (
      current.revision <= snapshot.revision ||
      current.policy.mode !== payload.mode ||
      current.policy.default_responder !== payload.default_responder ||
      current.policy.leader_member_id !== payload.leader_member_id ||
      current.policy.max_turns_per_window !== payload.max_turns_per_window ||
      current.policy.window_seconds !== payload.window_seconds
    ) {
      throw failure('verificationFailed')
    }

    return current
  } catch (error) {
    throw failure('verificationFailed', error)
  }
}
