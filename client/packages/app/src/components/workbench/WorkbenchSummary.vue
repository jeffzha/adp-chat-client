<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { workbenchAccessState, workbenchReadOnly, workbenchRuntime } from '@/workbench/runtime'
import { canOpenManagedSandbox } from '@/workbench/sandboxBoundary'

const { t, te, locale } = useI18n()
const expanded = ref(false)
const emit = defineEmits<{ openScheduledTasks: []; openIntegrations: []; openSandbox: [] }>()

const planName = computed(() => {
  const snapshot = workbenchRuntime.plan?.snapshot
  const value = snapshot?.display_name || snapshot?.plan_name
  return typeof value === 'string' && value ? value : t('workbench.unavailable')
})

const period = computed(() => {
  const start = workbenchRuntime.plan?.start_at
  const end = workbenchRuntime.plan?.end_at
  if (!start || !end) return t('workbench.unavailable')
  const language = locale.value === 'zh' ? 'zh-CN' : 'en-US'
  const options: Intl.DateTimeFormatOptions = { timeZone: 'Asia/Shanghai' }
  return `${new Date(start).toLocaleDateString(language, options)} – ${new Date(end).toLocaleDateString(language, options)}`
})

const capabilities = computed(() => workbenchRuntime.config?.capabilities || [])
const scheduledTasksAvailable = computed(() => capabilities.value.includes('scheduled_tasks'))
const integrationsAvailable = computed(() => capabilities.value.some((value) => (
  ['skills', 'tools', 'connectors'].includes(value)
)))
const sandboxAvailable = computed(() => canOpenManagedSandbox(
  workbenchRuntime.config?.sandbox_enabled,
  capabilities.value,
))
const limits = computed(() => Object.entries(workbenchRuntime.config?.limits || {}))
const stateLabel = computed(() => t(`workbench.state.${workbenchAccessState.value}`))
const stateDescription = computed(() => t(`workbench.stateDescription.${workbenchAccessState.value}`))

const translateCode = (scope: 'statusLabels' | 'capabilityLabels' | 'limitLabels', value?: string) => {
  if (!value) return t('workbench.unavailable')
  const normalized = value.trim().toLowerCase().replace(/-/g, '_')
  const key = `workbench.${scope}.${normalized}`
  return te(key) ? t(key) : value.replace(/_/g, ' ')
}

const formatLimit = (key: string, value: number) => {
  if (key === 'max_runtime_seconds') return t('workbench.seconds', { count: value })
  if (key === 'max_file_bytes' || key === 'max_file_size_bytes') {
    return new Intl.NumberFormat(locale.value === 'zh' ? 'zh-CN' : 'en-US', {
      style: 'unit',
      unit: 'megabyte',
      maximumFractionDigits: 1,
    }).format(value / (1024 * 1024))
  }
  return new Intl.NumberFormat(locale.value === 'zh' ? 'zh-CN' : 'en-US').format(value)
}
</script>

