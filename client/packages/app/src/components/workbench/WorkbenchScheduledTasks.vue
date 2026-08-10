<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  actOnScheduledTask,
  createScheduledTask,
  defaultScheduledTaskForm,
  deleteScheduledTask,
  describeScheduledError,
  formToTaskInput,
  getScheduledTask,
  listScheduledRuns,
  listScheduledTasks,
  runScheduledTaskNow,
  taskToForm,
  updateScheduledTask,
  validateScheduledTaskForm,
  type ScheduledRun,
  type ScheduledTask,
  type ScheduledTaskForm,
} from '@/workbench/scheduledTasks'

const props = defineProps<{ open: boolean; readOnly: boolean }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const { t, locale } = useI18n()
const tasks = ref<ScheduledTask[]>([])
const runs = ref<ScheduledRun[]>([])
const selectedTaskId = ref('')
const editingTaskId = ref('')
const form = ref<ScheduledTaskForm>(defaultScheduledTaskForm())
const mode = ref<'list' | 'form'>('list')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const formError = ref('')
const dialog = ref<HTMLElement | null>(null)
let pollTimer: number | undefined

const selectedTask = computed(() => tasks.value.find((task) => task.task_id === selectedTaskId.value) || null)
const canEditSelected = computed(() => !props.readOnly && selectedTask.value?.status === 'paused')
const hasActiveRuns = computed(() => runs.value.some((run) => ['queued', 'retrying', 'leased', 'executing'].includes(run.status)))

const formatTime = (value: string | null) => {
  if (!value) return t('workbench.scheduledTasks.notAvailable')
  return new Intl.DateTimeFormat(locale.value === 'zh' ? 'zh-CN' : 'en-US', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

const translateStatus = (status: string) => t(`workbench.scheduledTasks.status.${status}`, status)
const close = () => emit('update:open', false)
const handleKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape' && !saving.value) close()
}

const loadRuns = async (taskId: string) => {
  try {
    runs.value = await listScheduledRuns(taskId)
  } catch (requestError) {
    error.value = describeScheduledError(requestError)
  }
}

const loadTasks = async (preserveSelection = true) => {
  loading.value = true
  error.value = ''
  try {
    tasks.value = await listScheduledTasks()
    if (!preserveSelection || !tasks.value.some((task) => task.task_id === selectedTaskId.value)) {
      selectedTaskId.value = tasks.value[0]?.task_id || ''
    }
    if (selectedTaskId.value) await loadRuns(selectedTaskId.value)
    else runs.value = []
  } catch (requestError) {
    error.value = describeScheduledError(requestError)
  } finally {
    loading.value = false
  }
}

const selectTask = async (taskId: string) => {
  selectedTaskId.value = taskId
  error.value = ''
  await loadRuns(taskId)
}

const startCreate = () => {
  editingTaskId.value = ''
  form.value = defaultScheduledTaskForm()
  formError.value = ''
  mode.value = 'form'
}

const startEdit = async () => {
  if (!selectedTask.value || !canEditSelected.value) return
  saving.value = true
  formError.value = ''
  try {
    const detail = await getScheduledTask(selectedTask.value.task_id)
    editingTaskId.value = detail.task_id
    form.value = taskToForm(detail)
    mode.value = 'form'
  } catch (requestError) {
    error.value = describeScheduledError(requestError)
  } finally {
    saving.value = false
  }
}

const cancelForm = () => {
  mode.value = 'list'
  editingTaskId.value = ''
  formError.value = ''
}

const save = async () => {
  const validation = validateScheduledTaskForm(form.value)
  if (validation) {
    formError.value = t(`workbench.scheduledTasks.validation.${validation}`)
    return
  }
  saving.value = true
  formError.value = ''
  try {
    const payload = formToTaskInput(form.value)
    const task = editingTaskId.value
      ? await updateScheduledTask(editingTaskId.value, payload)
      : await createScheduledTask(payload)
    selectedTaskId.value = task.task_id
    mode.value = 'list'
    editingTaskId.value = ''
    await loadTasks(true)
  } catch (requestError) {
    formError.value = describeScheduledError(requestError)
  } finally {
    saving.value = false
  }
}

const changeState = async (action: 'pause' | 'resume') => {
  if (!selectedTask.value || props.readOnly) return
  saving.value = true
  error.value = ''
  try {
    await actOnScheduledTask(selectedTask.value.task_id, action)
    await loadTasks(true)
  } catch (requestError) {
    error.value = describeScheduledError(requestError)
  } finally {
    saving.value = false
  }
}

const runNow = async () => {
  if (!selectedTask.value || props.readOnly || selectedTask.value.status !== 'active') return
  saving.value = true
  error.value = ''
  try {
    await runScheduledTaskNow(selectedTask.value.task_id)
    await loadRuns(selectedTask.value.task_id)
  } catch (requestError) {
    error.value = describeScheduledError(requestError)
  } finally {
    saving.value = false
  }
}

const remove = async () => {
  if (!selectedTask.value || props.readOnly) return
  if (!window.confirm(t('workbench.scheduledTasks.deleteConfirm', { name: selectedTask.value.name }))) return
  saving.value = true
  error.value = ''
  try {
    await deleteScheduledTask(selectedTask.value.task_id)
    await loadTasks(false)
  } catch (requestError) {
    error.value = describeScheduledError(requestError)
  } finally {
    saving.value = false
  }
}

const stopPolling = () => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
  pollTimer = undefined
}
const startPolling = () => {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    if (props.open && mode.value === 'list' && selectedTaskId.value) {
      await loadRuns(selectedTaskId.value)
      if (!hasActiveRuns.value) await loadTasks(true)
    }
  }, 5000)
}

