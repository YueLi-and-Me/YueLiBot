/**
 * 判断一句话是不是在问屏幕上的事。
 *
 * 为什么需要它：截屏（尤其 capture_mode = 'screen'）既花钱又把画面送去云端，
 * 而绝大多数闲聊根本用不上。所以默认只在他真的问起时才看一眼；想让她一直
 * 看着，走托盘那个「让她看着屏幕」开关，不靠猜。
 *
 * 刻意做得保守：宁可漏判也不要每句话都截。漏判的代价很小——换个说法再问
 * 一次，或者把托盘开关打开；而误判的代价是一张不该传出去的整屏截图。
 */

/** 直接点名屏幕上的东西。 */
const SCREEN_NOUNS = /屏幕|桌面|界面|画面|窗口|屏上|这一屏/

/** 「我在干嘛」这类问自己状态的话——答它必须看画面。 */
const DOING_QUESTIONS = /我在(干|做|忙)(嘛|什么|啥)|干(嘛|什么|啥)呢|在忙(些)?(什么|啥)/

/** 「看看我这边」——招呼她往这儿看。单个「看看」不算，太容易误判。 */
const LOOK_AT_ME = /(看|瞧|瞅|瞄)(看|一眼|一下|下)?\s*(我|这边|这儿|这里)/

export function mentionsScreen(text: string): boolean {
  const line = text.trim()
  if (!line) return false
  return SCREEN_NOUNS.test(line) || DOING_QUESTIONS.test(line) || LOOK_AT_ME.test(line)
}
