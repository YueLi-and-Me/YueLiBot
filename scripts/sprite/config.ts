/**
 * 表情清单与指令模板。
 *
 * ★ 使用者不需要修改本文件，也不需要懂提示词工程。
 *   跑图只要提供：参考图 + 一句角色描述。
 *
 * 这张表同时被两套 CharacterView 实现消费：
 *   - AI 立绘差分：用 `desc` 生成对应表情图
 *   - Live2D 占位：用 `live2d` 切对应的 .exp3.json
 *   保证换渲染实现时，上层 setEmotion('happy') 的语义完全一致。
 */

export interface ExpressionDef {
  /** 程序内的稳定标识，也是产出文件名。 */
  id: string
  /** 中文名，仅用于日志和预览页展示。 */
  cn: string
  /** 喂给生图模型的自然中文描述 —— 只描述脸，不描述身体和服装。 */
  desc: string
  /** 对应的 Live2D 表情文件（不含 .exp3.json）。null 表示该表情走默认脸。 */
  live2d: string | null
  /** LLM 在 <say emotion="..."> 里可能吐出的写法，用于容错映射。 */
  aliases: string[]
}

/** 16 种表情，覆盖日常对话的绝大多数情绪。 */
export const EXPRESSIONS: ExpressionDef[] = [
  {
    id: 'normal',
    cn: '平静',
    desc: '自然平静的表情，眼睛正常睁开看向前方，嘴巴轻轻闭合，眉毛放松',
    live2d: null,
    aliases: ['平静', '普通', '中性', 'neutral', 'calm', 'default'],
  },
  {
    id: 'happy',
    cn: '开心',
    desc: '明显开心的表情，眼睛弯成月牙形笑眼，嘴巴张开露出灿烂笑容，眉毛微微上扬',
    live2d: null,
    aliases: ['开心', '高兴', '快乐', '兴奋', 'joy', 'excited', 'glad'],
  },
  {
    id: 'smile',
    cn: '微笑',
    desc: '温柔的微笑，眼睛正常睁开且眼神柔和，嘴角轻轻上扬但嘴巴闭合，神情安宁',
    live2d: null,
    aliases: ['微笑', '温柔', '柔和', 'gentle', 'soft', 'content'],
  },
  {
    id: 'shy',
    cn: '害羞',
    desc: '害羞的表情，脸颊泛起明显的红晕，眼睛微微看向侧下方不敢直视，嘴巴抿起，眉毛略微下垂',
    live2d: '脸红',
    aliases: ['害羞', '羞涩', '娇羞', '不好意思', '脸红', 'embarrassed', 'blush', 'bashful'],
  },
  {
    id: 'sad',
    cn: '难过',
    desc: '难过失落的表情，眉毛外端下垂成八字形，眼睛半睁且眼神黯淡，嘴角向下抿',
    live2d: null,
    aliases: ['难过', '伤心', '失落', '沮丧', '委屈', 'sorrow', 'upset', 'down'],
  },
  {
    id: 'cry',
    cn: '哭泣',
    desc: '哭泣的表情，眼睛里含着大颗泪水并有泪珠顺着脸颊滑落，眉毛八字下垂，嘴巴向下弯成哭泣的弧度',
    live2d: '哭哭',
    aliases: ['哭', '哭泣', '流泪', '大哭', 'crying', 'tears', 'sob'],
  },
  {
    id: 'angry',
    cn: '生气',
    desc: '生气的表情，眉毛向内向下压成愤怒的角度，眼睛瞪大，嘴巴张开像在斥责，脸颊微微鼓起',
    live2d: '生气',
    aliases: ['生气', '愤怒', '恼火', '火大', 'mad', 'furious', 'rage'],
  },
  {
    id: 'pout',
    cn: '嘟嘴',
    desc: '不满但可爱的表情，嘴巴嘟起噘着，眉毛微微下垂，眼睛看向侧面像在闹别扭，脸颊微鼓',
    live2d: null,
    aliases: ['嘟嘴', '不满', '闹别扭', '傲娇', '哼', 'sulk', 'annoyed', 'tsundere'],
  },
  {
    id: 'surprised',
    cn: '惊讶',
    desc: '吃惊的表情，眼睛睁得很大瞳孔缩小，眉毛高高扬起，嘴巴张成圆形',
    live2d: null,
    aliases: ['惊讶', '吃惊', '震惊', '意外', 'shock', 'surprise', 'astonished'],
  },
  {
    id: 'sleepy',
    cn: '困倦',
    desc: '困倦想睡的表情，眼睛半闭快要睁不开，眉毛放松下垂，嘴巴微张像在打哈欠，神情迷糊',
    live2d: 'ZZZ',
    aliases: ['困', '困倦', '想睡', '瞌睡', '睡着', 'tired', 'drowsy', 'sleep'],
  },
  {
    id: 'speechless',
    cn: '无语',
    desc: '无语的表情，眼睛眯成两条水平的细线，眉毛平直，嘴角抽搐般地歪向一边，一副无话可说的样子',
    live2d: '无语',
    aliases: ['无语', '无奈', '汗颜', '服了', 'speechless', 'awkward', 'sweatdrop'],
  },
  {
    id: 'dizzy',
    cn: '眩晕',
    desc: '眩晕懵掉的表情，眼睛变成打转的漩涡状，嘴巴张开成波浪线，一副被绕晕了的样子',
    live2d: '眩晕',
    aliases: ['眩晕', '懵', '晕', '发懵', 'confused', 'dazed', 'stunned'],
  },
  {
    id: 'starry_eyes',
    cn: '星星眼',
    desc: '极度兴奋憧憬的表情，眼睛变成闪亮的星星形状，嘴巴张开露出兴奋的笑容，脸颊微微泛红',
    live2d: '星星眼',
    aliases: ['星星眼', '崇拜', '憧憬', '闪闪发光', 'sparkle', 'starry', 'amazed'],
  },
  {
    id: 'heart_eyes',
    cn: '爱心眼',
    desc: '充满爱意的表情，眼睛变成粉红色的爱心形状，嘴巴弯成幸福的笑容，脸颊泛起浓浓的红晕',
    live2d: '爱心眼',
    aliases: ['爱心眼', '花痴', '喜欢', '心动', '爱慕', 'love', 'adore', 'infatuated'],
  },
  {
    id: 'smug',
    cn: '得意',
    desc: '得意自满的表情，眼睛微微眯起眼神上挑，嘴角单侧向上勾起露出坏笑，眉毛一高一低',
    live2d: null,
    aliases: ['得意', '自信', '骄傲', '坏笑', '嘚瑟', 'proud', 'smirk', 'confident'],
  },
  {
    id: 'thinking',
    cn: '思考',
    // 原描述「眼睛看向斜上方、嘴巴抿成一条线」被模型画成了半闭眼 + 脸红 +
    // 嘴角下撇，读出来是「不高兴」或「困」，完全不像在想事情。
    // 改成明确要求睁大眼睛、眉毛上扬、嘴角不下垂 —— 把误读的方向堵死
    desc: '正在想事情的表情，双眼明显睁开、眼珠朝斜上方看，一侧眉毛上扬，嘴巴轻轻抿起但嘴角不下垂，神情专注而不是不高兴，脸颊不要泛红',
    live2d: null,
    aliases: ['思考', '疑惑', '想', '琢磨', '沉思', 'think', 'ponder', 'wonder'],
  },
]

