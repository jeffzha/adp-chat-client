<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  changeSandboxState,
  createSandbox,
  describeSandboxError,
  executeSandboxCode,
  executeSandboxShell,
  querySandbox,
  readSandboxFile,
  openSandboxPty,
  streamSandboxShell,
  writeSandboxFile,
  type ManagedSandbox,
  type SandboxPtyControl,
} from '@/workbench/sandbox'

const props = defineProps<{
  open: boolean
  readOnly: boolean
  conversationId: string
  shellAvailable: boolean
  filesAvailable: boolean
  codeAvailable: boolean
  ptyAvailable: boolean
}>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const { t } = useI18n()
const WorkbenchTerminal = defineAsyncComponent(() => import('./WorkbenchTerminal.vue'))

type WorkbenchTerminalExposed = {
  write: (data: Uint8Array | string) => void
  reset: () => void
  focus: () => void
  dimensions: () => { rows: number; cols: number }
}

const conversationId = computed(() => props.conversationId)
const sandbox = ref<ManagedSandbox | null>(null)
const tab = ref<'shell' | 'code' | 'files' | 'terminal'>('shell')
const busy = ref(false)
const error = ref('')
const command = ref('pwd && ls -la')
const commandOutput = ref('')
const streamShell = ref(true)
const streamAbort = ref<AbortController | null>(null)
const codeLanguage = ref<'python' | 'javascript' | 'typescript' | 'java' | 'r' | 'bash'>('python')
const sourceCode = ref('print("Hello from the managed sandbox")')
const codeOutput = ref('')
const filePath = ref('notes/example.txt')
const fileContent = ref('')
const pty = ref<SandboxPtyControl | null>(null)
const ptyConnected = ref(false)
const terminal = ref<WorkbenchTerminalExposed | null>(null)
const pendingPtyOutput: Array<Uint8Array | string> = []
const panel = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null

const canRun = computed(() => Boolean(sandbox.value?.status === 'running'))
const tabAvailable = (value: typeof tab.value) => ({
  shell: props.shellAvailable,
  code: props.codeAvailable,
  files: props.filesAvailable,
  terminal: props.ptyAvailable,
})[value]
const availableTabs = () => (
  (['shell', 'code', 'files', 'terminal'] as const).filter(tabAvailable)
)
const translatedStatus = computed(() => {
  const key = `workbench.sandbox.status.${sandbox.value?.status || 'unknown'}`
  const translated = t(key)
  return translated === key ? t('workbench.sandbox.status.unknown') : translated
})

const closeDialog = () => emit('update:open', false)

const onDialogKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeDialog()
    return
  }
  if (event.key !== 'Tab' || !panel.value) return
  const focusable = Array.from(panel.value.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hidden)
  if (!focusable.length) {
    event.preventDefault()
    panel.value.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

const onTabKeydown = async (event: KeyboardEvent) => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const tabs = availableTabs()
  if (!tabs.length) return
  const current = Math.max(0, tabs.indexOf(tab.value))
  if (event.key === 'Home') tab.value = tabs[0]
  else if (event.key === 'End') tab.value = tabs[tabs.length - 1]
  else {
    const offset = event.key === 'ArrowRight' ? 1 : -1
    tab.value = tabs[(current + offset + tabs.length) % tabs.length]
  }
  await nextTick()
  panel.value?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus()
}

const execute = async (operation: () => Promise<void>) => {
  busy.value = true
  error.value = ''
  try {
    await operation()
  } catch (cause) {
    error.value = describeSandboxError(cause)
  } finally {
    busy.value = false
  }
}

const createOrReuse = () => execute(async () => {
  sandbox.value = await createSandbox(conversationId.value, 600)
})

const refresh = () => execute(async () => {
  if (!sandbox.value) return
  sandbox.value = await querySandbox(sandbox.value.sandbox_id, conversationId.value)
})

const lifecycle = (action: 'pause' | 'resume' | 'stop') => execute(async () => {
  if (!sandbox.value) return
  sandbox.value = await changeSandboxState(sandbox.value.sandbox_id, conversationId.value, action)
})

