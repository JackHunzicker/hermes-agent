import type * as HermesSdk from '@hermes/plugin-sdk'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { translateBots } from './i18n-test-helper'

const { request, route, rooms, capabilities } = vi.hoisted(() => ({
  request: vi.fn(),
  route: vi.fn(),
  rooms: { get: vi.fn() },
  capabilities: { get: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()

  return { ...sdk, usePluginI18n: () => translateBots }
})
vi.mock('./group-chat', () => ({
  $groupChats: rooms,
  groupChatHostedGateway: (room: { hosted: string }) => room.hosted
}))
vi.mock('./hosted-room-runtime', () => ({ hostedRouteForRoom: route }))
vi.mock('./hosted-room-transport', () => ({ requestHostedConnection: request }))
vi.mock('./hosted-room-capability-state', () => ({ $hostedRoomCapabilities: capabilities }))
import { HostedHistoryToolbar, HostedThreadActions } from './hosted-room-history-controls'
import { HostedPolicyControl } from './hosted-room-policy-control'

const state = (roomId = 'room', revision = 7) => ({
  room: {
    room_id: roomId,
    authority_gateway_id: 'authority',
    authority_epoch: 3,
    revision,
    responder_policy: {
      mode: 'event_driven',
      default_responder: 'leader',
      leader_member_id: 'stable-a',
      max_turns_per_window: revision,
      window_seconds: 120
    },
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
afterEach(cleanup)
it('opens the canonical room form, submits CAS through the real action and waits for authoritative read-back', async () => {
  const mounted = render(
    <HostedHistoryToolbar
      capability={undefined}
      group="Board"
      onResults={() => undefined}
      room={{ roomId: 'room', hosted: 'authority', log: [], watermarks: {} }}
    />
  )

  fireEvent.click(screen.getByRole('button', { name: 'Responder policy' }))
  await screen.findByRole('spinbutton', { name: 'Maximum turns per window' })
  expect(screen.getByText(/Room defaults/)).toBeTruthy()
  expect(screen.getByRole('combobox', { name: 'Leader member' }).textContent).toContain('stable-a')
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Maximum turns per window' }), { target: { value: '8' } })
  let resolve!: (value: unknown) => void
  request
    .mockResolvedValueOnce(state())
    .mockResolvedValueOnce({ room: { revision: 8 } })
    .mockReturnValueOnce(
      new Promise(done => {
        resolve = done
      })
    )
  fireEvent.click(screen.getByRole('button', { name: 'Save room defaults' }))
  await waitFor(() => expect(request).toHaveBeenCalledTimes(4))
  expect(request.mock.calls[2][2]).toEqual({
    room_id: 'room',
    expected_revision: 7,
    event_id: expect.any(String),
    policy: { ...state().room.responder_policy, max_turns_per_window: 8 }
  })
  expect(screen.queryByText(/Verified/)).toBeNull()
  resolve(state('room', 8))
  await screen.findByText(/Verified/)
  expect((screen.getByRole('spinbutton', { name: 'Maximum turns per window' }) as HTMLInputElement).value).toBe('8')
  mounted.unmount()
})
it('never promotes a thread edit to room scope and discards stale reads after switching rooms', async () => {
  const mounted = render(
    <HostedThreadActions capability={undefined} group="Board" roomId="room" thread="thread-a" throughSeq={9} />
  )

  fireEvent.click(screen.getByRole('button', { name: 'Thread responder policy' }))
  await screen.findByRole('spinbutton', { name: 'Maximum turns per window' })
  expect(screen.getByText(/Thread overrides are unavailable/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Save room defaults' })).toBeNull()
  expect((screen.getByRole('combobox', { name: 'Default responder' }) as HTMLButtonElement).disabled).toBe(true)
  mounted.unmount()
  let resolve!: (value: unknown) => void
  request.mockReturnValueOnce(
    new Promise(done => {
      resolve = done
    })
  )
  const control = render(<HostedPolicyControl group="Board" roomId="room" />)

  fireEvent.click(screen.getByRole('button', { name: 'Responder policy' }))
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
  rooms.get.mockReturnValue({
    Board: { roomId: 'replacement', hosted: 'authority', hostedConnectionId: 'home', hostedEpoch: 3 }
  })
  control.rerender(<HostedPolicyControl group="Board" roomId="replacement" />)
  resolve(state())
  expect(screen.queryByRole('dialog')).toBeNull()
  capabilities.get.mockReturnValue({ home: { authorityId: 'authority', methods: ['groups.state'], features: [] } })
  fireEvent.click(screen.getByRole('button', { name: 'Responder policy' }))
  await screen.findByText(/Update the room host/)
  expect(screen.queryByRole('spinbutton')).toBeNull()
  expect((screen.getByRole('button', { name: 'Save room defaults' }) as HTMLButtonElement).disabled).toBe(true)
  expect(request).toHaveBeenCalledTimes(2)
})
