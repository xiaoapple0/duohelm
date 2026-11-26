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
  effective: string;
  confirmed_at: string | null;
}

export interface EndpointConfirm {
  address: string;
  confirm: true;
  confirm_internet?: boolean;
}

export interface Plugin {
  id: string;
  status: PluginStatus;
  name: string | null;
  version: string | null;
  kind: PluginKind | null;
  path: string;
  problems: string[];
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

function getJson<T>(path: string): Promise<T> {
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

function deleteJson<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

function query(params: Record<string, string | number | undefined>): string {
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
  retryTask: (id: number) => postJson<Task>(`/api/tasks/${id}/retry`),
};

export const UNFINISHED: readonly TaskStatus[] = ["queued", "running"];

export function isUnfinished(status: TaskStatus): boolean {
  return UNFINISHED.includes(status);
}
