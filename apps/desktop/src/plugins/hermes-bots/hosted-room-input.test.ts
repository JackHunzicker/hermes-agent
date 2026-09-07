import { expect, it, vi } from 'vitest'

import { pluginSdkMock } from './group-test-utils'
vi.mock('@hermes/plugin-sdk', async () => pluginSdkMock({}))
import { $groupClarify } from './group-chat'
import { syncHostedRoomApprovals } from './hosted-room-approval-state'

it('projects a supported scoped input into the existing clarification card and clears expired input', () => {
  $groupClarify.set({})
  const members = [{ name: 'writer', memberId: 'member' }]
  const room = { room_id: 'room', members: [{ member_id: 'member' }] }
  const action = { kind: 'input', input_supported: true, member_id: 'member', task_id: 'task', execution_generation: 2, thread_id: 'thread', request_id: 'request', input: { question: 'Which target?', choices: ['staging', 'production'] } }
  syncHostedRoomApprovals('Board', room, members, [action], true)
  expect(Object.values($groupClarify.get())[0]).toMatchObject({ kind: 'clarify', question: 'Which target?', hostedInput: { roomId: 'room', memberId: 'member', taskId: 'task', threadId: 'thread', executionGeneration: 2 } })
  syncHostedRoomApprovals('Board', room, members, [], true)
  expect(Object.values($groupClarify.get())).toEqual([])
  syncHostedRoomApprovals('Board', room, members, [{ ...action, input_supported: false }], true)
  expect(Object.values($groupClarify.get())).toEqual([])
})