watch(() => props.open, async (open) => {
  if (open) {
    mode.value = 'list'
    await loadTasks(true)
    startPolling()
    await nextTick()
    dialog.value?.focus()
  } else stopPolling()
})
onBeforeUnmount(stopPolling)
</script>

<template>
  <div v-if="open" class="scheduled-overlay" @mousedown.self="close">
    <section ref="dialog" class="scheduled-dialog" role="dialog" aria-modal="true" aria-labelledby="scheduled-dialog-title" tabindex="-1" @keydown="handleKeydown">
      <header class="scheduled-dialog__header">
        <div><h2 id="scheduled-dialog-title">{{ t('workbench.scheduledTasks.title') }}</h2><p>{{ t('workbench.scheduledTasks.description') }}</p></div>
        <button type="button" class="icon-button" :aria-label="t('workbench.scheduledTasks.close')" @click="close">×</button>
      </header>
      <p v-if="readOnly" class="notice" role="status">{{ t('workbench.scheduledTasks.readOnly') }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>

      <form v-if="mode === 'form'" class="task-form" @submit.prevent="save">
        <div class="form-heading"><h3>{{ editingTaskId ? t('workbench.scheduledTasks.edit') : t('workbench.scheduledTasks.create') }}</h3><button type="button" class="button button--quiet" :disabled="saving" @click="cancelForm">{{ t('workbench.scheduledTasks.cancel') }}</button></div>
        <label><span>{{ t('workbench.scheduledTasks.fields.name') }}</span><input v-model="form.name" maxlength="120" required /></label>
        <label class="span-2"><span>{{ t('workbench.scheduledTasks.fields.prompt') }}</span><textarea v-model="form.prompt" rows="6" maxlength="16000" required /></label>
        <label><span>{{ t('workbench.scheduledTasks.fields.scheduleKind') }}</span><select v-model="form.scheduleKind"><option value="cron">{{ t('workbench.scheduledTasks.kinds.cron') }}</option><option value="once">{{ t('workbench.scheduledTasks.kinds.once') }}</option></select></label>
        <label><span>{{ t('workbench.scheduledTasks.fields.timezone') }}</span><input v-model="form.timezone" placeholder="Asia/Shanghai" required /></label>
        <label v-if="form.scheduleKind === 'cron'"><span>{{ t('workbench.scheduledTasks.fields.cron') }}</span><input v-model="form.cronExpression" placeholder="0 9 * * 1-5" required /><small>{{ t('workbench.scheduledTasks.cronHelp') }}</small></label>
        <label v-else><span>{{ t('workbench.scheduledTasks.fields.onceAt') }}</span><input v-model="form.onceAtLocal" type="datetime-local" required /></label>
        <label><span>{{ t('workbench.scheduledTasks.fields.misfire') }}</span><select v-model="form.misfirePolicy"><option value="skip">{{ t('workbench.scheduledTasks.misfire.skip') }}</option><option value="fire_once">{{ t('workbench.scheduledTasks.misfire.fireOnce') }}</option></select></label>
        <label><span>{{ t('workbench.scheduledTasks.fields.maxRuntime') }}</span><input v-model.number="form.maxRuntimeSeconds" type="number" min="30" max="86400" step="1" required /></label>
        <label><span>{{ t('workbench.scheduledTasks.fields.dailyLimit') }}</span><input v-model.number="form.dailyRunLimit" type="number" min="1" max="24" step="1" required /></label>
        <label><span>{{ t('workbench.scheduledTasks.fields.maxRetries') }}</span><input v-model.number="form.maxRetries" type="number" min="0" max="3" step="1" required /></label>
        <label><span>{{ t('workbench.scheduledTasks.fields.retryBackoff') }}</span><input v-model.number="form.retryBackoffSeconds" type="number" min="5" max="3600" step="1" required /></label>
        <label class="span-2"><span>{{ t('workbench.scheduledTasks.fields.conversationId') }}</span><input v-model="form.conversationId" :placeholder="t('workbench.scheduledTasks.conversationHelp')" /></label>
        <label class="span-2"><span>{{ t('workbench.scheduledTasks.fields.attachments') }}</span><textarea v-model="form.attachmentIds" rows="3" placeholder="wf_..." /><small>{{ t('workbench.scheduledTasks.attachmentHelp') }}</small></label>
        <p v-if="formError" class="error span-2" role="alert">{{ formError }}</p>
        <div class="form-actions span-2"><button class="button button--primary" type="submit" :disabled="saving">{{ saving ? t('workbench.scheduledTasks.saving') : t('workbench.scheduledTasks.save') }}</button></div>
      </form>

      <div v-else class="scheduled-content">
        <aside class="task-list" :aria-label="t('workbench.scheduledTasks.tasks')">
          <div class="task-list__toolbar"><strong>{{ t('workbench.scheduledTasks.tasks') }}</strong><button type="button" class="button button--primary" :disabled="readOnly || saving" @click="startCreate">{{ t('workbench.scheduledTasks.new') }}</button></div>
          <p v-if="loading && !tasks.length" class="empty">{{ t('workbench.scheduledTasks.loading') }}</p>
          <p v-else-if="!tasks.length" class="empty">{{ t('workbench.scheduledTasks.empty') }}</p>
          <button v-for="task in tasks" :key="task.task_id" type="button" class="task-card" :class="{ 'is-selected': task.task_id === selectedTaskId }" @click="selectTask(task.task_id)">
            <span class="task-card__top"><strong>{{ task.name }}</strong><span class="status" :data-status="task.status">{{ translateStatus(task.status) }}</span></span>
            <small>{{ task.schedule_kind === 'cron' ? task.cron_expression : formatTime(task.once_at) }}</small><small>{{ t('workbench.scheduledTasks.nextRun') }}: {{ formatTime(task.next_run_at) }}</small>
          </button>
        </aside>

        <main class="task-detail">
          <template v-if="selectedTask">
            <div class="detail-heading"><div><h3>{{ selectedTask.name }}</h3><p>{{ selectedTask.task_id }}</p></div></div>
            <dl class="task-metadata">
              <div><dt>{{ t('workbench.scheduledTasks.fields.schedule') }}</dt><dd>{{ selectedTask.schedule_kind === 'cron' ? selectedTask.cron_expression : formatTime(selectedTask.once_at) }}</dd></div>
              <div><dt>{{ t('workbench.scheduledTasks.fields.timezone') }}</dt><dd>{{ selectedTask.timezone }}</dd></div>
              <div><dt>{{ t('workbench.scheduledTasks.nextRun') }}</dt><dd>{{ formatTime(selectedTask.next_run_at) }}</dd></div>
              <div><dt>{{ t('workbench.scheduledTasks.fields.dailyLimit') }}</dt><dd>{{ selectedTask.daily_run_limit }}</dd></div>
            </dl>
            <div class="task-actions">
              <button v-if="selectedTask.status === 'active'" type="button" class="button" :disabled="readOnly || saving" @click="runNow">{{ t('workbench.scheduledTasks.runNow') }}</button>
              <button v-if="selectedTask.status === 'active'" type="button" class="button" :disabled="readOnly || saving" @click="changeState('pause')">{{ t('workbench.scheduledTasks.pause') }}</button>
              <button v-else-if="selectedTask.status === 'paused'" type="button" class="button" :disabled="readOnly || saving" @click="changeState('resume')">{{ t('workbench.scheduledTasks.resume') }}</button>
              <button type="button" class="button" :disabled="!canEditSelected || saving" @click="startEdit">{{ t('workbench.scheduledTasks.edit') }}</button>
              <button type="button" class="button button--danger" :disabled="readOnly || saving" @click="remove">{{ t('workbench.scheduledTasks.delete') }}</button>
            </div>
            <p v-if="selectedTask.status === 'active'" class="hint">{{ t('workbench.scheduledTasks.pauseToEdit') }}</p>
            <div class="runs-heading"><h3>{{ t('workbench.scheduledTasks.runs') }}</h3><button type="button" class="button button--quiet" :disabled="loading" @click="loadRuns(selectedTask.task_id)">{{ t('workbench.scheduledTasks.refresh') }}</button></div>
            <div class="run-table-wrap"><table v-if="runs.length" class="run-table"><thead><tr><th>{{ t('workbench.scheduledTasks.runTime') }}</th><th>{{ t('workbench.scheduledTasks.runStatus') }}</th><th>{{ t('workbench.scheduledTasks.attempts') }}</th><th>{{ t('workbench.scheduledTasks.finished') }}</th><th>{{ t('workbench.scheduledTasks.errorCode') }}</th></tr></thead><tbody><tr v-for="run in runs" :key="run.run_id"><td>{{ formatTime(run.scheduled_for) }}</td><td><span class="status" :data-status="run.status">{{ translateStatus(run.status) }}</span></td><td>{{ run.attempt_count }}</td><td>{{ formatTime(run.finished_at) }}</td><td>{{ run.error_code || '—' }}</td></tr></tbody></table><p v-else class="empty">{{ t('workbench.scheduledTasks.noRuns') }}</p></div>
          </template>
          <p v-else class="empty">{{ t('workbench.scheduledTasks.selectTask') }}</p>
        </main>
      </div>
    </section>
  </div>
</template>

<style scoped>
.scheduled-overlay { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 20px; background: rgb(0 0 0 / 45%); }
.scheduled-dialog { width: min(1120px, 100%); max-height: min(860px, calc(100dvh - 40px)); overflow: hidden; border-radius: 12px; background: var(--td-bg-color-container, #fff); color: var(--td-text-color-primary, #1f2329); box-shadow: 0 24px 80px rgb(0 0 0 / 25%); outline: none; }
.scheduled-dialog__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 20px 24px; border-bottom: 1px solid var(--td-border-level-1-color, #dcdcdc); }
h2, h3, p { margin-top: 0; } h2 { margin-bottom: 4px; font-size: 20px; } h3 { margin-bottom: 4px; }
.scheduled-dialog__header p, .detail-heading p { margin-bottom: 0; color: var(--td-text-color-secondary, #646a73); font-size: 13px; }
.icon-button { width: 34px; height: 34px; border: 0; border-radius: 6px; background: transparent; color: inherit; cursor: pointer; font-size: 24px; }
.icon-button:hover, .button--quiet:hover { background: var(--td-bg-color-secondarycontainer, #f2f3f5); }
.notice, .error { margin: 12px 24px; padding: 9px 12px; border-radius: 6px; } .notice { background: #fff3d6; color: #704600; } .error { background: #fff0ed; color: var(--td-error-color, #b11f26); }
.scheduled-content { display: grid; grid-template-columns: 320px minmax(0, 1fr); min-height: 520px; max-height: calc(100dvh - 132px); }
.task-list { overflow: auto; padding: 16px; border-right: 1px solid var(--td-border-level-1-color, #dcdcdc); background: var(--td-bg-color-secondarycontainer, #f7f8fa); }
.task-list__toolbar, .form-heading, .runs-heading, .task-actions, .form-actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.task-card { display: block; width: 100%; margin-top: 10px; padding: 12px; border: 1px solid transparent; border-radius: 8px; background: var(--td-bg-color-container, #fff); color: inherit; cursor: pointer; text-align: left; }
.task-card:hover, .task-card.is-selected { border-color: var(--td-brand-color, #0052d9); } .task-card__top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.task-card small { display: block; margin-top: 6px; overflow-wrap: anywhere; color: var(--td-text-color-secondary, #646a73); }
.task-detail { min-width: 0; overflow: auto; padding: 20px 24px; }
.task-metadata { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 18px 0; } .task-metadata div { min-width: 0; } .task-metadata dt { color: var(--td-text-color-secondary, #646a73); font-size: 12px; } .task-metadata dd { margin: 3px 0 0; overflow-wrap: anywhere; }
.task-actions { justify-content: flex-start; flex-wrap: wrap; } .hint { margin: 8px 0 22px; color: var(--td-text-color-secondary, #646a73); font-size: 12px; }
.button { min-height: 34px; border: 1px solid var(--td-border-level-2-color, #c9cdd4); border-radius: 6px; background: var(--td-bg-color-container, #fff); color: inherit; cursor: pointer; padding: 6px 12px; } .button:disabled { cursor: not-allowed; opacity: .5; } .button--primary { border-color: var(--td-brand-color, #0052d9); background: var(--td-brand-color, #0052d9); color: #fff; } .button--danger { border-color: var(--td-error-color, #d54941); color: var(--td-error-color, #b11f26); } .button--quiet { border-color: transparent; background: transparent; }
.status { display: inline-block; border-radius: 999px; background: #eef0f2; padding: 2px 8px; font-size: 12px; white-space: nowrap; } .status[data-status='active'], .status[data-status='completed'] { background: #e8f5ee; color: #16784a; } .status[data-status='paused'], .status[data-status='queued'], .status[data-status='retrying'], .status[data-status='leased'], .status[data-status='executing'] { background: #fff3d6; color: #8a5a00; } .status[data-status^='failed'], .status[data-status='provider_unknown'], .status[data-status='revoked'] { background: #fff0ed; color: #b11f26; }
.runs-heading { margin-top: 22px; } .runs-heading h3 { margin-bottom: 0; } .run-table-wrap { overflow-x: auto; } .run-table { width: 100%; border-collapse: collapse; font-size: 13px; } .run-table th, .run-table td { padding: 9px 8px; border-bottom: 1px solid var(--td-border-level-1-color, #e5e7eb); text-align: left; white-space: nowrap; }
.empty { margin: 24px 0; color: var(--td-text-color-secondary, #646a73); text-align: center; }
.task-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 20px; max-height: calc(100dvh - 132px); overflow: auto; padding: 20px 24px 24px; } .task-form .form-heading { grid-column: 1 / -1; } .task-form label { display: flex; min-width: 0; flex-direction: column; gap: 6px; font-size: 13px; } .task-form input, .task-form select, .task-form textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--td-border-level-2-color, #c9cdd4); border-radius: 6px; background: var(--td-bg-color-container, #fff); color: inherit; padding: 9px 10px; font: inherit; } .task-form textarea { resize: vertical; } .task-form small { color: var(--td-text-color-secondary, #646a73); } .span-2 { grid-column: 1 / -1; } .task-form .error { margin: 0; }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid var(--td-brand-color, #0052d9); outline-offset: 2px; }
@media (max-width: 760px) { .scheduled-overlay { align-items: stretch; padding: 0; } .scheduled-dialog { width: 100%; max-height: 100dvh; border-radius: 0; } .scheduled-content { grid-template-columns: 1fr; max-height: calc(100dvh - 92px); overflow: auto; } .task-list { max-height: 38dvh; border-right: 0; border-bottom: 1px solid var(--td-border-level-1-color, #dcdcdc); } .task-detail { overflow: visible; } .task-form { grid-template-columns: 1fr; max-height: calc(100dvh - 92px); } .span-2, .task-form .form-heading { grid-column: 1; } .task-metadata { grid-template-columns: 1fr; } }
</style>
