<script setup lang="ts">
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{
  connected: boolean
  label: string
}>()

const emit = defineEmits<{
  input: [data: string]
  resize: [rows: number, cols: number]
}>()

const host = ref<HTMLElement | null>(null)
let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let resizeObserver: ResizeObserver | null = null
let resizeFrame = 0

const fit = () => {
  if (!terminal || !fitAddon || !host.value || host.value.clientWidth === 0) return
  fitAddon.fit()
}

onMounted(async () => {
  await nextTick()
  if (!host.value) return

  terminal = new Terminal({
    allowProposedApi: false,
    convertEol: false,
    cursorBlink: true,
    cursorStyle: 'block',
    disableStdin: !props.connected,
    fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
    fontSize: 13,
    scrollback: 2000,
    theme: {
      background: '#111827',
      foreground: '#e5e7eb',
      cursor: '#f9fafb',
      selectionBackground: '#374151',
    },
  })
  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.open(host.value)
  terminal.onData((data) => {
    if (props.connected) emit('input', data)
  })
  terminal.onResize(({ rows, cols }) => {
    if (props.connected) emit('resize', rows, cols)
  })
  fit()

  resizeObserver = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame)
    resizeFrame = requestAnimationFrame(fit)
  })
  resizeObserver.observe(host.value)
})

watch(() => props.connected, (connected) => {
  if (!terminal) return
  terminal.options.disableStdin = !connected
  if (connected) {
    fit()
    terminal.focus()
  }
})

onBeforeUnmount(() => {
  cancelAnimationFrame(resizeFrame)
  resizeObserver?.disconnect()
  resizeObserver = null
  terminal?.dispose()
  terminal = null
  fitAddon = null
})

defineExpose({
  write: (data: Uint8Array | string) => terminal?.write(data),
  reset: () => terminal?.reset(),
  focus: () => terminal?.focus(),
  dimensions: () => ({ rows: terminal?.rows || 24, cols: terminal?.cols || 100 }),
})
</script>

<template>
  <div ref="host" class="workbench-terminal" role="application" tabindex="0" :aria-label="label" />
</template>

<style scoped>
.workbench-terminal {
  box-sizing: border-box;
  width: 100%;
  min-height: 320px;
  overflow: hidden;
  border: 1px solid #374151;
  border-radius: 6px;
  background: #111827;
  padding: 8px;
}

.workbench-terminal :deep(.xterm) {
  height: 304px;
}
</style>
