// 后端接口的类型。M1 起改为从 OpenAPI 自动生成，届时删除这里的手写类型。

export interface Health {
  status: string;
  version: string;
  sdk_version: string;
}

export type PluginStatus = "ready" | "invalid" | "incompatible" | "blocked";
export type PluginKind = "datasource" | "operator" | "factor" | "broker";

export interface Plugin {
  id: string;
  status: PluginStatus;
  name: string | null;
  version: string | null;
  kind: PluginKind | null;
  path: string;
  problems: string[];
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${path} 返回 ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => getJson<Health>("/api/health"),
  plugins: () => getJson<Plugin[]>("/api/plugins"),
};
