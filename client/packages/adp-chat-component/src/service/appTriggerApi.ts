/**
 * 应用触发器（AppTrigger）API 服务
 *
 * 所有请求通过 /adp 代理转发：
 *   POST { ApplicationId, Payload }
 *
 * 接口对应 proto: trpc.adp.time_scheduler (AppTrigger 系列)
 * 与旧 cronTaskApi.ts 接口一一对应，采用新 AppTrigger 数据结构。
 */
import { httpService } from './httpService';
import { defaultApiDetailConfig } from './api';
import { AppTriggerScope } from '../model/appTrigger';
import type {
    AppTrigger,
    AppTriggerSummary,
    AppTriggerRunLog,
    AppTriggerInstance,
} from '../model/appTrigger';
import type { TimerScheduleConfig, TimerPushConfig } from '../model/cronTask';

// ============================================================
// API 路径定义
// ============================================================

/** AppTrigger 相关 API 路径 */
export interface AppTriggerApiPaths {
    createAppTriggerApi?: string;
    modifyAppTriggerApi?: string;
    describeAppTriggerApi?: string;
    describeAppTriggerSummaryListApi?: string;
    pauseAppTriggerApi?: string;
    resumeAppTriggerApi?: string;
    deleteAppTriggerApi?: string;
    runAppTriggerNowApi?: string;
    describeAppTriggerRunLogListApi?: string;
    markAppTriggerRunLogReadApi?: string;
    describeAppTriggerInstanceApi?: string;
}

export const defaultAppTriggerApiPaths: Required<AppTriggerApiPaths> = {
    createAppTriggerApi: '/adp/CreateAppTrigger',
    modifyAppTriggerApi: '/adp/ModifyAppTrigger',
    describeAppTriggerApi: '/adp/DescribeAppTrigger',
    describeAppTriggerSummaryListApi: '/adp/DescribeAppTriggerSummaryList',
    pauseAppTriggerApi: '/adp/PauseAppTrigger',
    resumeAppTriggerApi: '/adp/ResumeAppTrigger',
    deleteAppTriggerApi: '/adp/DeleteAppTrigger',
    runAppTriggerNowApi: '/adp/RunAppTriggerNow',
    describeAppTriggerRunLogListApi: '/adp/DescribeAppTriggerRunLogList',
    markAppTriggerRunLogReadApi: '/adp/MarkAppTriggerRunLogRead',
    describeAppTriggerInstanceApi: '/adp/DescribeAppTriggerInstance',
};

// 合并到全局默认配置，供 useApiConfig 消费
Object.assign(defaultApiDetailConfig, defaultAppTriggerApiPaths);

// 让 ApiDetailConfig 类型层面也感知这些字段
declare module './api' {
    interface ApiDetailConfig extends AppTriggerApiPaths {}
}

// ============================================================
// 内部工具：统一错误检查
// ============================================================

/**
 * 检查 /adp 代理返回的 Response 中是否有 Error
 * 有 Error 时抛出异常，否则返回 Response 对象
 */
function _checkResponse(data: any, action: string): any {
    const response = data?.Response ?? data ?? {};
    const err = response.Error;
    if (err && (err.Code || err.Message)) {
        throw new Error(`[${action}] ${err.Code || ''}: ${err.Message || ''}`);
    }
    return response;
}

// ============================================================
// 请求 & 响应类型
// ============================================================

/** CreateAppTrigger 请求 Payload */
export interface CreateAppTriggerPayload {
    TriggerName: string;
    TriggerType: number;
    ExecuteType: number;
    PushConfig?: TimerPushConfig;
    TriggerConfig: { ScheduledConfig: { Schedule: TimerScheduleConfig } };
    ExecuteConfig: { PromptConfig: { ExecutePrompt: string; ParamBindingsApi?: any } };
    Scope: number;
}

/** ModifyAppTrigger 请求 Payload */
export interface ModifyAppTriggerPayload {
    TriggerId: string;
    UpdateMask: { Paths: string[] };
    Trigger: Partial<AppTrigger>;
    Scope: number;
}

/** AppTriggerSummaryList 请求 Payload */
export interface DescribeAppTriggerSummaryListPayload {
    Query?: string;
    PageNumber: number;
    PageSize: number;
    Scope: number;
}

/** AppTriggerRunLogList 请求 Payload */
export interface DescribeAppTriggerRunLogListPayload {
    TriggerId: string;
    PageNumber: number;
    PageSize: number;
    Scope: number;
}

/** MarkAppTriggerRunLogRead 请求 Payload */
export interface MarkAppTriggerRunLogReadPayload {
    TriggerId: string;
    InstanceIdList: string[];
    Scope: number;
}

// ============================================================
// 1. CreateAppTrigger — 创建触发器
// ============================================================

/**
 * 创建定时触发器
 * @returns 新建触发器 ID
 */
export async function createAppTrigger(
    payload: CreateAppTriggerPayload,
    applicationId: string,
    apiPath?: string,
): Promise<string> {
    const path = apiPath || defaultApiDetailConfig.createAppTriggerApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: payload,
    });
    const response = _checkResponse(data, 'CreateAppTrigger');
    return response.TriggerId;
}

// ============================================================
// 2. ModifyAppTrigger — 修改触发器
// ============================================================

/**
 * 修改触发器（⚠️ 返回空对象，无 next_fire_time）
 */
