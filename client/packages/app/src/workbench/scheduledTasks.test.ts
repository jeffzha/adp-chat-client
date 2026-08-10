// @ts-nocheck -- executed by Bun's test runner; Bun types are not a production dependency.
import { describe, expect, test } from 'bun:test'
import {
  defaultScheduledTaskForm,
  formToTaskInput,
  validateScheduledTaskForm,
} from './scheduledTaskForm'

describe('scheduled task form boundary', () => {
  test('normalizes opaque attachment references without client-controlled scope', () => {
    const form = defaultScheduledTaskForm()
    form.name = 'Morning summary'
    form.prompt = 'Summarize the existing conversation.'
    form.conversationId = '74bd8f3b-f81b-4e96-ad09-93833bb66a52'
    form.attachmentIds = 'wf_one, wf_two\nwf_one'

    expect(formToTaskInput(form)).toEqual({
      name: 'Morning summary',
      prompt: 'Summarize the existing conversation.',
      attachment_ids: ['wf_one', 'wf_two'],
      conversation_id: '74bd8f3b-f81b-4e96-ad09-93833bb66a52',
      schedule_kind: 'cron',
      cron_expression: '0 9 * * 1-5',
      timezone: form.timezone,
      once_at: null,
      misfire_policy: 'skip',
      max_runtime_seconds: 600,
      daily_run_limit: 1,
      max_retries: 1,
      retry_backoff_seconds: 30,
    })
  })

  test('requires a conversation whenever attachments are configured', () => {
    const form = defaultScheduledTaskForm()
    form.name = 'Task'
    form.prompt = 'Prompt'
    form.attachmentIds = 'wf_one'

    expect(validateScheduledTaskForm(form)).toBe('conversation')
  })

  test('rejects invalid multiplier limits before sending billing-sensitive work', () => {
    const form = defaultScheduledTaskForm()
    form.name = 'Task'
    form.prompt = 'Prompt'
    form.dailyRunLimit = 25

    expect(validateScheduledTaskForm(form)).toBe('daily')
  })
})
