import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { HostedHistoryToolbar, HostedThreadActions } from './hosted-room-history-controls'
const { search, mark, stop } = vi.hoisted(() => ({ search: vi.fn(), mark: vi.fn().mockResolvedValue({}), stop: vi.fn().mockResolvedValue({}) }))
vi.mock('./hosted-room-actions', () => ({ searchHostedHistory: search, markHostedRead: mark, stopHostedScope: stop }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('searches canonical history and marks only the rendered snapshot on explicit intent', async () => {
  const result = { messages: { found: { event_id: 'found' } }, snapshotSeq: 12 }
  search.mockResolvedValue(result)
  const onResults = vi.fn()
  render(<HostedHistoryToolbar capability={{ methods: ['groups.history.search', 'groups.read.mark'], features: ['message_history_search_v1', 'room_read_cursors_v1'] }} group="Board" onResults={onResults} room={{ roomId: 'r', log: [], watermarks: {}, hostedHistory: { messages: {}, snapshotSeq: 10 } }} />)
  expect(mark).not.toHaveBeenCalled()
  fireEvent.change(screen.getByRole('textbox', { name: 'Search room history' }), { target: { value: 'needle' } })
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
  await waitFor(() => expect(onResults).toHaveBeenCalledWith(result))
  expect(search).toHaveBeenCalledWith('Board', 'needle')
  fireEvent.click(screen.getByRole('button', { name: 'Mark room read' }))
  await waitFor(() => expect(mark).toHaveBeenCalledWith('Board', 10))
})
it('confirms an exact thread stop, never room-wide', async () => {
  render(<HostedThreadActions capability={{ methods: ['groups.stop_scope'], features: ['scoped_stop_v1'] }} group="Board" thread="thread-a" throughSeq={8} />)
  fireEvent.click(screen.getByRole('button', { name: 'Stop this thread' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm thread stop' }))
  await waitFor(() => expect(stop).toHaveBeenCalledWith('Board', { kind: 'thread', thread_id: 'thread-a' }, expect.any(String)))
})
