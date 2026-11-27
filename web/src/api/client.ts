// 后端接口的类型。M1 起改为从 OpenAPI 自动生成，届时删除这里的手写类型。

export interface Health {
  status: string;
  version: string;
  sdk_version: string;
}

export type PluginStatus = "ready" | "invalid" | "incompatible" | "blocked";
export type PluginKind = "datasource" | "operator" | "factor" | "broker";

export type EndpointScope = "loopback" | "lan" | "internet";
/** default：没有配置；confirmed：用已确认的地址；needs_confirmation：清单的 endpoints 变了，需要重新确认。 */
export type EndpointState = "default" | "confirmed" | "needs_confirmation";

export interface PluginEndpoint {
  id: string;
  description: string;
  scope: EndpointScope;
  default: string;
  state: EndpointState;
  configured: string | null;
  /** state 为 confirmed 时是插件拿到并放行的地址；其余状态下是清单的 default，只是显示用，插件拿不到也连不上 */
  effective: string;
  confirmed_at: string | null;
  /** 当前清单 endpoints 那一节的哈希，确认时原样传回 */
  digest: string;
}

export interface EndpointConfirm {
  address: string;
  confirm: true;
  confirm_internet?: boolean;
  digest: string;
}

/** none：没有声明；approved：已确认；needs_approval：没确认过或列表变了，确认前插件不能联网。 */
export type NetworkState = "none" | "approved" | "needs_approval";

export interface PluginNetwork {
  hosts: string[];
  state: NetworkState;
  approved_hosts: string[] | null;
  approved_at: string | null;
  /** 当前列表的哈希，确认时原样传回 */
  digest: string;
}

export interface Plugin {
  id: string;
  status: PluginStatus;
  name: string | null;
  version: string | null;
  kind: PluginKind | null;
  path: string;
  problems: string[];
  network: PluginNetwork | null;
  endpoints: PluginEndpoint[];
}

export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type LogLevel = "debug" | "info" | "warning" | "error";
export type JsonObject = Record<string, unknown>;

export interface Task {
  id: number;
  type: string;
  params: JsonObject;
  status: TaskStatus;
  progress: number | null;
  progress_message: string | null;
  attempt: number;
  max_attempts: number;
  run_after: string;
  cancel_requested: boolean;
  error: string | null;
  result: JsonObject | null;
  dedupe_key: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskLog {
  id: number;
  attempt: number;
  ts: string;
  level: LogLevel;
  message: string;
}

export interface TaskTypeInfo {
  name: string;
  description: string;
  max_attempts: number;
  params_schema: JsonObject;
}

export interface TaskQuery {
  status?: TaskStatus;
  type?: string;
  limit?: number;
}

export interface TaskCreate {
  type: string;
  params: JsonObject;
  dedupe_key?: string;
}

/** GET/PATCH /api/schedules/data-sync 的响应，与 packages/duohelm/src/duohelm/api/schedules.py 的 ScheduleOut 一致。 */
export interface DataSyncSchedule {
  enabled: boolean;
  plugin_id: string | null;
  /** 每个交易日触发的时刻，HH:MM，北京时间 */
  time: string;
  timezone: string;
  /** 上次处理的北京日期（触发、被去重跳过、判定为非交易日都会记下） */
  last_trigger_date: string | null;
  plugin_warning: string | null;
  /** 已启用但当前快照没有日线或日历：不会自动触发，要先手动全量同步。null 表示未知，原因见 state_error */
  needs_full_sync: boolean | null;
  state_error: string | null;
  /** 带 +08:00 偏移；未启用或未知时为 null。是预估 */
  next_run_at: string | null;
}

export interface DataSyncSchedulePatch {
  enabled?: boolean;
  plugin_id?: string;
  time?: string;
}

/** data.sync 的结果（SyncResult）。字段以后端为准，页面对没有认识的字段原样显示。 */
export interface DataSyncResult {
  plugin_id: string;
  start: string;
  end: string;
  snapshot_id: string | null;
  connection: string | null;
  securities: number;
  symbols: number;
  written: Record<string, number>;
  dropped_zero_volume: number;
  scanned?: number;
  rescanned?: number;
  rechecked?: number;
  snapshot_date?: string | null;
  snapshot_skipped?: string | null;
  st_snapshot_failed?: string | null;
  st_unknown_names?: number;
  index_snapshot_failed?: Record<string, string>;
}

/** 请求失败。message 优先使用后端返回的 detail。 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function describeDetail(detail: unknown): string | undefined {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    // pydantic 校验错误：[{loc: [...], msg: "..."}]
    return detail
      .map((item: unknown) => {
        if (typeof item !== "object" || item === null) {
          return String(item);
        }
        const { loc, msg } = item as { loc?: unknown; msg?: unknown };
        const where = Array.isArray(loc) ? loc.join(".") : "";
        return where ? `${where}: ${String(msg)}` : String(msg);
      })
      .join("；");
  }
  return undefined;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let message = `${path} 返回 ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      message = describeDetail(body.detail) ?? message;
    } catch {
      // 响应不是 JSON，用默认信息
    }
    throw new ApiError(message, response.status);
  }
  return (await response.json()) as T;
}

export function getJson<T>(path: string): Promise<T> {
  return request<T>(path);
}

function postJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function putJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function patchJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function deleteJson<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

export function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: () => getJson<Health>("/api/health"),
  plugins: () => getJson<Plugin[]>("/api/plugins"),
  approveNetwork: (pluginId: string, digest: string) =>
    putJson<PluginNetwork>(`/api/plugins/${encodeURIComponent(pluginId)}/network`, {
      confirm: true,
      digest,
    }),
  revokeNetwork: (pluginId: string) =>
    deleteJson<PluginNetwork>(`/api/plugins/${encodeURIComponent(pluginId)}/network`),
  confirmEndpoint: (pluginId: string, endpointId: string, body: EndpointConfirm) =>
    putJson<PluginEndpoint>(
      `/api/plugins/${encodeURIComponent(pluginId)}/endpoints/${encodeURIComponent(endpointId)}`,
      body,
    ),
  clearEndpoint: (pluginId: string, endpointId: string) =>
    deleteJson<PluginEndpoint>(
      `/api/plugins/${encodeURIComponent(pluginId)}/endpoints/${encodeURIComponent(endpointId)}`,
    ),
  taskTypes: () => getJson<TaskTypeInfo[]>("/api/task-types"),
  tasks: (q: TaskQuery = {}) => getJson<Task[]>(`/api/tasks${query({ ...q })}`),
  task: (id: number) => getJson<Task>(`/api/tasks/${id}`),
  taskLogs: (id: number, afterId = 0) =>
    getJson<TaskLog[]>(`/api/tasks/${id}/logs${query({ after_id: afterId })}`),
  createTask: (body: TaskCreate) => postJson<Task>("/api/tasks", body),
  cancelTask: (id: number) => postJson<Task>(`/api/tasks/${id}/cancel`),
  dataSyncSchedule: () => getJson<DataSyncSchedule>("/api/schedules/data-sync"),
  updateDataSyncSchedule: (body: DataSyncSchedulePatch) =>
    patchJson<DataSyncSchedule>("/api/schedules/data-sync", body),
  retryTask: (id: number) => postJson<Task>(`/api/tasks/${id}/retry`),
};

export const UNFINISHED: readonly TaskStatus[] = ["queued", "running"];

export function isUnfinished(status: TaskStatus): boolean {
  return UNFINISHED.includes(status);
}
