// 覆盖情况页面的全部界面文字。将来做多语言时，从这里替换。

export const TEXT = {
  title: "数据覆盖情况",
  reload: "重新读取",
  loading: "正在读取覆盖汇总……",
  timeNote: "日期是北京时间的交易日；时间按 Asia/Shanghai 显示。",
  missingNote:
    "缺失天数 = 首末日期之间按交易日历应有、但库里没有日线的天数。同步时不保存停牌日，所以停牌日也算缺失，这里分不出“停牌”和“漏数据”；它是进一步检查的线索，不是数据错误的判定。没有日线的证券缺失天数未知。",

  unknown: {
    title: "未知：没有取到覆盖统计",
    description: (message: string) =>
      `${message}。这不表示没有数据，只是没读到；页面不显示任何汇总，避免被当成 0。`,
  },
  notFound: {
    title: "未知：没有可读的快照，或快照里缺少所需的数据集",
    description: (message: string) => `${message}。请先运行数据同步任务。`,
  },
  empty: {
    title: "快照读取成功，但里面没有任何证券",
  },
  tableError: {
    title: "未知：没有取到这一页的覆盖统计",
    description: (message: string) => `${message}。这不表示没有数据，只是没读到。`,
    retry: "重试",
  },

  snapshot: {
    id: "快照 id",
    createdAt: "创建时间",
    state: "状态",
    current: "当前快照",
    notCurrent: "不是当前快照（读取过程中又有了新快照，重新读取可看到最新的）",
    stateUnknown: "是否为当前快照未知",
    createdUnknown: (reason: string) => `未知（${reason}）`,
    notInList: "快照列表里没有这个快照",
  },

  summary: {
    title: "汇总",
    exchange: "按交易所",
    kind: "按证券种类",
    status: "按在市/退市",
    count: "证券数",
    noBars: "无日线",
    withMissing: "有缺失",
    unknownLabel: "未知",
    statusActive: "在市",
    statusDelisted: "已退市",
    statusNote: (asOf: string) =>
      `已退市 = 证券信息里有退市日且不晚于 ${asOf}（接口按北京日期判定）；没有退市日的算在市。证券信息里没有的代码，交易所和在市/退市都是“未知”。部分数据源的退市日是近似值。`,
    total: "合计",
  },

  filter: {
    kind: "种类",
    note: "表格按代码排序，按种类筛选（种类未知的证券不能筛选）；汇总始终是整个快照。",
  },

  col: {
    symbol: "代码",
    name: "名称",
    exchange: "交易所",
    kind: "种类",
    status: "状态",
    first: "首日",
    last: "末日",
    bars: "K 线数",
    expected: "应有天数",
    missing: "缺失天数",
    offCalendar: "非交易日日线",
  },

  cell: {
    noName: "（无名称）",
    noBars: "无日线",
    unknown: "未知",
    delistedOn: (d: string) => `已退市（${d}）`,
    active: "在市",
    missingTip: "含停牌日",
    offCalendarTip: "首末之间不在交易日历上的日线数，正常为 0",
  },
};