const runShell = () => execute(async () => {
  if (!sandbox.value) return
  commandOutput.value = ''
  if (!streamShell.value) {
    const result = await executeSandboxShell(sandbox.value.sandbox_id, {
      conversation_id: conversationId.value,
      command: command.value,
      cwd: '.',
      timeout_seconds: 60,
    })
    commandOutput.value = `${result.stdout}${result.stderr}\n${t('workbench.sandbox.exitCode')}: ${result.exit_code}`
    return
  }
  const controller = new AbortController()
  streamAbort.value = controller
  try {
    await streamSandboxShell(
      sandbox.value.sandbox_id,
      {
        conversation_id: conversationId.value,
        command: command.value,
        cwd: '.',
        timeout_seconds: 60,
      },
      (chunk) => {
        if (chunk.type === 'exit') {
          commandOutput.value += `\n${t('workbench.sandbox.exitCode')}: ${chunk.exit_code}`
        } else if (chunk.type === 'error') {
          commandOutput.value += `\n${chunk.code || t('workbench.sandbox.streamClosed')}`
        } else {
          commandOutput.value += chunk.data
        }
      },
      controller.signal,
    )
  } finally {
    streamAbort.value = null
  }
})

const runCode = () => execute(async () => {
  if (!sandbox.value) return
  codeOutput.value = ''
  const result = await executeSandboxCode(sandbox.value.sandbox_id, {
    conversation_id: conversationId.value,
    code: sourceCode.value,
    language: codeLanguage.value,
    timeout_seconds: 60,
  })
  codeOutput.value = [
    result.stdout,
    result.stderr,
    ...result.results,
    result.error || '',
  ].filter(Boolean).join('\n')
})

const readFile = () => execute(async () => {
  if (!sandbox.value) return
  const data = await readSandboxFile(sandbox.value.sandbox_id, conversationId.value, filePath.value)
  fileContent.value = new TextDecoder('utf-8', { fatal: true }).decode(data)
})

const writeFile = () => execute(async () => {
  if (!sandbox.value) return
  await writeSandboxFile(
    sandbox.value.sandbox_id,
    conversationId.value,
    filePath.value,
    new TextEncoder().encode(fileContent.value),
  )
})

const disconnectPty = () => {
  pty.value?.close()
  pty.value = null
  ptyConnected.value = false
}

const writeTerminal = (data: Uint8Array | string) => {
  if (terminal.value) {
    terminal.value.write(data)
    return
  }
  pendingPtyOutput.push(data)
}

const connectPty = () => execute(async () => {
  if (!sandbox.value || pty.value) return
  terminal.value?.reset()
  const dimensions = terminal.value?.dimensions() || { rows: 24, cols: 100 }
  pty.value = await openSandboxPty(
    sandbox.value.sandbox_id,
    { conversation_id: conversationId.value, ...dimensions, timeout_seconds: 600 },
    {
      onOutput: writeTerminal,
      onControl: (event) => {
        if (event.type === 'ready') {
          ptyConnected.value = true
          terminal.value?.focus()
        }
        if (event.type === 'error') writeTerminal(`\r\n${event.code}\r\n`)
        if (event.type === 'exit') writeTerminal(`\r\n${t('workbench.sandbox.exitCode')}: ${event.exit_code}\r\n`)
      },
      onClose: () => {
        pty.value = null
        ptyConnected.value = false
      },
    },
  )
})

const sendPtyInput = (data: string) => pty.value?.sendInput(data)
const resizePty = (rows: number, cols: number) => pty.value?.resize(rows, cols)

