import type { WorkbenchConfig, WorkbenchPlan } from './runtime'

export type WorkbenchAccessState =
  | 'active'
  | 'read_only'
  | 'suspended'
  | 'expired'
  | 'disabled'

const normalize = (value?: string) => (value || '').trim().toLowerCase().replace(/-/g, '_')

const parsedTime = (value?: string) => {
  if (!value) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : null
}

export const classifyWorkbenchAccess = (
  config: WorkbenchConfig | null,
  plan: WorkbenchPlan | null,
  planUnavailable = false,
  now = Date.now(),
): WorkbenchAccessState => {
  const accessMode = normalize(config?.access_mode)
  const appStatus = normalize(config?.app_status)
  const planStatus = normalize(plan?.status)
  const paymentStatus = normalize(plan?.payment_status)
  const planEnd = parsedTime(plan?.end_at)

  if (
    ['disabled', 'archived', 'revoked'].includes(accessMode)
    || ['disabled', 'archived', 'revoked'].includes(appStatus)
  ) return 'disabled'

  if (
    ['expired', 'ended'].includes(accessMode)
    || ['expired', 'ended'].includes(planStatus)
    || (planEnd !== null && planEnd < now)
  ) return 'expired'

  if (
    ['suspended', 'paused'].includes(accessMode)
    || ['suspended', 'paused'].includes(appStatus)
    || ['suspended', 'paused'].includes(planStatus)
    || ['unpaid', 'past_due', 'overdue', 'failed'].includes(paymentStatus)
  ) return 'suspended'

  if (
    planUnavailable
    || !plan
    || !config
    || accessMode !== 'active'
    || appStatus !== 'active'
    || planStatus !== 'active'
    || paymentStatus !== 'paid'
    || planEnd === null
    || ['read_only', 'readonly', 'viewer'].includes(accessMode)
  ) return 'read_only'

  return 'active'
}
