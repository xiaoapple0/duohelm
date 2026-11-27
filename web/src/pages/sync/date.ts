// 日期一律按 Asia/Shanghai（固定 UTC+8）换算，不用浏览器本机时区。
// 规则与后端 last_closed_day 一致（packages/duohelm/src/duohelm/tasks/sync.py）。

const TIMEZONE = "Asia/Shanghai";
const CLOSE_MINUTES = 15 * 60 + 30;

interface BeijingNow {
  date: string; // YYYY-MM-DD
  minutes: number; // 当天从 0 点起的分钟数
}

function beijingNow(now: Date): BeijingNow {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    minutes: Number(get("hour")) * 60 + Number(get("minute")),
  };
}

/** 北京时间的今天，YYYY-MM-DD。 */
export function beijingToday(now: Date = new Date()): string {
  return beijingNow(now).date;
}

function splitDate(date: string): [number, number, number] {
  const [y = 0, m = 0, d = 0] = date.split("-").map(Number);
  return [y, m, d];
}

function previousDay(date: string): string {
  const [y, m, d] = splitDate(date);
  return new Date(Date.UTC(y, m - 1, d - 1)).toISOString().slice(0, 10);
}

/** 最近一个已收盘的日子：北京时间 15:30 之后是今天，之前是昨天。不判断是不是交易日。 */
export function lastClosedDay(now: Date = new Date()): string {
  const current = beijingNow(now);
  return current.minutes >= CLOSE_MINUTES ? current.date : previousDay(current.date);
}

/** 是不是真实存在的 YYYY-MM-DD 日期（2025-02-30 不是）。 */
export function isValidDate(text: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return false;
  }
  const [y, m, d] = splitDate(text);
  const parsed = new Date(Date.UTC(y, m - 1, d));
  return (
    parsed.getUTCFullYear() === y && parsed.getUTCMonth() === m - 1 && parsed.getUTCDate() === d
  );
}

/** 把带时区的时间显示为北京时间。 */
export function formatBeijing(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString("zh-CN", { timeZone: TIMEZONE, hour12: false }) : "-";
}

/** HH:MM 是否晚于 15:30（后端同样要求，早于或等于 15:30 返回 422）。格式不对返回 undefined。 */
export function isAfterClose(time: string): boolean | undefined {
  const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(time);
  if (!match) {
    return undefined;
  }
  return Number(match[1]) * 60 + Number(match[2]) > CLOSE_MINUTES;
}
