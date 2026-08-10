let workbenchMode = false;

export const setWorkbenchMode = (enabled: boolean) => {
    workbenchMode = enabled;
};

export const isWorkbenchMode = () => workbenchMode;

export const createWorkbenchClientRequestId = (): string => {
    if (!globalThis.crypto?.randomUUID) {
        throw new Error('A secure UUID generator is required in workbench mode');
    }
    return globalThis.crypto.randomUUID();
};

export type WorkbenchUploadResponse = {
    WorkbenchFileId: string;
    Name: string;
    Type: string;
    Size: number;
};

export const projectWorkbenchUploadResponse = (value: unknown): WorkbenchUploadResponse => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error('Workbench upload response is invalid');
    }
    const input = value as Record<string, unknown>;
    if (
        typeof input.WorkbenchFileId !== 'string'
        || !/^wf_[0-9a-f]{32}$/.test(input.WorkbenchFileId)
        || typeof input.Name !== 'string'
        || !input.Name
        || input.Name.length > 255
        || typeof input.Type !== 'string'
        || input.Type.length > 128
        || !Number.isSafeInteger(input.Size)
        || Number(input.Size) < 0
    ) {
        throw new Error('Workbench upload response is invalid');
    }
    return {
        WorkbenchFileId: input.WorkbenchFileId,
        Name: input.Name,
        Type: input.Type,
        Size: Number(input.Size),
    };
};
