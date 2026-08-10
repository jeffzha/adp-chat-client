<script setup lang="ts">
import { computed } from 'vue';
import CustomizedIcon from './CustomizedIcon.vue';
import type { ThemeProps } from '../model/type';
import { themePropsDefaults } from '../model/type';

interface Props extends ThemeProps {
    language?: string;
    label?: string;
}

const props = withDefaults(defineProps<Props>(), {
    ...themePropsDefaults,
    language: 'zh-CN',
    label: '',
});

const accessibleLabel = computed(() => props.label || (props.language.startsWith('en')
    ? 'Toggle conversation sidebar'
    : '展开或收起会话侧栏'));

const emit = defineEmits<{
    (e: 'toggle'): void;
}>();

const handleClick = () => {
    emit('toggle');
};
</script>

<template>
    <button type="button" class="sidebar-toggle" :aria-label="accessibleLabel" @click="handleClick">
        <CustomizedIcon remote class="sidebar-icon" name="basic_sidebarchat_line" :theme="theme" />
    </button>
</template>

<style scoped>
.sidebar-toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    color: inherit;
    background: transparent;
    cursor: pointer;
}
.sidebar-toggle:focus-visible { outline: 2px solid var(--td-brand-color, #0052d9); outline-offset: 2px; }
</style>
