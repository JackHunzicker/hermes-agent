import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  ErrorState,
  Input,
  Loader,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { HostedPolicyError, loadHostedPolicy, saveHostedPolicy, validateResponderPolicy } from './hosted-room-policy'
import type { HostedPolicyErrorCode, HostedPolicySnapshot, ResponderPolicy } from './hosted-room-policy'
import { useBots } from './i18n'
import type { BotsText } from './i18n'

interface PolicyControlProps {
  group: string
  roomId: string
  threadId?: string
}

interface PolicyFailure {
  value: unknown
  fallback: 'readFailed' | 'saveFailed'
}

function policyErrorMessage(error: unknown, text: BotsText['policy'], fallback: PolicyFailure['fallback']) {
  if (!(error instanceof HostedPolicyError)) {
    return text[fallback]
  }

  const messages: Record<HostedPolicyErrorCode, string> = {
    roomChanged: text.roomChanged,
    authorityUnavailable: text.authorityUnavailable,
    hostUpdateRequired: text.hostUpdateRequired,
    invalidPolicy: text.invalidPolicy,
    invalidResponse: text.invalidResponse,
    peerUnsupported: text.peerUnsupported,
    readFailed: text.readFailed,
    saveFailed: text.saveFailed,
    verificationFailed: text.verificationFailed
  }

  return messages[error.code]
}

