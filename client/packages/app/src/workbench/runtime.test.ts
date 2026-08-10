// @ts-nocheck -- executed by Bun's test runner; Bun types are not a production dependency.
import { describe, expect, test } from 'bun:test'

const config = {
  customer_code: 'customer-a',
  customer_display_name: 'Customer A',
  role: 'member',
  access_mode: 'active',
  app_display_name: 'Customer A Claw',
  app_selector: 'aps_primary',
  app_alias: 'primary',
  is_default: true,
  app_status: 'active',
  capabilities: ['chat', 'sandbox'],
  limits: { max_runtime_seconds: 600 },
}

const plan = {
  period_id: 7,
  start_at: '2026-08-01T00:00:00Z',
  end_at: '2026-09-01T00:00:00Z',
  status: 'active',
  payment_status: 'paid',
  amount_cny: '100.00',
  snapshot: { display_name: 'Standard' },
}

describe('workbench browser DTO projection', () => {
  test('requires the complete configuration and plan state used for authorization UI', async () => {
    Object.assign(globalThis, {
      window: { location: { pathname: '/', href: 'https://gateway.example/' } },
    })
    const { parseWorkbenchConfig, parseWorkbenchPlan } = await import('./runtime')

    expect(parseWorkbenchConfig(config)).toEqual(config)
    expect(parseWorkbenchPlan(plan)).toEqual(plan)
    expect(() => parseWorkbenchConfig({ ...config, access_mode: undefined }))
      .toThrow('Workbench access mode is invalid')
    expect(() => parseWorkbenchPlan({ ...plan, end_at: 'not-a-date' }))
      .toThrow('Workbench plan time is invalid')
  })
})
