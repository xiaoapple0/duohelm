// 从行情读取接口的响应推导图上要标注的内容。纯函数，不碰网络和时区。
// 为什么要推导、有哪些局限：docs/subsystems/data.md “K 线查看页面”。
import type { AdjustMode, Bar, BarsOut, Security } from "../../api/data";

/** 一个附加信息要么读到了，要么“未知”（没取到），两者不能混为一谈。 */
export type Known<T> = { known: true; value: T } | { known: false; reason: string };

/** 参考日历：用这只指数有日线的日子代替后端还没有提供的 CN 交易日历。 */
export const CALENDAR_SYMBOL = "000001.SH";

/** 除权判断的相对阈值：收盘价比（复权价 / 另一种复权价）相对前一根变化超过它。 */
const EX_EPSILON = 1e-6;

export interface Slot {
  date: string;
  /** null：这一天没有日线（停牌或缺数据） */
  bar: Bar | null;
  /** 参考日历有、这只证券没有日线：停牌或缺数据，分不出来 */
  suspended: boolean;
  exDate: boolean;
}

export function isComplete(bar: Bar): bar is Bar & {
  open: number;
  high: number;
  low: number;
  close: number;
} {
  return bar.open !== null && bar.high !== null && bar.low !== null && bar.close !== null;
}

export interface GapResult {
  /** 参考日历有、这只证券没有日线的日子：停牌或缺数据，入库时没有保存停牌日，两者分不出来 */
  dates: string[];
  /**
   * 没有退市日的证券，最后一根日线之后到区间结束的日子无法判断（可能正在停牌，也可能
   * 还没同步），不标；这个值是第一个这样的日子，没有这种日子时为 null。
   */
  unknownFrom: string | null;
}

/**
 * 缺日线的日子：证券上市（没有 list_date 时用区间内第一根日线）到退市日（没有 delist_date
 * 时用这只证券在快照里最后一根日线）之间。上市前、退市后不算。
 */
export function gapDates(
  result: BarsOut,
  security: Security,
  calendar: string[],
): GapResult {
  const have = new Set(result.bars.map((b) => b.date));
  const from = security.list_date ?? result.bars[0]?.date ?? null;
  const to = security.delist_date ?? result.last_bar_date;
  if (from === null || to === null) {
    return { dates: [], unknownFrom: null };
  }
  const unknownFrom =
    security.delist_date === null ? (calendar.find((d) => d > to) ?? null) : null;
  return { dates: calendar.filter((d) => !have.has(d) && d >= from && d <= to), unknownFrom };
}

/**
 * 除权日：main（所选复权方式）与 other（另一种复权方式）逐日收盘价之比，相对前一根日线
 * 变化的那根日线。两者来自同一快照、同一区间；任一收盘价缺失或为 0 的日子跳过。
 */
export function exDates(main: Bar[], other: Bar[]): string[] {
  const otherClose = new Map<string, number | null>(other.map((b) => [b.date, b.close]));
  const result: string[] = [];
  let prev: number | undefined;
  for (const bar of main) {
    const o = otherClose.get(bar.date);
    if (bar.close === null || o === null || o === undefined || o === 0 || bar.close === 0) {
      continue;
    }
    const ratio = bar.close / o;
    if (prev !== undefined && Math.abs(ratio / prev - 1) > EX_EPSILON) {
      result.push(bar.date);
    }
    prev = ratio;
  }
  return result;
}

/** 另一种复权方式：none 时读 hfq，否则读 none。 */
export function otherAdjust(adjust: AdjustMode): AdjustMode {
  return adjust === "none" ? "hfq" : "none";
}

/** 横轴的每一格：有日线的日子加上推导出的停牌日，按日期升序。 */
export function buildSlots(bars: Bar[], suspended: string[], ex: string[]): Slot[] {
  const byDate = new Map<string, Slot>();
  const exSet = new Set(ex);
  for (const bar of bars) {
    byDate.set(bar.date, { date: bar.date, bar, suspended: false, exDate: exSet.has(bar.date) });
  }
  for (const date of suspended) {
    if (!byDate.has(date)) {
      byDate.set(date, { date, bar: null, suspended: true, exDate: false });
    }
  }
  return [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
}

export interface Extent {
  min: number;
  max: number;
}

/** 只用非空值计算范围；没有任何非空值时返回 null（不画，也不补 0）。 */
export function extent(values: (number | null)[]): Extent | null {
  let min = Infinity;
  let max = -Infinity;
  for (const v of values) {
    if (v !== null && Number.isFinite(v)) {
      min = Math.min(min, v);
      max = Math.max(max, v);
    }
  }
  return min <= max ? { min, max } : null;
}

/** 取整齐的刻度。 */
export function ticks(min: number, max: number, count: number): number[] {
  if (min === max) {
    return [min];
  }
  const rough = (max - min) / count;
  const pow = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= rough) ?? rough;
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) {
    out.push(Number(v.toPrecision(12)));
  }
  return out;
}
