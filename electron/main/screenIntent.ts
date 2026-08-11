/**
 * 使用本地规则识别需要屏幕上下文的用户输入。
 *
 * 本模块只负责保守的关键词和句式匹配，供主进程决定是否触发视觉截图；持续截图由
 * 托盘开关单独控制。由于整屏截图会增加上传成本和隐私暴露面，规则优先降低误判率，
 * 即使漏判也允许用户改写问题后再次请求。
 */

/** 匹配直接指向屏幕、桌面、界面或窗口的名词。 */
const SCREEN_NOUNS = /屏幕|桌面|界面|画面|窗口|屏上|这一屏/

/** 匹配需要结合当前画面才能回答的自身状态问题。 */
const DOING_QUESTIONS = /我在(干|做|忙)(嘛|什么|啥)|干(嘛|什么|啥)呢|在忙(些)?(什么|啥)/

/** 匹配要求查看用户所在区域的表达；不匹配孤立的“看看”，以降低误触发。 */
const LOOK_AT_ME = /(看|瞧|瞅|瞄)(看|一眼|一下|下)?\s*(我|这边|这儿|这里)/

/**
 * 判断输入文本是否包含需要屏幕上下文的意图。
 *
 * @param text 用户输入文本；允许为空字符串或包含首尾空白。
 * @returns 去除首尾空白后命中任一屏幕意图规则时返回 `true`，否则返回 `false`。
 */
export function mentionsScreen(text: string): boolean {
  const line = text.trim()
  if (!line) return false
  return SCREEN_NOUNS.test(line) || DOING_QUESTIONS.test(line) || LOOK_AT_ME.test(line)
}