<template>
  <section class="workbench-summary" :class="[`is-${workbenchAccessState}`, { 'is-read-only': workbenchReadOnly }]" aria-labelledby="workbench-summary-title">
    <div class="workbench-summary__main">
      <div class="workbench-summary__title-group">
        <strong id="workbench-summary-title">{{ workbenchRuntime.config?.customer_display_name || t('workbench.customer') }}</strong>
        <span aria-hidden="true">·</span>
        <span>{{ workbenchRuntime.config?.app_display_name || t('workbench.application') }}</span>
        <span class="workbench-summary__badge" role="status" aria-live="polite">
          {{ stateLabel }}
        </span>
      </div>
      <div class="workbench-summary__plan">
        <span>{{ planName }}</span>
        <span v-if="workbenchRuntime.plan?.amount_cny">¥{{ workbenchRuntime.plan.amount_cny }} / {{ t('workbench.month') }}</span>
        <span class="workbench-summary__billing-note">{{ t('workbench.fixedPlanBillingNote') }}</span>
        <button v-if="scheduledTasksAvailable" type="button" class="workbench-summary__tasks" @click="emit('openScheduledTasks')">
          {{ t('workbench.scheduledTasks.open') }}
        </button>
        <button v-if="integrationsAvailable" type="button" class="workbench-summary__tasks" @click="emit('openIntegrations')">
          {{ t('workbench.integrations.open') }}
        </button>
        <button v-if="sandboxAvailable" type="button" class="workbench-summary__tasks" @click="emit('openSandbox')">
          {{ t('workbench.sandbox.open') }}
        </button>
        <button type="button" class="workbench-summary__toggle" aria-controls="workbench-summary-details" :aria-expanded="expanded" @click="expanded = !expanded">
          {{ expanded ? t('workbench.collapse') : t('workbench.details') }}
        </button>
      </div>
    </div>

    <p v-if="workbenchReadOnly" class="workbench-summary__state-description" role="status" aria-live="polite">
      {{ stateDescription }}
    </p>

    <div v-if="expanded" id="workbench-summary-details" class="workbench-summary__details">
      <dl>
        <div><dt>{{ t('workbench.period') }}</dt><dd>{{ period }}</dd></div>
        <div><dt>{{ t('workbench.paymentStatus') }}</dt><dd>{{ translateCode('statusLabels', workbenchRuntime.plan?.payment_status) }}</dd></div>
        <div><dt>{{ t('workbench.accessMode') }}</dt><dd>{{ translateCode('statusLabels', workbenchRuntime.config?.access_mode) }}</dd></div>
        <div><dt>{{ t('workbench.applicationStatus') }}</dt><dd>{{ translateCode('statusLabels', workbenchRuntime.config?.app_status) }}</dd></div>
      </dl>
      <div v-if="capabilities.length" class="workbench-summary__collection">
        <span>{{ t('workbench.capabilities') }}</span>
        <ul><li v-for="capability in capabilities" :key="capability">{{ translateCode('capabilityLabels', capability) }}</li></ul>
      </div>
      <div v-if="limits.length" class="workbench-summary__collection">
        <span>{{ t('workbench.limits') }}</span>
        <ul><li v-for="[key, value] in limits" :key="key">{{ translateCode('limitLabels', key) }}: {{ formatLimit(key, value) }}</li></ul>
      </div>
      <p v-if="workbenchRuntime.planError" class="workbench-summary__warning" role="status">
        {{ t('workbench.planUnavailable') }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.workbench-summary {
  position: relative;
  z-index: 5;
  flex: 0 0 auto;
  border-bottom: 1px solid var(--td-border-level-1-color, #dcdcdc);
  background: var(--td-bg-color-container, #fff);
  color: var(--td-text-color-primary, #1f2329);
  padding: 10px 16px;
  box-shadow: 0 1px 4px rgb(0 0 0 / 5%);
}

.workbench-summary.is-read-only { border-bottom-color: #e5a100; }
.workbench-summary.is-disabled { border-bottom-color: var(--td-error-color, #d54941); }
.workbench-summary__main, .workbench-summary__title-group, .workbench-summary__plan { display: flex; align-items: center; gap: 8px; }
.workbench-summary__main { justify-content: space-between; min-width: 0; }
.workbench-summary__title-group, .workbench-summary__plan { flex-wrap: wrap; }
.workbench-summary__badge { border-radius: 999px; background: #e8f5ee; color: #16784a; padding: 2px 8px; font-size: 12px; }
.is-read-only .workbench-summary__badge { background: #fff3d6; color: #8a5a00; }
.workbench-summary__toggle { border: 0; background: transparent; color: var(--td-brand-color, #0052d9); cursor: pointer; padding: 4px; }
.workbench-summary__tasks { border: 1px solid var(--td-brand-color, #0052d9); border-radius: 6px; background: transparent; color: var(--td-brand-color, #0052d9); cursor: pointer; padding: 4px 9px; }
.workbench-summary__toggle:focus-visible, .workbench-summary__tasks:focus-visible { outline: 2px solid var(--td-brand-color, #0052d9); outline-offset: 2px; }
.workbench-summary__details { margin-top: 10px; font-size: 13px; }
.workbench-summary__state-description { margin: 8px 0 0; color: #8a5a00; font-size: 13px; }
.is-disabled .workbench-summary__state-description { color: var(--td-error-color, #b11f26); }
.workbench-summary__details dl { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px 16px; margin: 0; }
.workbench-summary__details dl > div { min-width: 0; }
.workbench-summary__details dt { color: var(--td-text-color-secondary, #646a73); }
.workbench-summary__details dd { margin: 2px 0 0; overflow-wrap: anywhere; }
.workbench-summary__collection { display: flex; gap: 8px; margin-top: 8px; }
.workbench-summary__collection > span { color: var(--td-text-color-secondary, #646a73); flex: 0 0 auto; }
.workbench-summary__collection ul { display: flex; flex-wrap: wrap; gap: 4px 12px; list-style: none; margin: 0; padding: 0; }
.workbench-summary__warning { color: #8a5a00; margin: 8px 0 0; }

@media (max-width: 720px) {
  .workbench-summary { padding: 8px 12px; }
  .workbench-summary__main { align-items: flex-start; flex-direction: column; }
  .workbench-summary__details { max-height: 36dvh; overflow: auto; overscroll-behavior: contain; }
  .workbench-summary__details dl { grid-template-columns: repeat(2, minmax(100px, 1fr)); }
}

@media (max-width: 420px) {
  .workbench-summary__details dl { grid-template-columns: 1fr; }
  .workbench-summary__collection { align-items: flex-start; flex-direction: column; }
}
</style>
