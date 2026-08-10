export type ScheduleKind = 'cron' | 'once'
export type MisfirePolicy = 'skip' | 'fire_once'

export interface ScheduledTaskInput {
  name: string
  prompt: string
  attachment_ids: string[]
  conversation_id: string | null
  schedule_kind: ScheduleKind
  cron_expression: string | null
  timezone: string
  once_at: string | null
  misfire_policy: MisfirePolicy
  max_runtime_seconds: number
  daily_run_limit: number
  max_retries: number
  retry_backoff_seconds: number
}

export interface ScheduledTask extends ScheduledTaskInput {
  task_id: string
  status: 'active' | 'paused' | 'completed'
  next_run_at: string | null
  version: number
  created_at: string | null
  updated_at: string | null
}

export interface ScheduledRun {
  run_id: string
  task_id: string
  scheduled_for: string
  status: string
  attempt_count: number
  turn_id: string | null
  error_code: string | null
  started_at: string | null
  finished_at: string | null
}

export interface ScheduledTaskForm {
  name: string
  prompt: string
  attachmentIds: string
  conversationId: string
  scheduleKind: ScheduleKind
  cronExpression: string
  timezone: string
  onceAtLocal: string
  misfirePolicy: MisfirePolicy
  maxRuntimeSeconds: number
  dailyRunLimit: number
  maxRetries: number
  retryBackoffSeconds: number
}

export const defaultScheduledTaskForm = (): ScheduledTaskForm => ({
  name: '', prompt: '', attachmentIds: '', conversationId: '', scheduleKind: 'cron',
  cronExpression: '0 9 * * 1-5',
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
  onceAtLocal: '', misfirePolicy: 'skip', maxRuntimeSeconds: 600,
  dailyRunLimit: 1, maxRetries: 1, retryBackoffSeconds: 30,
})

const toLocalDateTimeInput = (value: string): string => {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}

export const taskToForm = (task: ScheduledTask): ScheduledTaskForm => ({
  name: task.name,
  prompt: task.prompt,
  attachmentIds: task.attachment_ids.join('\n'),
  conversationId: task.conversation_id || '',
  scheduleKind: task.schedule_kind,
  cronExpression: task.cron_expression || '',
  timezone: task.timezone,
  onceAtLocal: task.once_at ? toLocalDateTimeInput(task.once_at) : '',
  misfirePolicy: task.misfire_policy,
  maxRuntimeSeconds: task.max_runtime_seconds,
  dailyRunLimit: task.daily_run_limit,
  maxRetries: task.max_retries,
  retryBackoffSeconds: task.retry_backoff_seconds,
})

export const formToTaskInput = (form: ScheduledTaskForm): ScheduledTaskInput => {
  const attachmentIds = form.attachmentIds.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean)
  return {
    name: form.name.trim(),
    prompt: form.prompt.trim(),
    attachment_ids: [...new Set(attachmentIds)],
    conversation_id: form.conversationId.trim() || null,
    schedule_kind: form.scheduleKind,
    cron_expression: form.scheduleKind === 'cron' ? form.cronExpression.trim() : null,
    timezone: form.timezone.trim(),
    once_at: form.scheduleKind === 'once' && form.onceAtLocal ? new Date(form.onceAtLocal).toISOString() : null,
    misfire_policy: form.misfirePolicy,
    max_runtime_seconds: Number(form.maxRuntimeSeconds),
    daily_run_limit: Number(form.dailyRunLimit),
    max_retries: Number(form.maxRetries),
    retry_backoff_seconds: Number(form.retryBackoffSeconds),
  }
}

export const validateScheduledTaskForm = (form: ScheduledTaskForm): string | null => {
  if (!form.name.trim() || !form.prompt.trim()) return 'required'
  if (!form.timezone.trim()) return 'timezone'
  if (form.scheduleKind === 'cron' && !form.cronExpression.trim()) return 'cron'
  if (form.scheduleKind === 'once') {
    const onceAt = new Date(form.onceAtLocal)
    if (!form.onceAtLocal || Number.isNaN(onceAt.getTime()) || onceAt.getTime() < Date.now() + 60_000) return 'once'
  }
  const attachments = formToTaskInput(form).attachment_ids
  if (attachments.some((value) => !/^wf_[A-Za-z0-9_-]{1,61}$/.test(value))) return 'attachments'
  if (attachments.length > 0 && !form.conversationId.trim()) return 'conversation'
  if (!Number.isInteger(Number(form.maxRuntimeSeconds)) || form.maxRuntimeSeconds < 30 || form.maxRuntimeSeconds > 86400) return 'runtime'
  if (!Number.isInteger(Number(form.dailyRunLimit)) || form.dailyRunLimit < 1 || form.dailyRunLimit > 24) return 'daily'
  if (!Number.isInteger(Number(form.maxRetries)) || form.maxRetries < 0 || form.maxRetries > 3) return 'retries'
  if (!Number.isInteger(Number(form.retryBackoffSeconds)) || form.retryBackoffSeconds < 5 || form.retryBackoffSeconds > 3600) return 'backoff'
  return null
}
