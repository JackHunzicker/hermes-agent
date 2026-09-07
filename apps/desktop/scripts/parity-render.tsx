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
import { $hostedRoomCapabilities } from '../src/plugins/hermes-bots/hosted-room-capability-state'
import { classifyHostedRoomCapability, createHostedRoomReplayState, reduceHostedRoomEvents } from '../src/plugins/hermes-bots/hosted-room-client'
import { BOTS_LOCALES } from '../src/plugins/hermes-bots/i18n'
import { ID } from '../src/plugins/hermes-bots/shared'
import { ThemeProvider } from '../src/themes/context'

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
    <ThemeProvider><QueryClientProvider client={client}>
      <aside style={{ position: 'absolute', width: 210, top: 100 }}><Roster /></aside>
      <main style={{ height: '100vh', marginLeft: 220 }}>
        <GroupChatWorkspace group={group} members={room.members || []} />
      </main>
    </QueryClientProvider></ThemeProvider>
  )
}

Object.assign(window, {
  parityFixture: {
    show,
    filesToGroupAttachments,
    historyControls: () => {
      const connectionId = 'fixture-only'
      $hostedRoomCapabilities.set({ [connectionId]: classifyHostedRoomCapability({
        driver: true, persistent_process: true, authority_gateway_id: 'install:home',
        methods: ['groups.history', 'groups.history.search', 'groups.read.get', 'groups.read.mark',
          'groups.message.edit', 'groups.message.delete', 'groups.message.react', 'groups.stop_scope'],
        features: ['message_history_projection_v1', 'message_history_search_v1', 'room_read_cursors_v1', 'message_mutations_v1', 'scoped_stop_v1']
      }, { connectionId }) })
      $groupChats.set({ ...$groupChats.get(), Board: {
        ...room, hostedConnectionId: connectionId, running: true,
        hostedStatus: { state: 'working', label: 'Working', canStop: true },
        hostedHistory: { snapshotSeq: 2, messages: { 'human-demo': {
          event_id: 'human-demo', seq: 1, thread_id: 'thread-1',
          actor: { kind: 'user', id: 'desktop', display_name: 'Jordan' },
          original_text: human.text, text: human.text, revision: 1, deleted: false, attachments: [],
          reactions: [{ reaction: '👍', actors: [{ kind: 'user', id: 'phone-demo' }] }]
        } } },
        hostedRead: { room_id: 'room-1', thread_id: null, reader: { kind: 'user', id: 'desktop' }, through_seq: 0, latest_seq: 2, unread_count: 1 }
      } })
      show('Board')
    },
    mention: () => noteHostedRoomMentions('Board', 0, [{ ...human, seq: 2, from: { kind: 'member', name: 'Product' }, text: '@user review ready' }]),
    advanceTask: () =>
      $groupChats.set({
        ...$groupChats.get(),
        Board: { ...room, hostedStatus: { ...room.hostedStatus!, taskId: 'other-task' } }
      })
  }
})
show('Board')
