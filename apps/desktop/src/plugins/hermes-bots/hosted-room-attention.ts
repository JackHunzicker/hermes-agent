import { $groupNeedsYou } from './group-chat'
import type { HostedReplayMessage } from './hosted-room-client'

/** A replay cursor is delivery, not read state. Only newly delivered mentions
 * can raise this window's attention badge; replay must not undo dismissal. */
export function noteHostedRoomMentions(group: string, previousSeq: number, messages: HostedReplayMessage[]) {
  if (
    messages.some(
      message => message.seq > previousSeq && message.from.kind === 'member' && /@user\b/i.test(message.text)
    )
  ) {
    $groupNeedsYou.set({ ...$groupNeedsYou.get(), [group]: true })
  }
}
