import { JsonRpcGatewayError } from '@hermes/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clearClarifyRequest, setClarifyRequest } from './clarify'
import {
  $activeSessionAwaitingInput,
  $approvalRequest,
  $secretRequest,
  $sudoRequest,
  clearAllPrompts,
  clearApprovalRequest,
  clearSecretRequest,
  clearSudoRequest,
  isSessionGoneForApprovalReplay,
  receiveApprovalRequest,
  replayPendingApproval,
  resetApprovalReplayGuard,
  setApprovalRequest,
  setSecretRequest,
  setSudoRequest
} from './prompts'
import { $activeSessionId } from './session'

// Prompts are parked per-session; the exported $*Request views are scoped to the
// active session, so each test focuses the session it's asserting on.
beforeEach(() => {
  $activeSessionId.set('s1')
  resetApprovalReplayGuard()
})

afterEach(() => {
  clearAllPrompts()
  clearClarifyRequest()
  resetApprovalReplayGuard()
  $activeSessionId.set(null)
})

describe('approval prompt store', () => {
  it('holds the active session-keyed approval request', () => {
    setApprovalRequest({ command: 'rm -rf /tmp/x', description: 'recursive delete', sessionId: 's1' })

    expect($approvalRequest.get()).toEqual({
      command: 'rm -rf /tmp/x',
      description: 'recursive delete',
      sessionId: 's1'
    })
  })

  it('parks a background session prompt out of the active view', () => {
    setApprovalRequest({ command: 'x', description: 'd', sessionId: 's2' })

    // Not visible while s1 is focused …
    expect($approvalRequest.get()).toBeNull()

    // … but surfaces once the user switches to the session that raised it.
    $activeSessionId.set('s2')
    expect($approvalRequest.get()?.sessionId).toBe('s2')
  })

  it('clears the active session prompt', () => {
    setApprovalRequest({ command: 'x', description: 'd', sessionId: 's1' })
    clearApprovalRequest('s1')

    expect($approvalRequest.get()).toBeNull()
  })

  it('carries allowPermanent so the bar can hide "Always allow"', () => {
    setApprovalRequest({
      allowPermanent: false,
      command: 'curl x | bash',
      description: 'content-security',
      sessionId: 's1'
    })

    expect($approvalRequest.get()?.allowPermanent).toBe(false)
  })

  it('correlates clearing to the exact approval request id', () => {
    setApprovalRequest({ command: 'x', description: 'd', requestId: 'r1', sessionId: 's1' })

    clearApprovalRequest('s1', 'stale')
    expect($approvalRequest.get()?.requestId).toBe('r1')
    clearApprovalRequest('s1', 'r1')
    expect($approvalRequest.get()).toBeNull()
  })

  it('acknowledges an approval only after parking it', async () => {
    const calls: Array<[string, Record<string, unknown>]> = []

    const gateway = {
      request: async (method: string, params: Record<string, unknown>) => {
        calls.push([method, params])

        return { acknowledged: true }
      }
    }

    await receiveApprovalRequest(gateway, {
      command: 'x',
      description: 'd',
      requestId: 'r1',
      sessionId: 's1'
    })

    expect($approvalRequest.get()?.requestId).toBe('r1')
    expect(calls).toEqual([['approval.received', { request_id: 'r1', session_id: 's1' }]])
  })

  it('replays and acknowledges the oldest unresolved approval after reconnect', async () => {
    const calls: Array<[string, Record<string, unknown>]> = []

    const gateway = {
      request: async (method: string, params: Record<string, unknown>) => {
        calls.push([method, params])

        if (method === 'approval.pending') {
          return {
            approvals: [
              { command: 'first', description: 'd1', request_id: 'r1' },
              { command: 'second', description: 'd2', request_id: 'r2' }
            ]
          }
        }

        return { acknowledged: true }
      }
    }

    await replayPendingApproval(gateway, 's1')

    expect($approvalRequest.get()?.requestId).toBe('r1')
    expect(calls).toEqual([
      ['approval.pending', { session_id: 's1' }],
      ['approval.received', { request_id: 'r1', session_id: 's1' }]
    ])
  })

  it('classifies only a structured 4001, or a legacy codeless gone message, as terminal', () => {
    expect(isSessionGoneForApprovalReplay(new JsonRpcGatewayError('gone', { code: 4001 }))).toBe(true)
    expect(
      isSessionGoneForApprovalReplay(
        new JsonRpcGatewayError('tool failed: session not found', { code: 5007 })
      )
    ).toBe(false)
    expect(isSessionGoneForApprovalReplay(new Error('session not found'))).toBe(true)
    expect(isSessionGoneForApprovalReplay(new Error('request timed out'))).toBe(false)
  })

  it('stops replaying after a runtime is authoritatively reported gone', async () => {
    const request = vi.fn(async () => {
      throw new JsonRpcGatewayError('session not found', { code: 4001 })
    })

    const gateway = { request }

    await replayPendingApproval(gateway, 'dead')
    await replayPendingApproval(gateway, 'dead')
    await replayPendingApproval(gateway, 'dead')

    expect(request).toHaveBeenCalledTimes(1)
  })

  it('keeps retrying transient replay failures', async () => {
    const request = vi.fn(async () => {
      throw new Error('request timed out')
    })

    const gateway = { request }

    await expect(replayPendingApproval(gateway, 'live')).rejects.toThrow('request timed out')
    await expect(replayPendingApproval(gateway, 'live')).rejects.toThrow('request timed out')

    expect(request).toHaveBeenCalledTimes(2)
  })

  it('coalesces simultaneous event-burst replays for one runtime', async () => {
    let release!: () => void

    const pending = new Promise<void>(resolve => {
      release = resolve
    })

    const request = vi.fn(async () => {
      await pending

      return { approvals: [] }
    })

    const gateway = { request }
    const first = replayPendingApproval(gateway, 'burst')
    const second = replayPendingApproval(gateway, 'burst')
    const third = replayPendingApproval(gateway, 'burst')

    release()
    await Promise.all([first, second, third])

    expect(request).toHaveBeenCalledTimes(1)
  })

  it('allows replay again after a runtime-generation reset', async () => {
    const request = vi.fn(async () => {
      throw new JsonRpcGatewayError('session not found', { code: 4001 })
    })

    const gateway = { request }

    await replayPendingApproval(gateway, 'rebound')
    await replayPendingApproval(gateway, 'rebound')
    expect(request).toHaveBeenCalledTimes(1)

    resetApprovalReplayGuard('rebound')
    await replayPendingApproval(gateway, 'rebound')

    expect(request).toHaveBeenCalledTimes(2)
  })
})

