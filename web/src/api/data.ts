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
  securities: (q: SecuritiesQuery = {}) =>
    getJson<SecuritiesOut>(`/api/data/securities${query({ ...q })}`),
  bars: (symbol: string, q: BarsQuery = {}) =>
    getJson<BarsOut>(`/api/data/securities/${encodeURIComponent(symbol)}/bars${query({ ...q })}`),
};
