<template>
    <div class="cron-prompt-input">
        <div class="cron-prompt-input__label">
            {{ i18n.promptLabel }}
            <span class="cron-prompt-input__required">*</span>
        </div>

        <t-textarea
            v-model="innerValue"
            :placeholder="i18n.promptPlaceholder"
            :maxlength="maxLength"
            :autosize="{ minRows: 4, maxRows: 10 }"
        />
    </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { Textarea as TTextarea } from 'tdesign-vue-next';
import type { CronTaskI18n } from '../../../model/cronTask';
import { getCronTaskI18nByLanguage } from '../../../model/cronTask';

interface Props {
    modelValue: string;
    maxLength?: number;
    language?: string;
    i18n?: Partial<CronTaskI18n>;
}

const props = withDefaults(defineProps<Props>(), {
    modelValue: '',
    maxLength: 10000,
    language: 'zh-CN',
    i18n: () => ({}),
});

const emit = defineEmits<{
    (e: 'update:modelValue', val: string): void;
}>();

const i18n = computed<Required<CronTaskI18n>>(() => ({
    ...getCronTaskI18nByLanguage(props.language),
    ...props.i18n,
}));

const innerValue = computed({
    get: () => props.modelValue,
    set: (val) => emit('update:modelValue', val),
});

/** 表单校验 */
function validate(): { valid: boolean; message?: string } {
    if (!innerValue.value || !innerValue.value.trim()) {
        return { valid: false, message: i18n.value.promptPlaceholder };
    }
    return { valid: true };
}

/**
 * 获取表单数据（返回 AppTrigger 字段结构）
 * AppTrigger 使用 ExecutePrompt，不再携带 modelId / workspaceId
 */
function getFormData(): { ExecutePrompt: string } {
    return {
        ExecutePrompt: innerValue.value?.trim() || '',
    };
}

/**
 * 设置表单数据（编辑回填）
 * 同时兼容旧字段 prompt 和新字段 ExecutePrompt
 */
function setFormData(data: { ExecutePrompt?: string; prompt?: string }) {
    const val = data.ExecutePrompt ?? data.prompt ?? '';
    emit('update:modelValue', val);
}

/** 重置表单 */
function resetForm() {
    emit('update:modelValue', '');
}

defineExpose({ validate, getFormData, setFormData, resetForm });
</script>

<style scoped>
.cron-prompt-input {
    display: flex;
    flex-direction: column;
    gap: var(--td-size-4);
}

.cron-prompt-input__label {
    font-size: var(--td-font-size-body-medium);
    font-weight: 500;
    color: var(--td-text-color-primary);
    line-height: var(--td-line-height-body-medium);
}

.cron-prompt-input__required {
    color: var(--td-error-color);
    margin-left: var(--td-size-1);
}
</style>
