/**
 * 角色表现词表：表情、动作、装扮。
 *
 * 放在 shared 下供提示词构造、模型输出解析和渲染层共同使用；各层共享同一份
 * 类型和别名表，避免模型输出的标识与渲染层资源清单不一致。
 *
 * id 与 scripts/sprite/config.ts 的表情清单严格一致。
 */

// ---------------------------------------------------------------------------
// 四通道表现模型
//
// 之所以分通道而不是一个大枚举：这些状态在语义上是正交的 ——
// 「害羞地比心」是脸红(overlay) + 比心(gesture) 同时成立，
// 「戴着兽耳生气」是 生气(face) + 兽耳(outfit)。
// 塞进一个枚举会组合爆炸，分通道则各管各的。
// ---------------------------------------------------------------------------

/** face 通道：主情绪，互斥，同时只有一个。id 与 scripts/sprite/config.ts 严格一致。 */
export type Emotion =
  | 'normal'
  | 'happy'
  | 'smile'
  | 'shy'
  | 'sad'
  | 'cry'
  | 'angry'
  | 'pout'
  | 'surprised'
  | 'sleepy'
  | 'speechless'
  | 'dizzy'
  | 'starry_eyes'
  | 'heart_eyes'
  | 'smug'
  | 'thinking'

export const EMOTIONS: readonly Emotion[] = [
  'normal',
  'happy',
  'smile',
  'shy',
  'sad',
  'cry',
  'angry',
  'pout',
  'surprised',
  'sleepy',
  'speechless',
  'dizzy',
  'starry_eyes',
  'heart_eyes',
  'smug',
  // thinking 保留在类型和别名中，但不进入可渲染素材列表；当前资源缺少可靠的
  // thinking 图层，因此解析时回退到 normal，避免显示语义不一致的素材。
]

/** gesture 通道：瞬时动作，播完自动回落。 */
export type Gesture = 'heart' | 'hold_star' | 'clutch_chest' | 'pray' | 'tongue_out' | 'head_fly'

export const GESTURES: readonly Gesture[] = ['heart', 'hold_star', 'clutch_chest', 'pray', 'tongue_out', 'head_fly']

/** outfit 通道：长期装扮，由日程和场景决定，可叠加。 */
export type OutfitItem =
  | 'beast_ears'
  | 'moon_clip'
  | 'shark_clip'
  | 'pearl_clip'
  | 'halo'
  | 'shark_tail'
  | 'microphone'
  | 'game_console'
  | 'chibi'

export const OUTFIT_ITEMS: readonly OutfitItem[] = [
  'beast_ears',
  'moon_clip',
  'shark_clip',
  'pearl_clip',
  'halo',
  'shark_tail',
  'microphone',
  'game_console',
  'chibi',
]

/**
 * LLM 在 <say emotion="..."> 里会吐各种写法，中英混杂、同义词乱飞。
 * 认不出的一律回落到 normal —— 宁可表情平淡，不能整条链路报错。
 */
const EMOTION_ALIASES: Record<string, Emotion> = {
  平静: 'normal',
  普通: 'normal',
  中性: 'normal',
  neutral: 'normal',
  calm: 'normal',
  开心: 'happy',
  高兴: 'happy',
  快乐: 'happy',
  兴奋: 'happy',
  joy: 'happy',
  excited: 'happy',
  微笑: 'smile',
  温柔: 'smile',
  gentle: 'smile',
  害羞: 'shy',
  羞涩: 'shy',
  娇羞: 'shy',
  不好意思: 'shy',
  脸红: 'shy',
  embarrassed: 'shy',
  blush: 'shy',
  难过: 'sad',
  伤心: 'sad',
  失落: 'sad',
  沮丧: 'sad',
  委屈: 'sad',
  upset: 'sad',
  哭: 'cry',
  哭泣: 'cry',
  流泪: 'cry',
  crying: 'cry',
  tears: 'cry',
  生气: 'angry',
  愤怒: 'angry',
  恼火: 'angry',
  mad: 'angry',
  嘟嘴: 'pout',
  不满: 'pout',
  闹别扭: 'pout',
  傲娇: 'pout',
  sulk: 'pout',
  tsundere: 'pout',
  惊讶: 'surprised',
  吃惊: 'surprised',
  震惊: 'surprised',
  shock: 'surprised',
  困: 'sleepy',
  困倦: 'sleepy',
  想睡: 'sleepy',
  睡着: 'sleepy',
  tired: 'sleepy',
  无语: 'speechless',
  无奈: 'speechless',
  awkward: 'speechless',
  眩晕: 'dizzy',
  懵: 'dizzy',
  晕: 'dizzy',
  confused: 'dizzy',
  星星眼: 'starry_eyes',
  崇拜: 'starry_eyes',
  憧憬: 'starry_eyes',
  sparkle: 'starry_eyes',
  爱心眼: 'heart_eyes',
  花痴: 'heart_eyes',
  喜欢: 'heart_eyes',
  心动: 'heart_eyes',
  love: 'heart_eyes',
  得意: 'smug',
  自信: 'smug',
  骄傲: 'smug',
  坏笑: 'smug',
  smirk: 'smug',
  // thinking 相关别名统一回退到 normal，因为当前素材没有可用的思考表情。
  思考: 'normal',
  疑惑: 'normal',
  沉思: 'normal',
  think: 'normal',
  thinking: 'normal',
}

/**
 * 将模型返回的原始表情标识规范化为可渲染表情。
 *
 * @param raw 模型输出的中文别名或英文表情标识，首尾空白会被移除。
 * @returns ``EMOTIONS`` 中的合法标识；未知值统一回退到 ``normal``。
 * @sideEffects 不修改别名表和输入字符串。
 */
export function resolveEmotion(raw: string): Emotion {
  const trimmed = raw.trim()
  const lower = trimmed.toLowerCase()
  if ((EMOTIONS as readonly string[]).includes(lower)) return lower as Emotion
  return EMOTION_ALIASES[trimmed] ?? EMOTION_ALIASES[lower] ?? 'normal'
}

const GESTURE_ALIASES: Record<string, Gesture> = {
  比心: 'heart',
  笔芯: 'heart',
  手捧星: 'hold_star',
  捧星: 'hold_star',
  捂胸口: 'clutch_chest',
  祈祷: 'pray',
  吐舌: 'tongue_out',
  飞头: 'head_fly',
}

/**
 * 将模型返回的原始动作标识规范化为可渲染动作。
 *
 * @param raw 模型输出的中文别名或英文动作标识，首尾空白会被移除。
 * @returns 合法 Gesture；未知值返回 ``null``，调用方应跳过该动作而非抛错。
 */
export function resolveGesture(raw: string): Gesture | null {
  const trimmed = raw.trim()
  const lower = trimmed.toLowerCase()
  if ((GESTURES as readonly string[]).includes(lower)) return lower as Gesture
  return GESTURE_ALIASES[trimmed] ?? GESTURE_ALIASES[lower] ?? null
}
