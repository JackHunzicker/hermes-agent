import { $groupChats, groupChatHostedGateway, updateGroupChat } from './group-chat'
import { $hostedRoomCapabilities } from './hosted-room-capability-state'
import { readHostedHistory, supportsHostedMethod } from './hosted-room-history'
import type { HostedHistoryMessage, HostedReadCursor } from './hosted-room-history'
import { hostedRouteForRoom, refreshHostedRooms } from './hosted-room-runtime'
import { requestHostedConnection } from './hosted-room-transport'
import type { GroupPrompt } from './types'

export type HostedStopScope = { kind: 'thread'; thread_id: string } | {
  kind: 'task'; thread_id: string; task_id: string; execution_generation: number; cancel_generation: number
}

async function roomAction(group: string, method: string, feature: string) {
  const room = $groupChats.get()[group]

  if (!room?.roomId || !groupChatHostedGateway(room)) {throw new Error('Room authority unavailable')}
  const route = await hostedRouteForRoom(room, method.startsWith('groups.history') || method.startsWith('groups.read') ? 'read' : 'control')

  if (!route) {throw new Error('Room authority unavailable')}

  if (!supportsHostedMethod($hostedRoomCapabilities.get()[route.connectionId], method, feature)) {
    throw new Error('Host update required for this room action')
  }

  if ($groupChats.get()[group]?.roomId !== room.roomId) {throw new Error('Room changed')}

  return { room, request: (params: Record<string, unknown>) => requestHostedConnection(route, method, { room_id: room.roomId, ...params }), route }
}

export async function mutateHostedMessage(group: string, operation: 'edit' | 'delete' | 'react', params: {
  event_id: string; target_event_id: string; expected_revision?: number; text?: string; reaction?: string; present?: boolean
}) {
  const { room, request } = await roomAction(group, `groups.message.${operation}`, 'message_mutations_v1')
  const result = await request(params) as { message?: HostedHistoryMessage }

  if (result.message && $groupChats.get()[group]?.roomId === room.roomId) {
    const message = result.message
    updateGroupChat(group, current => ({ ...current, hostedHistory: {
      messages: { ...current.hostedHistory?.messages, [message.event_id]: message },
      snapshotSeq: Math.max(current.hostedHistory?.snapshotSeq || 0, message.revision)
    } }), { sync: false })
  }

  return result
}

export async function markHostedRead(group: string, throughSeq: number, threadId?: string) {
  const { room, request } = await roomAction(group, 'groups.read.mark', 'room_read_cursors_v1')
  const result = await request({ through_seq: throughSeq, ...(threadId ? { thread_id: threadId } : {}) }) as HostedReadCursor

  if (!threadId && $groupChats.get()[group]?.roomId === room.roomId) {
    updateGroupChat(group, current => ({ ...current, hostedRead: result }), { sync: false })
  }

  return result
}

export async function searchHostedHistory(group: string, query: string) {
  const { room, route } = await roomAction(group, 'groups.history.search', 'message_history_search_v1')

  return readHostedHistory((method, params) => requestHostedConnection(route, method, params), room.roomId!, query)
}

export async function respondHostedInput(group: string, scope: NonNullable<GroupPrompt['hostedInput']>, requestId: string, answer: string, commandId: string, questionId?: string) {
  const { room, request } = await roomAction(group, 'groups.input.respond', 'scoped_input_v1')

  if (room.roomId !== scope.roomId) {throw new Error('Input room changed')}

  return request({ member_id: scope.memberId, thread_id: scope.threadId, task_id: scope.taskId,
    execution_generation: scope.executionGeneration, request_id: requestId, answer, command_id: commandId,
    ...(questionId === undefined ? {} : { question_id: questionId }) })
}

export async function stopHostedScope(group: string, scope: HostedStopScope, cancelId: string) {
  const { request } = await roomAction(group, 'groups.stop_scope', 'scoped_stop_v1')
  const result = await request({ cancel_id: cancelId, scope })
  await refreshHostedRooms()

  return result
}