/**
 * 只给最常用的几个表情做闭眼差分。
 * 其余表情在对话中通常只持续 2–3 秒，期间不眨眼肉眼看不出来 ——
 * 全做的话图量和一致性风险都要翻倍，不划算。
 */
export const BLINK_TARGETS = ['normal', 'happy', 'smile', 'shy'] as const

/** 说话时的嘴型。TTS 的 RMS 音量按阈值映射到这三档。 */
export const MOUTH_SHAPES = [
  { id: 'closed', cn: '闭嘴', desc: '嘴巴完全闭合' },
  { id: 'half', cn: '半开', desc: '嘴巴微微张开，像在说话时的中间状态' },
  { id: 'open', cn: '张开', desc: '嘴巴明显张开，像在说话发出元音' },
] as const

// ---------------------------------------------------------------------------
// 指令模板
// ---------------------------------------------------------------------------

/**
 * 一致性是整条管线的命门。这段前缀把「不许改什么」写死，
 * 比在每条表情描述里重复叮嘱更可靠。
 */
const KEEP_IDENTICAL = [
  '这是一张角色立绘。请严格保持以下要素与原图完全一致，一个像素都不要改动：',
  '角色的五官轮廓、发型与每一缕头发、发饰、服装的款式与褶皱、配饰、身体姿势、手臂与手的位置、光照方向与阴影、色调。',
  // 实测：不显式禁止缩放的话，模型会把角色整体放大 3% 左右。
  // 位置可以靠后处理对齐救回来，缩放救不回来 —— 切表情时会出现「呼吸感」
  '尤其重要：不要改变角色的整体大小和缩放比例，不要改变角色在画布中所占的面积，',
  '角色的头顶、脚底、左右边缘必须停留在与原图完全相同的位置上。',
].join('')

