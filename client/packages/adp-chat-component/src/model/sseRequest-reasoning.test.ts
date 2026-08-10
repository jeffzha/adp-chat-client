// @ts-nocheck -- executed by Bun's test runner; Bun types are not a production dependency.
import { describe, expect, test } from 'bun:test'
import { fetchSSE, type WorkbenchTurnStatus } from './sseRequest-reasoning'

const stream = async function* (...lines: string[]) {
  const encoder = new TextEncoder()
  for (const line of lines) yield encoder.encode(`${line}\n`)
}

describe('workbench durable Turn stream', () => {
  test('announces accepted and completed states', async () => {
    const states: WorkbenchTurnStatus[] = []
    const events: string[] = []

    await fetchSSE(
      async () => stream(
        'data: {"Type":"workbench.turn","TurnId":"wt_1","ClientRequestId":"c1","Status":"running"}',
        'id: 1',
        'data: {"Type":"text.delta","MessageId":"m1","ContentIndex":0,"Text":"ok"}',
        'id: 2',
        'data: {"Type":"workbench.turn_status","TurnId":"wt_1","Status":"completed"}',
      ),
      {
        success: event => events.push(event.Type),
        stateChange: state => states.push(state.status),
      },
    )

    expect(events).toEqual(['text.delta'])
    expect(states).toEqual(['submitting', 'running', 'completed'])
  })

  test('resumes from the last persisted event without resubmitting the Turn', async () => {
    const states: WorkbenchTurnStatus[] = []
    const resumeCalls: Array<[string, number]> = []
    let submitCalls = 0

    await fetchSSE(
      async () => {
        submitCalls += 1
        return stream(
          'data: {"Type":"workbench.turn","TurnId":"wt_2","ClientRequestId":"c2","Status":"running"}',
          'id: 7',
          'data: {"Type":"text.delta","MessageId":"m2","ContentIndex":0,"Text":"partial"}',
        )
      },
      {
        success: () => undefined,
        maxReconnectAttempts: 1,
        stateChange: state => states.push(state.status),
        resume: async (turnId, lastEventId) => {
          resumeCalls.push([turnId, lastEventId])
          return stream(
            'data: {"Type":"workbench.turn","TurnId":"wt_2","ClientRequestId":"c2","Status":"running"}',
            'id: 8',
            'data: {"Type":"workbench.turn_status","TurnId":"wt_2","Status":"completed"}',
          )
        },
      },
    )

    expect(submitCalls).toBe(1)
    expect(resumeCalls).toEqual([['wt_2', 7]])
    expect(states).toEqual(['submitting', 'running', 'reconnecting', 'resumed', 'completed'])
  })

  test('surfaces an unknown provider state when replay is exhausted', async () => {
    const states: WorkbenchTurnStatus[] = []
    await fetchSSE(
      async () => stream('data: {"Type":"workbench.turn","TurnId":"wt_3","ClientRequestId":"c3","Status":"running"}'),
      {
        success: () => undefined,
        maxReconnectAttempts: 0,
        resume: async () => stream(),
        stateChange: state => states.push(state.status),
      },
    )
    expect(states.at(-1)).toBe('provider_unknown')
  })

  test('treats cancel_requested as intent and waits for a real terminal state', async () => {
    const states: WorkbenchTurnStatus[] = []
    await fetchSSE(
      async () => stream(
        'data: {"Type":"workbench.turn","TurnId":"wt_4","ClientRequestId":"c4","Status":"running"}',
        'id: 2',
        'data: {"Type":"workbench.turn_status","TurnId":"wt_4","Status":"cancel_requested"}',
        'id: 3',
        'data: {"Type":"workbench.turn_status","TurnId":"wt_4","Status":"completed"}',
      ),
      {
        success: () => undefined,
        stateChange: state => states.push(state.status),
      },
    )

    expect(states).toEqual(['submitting', 'running', 'cancel_requested', 'completed'])
  })
})
