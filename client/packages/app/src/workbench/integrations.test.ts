// @ts-nocheck -- executed by Bun's test runner; Bun types are not a production dependency.
import { describe, expect, test } from 'bun:test'
import type { IntegrationResource } from './integrations'
import { integrationBindingPolicy } from './integrationPolicy'

const resource = (overrides: Partial<IntegrationResource> = {}): IntegrationResource => ({
  kind: 'plugin',
  resource_id: 'plugin-a',
  parent_resource_id: null,
  name_zh: '只读插件',
  name_en: 'Read plugin',
  provider_id: null,
  requires_oauth: false,
  authorization_mode: 'provider_managed',
  binding_status: 'unbound',
  provider_sync_status: 'not_synced',
  connection: { status: 'disconnected', execution_status: 'not_used_for_agent_binding' },
  can_bind: true,
  blocked_reason: null,
  ...overrides,
})

describe('integration binding UI boundary', () => {
  test('never offers local OAuth or physical provider removal', () => {
    expect(integrationBindingPolicy(resource())).toEqual({
      canMutate: true,
      usesLocalOAuth: false,
      physicalUnbind: false,
    })
  })

  test('keeps user OAuth and write/delete tools disabled', () => {
    for (const blocked_reason of ['user_oauth_unsupported', 'write_tool_unsupported']) {
      expect(integrationBindingPolicy(resource({ can_bind: false, blocked_reason })).canMutate).toBe(false)
    }
  })
})