function PolicySelect({
  label,
  value,
  options,
  onChange,
  disabled
}: {
  label: string
  value: string
  options: Array<{ value: string; label: string }>
  onChange: (value: string) => void
  disabled: boolean
}) {
  return (
    <label className="grid gap-1 text-xs">
      <span>{label}</span>
      <Select disabled={disabled} onValueChange={onChange} value={value}>
        <SelectTrigger aria-label={label}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map(option => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  )
}

export function HostedPolicyControl(props: PolicyControlProps) {
  return <PolicyControl key={JSON.stringify([props.group, props.roomId, props.threadId])} {...props} />
}

function PolicyControl({ group, roomId, threadId }: PolicyControlProps) {
  const [open, setOpen] = useState(false)
  const p = useBots().policy

  return (
    <>
      <Button onClick={() => setOpen(true)} size="xs" variant="text">
        {threadId ? p.threadAction : p.roomAction}
      </Button>
      {open ? (
        <HostedPolicyDialog group={group} onClose={() => setOpen(false)} roomId={roomId} threadId={threadId} />
      ) : null}
    </>
  )
}

function HostedPolicyDialog({ group, roomId, threadId, onClose }: PolicyControlProps & { onClose: () => void }) {
  const p = useBots().policy
  const [snapshot, setSnapshot] = useState<HostedPolicySnapshot | null>(null)
  const [draft, setDraft] = useState<ResponderPolicy | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<PolicyFailure | null>(null)
  const [verified, setVerified] = useState(false)
  const generation = useRef(0)

  const read = async () => {
    const token = ++generation.current

    setBusy(true)
    setError(null)
    setVerified(false)
    setSnapshot(null)
    setDraft(null)

    try {
      const next = await loadHostedPolicy(group, roomId)

      if (generation.current === token) {
        setSnapshot(next)
        setDraft(next.policy)
      }
    } catch (failure) {
      if (generation.current === token) {
        setError({ value: failure, fallback: 'readFailed' })
      }
    } finally {
      if (generation.current === token) {
        setBusy(false)
      }
    }
  }

  useEffect(() => {
    void read()
    const lifetime = generation

    return () => {
      lifetime.current++
    }
    // Each mounted dialog is bound to one immutable control key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  let invalid = ''

  if (draft && snapshot) {
    try {
      validateResponderPolicy(draft, snapshot.members)
    } catch (failure) {
      invalid = policyErrorMessage(failure, p, 'saveFailed')
    }
  }

  const peer = snapshot?.members.some(member => member.target.kind === 'peer')
  const readOnly = Boolean(threadId || peer)
  const disabled = busy || readOnly || !snapshot

  const save = async () => {
    if (!snapshot || !draft || busy || readOnly || invalid) {
      return
    }

    const token = ++generation.current

    setBusy(true)
    setError(null)
    setVerified(false)

    try {
      const next = await saveHostedPolicy(snapshot, draft, crypto.randomUUID())

      if (generation.current === token) {
        setSnapshot(next)
        setDraft(next.policy)
        setVerified(true)
      }
    } catch (failure) {
      if (generation.current === token) {
        setError({ value: failure, fallback: 'saveFailed' })
        setSnapshot(null)
        setDraft(null)
      }
    } finally {
      if (generation.current === token) {
        setBusy(false)
      }
    }
  }

  return (
    <Dialog
      onOpenChange={value => {
        if (!value) {
          onClose()
        }
      }}
      open
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{threadId ? p.threadAction : p.roomAction}</DialogTitle>
          <DialogDescription>
            {threadId ? p.threadContext(threadId, group) : p.roomContext(group)}
            <br />
            {p.roomId(roomId)}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <p className="text-xs text-(--ui-text-secondary)">{threadId ? p.threadReadOnly : p.roomHint}</p>
          {peer ? <p className="text-xs text-(--ui-text-secondary)">{p.peerReadOnly}</p> : null}
          {snapshot ? <p className="text-xs text-(--ui-text-tertiary)">{p.revision(snapshot.revision)}</p> : null}
          {draft ? (
            <fieldset className="grid gap-3" disabled={disabled}>
              <PolicySelect
                disabled={disabled}
                label={p.continuationMode}
                onChange={mode => setDraft({ ...draft, mode: mode as ResponderPolicy['mode'] })}
                options={[
                  { value: 'legacy_bounded', label: p.legacyBounded },
                  { value: 'event_driven', label: p.eventDriven }
                ]}
                value={draft.mode}
              />
              <PolicySelect
                disabled={disabled}
                label={p.defaultResponder}
                onChange={value =>
                  setDraft({ ...draft, default_responder: value as ResponderPolicy['default_responder'] })
                }
                options={[
                  { value: 'all', label: p.allMembers },
                  { value: 'leader', label: p.leader },
                  { value: 'mentions_only', label: p.mentionsOnly }
                ]}
                value={draft.default_responder}
              />
              <PolicySelect
                disabled={disabled}
                label={p.leaderMember}
                onChange={value => setDraft({ ...draft, leader_member_id: value === '__none__' ? null : value })}
                options={[
                  { value: '__none__', label: p.noLeader },
                  ...(snapshot?.members || []).map(member => ({
                    value: member.member_id,
                    label: `${member.display_name || member.handle} · ${member.member_id}`
                  }))
                ]}
                value={draft.leader_member_id ?? '__none__'}
              />
              <div className="grid grid-cols-2 gap-3">
                <label className="grid gap-1 text-xs">
                  <span>{p.maxTurns}</span>
                  <Input
                    aria-label={p.maxTurns}
                    max={32}
                    min={1}
                    onChange={event => setDraft({ ...draft, max_turns_per_window: event.target.valueAsNumber })}
                    step={1}
                    type="number"
                    value={Number.isNaN(draft.max_turns_per_window) ? '' : draft.max_turns_per_window}
                  />
                </label>
                <label className="grid gap-1 text-xs">
                  <span>{p.windowSeconds}</span>
                  <Input
                    aria-label={p.windowSeconds}
                    max={3600}
                    min={1}
                    onChange={event => setDraft({ ...draft, window_seconds: event.target.valueAsNumber })}
                    step={1}
                    type="number"
                    value={Number.isNaN(draft.window_seconds) ? '' : draft.window_seconds}
                  />
                </label>
              </div>
            </fieldset>
          ) : null}
          {busy ? (
            <div aria-label={p.reading} role="status">
              <Loader />
            </div>
          ) : null}
          {error ? (
            <div role="alert">
              <ErrorState description={policyErrorMessage(error.value, p, error.fallback)} title={p.unavailableTitle} />
            </div>
          ) : null}
          {invalid ? <p role="alert">{invalid}</p> : null}
          {verified ? <p className="text-xs text-(--ui-text-secondary)" role="status">{p.verified}</p> : null}
        </div>
        <DialogFooter>
          <Button disabled={busy} onClick={() => void read()} variant="secondary">
            {p.reload}
          </Button>
          {!readOnly ? (
            <Button disabled={disabled || Boolean(invalid) || !draft} onClick={() => void save()}>
              {p.save}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
