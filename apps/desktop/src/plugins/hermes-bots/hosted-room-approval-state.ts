/** Mirrored hosted approval cards and their independent roster attention. */

import { $groupClarify, $groupHostedNeedsYou } from './group-chat'
import { groupMemberKey } from './group-membership'
import type { GroupMember, GroupPrompt } from './types'

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

export function clearHostedRoomApprovalState(group: string) {
  const prompts: Record<string, GroupPrompt> = Object.fromEntries(
    Object.entries($groupClarify.get()).filter(([, prompt]) => prompt.group !== group || !(prompt.hostedApproval || prompt.hostedInput))
  )

  $groupClarify.set(prompts)
  const attention = { ...$groupHostedNeedsYou.get() }

  delete attention[group]
  $groupHostedNeedsYou.set(attention)
}

export function resolveHostedRoomApprovalAttention(entry: GroupPrompt) {
  const approval = entry.hostedApproval || entry.hostedInput

  const remaining = Object.values($groupClarify.get()).some(prompt => {
    const pending = prompt.hostedApproval || prompt.hostedInput

    if (prompt.group !== entry.group || !pending) {
      return false
    }

    return !(
      prompt.requestId === entry.requestId &&
      pending.roomId === approval?.roomId &&
      pending.memberId === approval?.memberId &&
      pending.taskId === approval?.taskId &&
      pending.executionGeneration === approval?.executionGeneration
    )
  })

  $groupHostedNeedsYou.set({
    ...$groupHostedNeedsYou.get(),
    [entry.group]: remaining
  })
}

export function resetHostedRoomApprovalState() {
  $groupHostedNeedsYou.set({})
}

export function syncHostedRoomApprovals(
  group: string,
  room: { members?: unknown; room_id?: unknown },
  members: GroupMember[],
  pendingActions: unknown[],
  scopedInput = false
) {
  const current = $groupClarify.get()

  const next: Record<string, GroupPrompt> = Object.fromEntries(
    Object.entries(current).filter(([, prompt]) => prompt.group !== group || !(prompt.hostedApproval || prompt.hostedInput))
  )

  const serverMembers = Array.isArray(room.members) ? room.members : []
  let waiting = false

  for (const raw of pendingActions) {
    const action = record(raw)

    const isInput = scopedInput && action?.kind === 'input' && action.input_supported === true && Boolean(action.thread_id)

    if (action?.kind !== 'approval' && !isInput) {
      continue
    }

    const memberId = String(action.member_id || '')
    const taskId = String(action.task_id || '')
    const requestId = String(action.request_id || '')
    const executionGeneration = Number(action.execution_generation || 0)
    const memberIndex = serverMembers.findIndex(rawMember => String(record(rawMember)?.member_id || '') === memberId)
    const member = memberIndex >= 0 ? members[memberIndex] : null
    const approval = record(isInput ? action.input : action.approval)

    if (
      !member ||
      !memberId ||
      !taskId ||
      !requestId ||
      !Number.isSafeInteger(executionGeneration) ||
      executionGeneration < 1
    ) {
      continue
    }

    const key = `${group}::${groupMemberKey(member)}`
    const prior = current[key]

    const identity = {
      executionGeneration,
      memberId,
      roomId: String(room.room_id || ''),
      taskId,
      ...(action.thread_id ? { threadId: String(action.thread_id) } : {})
    }

    const choices = (Array.isArray(approval?.choices) ? approval.choices : [])
      .filter(choice => isInput ? typeof choice === 'string' : choice === 'once' || choice === 'deny')
      .map(String)

    const priorIdentity = prior?.hostedApproval || prior?.hostedInput
    next[key] =
      prior?.requestId === requestId &&
      priorIdentity?.executionGeneration === identity.executionGeneration &&
      priorIdentity.memberId === identity.memberId &&
      priorIdentity.roomId === identity.roomId &&
      priorIdentity.taskId === identity.taskId
        ? prior
        : {
            at: Date.now(),
            choices: isInput ? choices : choices.length ? choices : ['once', 'deny'],
            command: typeof approval?.command === 'string' ? approval.command : '',
            group,
            ...(isInput ? { hostedInput: { ...identity, threadId: String(action.thread_id) } } : { hostedApproval: identity }),
            kind: isInput ? 'clarify' : 'approval',
            member: member.name,
            memberKey: groupMemberKey(member),
            multiSelect: false,
            question: isInput ? String(approval?.question || '') : typeof approval?.description === 'string' ? approval.description : '',
            questions: isInput && Array.isArray(approval?.questions) ? approval.questions : null,
            requestId,
            sessionId: null
          }
    waiting = true
  }

  $groupClarify.set(next)
  $groupHostedNeedsYou.set({
    ...$groupHostedNeedsYou.get(),
    [group]: waiting
  })
}