watch(() => props.open, async (open) => {
  if (open) {
    previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    await nextTick()
    panel.value?.focus()
    return
  }
  disconnectPty()
  previousFocus?.focus()
  previousFocus = null
}, { immediate: true })
watch(
  () => [props.shellAvailable, props.codeAvailable, props.filesAvailable, props.ptyAvailable],
  () => {
    if (tabAvailable(tab.value)) return
    tab.value = (
      (['shell', 'code', 'files', 'terminal'] as const).find(tabAvailable) || 'shell'
    )
  },
  { immediate: true },
)
watch(terminal, (value) => {
  if (!value) return
  for (const data of pendingPtyOutput.splice(0)) value.write(data)
})
onBeforeUnmount(disconnectPty)
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="sandbox-overlay" role="presentation" @click.self="closeDialog">
      <section ref="panel" class="sandbox-panel" role="dialog" aria-modal="true" tabindex="-1" :aria-label="t('workbench.sandbox.title')" @keydown="onDialogKeydown">
        <header>
          <div>
            <h2>{{ t('workbench.sandbox.title') }}</h2>
            <p>{{ t('workbench.sandbox.description') }}</p>
          </div>
          <button type="button" class="quiet" :aria-label="t('common.close')" @click="closeDialog">&times;</button>
        </header>

        <p class="security-note">{{ t('workbench.sandbox.securityNote') }}</p>
        <div class="scope-row">
          <label>
            <span>{{ t('workbench.sandbox.conversationId') }}</span>
            <code>{{ conversationId || t('workbench.sandbox.conversationRequired') }}</code>
          </label>
          <button type="button" :disabled="busy || readOnly || !conversationId" @click="createOrReuse">
            {{ t('workbench.sandbox.createOrReuse') }}
          </button>
          <button v-if="sandbox" type="button" class="secondary" :disabled="busy" @click="refresh">
            {{ t('workbench.sandbox.refresh') }}
          </button>
        </div>

        <div v-if="sandbox" class="status-row" role="status">
          <code>{{ sandbox.sandbox_id }}</code>
          <strong>{{ translatedStatus }}</strong>
          <button type="button" class="secondary" :disabled="busy || readOnly" @click="lifecycle('pause')">{{ t('workbench.sandbox.pause') }}</button>
          <button type="button" class="secondary" :disabled="busy || readOnly" @click="lifecycle('resume')">{{ t('workbench.sandbox.resume') }}</button>
          <button type="button" class="danger" :disabled="busy || readOnly" @click="lifecycle('stop')">{{ t('workbench.sandbox.stop') }}</button>
        </div>

        <p v-if="error" class="error" role="alert">{{ error }}</p>

        <template v-if="sandbox">
          <nav class="tabs" role="tablist" :aria-label="t('workbench.sandbox.tools')" @keydown="onTabKeydown">
            <button v-if="shellAvailable" id="sandbox-tab-shell" role="tab" aria-controls="sandbox-panel-shell" :aria-selected="tab === 'shell'" :tabindex="tab === 'shell' ? 0 : -1" type="button" :class="{ active: tab === 'shell' }" @click="tab = 'shell'">{{ t('workbench.sandbox.shell') }}</button>
            <button v-if="codeAvailable" id="sandbox-tab-code" role="tab" aria-controls="sandbox-panel-code" :aria-selected="tab === 'code'" :tabindex="tab === 'code' ? 0 : -1" type="button" :class="{ active: tab === 'code' }" @click="tab = 'code'">{{ t('workbench.sandbox.code') }}</button>
            <button v-if="filesAvailable" id="sandbox-tab-files" role="tab" aria-controls="sandbox-panel-files" :aria-selected="tab === 'files'" :tabindex="tab === 'files' ? 0 : -1" type="button" :class="{ active: tab === 'files' }" @click="tab = 'files'">{{ t('workbench.sandbox.files') }}</button>
            <button v-if="ptyAvailable" id="sandbox-tab-terminal" role="tab" aria-controls="sandbox-panel-terminal" :aria-selected="tab === 'terminal'" :tabindex="tab === 'terminal' ? 0 : -1" type="button" :class="{ active: tab === 'terminal' }" @click="tab = 'terminal'">{{ t('workbench.sandbox.terminal') }}</button>
          </nav>

          <div v-if="tab === 'shell' && shellAvailable" id="sandbox-panel-shell" role="tabpanel" aria-labelledby="sandbox-tab-shell" class="tool-pane">
            <label><span>{{ t('workbench.sandbox.command') }}</span><textarea v-model="command" rows="4" spellcheck="false" /></label>
            <label class="checkbox"><input v-model="streamShell" type="checkbox">{{ t('workbench.sandbox.streamOutput') }}</label>
            <div class="actions">
              <button type="button" :disabled="busy || readOnly || !canRun" @click="runShell">{{ t('workbench.sandbox.run') }}</button>
              <button v-if="streamAbort" type="button" class="danger" @click="streamAbort.abort()">{{ t('workbench.sandbox.cancel') }}</button>
            </div>
            <pre v-if="commandOutput">{{ commandOutput }}</pre>
          </div>

          <div v-else-if="tab === 'code'" id="sandbox-panel-code" role="tabpanel" aria-labelledby="sandbox-tab-code" class="tool-pane">
            <label>
              <span>{{ t('workbench.sandbox.language') }}</span>
              <select v-model="codeLanguage">
                <option value="python">Python</option>
                <option value="javascript">JavaScript</option>
                <option value="typescript">TypeScript</option>
                <option value="java">Java</option>
                <option value="r">R</option>
                <option value="bash">Bash</option>
              </select>
            </label>
            <label><span>{{ t('workbench.sandbox.sourceCode') }}</span><textarea v-model="sourceCode" rows="12" spellcheck="false" /></label>
            <button type="button" :disabled="busy || readOnly || !canRun || !sourceCode" @click="runCode">{{ t('workbench.sandbox.runCode') }}</button>
            <label v-if="codeOutput"><span>{{ t('workbench.sandbox.result') }}</span><pre>{{ codeOutput }}</pre></label>
          </div>

          <div v-else-if="tab === 'files'" id="sandbox-panel-files" role="tabpanel" aria-labelledby="sandbox-tab-files" class="tool-pane">
            <label><span>{{ t('workbench.sandbox.relativePath') }}</span><input v-model.trim="filePath" autocomplete="off"></label>
            <label><span>{{ t('workbench.sandbox.textContent') }}</span><textarea v-model="fileContent" rows="9" /></label>
            <div class="actions">
              <button type="button" class="secondary" :disabled="busy || !canRun" @click="readFile">{{ t('workbench.sandbox.read') }}</button>
              <button type="button" :disabled="busy || readOnly || !canRun" @click="writeFile">{{ t('workbench.sandbox.write') }}</button>
            </div>
          </div>

          <div v-else-if="tab === 'terminal' && ptyAvailable" id="sandbox-panel-terminal" role="tabpanel" aria-labelledby="sandbox-tab-terminal" class="tool-pane">
            <p class="security-note">{{ t('workbench.sandbox.terminalNoReconnect') }}</p>
            <div class="actions">
              <button v-if="!pty" type="button" :disabled="busy || readOnly || !canRun" @click="connectPty">{{ t('workbench.sandbox.connectTerminal') }}</button>
              <button v-else type="button" class="danger" @click="disconnectPty">{{ t('workbench.sandbox.disconnectTerminal') }}</button>
              <span role="status">{{ ptyConnected ? t('workbench.sandbox.terminalConnected') : t('workbench.sandbox.terminalDisconnected') }}</span>
            </div>
            <Suspense>
              <WorkbenchTerminal
                ref="terminal"
                :connected="ptyConnected"
                :label="t('workbench.sandbox.terminalAriaLabel')"
                @input="sendPtyInput"
                @resize="resizePty"
              />
              <template #fallback><div class="terminal-loading">{{ t('common.loading') }}</div></template>
            </Suspense>
          </div>
        </template>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.sandbox-overlay { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 20px; background: rgb(15 23 42 / 55%); }
