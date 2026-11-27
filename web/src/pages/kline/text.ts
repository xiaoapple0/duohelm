// K 线查看页面的全部界面文字。将来做多语言时，从这里替换。

import type { AdjustMode } from "../../api/data";

export const TEXT = {
  title: "K 线查看",
  submit: "查看",
  idle: "选择证券后点“查看”。",
  timeNote: "日期是北京时间的交易日。前复权以所选区间的最后一根日线为基准。",

  adjust: {
    none: "不复权",
    hfq: "后复权",
    qfq: "前复权",
  } satisfies Record<AdjustMode, string>,

  form: {
    startLabel: "起始日期",
    endLabel: "结束日期",
    to: "至",
    needSymbol: "请先选择证券",
    badDate: "起止日期必须是有效的日期",
    startAfterEnd: "起始日期晚于结束日期",
  },

  select: {
    placeholder: "输入名称前缀或代码，如 600000.SH",
    listUnknown: (reason: string) => `证券列表未知：${reason}`,
    noMatch: "没有匹配的证券",
    noName: "（无名称）",
  },

  unknown: {
    title: "未知：没有取到数据",
    description: (message: string) => `${message}。这不表示这段时间没有行情，只是没读到。`,
  },
  invalid: { title: "请求参数不合法" },

  empty: {
    title: "没有数据：这只证券在所选区间内没有日线",
    reasons: "可能是区间在上市前、退市后，或停牌或缺数据（含还没同步）。",
    lastBar: (date: string) => `这只证券在快照里的最后一根日线是 ${date}。`,
    noBar: "这只证券在快照里没有任何日线。",
    jump: "跳到最后一根日线",
  },

  summary: {
    symbol: "证券",
    adjust: "复权",
    baseDate: (date: string) => `（基准日 ${date}）`,
    bars: "日线",
    barsUnit: (n: number) => `${n} 根`,
    snapshot: "快照",
  },

  tag: {
    suspension: "停牌或缺数据",
    suspensionUnit: "天",
    ex: "除权",
    exUnit: "次",
    known: (label: string, value: number, unit: string) => `${label}：${value} ${unit}`,
    unknown: (label: string) => `${label}：未知`,
  },

  warn: {
    suspensionTitle: "停牌或缺数据的日子未知，图上没有标注",
    tailTitle: (from: string) => `${from} 起是否停牌未知，图上没有标注`,
    tailDescription:
      "这只证券没有退市日，最后一根日线之后到区间结束的日子，可能在停牌，也可能数据还没同步，分不出来。",
    exTitle: "除权日未知，图上没有标注除权",
  },

  reason: {
    isCalendar: "所选证券就是参考指数，无法发现它自己缺的日子",
    noSecurity: "证券列表里没有这只证券的信息，不知道上市日和退市日",
    securityFailed: (reason: string) => `证券信息读取失败：${reason}`,
    calendarFailed: (symbol: string, reason: string) => `参考日历（${symbol}）读取失败：${reason}`,
    calendarEmpty: (symbol: string) => `参考日历（${symbol}）在区间内没有日线`,
  },

  chart: {
    hoverHint: "把鼠标移到图上查看每天的数据",
    ariaLabel: "K 线和成交量",
    missing: "缺失",
    volumeBillion: "亿股",
    volumeTenThousand: "万股",
    volumeShares: "股",
    axisBillion: "亿",
    axisTenThousand: "万",
    suspendedTip: "停牌或缺数据（参考日历有、这只证券没有日线）",
    exTip: "除权",
    open: "开",
    high: "高",
    low: "低",
    close: "收",
    volume: "量",
    exMark: "除",
    priceAxis: "价格",
    volumeAxis: "成交量（股，不复权）",
    legendSuspended: "停牌或缺数据",
    legendEx: "除权日",
    legendMissing: "价格缺失",
  },
};
