<template>
    <div class="cron-task-card" @click="onCardClick">
        <!-- 顶部：标题 + 状态 -->
        <div class="card-header">
            <span class="card-title" :title="taskName">{{ taskName }}</span>
            <span class="card-status" :class="`card-status--${statusClass}`">
                <span class="status-dot" />
                {{ statusText }}
            </span>
        </div>

        <!-- 中间：策略摘要 + 成功/失败次数 -->
        <div class="card-tags">
            <t-tooltip :content="scheduleText" placement="top">
                <span class="card-tags__time">
                    <CustomizedIcon
                        remote
                        name="basic_time_line"
                        size="xxs"
                        :show-hover-bg="false"
                        :theme="theme"
                    />
                    <span class="card-tags__time-text">{{ scheduleText }}</span>
                </span>
            </t-tooltip>
            <span v-if="successCount > 0" class="card-tags__count card-tags__count--success">
                <CustomizedIcon
                    remote
                    name="basic_finish_line"
                    size="xxs"
                    :show-hover-bg="false"
                    :theme="theme"
                />
                {{ successCount }}
            </span>
            <span v-if="failureCount > 0" class="card-tags__count card-tags__count--fail">
                <CustomizedIcon
                    remote
                    name="basic_close_line"
                    size="xxs"
                    :show-hover-bg="false"
                    :theme="theme"
                />
                {{ failureCount }}
            </span>
        </div>

        <!-- 底部：操作按钮 -->
        <div class="card-actions">
            <t-tooltip v-if="taskStatus === TimerTaskStatus.ACTIVE" :content="i18n.pause" placement="top">
                <t-button
                    size="small"
                    variant="text"
                    shape="square"
                    :disabled="actionLoading"
                    @click.stop="emit('pause', task)"
                >
                    <CustomizedIcon
                        remote
                        name="basic_pause_line"
                        size="xs"
                        :show-hover-bg="false"
                        :theme="theme"
                    />
                </t-button>
            </t-tooltip>
            <t-tooltip v-else-if="taskStatus === TimerTaskStatus.PAUSED" :content="i18n.resume" placement="top">
                <t-button
                    size="small"
                    variant="text"
                    shape="square"
                    :disabled="actionLoading"
                    @click.stop="emit('resume', task)"
                >
                    <CustomizedIcon
                        remote
                        name="basic_play_round_line"
                        size="xs"
                        :show-hover-bg="false"
                        :theme="theme"
                    />
                </t-button>
            </t-tooltip>
            <t-tooltip :content="i18n.edit" placement="top">
                <t-button
                    size="small"
                    variant="text"
                    shape="square"
                    :disabled="actionLoading"
                    @click.stop="emit('edit', task)"
                >
                    <CustomizedIcon
                        remote
                        name="basic_edit_line"
                        size="xs"
                        :show-hover-bg="false"
                        :theme="theme"
                    />
                </t-button>
            </t-tooltip>
            <t-tooltip :content="i18n.del" placement="top">
                <t-button
                    size="small"
                    variant="text"
                    shape="square"
                    :disabled="actionLoading"
                    @click.stop="emit('delete', task)"
                >
                    <CustomizedIcon
                        remote
                        name="basic_delete_line"
                        size="xs"
                        :show-hover-bg="false"
                        :theme="theme"
                    />
                </t-button>
            </t-tooltip>
            <t-button
                size="small"
                theme="primary"
                variant="outline"
                :disabled="actionLoading"
                :loading="actionLoading"
                @click.stop="emit('run', task)"
            >
                <template #icon>
                    <CustomizedIcon
                        remote
                        name="basic_play_line"
                        size="xxs"
                        :show-hover-bg="false"
                        :theme="theme"
                    />
                </template>
                {{ i18n.runNow }}
            </t-button>
        </div>
    </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import {
    Button as TButton,
    Tooltip as TTooltip,
} from 'tdesign-vue-next';
import CustomizedIcon from '../CustomizedIcon.vue';
import type { ThemeProps } from '../../model/type';
import { themePropsDefaults } from '../../model/type';
import type { CronTaskI18n, TimerTaskSummary, TimerTask } from '../../model/cronTask';
import { TimerTaskStatus, getCronTaskI18nByLanguage } from '../../model/cronTask';
import { AppTriggerStatus } from '../../model/appTrigger';
import {
    getTaskName,
    getPolicySummary,
    getTaskStatus,
    getSuccessCount,
    getFailureCount,
} from '../../utils/cronTask';
import {
    getTriggerName,
    getTriggerPolicySummary,
    getTriggerStatus,
    getTriggerSuccessCount,
    getTriggerFailureCount,
    isAppTriggerSummary,
} from '../../utils/appTrigger';

interface Props extends ThemeProps {
    /** 任务数据（AppTriggerSummary / TimerTaskSummary / 完整 Trigger / 完整 Task） */
    task: TimerTaskSummary | TimerTask | Record<string, any>;
    /** 是否处于操作 loading 态 */
    actionLoading?: boolean;
    /** i18n 覆盖 */
    i18n?: Partial<CronTaskI18n>;
    /** 语言 */
    language?: string;
}

