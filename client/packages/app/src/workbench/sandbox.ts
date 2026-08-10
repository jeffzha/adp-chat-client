import Cookies from 'js-cookie'
import instance from '../service/axiosInstance'
import { getBaseURL } from '../utils/url'
import {
  buildSameOriginPtyURL,
  projectSandboxCodeExecution,
  type PublicSandboxCodeExecution,
} from './sandboxBoundary'
export { describeSandboxError } from './sandboxBoundary'

const CSRF_COOKIE = 'claw_workbench_csrf'

export type ManagedSandbox = {
  sandbox_id: string
  conversation_id: string
  status: string
  timeout_seconds: number
  expires_at: string | null
  created_at: string | null
  updated_at: string | null
}

export type ShellExecution = {
  stdout: string
  stderr: string
  exit_code: number
}

export type CodeExecution = PublicSandboxCodeExecution

export type ShellChunk = {
  type: 'stdout' | 'stderr' | 'exit' | 'error'
  data: string
  exit_code: number | null
  code?: string
}

export type SandboxPtyControl = {
  sendInput: (data: string) => void
  resize: (rows: number, cols: number) => void
  close: () => void
}

type SandboxPtyTicket = {
  pty_session_id: string
  ticket: string
  expires_at: string
  protocol: 'claw-workbench-pty-v1'
}

export type SandboxPtyEvent =
  | { type: 'ready'; pty_session_id: string }
  | { type: 'exit'; exit_code: number }
  | { type: 'error'; code: string }

const mutationHeaders = () => {
  const token = Cookies.get(CSRF_COOKIE)
  if (!token) throw new Error('workbench CSRF token is unavailable')
  return { 'X-Workbench-CSRF': token }
}

export const createSandbox = async (
  conversationId: string,
  timeoutSeconds = 600,
): Promise<ManagedSandbox> => (
  await instance.post('/sandbox', {
    conversation_id: conversationId,
    timeout_seconds: timeoutSeconds,
  }, { headers: mutationHeaders() }) as unknown as ManagedSandbox
)

export const querySandbox = async (
  sandboxId: string,
  conversationId: string,
): Promise<ManagedSandbox> => (
  await instance.get(`/sandbox/${encodeURIComponent(sandboxId)}`, {
    params: { conversation_id: conversationId },
  }) as unknown as ManagedSandbox
)

export const changeSandboxState = async (
  sandboxId: string,
  conversationId: string,
  action: 'pause' | 'resume' | 'stop',
): Promise<ManagedSandbox> => (
  await instance.post(
    `/sandbox/${encodeURIComponent(sandboxId)}/${action}`,
    { conversation_id: conversationId },
    { headers: mutationHeaders() },
  ) as unknown as ManagedSandbox
)

export const executeSandboxShell = async (
  sandboxId: string,
  input: { conversation_id: string; command: string; cwd?: string; timeout_seconds: number },
): Promise<ShellExecution> => (
  await instance.post(`/sandbox/${encodeURIComponent(sandboxId)}/shell`, input, {
    headers: mutationHeaders(),
  }) as unknown as ShellExecution
)

export const executeSandboxCode = async (
  sandboxId: string,
  input: {
    conversation_id: string
    code: string
    language?: 'python' | 'javascript' | 'typescript' | 'java' | 'r' | 'bash'
    timeout_seconds: number
  },
): Promise<CodeExecution> => projectSandboxCodeExecution(
  await instance.post(`/sandbox/${encodeURIComponent(sandboxId)}/code`, input, {
    headers: mutationHeaders(),
  }),
)

export const streamSandboxShell = async (
  sandboxId: string,
  input: { conversation_id: string; command: string; cwd?: string; timeout_seconds: number },
  onChunk: (chunk: ShellChunk) => void,
  signal?: AbortSignal,
): Promise<void> => {
  const baseURL = getBaseURL()
  const base = new URL(baseURL.endsWith('/') ? baseURL : `${baseURL}/`, window.location.href)
  const url = new URL(`sandbox/${encodeURIComponent(sandboxId)}/shell/stream`, base)
  if (url.origin !== window.location.origin) throw new Error('sandbox stream must use the current origin')
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...mutationHeaders() },
    body: JSON.stringify(input),
    signal,
  })
  if (!response.ok || !response.body) throw new Error(`sandbox stream failed (${response.status})`)

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let pending = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      pending += value
      const lines = pending.split('\n')
      pending = lines.pop() || ''
      for (const line of lines) {
        if (line.trim()) onChunk(JSON.parse(line) as ShellChunk)
      }
    }
    if (pending.trim()) onChunk(JSON.parse(pending) as ShellChunk)
  } finally {
    reader.releaseLock()
  }
}

