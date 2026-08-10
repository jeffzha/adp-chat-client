import axios, { AxiosError } from 'axios'
import { computed, reactive } from 'vue'
import { classifyWorkbenchAccess } from './accessState'
import { getBaseURL } from '../utils/url'

interface ApiEnvelope<T> {
  success: boolean
  data?: T
  error?: {
    code?: string
    message?: string
    request_id?: string
  }
}

export interface WorkbenchConfig {
  customer_code: string
  customer_display_name: string
  role: string
  access_mode: string
  app_display_name: string
  app_selector: string
  app_alias: string
  is_default: boolean
  app_status: string
  capabilities: string[]
  limits: Record<string, number>
  sandbox_enabled?: boolean
  shell_enabled?: boolean
  files_enabled?: boolean
  code_execution_enabled?: boolean
  pty_enabled?: boolean
}

export interface WorkbenchPlan {
  period_id: number
  amount_cny: string
  start_at: string
  end_at: string
  status: string
  payment_status: string
  snapshot: {
    plan_name?: string
    display_name?: string
    [key: string]: unknown
  }
}

type RuntimeStatus = 'idle' | 'loading' | 'ready' | 'error'

const pathEnablesWorkbench = () => {
  const path = window.location.pathname.replace(/\/+$/, '')
  return path === '/workbench' || path.startsWith('/workbench/')
}

export const workbenchRuntime = reactive({
  enabled: pathEnablesWorkbench() || import.meta.env.VITE_WORKBENCH_MODE === 'true',
  status: 'idle' as RuntimeStatus,
  config: null as WorkbenchConfig | null,
  plan: null as WorkbenchPlan | null,
  planError: '',
  error: '',
  errorCode: '',
  requestId: '',
})

let initialization: Promise<void> | null = null

const unwrap = <T>(response: { data: ApiEnvelope<T> | T }): T => {
  const payload = response.data as ApiEnvelope<T>
  if (payload && typeof payload === 'object' && 'success' in payload) {
    if (!payload.success || !payload.data) {
      throw new Error(payload.error?.message || 'Workbench request failed')
    }
    return payload.data
  }
  return response.data as T
}

