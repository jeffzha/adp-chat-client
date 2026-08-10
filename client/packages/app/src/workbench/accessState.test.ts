// @ts-nocheck -- executed by Bun's test runner; Bun types are not a production dependency.
import { describe, expect, test } from 'bun:test'
import { classifyWorkbenchAccess } from './accessState'

const activeConfig = { access_mode: 'active', app_status: 'active' }
const activePlan = {
  status: 'active',
  payment_status: 'paid',
  end_at: '2030-01-01T00:00:00Z',
}
const now = Date.parse('2026-08-09T00:00:00Z')

describe('classifyWorkbenchAccess', () => {
  test('allows writes only when application, plan and payment are active', () => {
    expect(classifyWorkbenchAccess(activeConfig, activePlan, false, now)).toBe('active')
    expect(classifyWorkbenchAccess(activeConfig, null, true, now)).toBe('read_only')
  })

  test('distinguishes paused, expired and disabled customer states', () => {
    expect(classifyWorkbenchAccess({ ...activeConfig, app_status: 'suspended' }, activePlan, false, now)).toBe('suspended')
    expect(classifyWorkbenchAccess(activeConfig, { ...activePlan, end_at: '2026-08-08T00:00:00Z' }, false, now)).toBe('expired')
    expect(classifyWorkbenchAccess({ ...activeConfig, app_status: 'disabled' }, activePlan, false, now)).toBe('disabled')
  })

  test('fails closed for unknown non-active states', () => {
    expect(classifyWorkbenchAccess({ ...activeConfig, app_status: 'verified' }, activePlan, false, now)).toBe('read_only')
    expect(classifyWorkbenchAccess(activeConfig, { ...activePlan, payment_status: 'pending' }, false, now)).toBe('read_only')
    expect(classifyWorkbenchAccess(activeConfig, { ...activePlan, payment_status: 'past_due' }, false, now)).toBe('suspended')
    expect(classifyWorkbenchAccess({}, activePlan, false, now)).toBe('read_only')
    expect(classifyWorkbenchAccess(activeConfig, {} as never, false, now)).toBe('read_only')
    expect(classifyWorkbenchAccess(activeConfig, { ...activePlan, end_at: 'not-a-date' }, false, now)).toBe('read_only')
  })
})
