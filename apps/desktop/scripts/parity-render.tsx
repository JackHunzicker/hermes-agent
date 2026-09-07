import '../src/styles.css'

import { useStore } from '@nanostores/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// Isolated rendered acceptance: real UI/components, no backend/model execution.
import React from 'react'
import { createRoot } from 'react-dom/client'

import { registerPluginLocales } from '../src/i18n'
import { GroupRow } from '../src/plugins/hermes-bots/bot-row'
import { filesToGroupAttachments } from '../src/plugins/hermes-bots/group-attachments'
import { $groupChats, $groupNeedsYou } from '../src/plugins/hermes-bots/group-chat'
import { GroupChatWorkspace } from '../src/plugins/hermes-bots/group-chat-view'
import { speakerRoom } from '../src/plugins/hermes-bots/group-speaker-test-fixtures'
import { noteHostedRoomMentions } from '../src/plugins/hermes-bots/hosted-room-attention'
import { createHostedRoomReplayState, reduceHostedRoomEvents } from '../src/plugins/hermes-bots/hosted-room-client'
import { BOTS_LOCALES } from '../src/plugins/hermes-bots/i18n'
import { ID } from '../src/plugins/hermes-bots/shared'

registerPluginLocales(ID, BOTS_LOCALES)
const room = speakerRoom()
room.log[0].at = Date.now()

const human = reduceHostedRoomEvents(createHostedRoomReplayState({ roomId: room.roomId }), [{
  room_id: room.roomId, event_id: 'human-demo', seq: 1, kind: 'message.user',
  actor: { kind: 'user', id: 'human-demo-client', display_name: 'Jordan', connection_id: 'phone-demo' },
  payload: { text: 'Please review the original image.', thread_id: room.log[0].thread }, created_at: Date.now() / 1000
}]).messages[0]

room.log.unshift(human)

function Roster() {
  const attention = useStore($groupNeedsYou)

  return <GroupRow active group="Board" members={room.members || []} needsYou={Boolean(attention.Board)} onDisband={() => {}} onOpen={() => show('Board')} />
}

room.hostedStatus = { state: 'indeterminate', label: 'Needs attention', canRetry: true, taskId: 'selected-task' }
$groupChats.set({ Board: room, Other: { ...room, roomId: 'other-room', log: [] } })
const root = createRoot(document.getElementById('root')!)
const client = new QueryClient()

function show(group: string) {
  root.render(
    <QueryClientProvider client={client}>
      <aside style={{ position: 'absolute', width: 210, top: 100 }}><Roster /></aside>
      <main style={{ height: '100vh', marginLeft: 220 }}>
        <GroupChatWorkspace group={group} members={room.members || []} />
      </main>
    </QueryClientProvider>
  )
}

Object.assign(window, {
  parityFixture: {
    show,
    filesToGroupAttachments,
    mention: () => noteHostedRoomMentions('Board', 0, [{ ...human, seq: 2, from: { kind: 'member', name: 'Product' }, text: '@user review ready' }]),
    advanceTask: () =>
      $groupChats.set({
        ...$groupChats.get(),
        Board: { ...room, hostedStatus: { ...room.hostedStatus!, taskId: 'other-task' } }
      })
  }
})
show('Board')