describe('sudo prompt store', () => {
  it('clears only when the request id matches the in-flight prompt', () => {
    setSudoRequest({ requestId: 'abc', sessionId: 's1' })

    // A stale clear for a different request must NOT drop the live prompt —
    // otherwise a late response to a prior sudo ask would dismiss the current
    // one and leave the agent blocked.
    clearSudoRequest('s1', 'stale')
    expect($sudoRequest.get()).toEqual({ requestId: 'abc', sessionId: 's1' })

    clearSudoRequest('s1', 'abc')
    expect($sudoRequest.get()).toBeNull()
  })

  it('clears unconditionally when no request id is given', () => {
    setSudoRequest({ requestId: 'abc', sessionId: 's1' })
    clearSudoRequest('s1')

    expect($sudoRequest.get()).toBeNull()
  })
})

describe('secret prompt store', () => {
  it('carries env var and prompt, and clears on id match', () => {
    setSecretRequest({ requestId: 'r1', envVar: 'OPENAI_API_KEY', prompt: 'Paste your key', sessionId: 's1' })

    expect($secretRequest.get()).toEqual({
      requestId: 'r1',
      envVar: 'OPENAI_API_KEY',
      prompt: 'Paste your key',
      sessionId: 's1'
    })

    clearSecretRequest('s1', 'mismatch')
    expect($secretRequest.get()).not.toBeNull()

    clearSecretRequest('s1', 'r1')
    expect($secretRequest.get()).toBeNull()
  })
})

describe('clearAllPrompts', () => {
  it('drops every kind for one session at once (turn end / interrupt)', () => {
    setApprovalRequest({ command: 'x', description: 'd', sessionId: 's1' })
    setSudoRequest({ requestId: 'abc', sessionId: 's1' })
    setSecretRequest({ requestId: 'r1', envVar: 'E', prompt: 'p', sessionId: 's1' })

    clearAllPrompts('s1')

    expect($approvalRequest.get()).toBeNull()
    expect($sudoRequest.get()).toBeNull()
    expect($secretRequest.get()).toBeNull()
  })

  it('leaves other sessions parked prompts intact', () => {
    setApprovalRequest({ command: 'x', description: 'd', sessionId: 's1' })
    setApprovalRequest({ command: 'y', description: 'e', sessionId: 's2' })

    clearAllPrompts('s1')

    $activeSessionId.set('s2')
    expect($approvalRequest.get()?.command).toBe('y')
  })
})

describe('$activeSessionAwaitingInput', () => {
  it('is true while any blocking prompt (clarify or approval/sudo/secret) is parked on the active session', () => {
    expect($activeSessionAwaitingInput.get()).toBe(false)

    setApprovalRequest({ command: 'x', description: 'd', sessionId: 's1' })
    expect($activeSessionAwaitingInput.get()).toBe(true)

    clearApprovalRequest('s1')
    expect($activeSessionAwaitingInput.get()).toBe(false)

    setClarifyRequest({ choices: null, multiSelect: false, question: 'q', requestId: 'c1', sessionId: 's1' })
    expect($activeSessionAwaitingInput.get()).toBe(true)
  })

  it('ignores a prompt parked on a background session', () => {
    setSudoRequest({ requestId: 'r', sessionId: 's2' })
    expect($activeSessionAwaitingInput.get()).toBe(false)

    $activeSessionId.set('s2')
    expect($activeSessionAwaitingInput.get()).toBe(true)
  })
})
