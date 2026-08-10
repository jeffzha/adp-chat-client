<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  changeIntegrationBinding,
  describeIntegrationError,
  listIntegrations,
  type IntegrationCatalog,
  type IntegrationResource,
} from '@/workbench/integrations'
import { integrationBindingPolicy } from '@/workbench/integrationPolicy'

const props = defineProps<{ open: boolean; readOnly: boolean }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const { t, locale } = useI18n()
const catalog = ref<IntegrationCatalog | null>(null)
const loading = ref(false)
const changing = ref('')
const error = ref('')
const dialog = ref<HTMLElement | null>(null)
const callbackUrl = new URL(window.location.href)
const callbackResult = ref(callbackUrl.searchParams.get('integration_oauth') || '')
if (callbackResult.value) {
  callbackUrl.searchParams.delete('integration_oauth')
  window.history.replaceState(window.history.state, '', callbackUrl)
}

const grouped = computed(() => {
  const groups: Record<string, IntegrationResource[]> = { skill: [], plugin: [], tool: [], connector: [] }
  for (const resource of catalog.value?.resources || []) groups[resource.kind].push(resource)
  return groups
})

const name = (resource: IntegrationResource) => locale.value === 'zh' ? resource.name_zh : resource.name_en
const close = () => emit('update:open', false)
const isBound = (resource: IntegrationResource) => resource.binding_status === 'active'
const busy = (resource: IntegrationResource) => changing.value === `${resource.kind}:${resource.resource_id}`

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    catalog.value = await listIntegrations()
  } catch (requestError) {
    error.value = describeIntegrationError(requestError)
  } finally {
    loading.value = false
  }
}

const changeBinding = async (resource: IntegrationResource) => {
  if (props.readOnly || !resource.can_bind) return
  changing.value = `${resource.kind}:${resource.resource_id}`
  error.value = ''
  try {
    await changeIntegrationBinding(resource, isBound(resource) ? 'unbind' : 'bind')
    await load()
  } catch (requestError) {
    error.value = describeIntegrationError(requestError)
  } finally {
    changing.value = ''
  }
}

const handleKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape' && !changing.value) close()
}

watch(() => props.open, async (open) => {
  if (!open) return
  await load()
  await nextTick()
  dialog.value?.focus()
})
</script>

