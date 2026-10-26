/**
 * 模型任务槽的名单与中文名，模型与厂商页、用量统计页共用。
 *
 * 写成 `Record<任务名, …>` 而不是可选表：新增模型槽时漏写中文名会是编译错误。
 * 文字与 `src/core/config/settings_schema.json` 的 model_tasks 条目一致，后端的用量
 * 报告文件与控制台汇总从那份 schema 取同一套中文名。
 */

/** 全部模型任务槽，顺序即模型页功能分配的展示顺序。 */
export const TASK_NAMES = [
  'chat', 'planner', 'replyer', 'scene', 'proactive', 'summary', 'schedule', 'vision',
  'video', 'expression', 'memory', 'tts', 'embedding', 'multimodal_embedding', 'rerank',
] as const

/** 模型任务槽名。 */
export type TaskName = (typeof TASK_NAMES)[number]

/** 任务字段的中文名称；TOML 配置键名保持英文不变。 */
export const TASK_LABELS: Record<TaskName, string> = {
  chat: '日常对话',
  planner: '行动决策',
  replyer: '回复生成',
  scene: '情景分析',
  proactive: '主动搭话',
  summary: '对话摘要',
  schedule: '日程安排',
  vision: '屏幕视觉',
  video: '视频理解',
  expression: '表达选择',
  memory: '记忆抽取',
  tts: '语音合成',
  embedding: '向量嵌入',
  multimodal_embedding: '多模态向量',
  rerank: '重排',
}

/**
 * 把任务英文键名转为中文展示名。
 *
 * @param task 任务键名。
 * @returns 中文名；未知键名原样返回，避免掩盖配置异常。
 */
export function taskLabel(task: string): string {
  return TASK_LABELS[task as TaskName] ?? task
}
