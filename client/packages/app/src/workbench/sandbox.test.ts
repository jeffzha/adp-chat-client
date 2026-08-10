// @ts-nocheck -- executed by Bun's test runner; Bun types are not a production dependency.
import { describe, expect, test } from 'bun:test'
import {
  canOpenManagedSandbox,
  buildSameOriginPtyURL,
  describeSandboxError,
  MANAGED_SANDBOX_PUBLIC_FIELDS,
  projectSandboxCodeExecution,
  SANDBOX_CODE_PUBLIC_FIELDS,
} from './sandboxBoundary'

describe('managed sandbox client boundary', () => {
  test('projects only the server-provided safe error description', () => {
    expect(describeSandboxError({ response: { data: { description: 'sandbox_disabled' } } }))
      .toBe('sandbox_disabled')
  })

  test('does not invent provider connection details in the public sandbox type', () => {
    const sandbox = {
      sandbox_id: 'sbx_123',
      conversation_id: 'conversation-1',
      status: 'running',
      timeout_seconds: 600,
      expires_at: null,
      created_at: null,
      updated_at: null,
    }
    expect(Object.keys(sandbox)).toEqual([...MANAGED_SANDBOX_PUBLIC_FIELDS])
    expect(MANAGED_SANDBOX_PUBLIC_FIELDS).not.toContain('provider_instance_id')
    expect(MANAGED_SANDBOX_PUBLIC_FIELDS).not.toContain('token')
    expect(MANAGED_SANDBOX_PUBLIC_FIELDS).not.toContain('url')
  })

  test('uses a dedicated sandbox capability and never reuses generic tools', () => {
    expect(canOpenManagedSandbox(false, ['sandbox'])).toBeFalse()
    expect(canOpenManagedSandbox(true, ['chat'])).toBeFalse()
    expect(canOpenManagedSandbox(true, ['chat', 'tools'])).toBeFalse()
    expect(canOpenManagedSandbox(true, ['chat', 'sandbox'])).toBeTrue()
  })

  test('crops code execution responses to plain browser-safe fields', () => {
    const execution = projectSandboxCodeExecution({
      stdout: 'hello',
      stderr: '',
      results: ['42', { png: 'not-public' }, 7],
      error: null,
      provider_instance_id: 'provider-secret',
      traceback: 'private provider traceback',
      html: '<script>alert(1)</script>',
    })
    expect(Object.keys(execution)).toEqual([...SANDBOX_CODE_PUBLIC_FIELDS])
    expect(execution).toEqual({
      stdout: 'hello',
      stderr: '',
      results: ['42'],
      error: null,
    })
  })

  test('builds PTY WebSockets only on the current origin', () => {
    const sandboxId = `sbx_${'a'.repeat(32)}`
    expect(buildSameOriginPtyURL('/api', 'https://gateway.example/workbench', sandboxId))
      .toBe(`wss://gateway.example/api/sandbox/${sandboxId}/pty/connect`)
    expect(() => buildSameOriginPtyURL('https://provider.example/', 'https://gateway.example/', sandboxId))
      .toThrow('sandbox PTY must use the current origin')
    expect(() => buildSameOriginPtyURL('/api/', 'https://gateway.example/', '../escape'))
      .toThrow('sandbox id is invalid')
  })
})
