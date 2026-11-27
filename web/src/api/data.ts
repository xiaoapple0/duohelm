// 行情读取接口 /api/data 的类型和调用。类型从 OpenAPI 生成（schema.d.ts，`npm run gen:api`）。
import { getJson, query } from "./client";
import type { components, operations } from "./schema";

type Schemas = components["schemas"];

export type AdjustMode = Schemas["BarsOut"]["adjust"];

/** list_date、delist_date 是 YYYY-MM-DD，北京时间的交易日。 */
export type Security = Schemas["SecurityOut"];

export type SecuritiesOut = Schemas["SecuritiesOut"];

/** 价格、成交量、成交额都可能是 null（缺失），不能当成 0。 */
export type Bar = Schemas["BarOut"];

/**
 * last_bar_date：这只证券在这个快照里的最后一根日线，没有任何日线时为 null。
 * empty：证券存在但所读区间内没有日线；证券不存在是 404，不是 empty。
 */
export type BarsOut = Schemas["BarsOut"];

/** created_at 是带时区的 ISO 时间。 */
export type Snapshot = Schemas["SnapshotOut"];

export type SnapshotsOut = Schemas["SnapshotsOut"];

/**
 * 没有日线的证券：bar_count 为 0，其余统计为 null（未知，不是 0）。
 * exchange 为 null 表示未知（证券列表里没有这个代码），不要按代码前缀猜。
 * missing_days：按 CN 交易日历在首末日期之间应有而缺失的天数，停牌日也算缺失。
 */
export type CoverageRow = Schemas["CoverageRowOut"];

export type CoverageOut = Schemas["CoverageOut"];

/** 汇总的一个分组。exchange、kind、status 为 null 都表示“未知”，单独成组。 */
export type CoverageGroup = Schemas["CoverageGroupOut"];

/** as_of 是判定在市/退市用的北京日期。 */
export type CoverageSummaryOut = Schemas["CoverageSummaryOut"];

export type CoverageQuery = NonNullable<
  operations["coverage_api_data_coverage_get"]["parameters"]["query"]
>;

export type SecuritiesQuery = NonNullable<
  operations["securities_api_data_securities_get"]["parameters"]["query"]
>;

/** 含后端的 base_date（前复权基准日）；页面暂时不用它。 */
export type BarsQuery = NonNullable<
  operations["bars_api_data_securities__symbol__bars_get"]["parameters"]["query"]
>;

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