export const openSandboxPty = async (
  sandboxId: string,
  input: { conversation_id: string; rows?: number; cols?: number; timeout_seconds?: number },
  handlers: {
    onOutput: (data: Uint8Array) => void
    onControl: (event: SandboxPtyEvent) => void
    onClose?: () => void
  },
): Promise<SandboxPtyControl> => {
  const minted = await instance.post(
    `/sandbox/${encodeURIComponent(sandboxId)}/pty`,
    input,
    { headers: mutationHeaders() },
  ) as unknown as SandboxPtyTicket
  if (
    minted.protocol !== 'claw-workbench-pty-v1'
    || !/^pty_[0-9a-f]{32}$/.test(minted.pty_session_id)
    || !/^[A-Za-z0-9_-]{40,64}$/.test(minted.ticket)
  ) throw new Error('sandbox PTY ticket is invalid')

  const url = buildSameOriginPtyURL(getBaseURL(), window.location.href, sandboxId)
  const socket = new WebSocket(url, [minted.protocol, `ticket.${minted.ticket}`])
  socket.binaryType = 'arraybuffer'
  let closed = false
  const protocolFailure = () => {
    if (closed) return
    handlers.onControl({ type: 'error', code: 'pty_protocol_invalid' })
    socket.close(1002, 'protocol_error')
    closed = true
  }
  socket.addEventListener('open', () => {
    if (socket.protocol !== minted.protocol) protocolFailure()
  })
  socket.addEventListener('message', (event) => {
    if (event.data instanceof ArrayBuffer) {
      handlers.onOutput(new Uint8Array(event.data))
      return
    }
    if (typeof event.data !== 'string') return
    let value: unknown
    try {
      value = JSON.parse(event.data)
    } catch {
      protocolFailure()
      return
    }
    if (!value || typeof value !== 'object') return protocolFailure()
    const record = value as Record<string, unknown>
    if (
      record.type === 'ready'
      && Object.keys(record).length === 2
      && record.pty_session_id === minted.pty_session_id
    ) {
      handlers.onControl(record as SandboxPtyEvent)
      return
    }
    if (
      record.type === 'exit'
      && Object.keys(record).length === 2
      && Number.isSafeInteger(record.exit_code)
    ) {
      handlers.onControl(record as SandboxPtyEvent)
      return
    }
    if (
      record.type === 'error'
      && Object.keys(record).length === 2
      && typeof record.code === 'string'
      && /^[a-z0-9_]{1,64}$/.test(record.code)
    ) {
      handlers.onControl(record as SandboxPtyEvent)
      return
    }
    protocolFailure()
  })
  socket.addEventListener('close', () => {
    closed = true
    handlers.onClose?.()
  })

  const send = (value: object) => {
    if (closed || socket.readyState !== WebSocket.OPEN) return
    socket.send(JSON.stringify(value))
  }
  return {
    sendInput: (data) => send({ type: 'input', data }),
    resize: (rows, cols) => send({ type: 'resize', rows, cols }),
    close: () => {
      if (closed) return
      send({ type: 'close' })
      socket.close(1000, 'client_close')
      closed = true
    },
  }
}

export const readSandboxFile = async (
  sandboxId: string,
  conversationId: string,
  path: string,
): Promise<Uint8Array> => {
  const data = await instance.get(`/sandbox/${encodeURIComponent(sandboxId)}/files`, {
    params: { conversation_id: conversationId, path },
    responseType: 'arraybuffer',
  }) as unknown as ArrayBuffer
  return new Uint8Array(data)
}

export const writeSandboxFile = async (
  sandboxId: string,
  conversationId: string,
  path: string,
  data: Uint8Array,
): Promise<void> => {
  await instance.put(`/sandbox/${encodeURIComponent(sandboxId)}/files`, data, {
    params: { conversation_id: conversationId, path },
    headers: { ...mutationHeaders(), 'Content-Type': 'application/octet-stream' },
  })
}
