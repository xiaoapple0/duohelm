// 读取覆盖统计的全部分页，并和证券信息、快照信息合并。纯数据逻辑，不含界面。
import { dataApi } from "../../api/data";
import type { CoverageRow, Security, Snapshot } from "../../api/data";
import { shanghaiToday } from "../kline/dates";
import { TEXT } from "./text";

const PAGE = 500; // 接口允许的最大页大小

export type Status = "active" | "delisted" | "unknown";

export interface Row extends CoverageRow {
  /** null：未知（证券信息没读到、没有这只证券、或证券信息里没填） */
  exchange: string | null;
  status: Status;
  delist_date: string | null;
}

export type Known<T> = { known: true; value: T } | { known: false; reason: string };

export type SnapshotInfo =
  | { kind: "ok"; value: Snapshot }
  /** 快照列表读到了，但里面没有这个快照（可能已被清理） */
  | { kind: "notInList" }
  | { kind: "error"; message: string };

export interface Loaded {
  snapshot: string;
  /** 接口报告的总数 */
  total: number;
  rows: Row[];
  /** 创建时间、是否当前快照：快照列表读不到、或不在列表里时未知 */
  info: SnapshotInfo;
  /** 证券信息读不到时，交易所和在市/退市未知 */
  securitiesProblem: string | undefined;
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function known<T>(promise: Promise<T>): Promise<Known<T>> {
  try {
    return { known: true, value: await promise };
  } catch (err) {
    return { known: false, reason: message(err) };
  }
}

function offsets(total: number): number[] {
  const out: number[] = [];
  for (let o = PAGE; o < total; o += PAGE) {
    out.push(o);
  }
  return out;
}

async function allSecurities(snapshot: string): Promise<Security[]> {
  const first = await dataApi.securities({ snapshot, limit: PAGE, offset: 0 });
  const items = [...first.items];
  // 逐页串行读取，不并发
  for (const offset of offsets(first.total)) {
    items.push(...(await dataApi.securities({ snapshot, limit: PAGE, offset })).items);
  }
  if (items.length !== first.total) {
    throw new Error(TEXT.errors.securitiesIncomplete(items.length, first.total));
  }
  return items;
}

/** 在市/退市：有退市日且不晚于今天（北京日期）为已退市；没有退市日算在市。 */
export function statusOf(delistDate: string | null, today: string): Status {
  return delistDate !== null && delistDate <= today ? "delisted" : "active";
}

/**
 * 取全部覆盖统计。第一页不指定快照，拿到它报告的快照 id 之后，
 * 其余分页、证券信息都固定读这个快照，不会混合两个快照。
 * 覆盖统计任何一页失败都抛错（整体未知）；证券信息、快照列表失败只让对应字段未知。
 */
export async function loadCoverage(): Promise<Loaded> {
  const first = await dataApi.coverage({ limit: PAGE, offset: 0 });
  const snapshot = first.snapshot;
  // 后端每页都要全量扫描日线，所以逐页串行读取，不并发
  const items = [...first.items];
  for (const offset of offsets(first.total)) {
    const page = await dataApi.coverage({ snapshot, limit: PAGE, offset });
    // 每页都必须来自同一个快照
    if (page.snapshot !== snapshot) {
      throw new Error(TEXT.errors.snapshotChanged);
    }
    items.push(...page.items);
  }
  const [securities, snapshots] = await Promise.all([
    known(allSecurities(snapshot)),
    known(dataApi.snapshots()),
  ]);

  const today = shanghaiToday();
  const bySymbol = new Map<string, Security>();
  if (securities.known) {
    for (const s of securities.value) {
      bySymbol.set(s.symbol, s);
    }
  }
  const rows: Row[] = items.map((r) => {
    const sec = bySymbol.get(r.symbol);
    if (!securities.known || sec === undefined) {
      return { ...r, exchange: null, status: "unknown", delist_date: null };
    }
    if (sec.exchange === null) {
      // 证券信息不完整：保守起见，交易所和状态都按未知
      return { ...r, exchange: null, status: "unknown", delist_date: null };
    }
    return {
      ...r,
      exchange: sec.exchange,
      status: statusOf(sec.delist_date, today),
      delist_date: sec.delist_date,
    };
  });

  let info: SnapshotInfo;
  if (!snapshots.known) {
    info = { kind: "error", message: snapshots.reason };
  } else {
    const hit = snapshots.value.snapshots.find((s) => s.id === snapshot);
    info = hit ? { kind: "ok", value: hit } : { kind: "notInList" };
  }

  return {
    snapshot,
    total: first.total,
    rows,
    info,
    securitiesProblem: securities.known ? undefined : securities.reason,
  };
}

export interface Group {
  label: string | null; // null：未知
  count: number;
  noBars: number;
  withMissing: number;
}

/** 按某个维度汇总全部行（不是当前页）。label 为 null 的归到“未知”。 */
export function summarize(rows: Row[], key: (r: Row) => string | null): Group[] {
  const map = new Map<string | null, Group>();
  for (const r of rows) {
    const label = key(r);
    let g = map.get(label);
    if (!g) {
      g = { label, count: 0, noBars: 0, withMissing: 0 };
      map.set(label, g);
    }
    g.count += 1;
    if (r.bar_count === 0) {
      g.noBars += 1;
    }
    if (r.missing_days !== null && r.missing_days > 0) {
      g.withMissing += 1;
    }
  }
  return [...map.values()].sort((a, b) => {
    if (a.label === null) return 1;
    if (b.label === null) return -1;
    return a.label.localeCompare(b.label);
  });
}

/** 北京时间的时间显示（不依赖本机时区）。 */
export function formatShanghai(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const pick = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${pick("year")}-${pick("month")}-${pick("day")} ${pick("hour")}:${pick("minute")}:${pick("second")}`;
}