const props = withDefaults(defineProps<Props>(), {
    ...themePropsDefaults,
    actionLoading: false,
    i18n: () => ({}),
    language: 'zh-CN',
});

const emit = defineEmits<{
    (e: 'click', task: Props['task']): void;
    (e: 'pause', task: Props['task']): void;
    (e: 'resume', task: Props['task']): void;
    (e: 'edit', task: Props['task']): void;
    (e: 'delete', task: Props['task']): void;
    (e: 'run', task: Props['task']): void;
}>();

const i18n = computed<Required<CronTaskI18n>>(() => ({
    ...getCronTaskI18nByLanguage(props.language),
    ...props.i18n,
}));

/** 是否为新 AppTrigger 数据结构 */
const isAppTrigger = computed(() => isAppTriggerSummary(props.task));

const taskName = computed(() =>
    isAppTrigger.value ? getTriggerName(props.task) : getTaskName(props.task)
);
const taskStatus = computed(() =>
    isAppTrigger.value ? getTriggerStatus(props.task) : getTaskStatus(props.task)
);
const successCount = computed(() =>
    isAppTrigger.value ? getTriggerSuccessCount(props.task) : getSuccessCount(props.task)
);
const failureCount = computed(() =>
    isAppTrigger.value ? getTriggerFailureCount(props.task) : getFailureCount(props.task)
);

const scheduleText = computed(() => {
    if (isAppTrigger.value) return getTriggerPolicySummary(props.task) || '-';
    return getPolicySummary(props.task) || '-';
});

/** 状态 CSS class（兼容新旧枚举值：ACTIVE=1/ENABLED=1, PAUSED=2） */
const statusClass = computed(() => {
    const s = taskStatus.value;
    if (s === TimerTaskStatus.ACTIVE || s === AppTriggerStatus.ENABLED) return 'active';
    if (s === TimerTaskStatus.PAUSED || s === AppTriggerStatus.PAUSED) return 'paused';
    return 'stopped';
});

/** 状态展示文本 */
const statusText = computed(() => {
    const s = taskStatus.value;
    if (s === TimerTaskStatus.ACTIVE || s === AppTriggerStatus.ENABLED) return i18n.value.running;
    if (s === TimerTaskStatus.PAUSED || s === AppTriggerStatus.PAUSED) return i18n.value.paused;
    if (s === TimerTaskStatus.COMPLETED || s === AppTriggerStatus.DELETED) return i18n.value.completed;
    return '';
});

function onCardClick() {
    emit('click', props.task);
}
</script>

<style scoped>
.cron-task-card {
    display: flex;
    flex-direction: column;
    height: 120px;
    padding: var(--td-size-6);
    background: var(--td-bg-color-container);
    border: 1px solid var(--td-border-level-2-color);
    border-radius: var(--td-radius-default);
    cursor: pointer;
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
    box-sizing: border-box;
}

.cron-task-card:hover {
    border-color: var(--td-brand-color);
    box-shadow: var(--td-shadow-1);
}

/* 顶部 */
.card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 24px;
}

.card-title {
    flex: 1;
    min-width: 0;
    font-size: var(--td-font-size-body-large);
    font-weight: 500;
    line-height: var(--td-line-height-body-large);
    color: var(--td-text-color-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.card-status {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    margin-left: var(--td-size-5);
    font-size: var(--td-font-size-body-small);
    line-height: var(--td-line-height-body-small);
    color: var(--td-text-color-secondary);
}

.card-status .status-dot {
    width: 8px;
    height: 8px;
    margin-right: var(--td-size-2);
    border-radius: var(--td-radius-circle);
    background: var(--td-text-color-disabled);
}

.card-status--active .status-dot {
    background: var(--td-success-color);
}

.card-status--paused .status-dot {
    background: var(--td-warning-color);
}

.card-status--stopped .status-dot {
    background: var(--td-text-color-disabled);
}

/* 标签行 */
.card-tags {
    display: flex;
    align-items: center;
    flex-wrap: nowrap;
    gap: var(--td-size-3);
    margin-top: var(--td-size-2);
    overflow: hidden;
}

.card-tags__time {
    flex: 1 1 auto;
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--td-size-2);
    padding: var(--td-size-1) var(--td-size-4);
    background: var(--td-bg-color-container-hover);
    color: var(--td-text-color-secondary);
    border-radius: var(--td-radius-small);
    font-size: var(--td-font-size-body-small);
    line-height: 18px;
    overflow: hidden;
}

.card-tags__time-text {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.card-tags__count {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--td-size-1);
    padding: var(--td-size-1) var(--td-size-4);
    border-radius: var(--td-radius-small);
    font-size: var(--td-font-size-body-small);
    line-height: 18px;
}

.card-tags__count--success {
    color: var(--td-success-color);
    background: var(--td-success-color-light);
}

.card-tags__count--fail {
    color: var(--td-error-color);
    background: var(--td-error-color-light);
}

/* 底部操作 */
.card-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--td-size-3);
    margin-top: auto;
}
</style>
