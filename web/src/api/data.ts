// 行情读取接口 /api/data 的类型和调用，与 packages/duohelm/src/duohelm/api/data.py 一致。
// 放在单独的文件里；M1 改为从 OpenAPI 生成后删除。
import { getJson, query } from "./client";

export type AdjustMode = "none" | "hfq" | "qfq";

export interface Security {
  symbol: string;
  name: string | null;
  exchange: string | null;
  kind: string | null;
  /** YYYY-MM-DD，北京时间的交易日 */
  list_date: string | null;
  delist_date: string | null;
}

export interface SecuritiesOut {
  snapshot: string;
  total: number;
  items: Security[];
}

/** 价格、成交量、成交额都可能是 null（缺失），不能当成 0。 */
export interface Bar {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  amount: number | null;
}

export interface BarsOut {
  snapshot: string;
  symbol: string;
  adjust: AdjustMode;
  base_date: string | null;
  /** 这只证券在这个快照里的最后一根日线，没有任何日线时为 null */
  last_bar_date: string | null;
  /** 证券存在但所读区间内没有日线；证券不存在是 404，不是 empty */
  empty: boolean;
  bars: Bar[];
}

export interface Snapshot {
  id: string;
  parent: string | null;
  /** 带时区的 ISO 时间 */
  created_at: string;
  task_id: number | null;
  note: string | null;
  stats: Record<string, unknown>;
  is_current: boolean;
}

export interface SnapshotsOut {
  current: string | null;
  snapshots: Snapshot[];
}

/** 没有日线的证券：bar_count 为 0，其余统计为 null（未知，不是 0）。 */
export interface CoverageRow {
  symbol: string;
  name: string | null;
  kind: string | null;
  /** null：未知（证券列表里没有这个代码），不要按代码前缀猜 */
  exchange: string | null;
  /** null：没有退市日，或证券列表里没有这个代码（此时 exchange 也是 null） */
  delist_date: string | null;
  first_date: string | null;
  last_date: string | null;
  bar_count: number;
  expected_days: number | null;
  /** 按 CN 交易日历在首末日期之间应有而缺失的天数，停牌日也算缺失 */
  missing_days: number | null;
  off_calendar_bars: number | null;
}

export interface CoverageOut {
  snapshot: string;
  total: number;
  items: CoverageRow[];
}

/** 汇总的一个分组。exchange、kind、status 为 null 都表示“未知”，单独成组。 */
export interface CoverageGroup {
  exchange: string | null;
  kind: string | null;
  /** null：未知（证券列表里没有这个代码） */
  status: "listed" | "delisted" | null;
  securities: number;
  with_bars: number;
  no_bars: number;
  bar_count: number;
  with_missing: number;
  missing_days: number;
  off_calendar_bars: number;
}

export interface CoverageSummaryOut {
  snapshot: string;
  /** 判定在市/退市用的北京日期 */
  as_of: string;
  total: number;
  groups: CoverageGroup[];
}

export interface CoverageQuery {
  snapshot?: string;
  kind?: string;
  limit?: number;
  offset?: number;
}

export interface SecuritiesQuery {
  snapshot?: string;
  symbol?: string;
  name_prefix?: string;
  kind?: string;
  limit?: number;
  offset?: number;
}

export interface BarsQuery {
  snapshot?: string;
  start?: string;
  end?: string;
  adjust?: AdjustMode;
}

export const dataApi = {
  snapshots: (limit = 200) => getJson<SnapshotsOut>(`/api/data/snapshots${query({ limit })}`),
  coverage: (q: CoverageQuery = {}) => getJson<CoverageOut>(`/api/data/coverage${query({ ...q })}`),
  coverageSummary: (snapshot?: string) =>
    getJson<CoverageSummaryOut>(`/api/data/coverage/summary${query({ snapshot })}`),
  securities: (q: SecuritiesQuery = {}) =>
    getJson<SecuritiesOut>(`/api/data/securities${query({ ...q })}`),
  bars: (symbol: string, q: BarsQuery = {}) =>
    getJson<BarsOut>(`/api/data/securities/${encodeURIComponent(symbol)}/bars${query({ ...q })}`),
};