.sandbox-panel { width: min(900px, 100%); max-height: calc(100dvh - 40px); overflow: auto; border-radius: 12px; background: var(--td-bg-color-container, #fff); color: var(--td-text-color-primary, #1f2329); box-shadow: 0 20px 60px rgb(0 0 0 / 25%); padding: 20px; }
header, .scope-row, .status-row, .actions { display: flex; align-items: center; gap: 10px; }
header { justify-content: space-between; } h2 { margin: 0; font-size: 20px; } header p { margin: 4px 0 0; color: var(--td-text-color-secondary, #646a73); }
button { border: 1px solid var(--td-brand-color, #0052d9); border-radius: 6px; background: var(--td-brand-color, #0052d9); color: #fff; cursor: pointer; padding: 7px 12px; }
button:disabled { cursor: not-allowed; opacity: .5; } button.secondary, button.quiet { background: transparent; color: var(--td-brand-color, #0052d9); } button.quiet { border: 0; font-size: 20px; } button.danger { border-color: #b42318; background: #b42318; }
.security-note { border-left: 3px solid #d97706; background: #fffbeb; color: #7c2d12; padding: 9px 12px; }
.scope-row { align-items: flex-end; flex-wrap: wrap; } .scope-row label { flex: 1 1 300px; }
label { display: grid; gap: 5px; font-size: 13px; } input, textarea, select { box-sizing: border-box; width: 100%; border: 1px solid var(--td-border-level-1-color, #c9cdd4); border-radius: 6px; background: transparent; color: inherit; padding: 8px; } textarea, pre { font: 13px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
.status-row { flex-wrap: wrap; margin-top: 14px; } .status-row code { overflow-wrap: anywhere; } .status-row strong { margin-right: auto; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--td-border-level-1-color, #dcdcdc); margin-top: 18px; } .tabs button { border: 0; border-radius: 6px 6px 0 0; background: transparent; color: inherit; } .tabs button.active { background: #eaf2ff; color: #0052d9; }
.tool-pane { display: grid; gap: 12px; padding-top: 14px; } .tool-pane > button { justify-self: start; } .checkbox { display: flex; align-items: center; gap: 7px; } .checkbox input { width: auto; }
pre { max-height: 280px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; border-radius: 6px; background: #111827; color: #e5e7eb; margin: 0; padding: 12px; }
.terminal-loading { min-height: 320px; display: grid; place-items: center; border-radius: 6px; background: #111827; color: #e5e7eb; }
.error { color: #b42318; overflow-wrap: anywhere; }
@media (max-width: 640px) { .sandbox-overlay { padding: 0; place-items: stretch; } .sandbox-panel { max-height: 100dvh; border-radius: 0; } .scope-row > button { flex: 1; } }
</style>
