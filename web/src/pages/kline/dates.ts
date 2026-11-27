// 日期一律是北京时间交易日的 YYYY-MM-DD 字符串。日期运算在 UTC 日历上做，
// 结果与浏览器本机时区无关；“今天”用 Asia/Shanghai 换算。

const ISO = /^\d{4}-\d{2}-\d{2}$/;

export function isIsoDate(text: string): boolean {
  if (!ISO.test(text)) {
    return false;
  }
  const d = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === text;
}

/** 现在的北京日期（Asia/Shanghai），不依赖本机时区。 */
export function shanghaiToday(now: number = Date.now()): string {
  // en-CA 的日期格式就是 YYYY-MM-DD
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(now));
}

export function addDays(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
