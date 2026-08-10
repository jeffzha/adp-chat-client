import type { AxiosError } from 'axios'
import Cookies from 'js-cookie'
import instance from '@/service/axiosInstance'
import type { ScheduledRun, ScheduledTask, ScheduledTaskInput } from './scheduledTaskForm'

export * from './scheduledTaskForm'

const CSRF_COOKIE = 'claw_workbench_csrf'

export const describeScheduledError = (error: unknown): string => {
  const axiosError = error as AxiosError<{ description?: string; message?: string; error?: { message?: string } }>
  return axiosError.response?.data?.error?.message
    || axiosError.response?.data?.description
    || axiosError.response?.data?.message
    || (error instanceof Error ? error.message : 'scheduled task request failed')
}

const mutationHeaders = () => {
  const token = Cookies.get(CSRF_COOKIE)
  if (!token) throw new Error('workbench CSRF token is unavailable')
  return { 'X-Workbench-CSRF': token }
}

export const listScheduledTasks = async (): Promise<ScheduledTask[]> => {
  const response = await instance.get('/scheduled-tasks') as unknown as { tasks: ScheduledTask[] }
  return response.tasks
}

export const getScheduledTask = async (taskId: string): Promise<ScheduledTask> => (
  await instance.get(`/scheduled-tasks/${encodeURIComponent(taskId)}`) as unknown as ScheduledTask
)

export const createScheduledTask = async (payload: ScheduledTaskInput): Promise<ScheduledTask> => (
  await instance.post('/scheduled-tasks', payload, { headers: mutationHeaders() }) as unknown as ScheduledTask
)

export const updateScheduledTask = async (taskId: string, payload: ScheduledTaskInput): Promise<ScheduledTask> => (
  await instance.patch(`/scheduled-tasks/${encodeURIComponent(taskId)}`, payload, { headers: mutationHeaders() }) as unknown as ScheduledTask
)

export const deleteScheduledTask = async (taskId: string): Promise<void> => {
  await instance.delete(`/scheduled-tasks/${encodeURIComponent(taskId)}`, { headers: mutationHeaders() })
}

export const actOnScheduledTask = async (taskId: string, action: 'pause' | 'resume'): Promise<ScheduledTask> => (
  await instance.post(`/scheduled-tasks/${encodeURIComponent(taskId)}/${action}`, {}, { headers: mutationHeaders() }) as unknown as ScheduledTask
)

export const runScheduledTaskNow = async (taskId: string): Promise<ScheduledRun> => (
  await instance.post(`/scheduled-tasks/${encodeURIComponent(taskId)}/run`, {}, { headers: mutationHeaders() }) as unknown as ScheduledRun
)

export const listScheduledRuns = async (taskId: string, limit = 20): Promise<ScheduledRun[]> => {
  const response = await instance.get(`/scheduled-tasks/${encodeURIComponent(taskId)}/runs`, { params: { limit } }) as unknown as { runs: ScheduledRun[] }
  return response.runs
}
