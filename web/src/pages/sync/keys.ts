/** 手动同步提交时用的去重键：同一时刻只允许一个手动同步（后端对未结束的任务强制唯一）。 */
export const MANUAL_DEDUPE_KEY = "manual:data.sync";

/** 定时触发提交任务用的去重键，见 docs/subsystems/tasks.md 的“定时触发”。 */
export const SCHEDULE_DEDUPE_KEY = "schedule:data.sync";

export const SYNC_TASK_TYPE = "data.sync";

/** 数据最早的日期（上证指数第一个交易日），与后端 SyncParams.start 的默认值一致。 */
export const EARLIEST_START = "1990-12-19";
