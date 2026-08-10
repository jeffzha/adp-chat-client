<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { initializeWorkbench, workbenchRuntime } from '@/workbench/runtime'

const { t } = useI18n()
const headingRef = ref<HTMLElement | null>(null)

const retry = async () => {
  await initializeWorkbench(true)
  if (workbenchRuntime.status === 'ready') window.location.replace('/workbench/')
  else await nextTick(() => headingRef.value?.focus())
}

onMounted(() => headingRef.value?.focus())
</script>

<template>
  <main class="workbench-error">
    <section class="workbench-error__card" aria-labelledby="workbench-error-title">
      <h1 id="workbench-error-title" ref="headingRef" tabindex="-1">{{ t('workbench.errorTitle') }}</h1>
      <p>{{ t('workbench.errorDescription') }}</p>
      <p class="sr-only" role="status" aria-live="assertive">
        {{ workbenchRuntime.status === 'loading' ? t('workbench.retrying') : '' }}
      </p>
      <p v-if="workbenchRuntime.errorCode" class="workbench-error__request">{{ t('workbench.errorCode') }}: {{ workbenchRuntime.errorCode }}</p>
      <p v-if="workbenchRuntime.requestId" class="workbench-error__request">{{ t('workbench.requestId') }}: {{ workbenchRuntime.requestId }}</p>
      <div class="workbench-error__actions">
        <TButton theme="primary" :loading="workbenchRuntime.status === 'loading'" @click="retry">{{ t('workbench.retry') }}</TButton>
        <TButton variant="outline" tag="a" href="/">{{ t('workbench.returnGateway') }}</TButton>
      </div>
    </section>
  </main>
</template>

<style scoped>
.workbench-error { min-height: 100%; display: grid; place-items: center; padding: 24px; background: var(--td-bg-color-page, #f3f3f3); }
.workbench-error__card { width: min(100%, 520px); padding: 28px; border-radius: 12px; background: var(--td-bg-color-container, #fff); box-shadow: 0 8px 28px rgb(0 0 0 / 10%); }
.workbench-error__card h1 { margin: 0 0 12px; font-size: 22px; }
.workbench-error__card h1:focus { outline: none; }
.workbench-error__card p { color: var(--td-text-color-secondary, #646a73); overflow-wrap: anywhere; }
.workbench-error__request { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.workbench-error__actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 20px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }

@media (max-width: 480px) {
  .workbench-error { padding: 16px; }
  .workbench-error__card { padding: 22px 18px; }
  .workbench-error__actions { align-items: stretch; flex-direction: column; }
}
</style>