export async function modifyAppTrigger(
    payload: ModifyAppTriggerPayload,
    applicationId: string,
    apiPath?: string,
): Promise<void> {
    const path = apiPath || defaultApiDetailConfig.modifyAppTriggerApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: payload,
    });
    _checkResponse(data, 'ModifyAppTrigger');
}

// ============================================================
// 3. DescribeAppTrigger — 触发器详情
// ============================================================

/**
 * 获取触发器详情
 */
export async function describeAppTrigger(
    triggerId: string,
    applicationId: string,
    scope: number = AppTriggerScope.APP,
    apiPath?: string,
): Promise<AppTrigger> {
    const path = apiPath || defaultApiDetailConfig.describeAppTriggerApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: { TriggerId: triggerId, Scope: scope },
    });
    const response = _checkResponse(data, 'DescribeAppTrigger');
    return response.Trigger;
}

// ============================================================
// 4. DescribeAppTriggerSummaryList — 触发器列表
// ============================================================

/**
 * 获取触发器摘要列表（分页）
 */
export async function describeAppTriggerSummaryList(
    payload: DescribeAppTriggerSummaryListPayload,
    applicationId: string,
    apiPath?: string,
): Promise<{ TotalCount: number; TriggerList: AppTriggerSummary[] }> {
    const path = apiPath || defaultApiDetailConfig.describeAppTriggerSummaryListApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: payload,
    });
    return _checkResponse(data, 'DescribeAppTriggerSummaryList');
}

// ============================================================
// 5. PauseAppTrigger — 暂停
// ============================================================

/**
 * 暂停触发器
 */
export async function pauseAppTrigger(
    triggerId: string,
    applicationId: string,
    scope: number = AppTriggerScope.APP,
    apiPath?: string,
): Promise<void> {
    const path = apiPath || defaultApiDetailConfig.pauseAppTriggerApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: { TriggerId: triggerId, Scope: scope },
    });
    _checkResponse(data, 'PauseAppTrigger');
}

// ============================================================
// 6. ResumeAppTrigger — 恢复
// ============================================================

/**
 * 恢复触发器（⚠️ 返回空对象，无 next_fire_time）
 */
export async function resumeAppTrigger(
    triggerId: string,
    applicationId: string,
    scope: number = AppTriggerScope.APP,
    apiPath?: string,
): Promise<void> {
    const path = apiPath || defaultApiDetailConfig.resumeAppTriggerApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: { TriggerId: triggerId, Scope: scope },
    });
    _checkResponse(data, 'ResumeAppTrigger');
}

// ============================================================
// 7. DeleteAppTrigger — 删除
// ============================================================

/**
 * 删除触发器
 */
export async function deleteAppTrigger(
    triggerId: string,
    applicationId: string,
    scope: number = AppTriggerScope.APP,
    apiPath?: string,
): Promise<void> {
    const path = apiPath || defaultApiDetailConfig.deleteAppTriggerApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: { TriggerId: triggerId, Scope: scope },
    });
    _checkResponse(data, 'DeleteAppTrigger');
}

// ============================================================
// 8. RunAppTriggerNow — 立即执行
// ============================================================

/**
 * 立即执行一次触发器
 * @returns 执行实例 ID
 */
export async function runAppTriggerNow(
    triggerId: string,
    applicationId: string,
    scope: number = AppTriggerScope.APP,
    apiPath?: string,
): Promise<string> {
    const path = apiPath || defaultApiDetailConfig.runAppTriggerNowApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: { TriggerId: triggerId, Scope: scope },
    });
    const response = _checkResponse(data, 'RunAppTriggerNow');
    return response.InstanceId;
}

// ============================================================
// 9. DescribeAppTriggerRunLogList — 运行日志列表
// ============================================================

/**
 * 获取触发器运行日志列表（分页）
 */
export async function describeAppTriggerRunLogList(
    payload: DescribeAppTriggerRunLogListPayload,
    applicationId: string,
    apiPath?: string,
): Promise<{ TotalCount: number; RunLogList: AppTriggerRunLog[] }> {
    const path = apiPath || defaultApiDetailConfig.describeAppTriggerRunLogListApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: payload,
    });
    return _checkResponse(data, 'DescribeAppTriggerRunLogList');
}

// ============================================================
// 10. MarkAppTriggerRunLogRead — 标记已读
// ============================================================

/**
 * 标记运行日志已读
 * @param instanceIdList 空数组表示全部标记已读
 * @returns 实际标记条数
 */
export async function markAppTriggerRunLogRead(
    payload: MarkAppTriggerRunLogReadPayload,
    applicationId: string,
    apiPath?: string,
): Promise<number> {
    const path = apiPath || defaultApiDetailConfig.markAppTriggerRunLogReadApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: payload,
    });
    const response = _checkResponse(data, 'MarkAppTriggerRunLogRead');
    return response.MarkedCount;
}

// ============================================================
// 11. DescribeAppTriggerInstance — 实例详情（新增接口）
// ============================================================

/**
 * 按 instance_id 查询执行实例详情
 */
export async function describeAppTriggerInstance(
    instanceId: string,
    applicationId: string,
    scope: number = AppTriggerScope.APP,
    apiPath?: string,
): Promise<AppTriggerInstance> {
    const path = apiPath || defaultApiDetailConfig.describeAppTriggerInstanceApi!;
    const data = await httpService.post(path, {
        ApplicationId: applicationId,
        Payload: { InstanceId: instanceId, Scope: scope },
    });
    const response = _checkResponse(data, 'DescribeAppTriggerInstance');
    return response.Instance;
}