<template>
  <div v-if="open" class="integration-overlay" @mousedown.self="close">
    <section ref="dialog" class="integration-dialog" role="dialog" aria-modal="true" aria-labelledby="integration-title" tabindex="-1" @keydown="handleKeydown">
      <header>
        <div>
          <h2 id="integration-title">{{ t('workbench.integrations.title') }}</h2>
          <p>{{ t('workbench.integrations.description') }}</p>
        </div>
        <button type="button" class="icon-button" :aria-label="t('workbench.integrations.close')" @click="close">×</button>
      </header>
      <p v-if="readOnly" class="notice" role="status">{{ t('workbench.integrations.readOnly') }}</p>
      <p v-if="callbackResult" class="notice" role="status">
        {{ callbackResult === 'success' ? t('workbench.integrations.oauthSuccess') : t('workbench.integrations.oauthFailed') }}
      </p>
      <p class="contract-notice" role="note">{{ t('workbench.integrations.contractNotice') }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="loading && !catalog" class="empty">{{ t('workbench.integrations.loading') }}</p>
      <div v-else class="integration-content">
        <section v-for="kind in ['skill', 'plugin', 'tool', 'connector']" :key="kind" class="resource-group">
          <h3>{{ t(`workbench.integrations.kinds.${kind}`) }}</h3>
          <p v-if="!grouped[kind].length" class="empty">{{ t('workbench.integrations.empty') }}</p>
          <article v-for="resource in grouped[kind]" :key="`${resource.kind}:${resource.parent_resource_id || ''}:${resource.resource_id}`" class="resource-card">
            <div class="resource-main">
              <strong>{{ name(resource) }}</strong>
              <code>{{ resource.resource_id }}</code>
              <span class="status" :data-status="resource.binding_status">{{ t(`workbench.integrations.status.${resource.binding_status}`, resource.binding_status) }}</span>
            </div>
            <p v-if="resource.blocked_reason" class="blocked">{{ resource.blocked_reason }}</p>
            <p v-else-if="resource.kind === 'skill' && isBound(resource)" class="blocked">{{ t('workbench.integrations.skillExecutionBlocked') }}</p>
            <div class="resource-actions">
              <button type="button" class="button" :disabled="readOnly || busy(resource) || !integrationBindingPolicy(resource).canMutate" @click="changeBinding(resource)">
                {{ isBound(resource) ? t('workbench.integrations.unbind') : t('workbench.integrations.bind') }}
              </button>
            </div>
          </article>
        </section>
      </div>
    </section>
  </div>
</template>

<style scoped>
.integration-overlay { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 20px; background: rgb(0 0 0 / 45%); }
.integration-dialog { width: min(980px, 100%); max-height: min(860px, calc(100dvh - 40px)); overflow: auto; border-radius: 12px; background: var(--td-bg-color-container, #fff); color: var(--td-text-color-primary, #1f2329); box-shadow: 0 24px 80px rgb(0 0 0 / 25%); outline: none; }
header { position: sticky; top: 0; z-index: 1; display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 20px 24px; border-bottom: 1px solid var(--td-border-level-1-color, #dcdcdc); background: inherit; }
h2, h3, p { margin-top: 0; } h2 { margin-bottom: 4px; font-size: 20px; } header p { margin-bottom: 0; color: var(--td-text-color-secondary, #646a73); font-size: 13px; }
.icon-button { width: 34px; height: 34px; border: 0; border-radius: 6px; background: transparent; color: inherit; cursor: pointer; font-size: 24px; }
.notice, .error, .contract-notice { margin: 12px 24px; padding: 9px 12px; border-radius: 6px; }
.notice { background: #fff3d6; color: #704600; } .error { background: #fff0ed; color: var(--td-error-color, #b11f26); } .contract-notice { background: #eef4ff; color: #234a86; }
.integration-content { padding: 4px 24px 24px; }
.resource-group { margin-top: 20px; } .resource-group h3 { margin-bottom: 8px; }
.resource-card { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 8px; padding: 12px 14px; border: 1px solid var(--td-border-level-1-color, #dcdcdc); border-radius: 8px; }
.resource-main { display: flex; min-width: 0; align-items: center; flex-wrap: wrap; gap: 8px; } code { overflow-wrap: anywhere; color: var(--td-text-color-secondary, #646a73); }
.status { border-radius: 999px; background: #eef0f2; padding: 2px 8px; font-size: 12px; } .status[data-status='active'] { background: #e8f5ee; color: #16784a; } .status[data-status='provider_unknown'] { background: #fff0ed; color: #b11f26; }
.blocked { flex: 1 1 260px; margin: 0; color: #8a5a00; font-size: 12px; }
.resource-actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
.button { min-height: 34px; border: 1px solid var(--td-border-level-2-color, #c9cdd4); border-radius: 6px; background: var(--td-bg-color-container, #fff); color: inherit; cursor: pointer; padding: 6px 12px; } .button:disabled { cursor: not-allowed; opacity: .5; } .button--primary { border-color: var(--td-brand-color, #0052d9); background: var(--td-brand-color, #0052d9); color: #fff; } .button--danger { border-color: var(--td-error-color, #d54941); color: var(--td-error-color, #b11f26); }
.connection-status, .empty { color: var(--td-text-color-secondary, #646a73); font-size: 13px; } .empty { margin: 18px 0; text-align: center; }
button:focus-visible { outline: 2px solid var(--td-brand-color, #0052d9); outline-offset: 2px; }
@media (max-width: 720px) { .integration-overlay { align-items: stretch; padding: 0; } .integration-dialog { width: 100%; max-height: 100dvh; border-radius: 0; } .resource-card { align-items: flex-start; flex-direction: column; } .resource-actions { justify-content: flex-start; } }
</style>
