import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { ComponentProps, ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type * as hostedRuntime from './hosted-room-runtime'
import { translateBots } from './i18n-test-helper'
import type { GroupMember } from './types'

const { host, retryHostedGroupChat } = vi.hoisted(() => ({
  host: {} as Record<string, unknown>,
  retryHostedGroupChat: vi.fn(async () => true)
}))

vi.mock('./hosted-room-runtime', async importOriginal => ({
  ...(await importOriginal<typeof hostedRuntime>()),
  retryHostedGroupChat
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const { pluginSdkMock } = await import('./group-test-utils')
  const base = await pluginSdkMock(host)

  return {
    ...base,
    Button: (props: ComponentProps<'button'>) => <button type="button" {...props} />,
    cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
    Codicon: ({ name }: { name: string }) => <span aria-hidden data-icon={name} />,
    ConfirmDialog: ({
      open,
      title,
      description,
      confirmLabel,
      onConfirm
    }: {
      open: boolean
      title: ReactNode
      description: ReactNode
      confirmLabel: string
      onConfirm: () => Promise<void>
    }) =>
      open ? (
        <div role="dialog">
          <h2>{title}</h2>
          <div>{description}</div>
          <button onClick={() => void onConfirm()}>{confirmLabel}</button>
        </div>
      ) : null,
    CopyButton: () => null,
    Dialog: () => null,
    DialogContent: ({ children }: { children?: ReactNode }) => <>{children}</>,
    DialogDescription: ({ children }: { children?: ReactNode }) => <>{children}</>,
    DialogFooter: ({ children }: { children?: ReactNode }) => <>{children}</>,
    DialogHeader: ({ children }: { children?: ReactNode }) => <>{children}</>,
    DialogTitle: ({ children }: { children?: ReactNode }) => <>{children}</>,
    Input: (props: ComponentProps<'input'>) => <input {...props} />,
    relativeTime: () => 'now',
    RowButton: (props: ComponentProps<'button'>) => <button type="button" {...props} />,
    Tip: ({ children }: { children?: ReactNode }) => <>{children}</>,
    useI18n: () => ({ t: { common: { cancel: 'Cancel', save: 'Save' } } }),
    usePluginI18n: () => translateBots
  }
})

vi.mock('./group-chat-parts', () => ({
  GroupClarifyCard: () => null,
  GroupImageControls: () => null,
  GroupMentionInput: (props: { 'aria-label'?: string; value?: string }) => (
    <textarea aria-label={props['aria-label']} readOnly value={props.value} />
  )
}))

const MEMBERS: GroupMember[] = [
  { connectionId: 'gateway-a', name: 'writer', sourceScoped: true, targetProfile: 'writer' },
  { connectionId: 'gateway-a', name: 'reviewer', sourceScoped: true, targetProfile: 'reviewer' }
]

beforeEach(() => {
  vi.resetModules()
  Object.assign(host, { notify: vi.fn() })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('retries the task the user selected even when live status advances during confirmation', async () => {
  const [{ GroupChatWorkspace }, chat] = await Promise.all([import('./group-chat-view'), import('./group-chat')])

  const room = {
    continuityMode: 'gateway' as const,
    hosted: 'install:home',
    hostedConnectionId: 'gateway-a',
    hostedStatus: { state: 'indeterminate', label: 'Needs attention', canRetry: true, taskId: 'selected-task' },
    log: [],
    members: MEMBERS,
    roomId: 'room-1',
    watermarks: {}
  }

  chat.$groupChats.set({ Core: room })
  const view = render(<GroupChatWorkspace group="Core" members={MEMBERS} />)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  chat.$groupChats.set({ Core: { ...room, hostedStatus: { ...room.hostedStatus, taskId: 'other-task' } } })
  view.rerender(<GroupChatWorkspace group="Core" members={MEMBERS} />)
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button'))
  expect(retryHostedGroupChat).toHaveBeenCalledWith('Core', 'selected-task')
})

it('dismisses a pending retry when the workspace is reused for a different room', async () => {
  const [{ GroupChatWorkspace }, chat] = await Promise.all([import('./group-chat-view'), import('./group-chat')])

  const room = {
    hosted: 'install:home',
    hostedConnectionId: 'gateway-a',
    hostedStatus: { state: 'indeterminate', label: 'Needs attention', canRetry: true, taskId: 'task-a' },
    log: [],
    members: MEMBERS,
    roomId: 'room-1',
    watermarks: {}
  }

  chat.$groupChats.set({ Core: room, Other: { ...room, roomId: 'room-2' } })
  const view = render(<GroupChatWorkspace group="Core" members={MEMBERS} />)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(screen.getByRole('dialog')).toBeTruthy()
  view.rerender(<GroupChatWorkspace group="Other" members={MEMBERS} />)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(retryHostedGroupChat).not.toHaveBeenCalled()
})