/** 表情差分：只改脸。 */
export function editExpressionInstruction(expr: ExpressionDef): string {
  return [
    KEEP_IDENTICAL,
    `在此前提下，只修改角色的面部表情，改为：${expr.desc}。`,
    '背景保持纯白色。不要添加任何文字、水印、边框或特效。',
  ].join('')
}

/** 闭眼差分：在既有表情基础上只闭眼，用于眨眼动画。 */
export function editBlinkInstruction(expr: ExpressionDef): string {
  return [
    KEEP_IDENTICAL,
    '在此前提下，只把角色的双眼改为闭合状态（眼睑自然闭上，保留原有的睫毛和眉毛形状），',
    '其余面部表情、嘴型、脸颊红晕等全部保持不变。',
    '背景保持纯白色。不要添加任何文字、水印、边框或特效。',
  ].join('')
}

/** 嘴型差分：只改嘴。 */
export function editMouthInstruction(shape: (typeof MOUTH_SHAPES)[number]): string {
  return [
    KEEP_IDENTICAL,
    `在此前提下，只修改角色的嘴部，改为：${shape.desc}。眼睛、眉毛、脸颊和其余一切保持不变。`,
    '背景保持纯白色。不要添加任何文字、水印、边框或特效。',
  ].join('')
}

/**
 * 重试强化指令。预览页标记「这张漂了」之后追加，
 * 把上一次的具体问题回喂给模型，比单纯换 seed 重跑有效得多。
 */
export function reinforce(instruction: string, complaint?: string): string {
  const what = complaint?.trim() || '改动了不该改的部分（如服装、发型或姿势）'
  return [
    instruction,
    `\n\n【重要】上一次生成的结果${what}，这是错误的。`,
    '本次请极其严格地只改动指定部位，其他所有像素必须与原图保持一致。',
  ].join('')
}

/**
 * 底图生成。这是唯一需要用户输入的地方 ——
 * 一句角色描述（发色/瞳色/服装/气质），其余由模板补全。
 */
export function baseImagePrompt(characterDesc: string): string {
  return [
    `请以参考图的画风和角色设定为基础，生成一张全身立绘：${characterDesc}。`,
    '要求：角色正面站立面向观众，双臂自然下垂放在身体两侧，表情自然平静，双眼正常睁开，嘴巴轻轻闭合。',
    '全身完整入画，从头顶到脚底都不要被裁切，头顶和脚下留出少量空白。',
    '背景为纯白色，无任何背景元素。光照均匀柔和，无强烈投影。',
    '不要添加任何文字、水印、logo、边框或装饰性特效。',
  ].join('')
}

/** 兼容 LLM 吐出的各种情绪写法 —— 认不出的一律回落到 normal。 */
export function resolveEmotion(raw: string): ExpressionDef {
  const key = raw.trim().toLowerCase()
  const hit = EXPRESSIONS.find((e) => e.id === key || e.cn === raw.trim() || e.aliases.some((a) => a.toLowerCase() === key))
  return hit ?? EXPRESSIONS[0]!
}
