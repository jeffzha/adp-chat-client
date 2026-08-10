import type { AxiosError } from 'axios'

export const describeSandboxError = (error: unknown): string => {
  const axiosError = error as AxiosError<{
    description?: string
    message?: string
    error?: { message?: string }
  }>
  return axiosError.response?.data?.error?.message
    || axiosError.response?.data?.description
    || axiosError.response?.data?.message
    || (error instanceof Error ? error.message : 'sandbox request failed')
}

export const MANAGED_SANDBOX_PUBLIC_FIELDS = [
  'sandbox_id',
  'conversation_id',
  'status',
  'timeout_seconds',
  'expires_at',
  'created_at',
  'updated_at',
] as const

export const SANDBOX_CODE_PUBLIC_FIELDS = [
  'stdout',
  'stderr',
  'results',
  'error',
] as const

export type PublicSandboxCodeExecution = {
  stdout: string
  stderr: string
  results: string[]
  error: string | null
}

export const projectSandboxCodeExecution = (value: unknown): PublicSandboxCodeExecution => {
  const input = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  return {
    stdout: typeof input.stdout === 'string' ? input.stdout : '',
    stderr: typeof input.stderr === 'string' ? input.stderr : '',
    results: Array.isArray(input.results)
      ? input.results.filter((item): item is string => typeof item === 'string')
      : [],
    error: typeof input.error === 'string' ? input.error : null,
  }
}

export const canOpenManagedSandbox = (
  enabled: boolean | undefined,
  capabilities: string[] | undefined,
): boolean => enabled === true && Boolean(capabilities?.includes('sandbox'))

export const buildSameOriginPtyURL = (
  baseURL: string,
  pageURL: string,
  sandboxId: string,
): string => {
  if (!/^sbx_[0-9a-f]{32}$/.test(sandboxId)) throw new Error('sandbox id is invalid')
  const page = new URL(pageURL)
  const base = new URL(baseURL.endsWith('/') ? baseURL : `${baseURL}/`, page)
  const url = new URL(`sandbox/${encodeURIComponent(sandboxId)}/pty/connect`, base)
  if (url.origin !== page.origin || url.username || url.password) {
    throw new Error('sandbox PTY must use the current origin')
  }
  url.protocol = page.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}