const record = (value: unknown, label: string): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} is invalid`)
  }
  return value as Record<string, unknown>
}

const requiredString = (value: unknown, label: string): string => {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`${label} is invalid`)
  return value
}

export const parseWorkbenchConfig = (value: unknown): WorkbenchConfig => {
  const input = record(value, 'Workbench configuration')
  if (!Array.isArray(input.capabilities) || input.capabilities.some((item) => typeof item !== 'string')) {
    throw new Error('Workbench capabilities are invalid')
  }
  const rawLimits = record(input.limits, 'Workbench limits')
  const limits: Record<string, number> = {}
  for (const [key, limit] of Object.entries(rawLimits)) {
    if (typeof limit !== 'number' || !Number.isFinite(limit) || limit < 0) {
      throw new Error('Workbench limits are invalid')
    }
    limits[key] = limit
  }
  if (typeof input.is_default !== 'boolean') throw new Error('Workbench default application state is invalid')
  return {
    customer_code: requiredString(input.customer_code, 'Customer code'),
    customer_display_name: requiredString(input.customer_display_name, 'Customer name'),
    role: requiredString(input.role, 'Customer role'),
    access_mode: requiredString(input.access_mode, 'Workbench access mode'),
    app_display_name: requiredString(input.app_display_name, 'Application name'),
    app_selector: requiredString(input.app_selector, 'Application selector'),
    app_alias: requiredString(input.app_alias, 'Application alias'),
    is_default: input.is_default,
    app_status: requiredString(input.app_status, 'Application status'),
    capabilities: [...input.capabilities] as string[],
    limits,
  }
}

export const parseWorkbenchPlan = (value: unknown): WorkbenchPlan => {
  const input = record(value, 'Workbench plan')
  if (!Number.isSafeInteger(input.period_id) || Number(input.period_id) <= 0) {
    throw new Error('Workbench plan period is invalid')
  }
  const startAt = requiredString(input.start_at, 'Plan start time')
  const endAt = requiredString(input.end_at, 'Plan end time')
  if (!Number.isFinite(Date.parse(startAt)) || !Number.isFinite(Date.parse(endAt))) {
    throw new Error('Workbench plan time is invalid')
  }
  return {
    period_id: Number(input.period_id),
    start_at: startAt,
    end_at: endAt,
    status: requiredString(input.status, 'Plan status'),
    payment_status: requiredString(input.payment_status, 'Plan payment status'),
    amount_cny: requiredString(input.amount_cny, 'Plan amount'),
    snapshot: record(input.snapshot, 'Plan snapshot'),
  }
}

const describeError = (error: unknown) => {
  const axiosError = error as AxiosError<ApiEnvelope<never>>
  return {
    message: axiosError.response?.data?.error?.message
      || (error instanceof Error ? error.message : 'Workbench is temporarily unavailable'),
    requestId: axiosError.response?.data?.error?.request_id || '',
    code: axiosError.response?.data?.error?.code || '',
  }
}

export const initializeWorkbench = async (force = false): Promise<void> => {
  if (!workbenchRuntime.enabled) {
    workbenchRuntime.status = 'ready'
    return
  }
  if (!force && (workbenchRuntime.status === 'ready' || workbenchRuntime.status === 'error')) return
  if (initialization && !force) return initialization

  initialization = (async () => {
    workbenchRuntime.status = 'loading'
    workbenchRuntime.error = ''
    workbenchRuntime.requestId = ''
    workbenchRuntime.errorCode = ''
    workbenchRuntime.planError = ''

    try {
      const configResponse = await axios.get<ApiEnvelope<WorkbenchConfig>>('/api/workbench/config', {
        withCredentials: true,
        headers: { Accept: 'application/json' },
      })
      workbenchRuntime.config = parseWorkbenchConfig(unwrap(configResponse))
      try {
        const baseURL = getBaseURL()
        const base = new URL(baseURL.endsWith('/') ? baseURL : `${baseURL}/`, window.location.href)
        const featureUrl = new URL('sandbox/config', base)
        if (featureUrl.origin !== window.location.origin) {
          throw new Error('Workbench feature configuration must use the current origin')
        }
        const featureResponse = await axios.get<{
          sandbox_enabled?: boolean
          shell_enabled?: boolean
          files_enabled?: boolean
          code_execution_enabled?: boolean
          pty_enabled?: boolean
        }>(featureUrl.toString(), {
          withCredentials: true,
          headers: { Accept: 'application/json' },
        })
        workbenchRuntime.config.sandbox_enabled = featureResponse.data.sandbox_enabled === true
        workbenchRuntime.config.shell_enabled = featureResponse.data.shell_enabled === true
        workbenchRuntime.config.files_enabled = featureResponse.data.files_enabled === true
        workbenchRuntime.config.code_execution_enabled = (
          featureResponse.data.code_execution_enabled === true
        )
        workbenchRuntime.config.pty_enabled = featureResponse.data.pty_enabled === true
      } catch {
        workbenchRuntime.config.sandbox_enabled = false
      }
      workbenchRuntime.status = 'ready'
    } catch (error) {
      const described = describeError(error)
      workbenchRuntime.config = null
      workbenchRuntime.status = 'error'
      workbenchRuntime.error = described.message
      workbenchRuntime.requestId = described.requestId
      workbenchRuntime.errorCode = described.code
      return
    }

    try {
      const planResponse = await axios.get<ApiEnvelope<WorkbenchPlan>>('/api/workbench/plan', {
        withCredentials: true,
        headers: { Accept: 'application/json' },
      })
      workbenchRuntime.plan = parseWorkbenchPlan(unwrap(planResponse))
    } catch (error) {
      workbenchRuntime.plan = null
      workbenchRuntime.planError = describeError(error).message
    }
  })().finally(() => {
    initialization = null
  })

  return initialization
}

export const setWorkbenchSessionError = (message: string) => {
  if (!workbenchRuntime.enabled) return
  workbenchRuntime.status = 'error'
  workbenchRuntime.error = message
  workbenchRuntime.errorCode = ''
  workbenchRuntime.requestId = ''
}

export const workbenchAccessState = computed(() => classifyWorkbenchAccess(
  workbenchRuntime.config,
  workbenchRuntime.plan,
  Boolean(workbenchRuntime.planError),
))

export const workbenchReadOnly = computed(() => (
  workbenchRuntime.enabled && workbenchAccessState.value !== 'active'
))
