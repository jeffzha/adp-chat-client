import type { SseEvent, ErrorEvent } from './chat-v2'

interface FetchSSEOptions {
  success: (event: SseEvent) => void
  fail?: (msg?: string | null | undefined | unknown, errorEvent?: ErrorEvent) => void
  complete?: (isOk: boolean, msg?: string) => void
  resume?: (turnId: string, lastEventId: number) => Promise<any>
  signal?: AbortSignal
  maxReconnectAttempts?: number
  stateChange?: (state: WorkbenchTurnProgress) => void
}

export type WorkbenchTurnStatus =
  | 'submitting'
  | 'running'
  | 'reconnecting'
  | 'resumed'
  | 'completed'
  | 'failed_before_accept'
  | 'failed_after_accept'
  | 'cancel_requested'
  | 'cancel_confirmed'
  | 'provider_unknown'

export interface WorkbenchTurnProgress {
  status: WorkbenchTurnStatus
  turnId?: string
  attempt?: number
  maxAttempts?: number
}

type FetchFn = () => Promise<any>

const getErrorMessage = (error: unknown): string | undefined => {
  if (typeof error === 'string') {
    return error
  }
  if (error && typeof error === 'object' && 'message' in error && typeof error.message === 'string') {
    return error.message
  }
  return undefined
}

/**
 * Consume an SSE request. Workbench streams persist a Turn id and event ids;
 * an interrupted subscriber can resume without submitting the provider Turn again.
 */
export const fetchSSE = async (fetchFn: FetchFn, options: FetchSSEOptions): Promise<void> => {
  const { success, fail, complete, resume, signal, stateChange } = options
  const maxReconnectAttempts = options.maxReconnectAttempts ?? 3
  let reconnectAttempts = 0
  let lastEventId = 0
  let turnId = ''
  let terminal = false
  let response: any
  let nextFetch: FetchFn = fetchFn
  let reconnecting = false

  const report = (status: WorkbenchTurnStatus, attempt?: number) => stateChange?.({
    status,
    turnId: turnId || undefined,
    attempt,
    maxAttempts: maxReconnectAttempts,
  })

  report('submitting')

  while (true) {
    try {
      response = response ?? await nextFetch()
      for await (const line of chunkSplitter(response)) {
        if (line.startsWith('id:')) {
          const parsed = Number.parseInt(line.slice('id:'.length).trim(), 10)
          if (Number.isSafeInteger(parsed) && parsed >= 0) {
            lastEventId = parsed
          }
          continue
        }
        if (!line.startsWith('data:')) {
          continue
        }
        const payload = line.slice('data:'.length).trim()
        if (!payload || payload === '[DONE]') {
          continue
        }
        let event: SseEvent
        try {
          event = JSON.parse(payload) as SseEvent
        } catch {
          continue
        }
        const justResumed = reconnecting
        if (justResumed) {
          reconnecting = false
          report('resumed', reconnectAttempts)
        }
        if (event.Type === 'workbench.turn') {
          turnId = event.TurnId
          if (!justResumed) {
            if (event.Status === 'running') report('running')
            else if (event.Status !== 'submitted') report('submitting')
          }
          continue
        }
        if (event.Type === 'workbench.turn_status') {
          if (event.Status === 'cancel_requested') {
            report('cancel_requested')
            continue
          }
          terminal = true
          const terminalStatuses: WorkbenchTurnStatus[] = [
            'completed',
            'failed_before_accept',
            'failed_after_accept',
            'cancel_confirmed',
            'provider_unknown',
          ]
          const status = terminalStatuses.includes(event.Status as WorkbenchTurnStatus)
            ? event.Status as WorkbenchTurnStatus
            : 'provider_unknown'
          report(status)
          if (event.Status !== 'completed') {
            const message = `Workbench Turn ended with status ${event.Status}`
            fail?.(message)
            complete?.(false, message)
            return
          }
          continue
        }
        if (event.Type === 'error') {
          const errorMsg = event.Error?.Message
          report(turnId ? 'failed_after_accept' : 'failed_before_accept')
          fail?.(errorMsg, event)
          complete?.(false, errorMsg)
          return
        }
        success(event)
      }
      if (!turnId && resume && reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts += 1
        await new Promise(resolve => setTimeout(resolve, 250 * reconnectAttempts))
        response = undefined
        nextFetch = fetchFn
        continue
      }
      if (!turnId || terminal) {
        complete?.(true)
        return
      }
      throw new Error('Workbench Turn stream disconnected before terminal status')
    } catch (error) {
      if (signal?.aborted) {
        report('cancel_requested')
        fail?.(error)
        complete?.(false, getErrorMessage(error))
        return
      }
      if (turnId && resume && reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts += 1
        reconnecting = true
        report('reconnecting', reconnectAttempts)
        await new Promise(resolve => setTimeout(resolve, 250 * reconnectAttempts))
        response = undefined
        nextFetch = () => resume(turnId, lastEventId)
        continue
      }
      if (!turnId && resume && reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts += 1
        reconnecting = true
        report('reconnecting', reconnectAttempts)
        await new Promise(resolve => setTimeout(resolve, 250 * reconnectAttempts))
        response = undefined
        nextFetch = fetchFn
        continue
      }
      report(turnId ? 'provider_unknown' : 'failed_before_accept')
      fail?.(error)
      complete?.(false, getErrorMessage(error))
      return
    }
  }
}

export async function* chunkSplitter(src: any): AsyncGenerator<string> {
  let buffer = new Uint8Array(0)
  const textDecoder = new TextDecoder('utf-8')
  const newlineChar = '\n'.charCodeAt(0)

  for await (const chunk of src) {
    const newBuffer = new Uint8Array(buffer.length + chunk.length)
    newBuffer.set(buffer)
    newBuffer.set(chunk, buffer.length)
    buffer = newBuffer

    let lineStart = 0
    for (let i = 0; i < buffer.length; i++) {
      if (buffer[i] === newlineChar) {
        const lineBytes = buffer.slice(lineStart, i)
        const line = textDecoder.decode(lineBytes).trim()
        if (line) {
          yield line
        }
        lineStart = i + 1
      }
    }
    buffer = buffer.slice(lineStart)
  }

  if (buffer.length > 0) {
    const line = textDecoder.decode(buffer).trim()
    if (line) {
      yield line
    }
  }
}
