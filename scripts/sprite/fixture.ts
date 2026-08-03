/**
 * 生成合成素材，用于在**不消耗任何 API 配额**的前提下验证后处理链路。
 *
 *   npm run sprite:fixture
 *   npm run sprite:process:fixture
 *
 * 合成图刻意模拟 AI 生图的真实毛病：同一个「角色」在每张图里
 * 位置漂移几个像素。对齐若正确，process 输出的所有图角色位置应完全重合。
 * 其中 cry 那张故意把身体加宽，用来验证一致性报告能不能抓出「模型改了身体」。
 */
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import sharp from 'sharp'
import { CharPaths, KINDS } from './paths.ts'

const W = 512
const H = 768

/**
 * 画一个「角色」：头 + 眼 + 身体 + 两条腿，整体带偏移。白底，供降级抠图处理。
 *
 * `scale` 模拟实测中方舟做指令编辑时那种「整体等比放大约 3%」的行为 ——
 * 这类漂移能被后处理的缩放归一修掉，不该被当成体型改动。
 * `bodyW` 变化则模拟真正的体型改动，归一后依然对不上，只能重跑。
 */
function character(opts: {
  dx: number
  dy: number
  bodyW: number
  eyeColor: string
  scale?: number
  /** 0 = 闭眼（画成一条横线），1 = 正常睁开。用于验证眼部区域自动检测。 */
  eyeOpen?: number
  /** 嘴张开的高度（px）。0 = 闭合。用于验证嘴部区域自动检测。 */
  mouthOpen?: number
  /**
   * 身体纹理种子。改变它会让衣服上的褶皱线换位置，
   * 模拟实测中「模型把布料褶皱、发丝整体重画一遍」的行为 ——
   * 角色设计没变，但身体区域逐像素差异高达 18~20%。
   * 用于验证脸部烘焙能否把这类差异清零。
   */
  bodySeed?: number
}): Promise<Buffer> {
  const { dx, dy, bodyW, eyeColor, scale = 1, eyeOpen = 1, mouthOpen = 0, bodySeed = 0 } = opts
  const cx = W / 2 + dx
  const headR = 70
  const headTop = 80 + dy
  const bodyTop = headTop + headR * 2
  const eyeY = headTop + headR - 10
  const mouthY = headTop + headR + 28

  const eye = (ex: number) =>
    eyeOpen > 0.5
      ? `<circle cx="${ex}" cy="${eyeY}" r="9" fill="${eyeColor}"/>`
      : `<rect x="${ex - 10}" y="${eyeY - 2}" width="20" height="4" rx="2" fill="${eyeColor}"/>`

  const mouth =
    mouthOpen > 0
      ? `<ellipse cx="${cx}" cy="${mouthY}" rx="16" ry="${mouthOpen}" fill="#8a3b3b"/>`
      : `<rect x="${cx - 16}" y="${mouthY - 2}" width="32" height="4" rx="2" fill="#8a3b3b"/>`

  // 衣褶：位置由种子决定。不同表情种子不同 → 身体像素大面积不一致
  let folds = ''
  let s = bodySeed * 9301 + 49297
  const rnd = () => ((s = (s * 9301 + 49297) % 233280) / 233280)
  for (let i = 0; i < 7; i++) {
    const fy = bodyTop + 24 + rnd() * 250
    const fx = cx - bodyW / 2 + 10 + rnd() * (bodyW - 40)
    folds += `<rect x="${fx.toFixed(1)}" y="${fy.toFixed(1)}" width="${(12 + rnd() * 26).toFixed(1)}" height="5" rx="2" fill="#48699f"/>`
  }

  const shapes = `
    <circle cx="${cx}" cy="${headTop + headR}" r="${headR}" fill="#f2d0b8"/>
    ${eye(cx - 24)}${eye(cx + 24)}${mouth}
    <rect x="${cx - bodyW / 2}" y="${bodyTop}" width="${bodyW}" height="300" rx="18" fill="#5b7cc4"/>${folds}
    <rect x="${cx - 40}" y="${bodyTop + 300}" width="30" height="180" rx="12" fill="#3b4a70"/>
    <rect x="${cx + 10}" y="${bodyTop + 300}" width="30" height="180" rx="12" fill="#3b4a70"/>`

  // 以画布中心为基准等比缩放，模拟模型「把角色整体放大一圈」
  const g =
    scale === 1
      ? shapes
      : `<g transform="translate(${(W / 2) * (1 - scale)} ${(H / 2) * (1 - scale)}) scale(${scale})">${shapes}</g>`

  const svg = `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg">
    <rect width="${W}" height="${H}" fill="#ffffff"/>${g}</svg>`

  return sharp(Buffer.from(svg)).png().toBuffer()
}

async function main() {
  const paths = new CharPaths('fixture')
  await paths.ensureRawDirs()

  await writeFile(paths.rawBase, await character({ dx: 0, dy: 0, bodyW: 150, eyeColor: '#222222', bodySeed: 1 }))

  // 身体尺寸一致、只有脸不同，但各自在画布里漂移几像素、且衣褶被重画
  // （bodySeed 各不相同）—— 对齐修位移，脸部烘焙修重绘
  const faces: Array<[string, number, number, string, number]> = [
    ['normal', 0, 0, '#222222', 2],
    ['happy', 5, -3, '#1a7f4b', 3],
    ['smile', -7, 4, '#2a5f8f', 4],
    ['shy', 3, 6, '#b4436c', 5],
    ['angry', -4, -5, '#c0392b', 6],
  ]
  for (const [id, dx, dy, eyeColor, bodySeed] of faces) {
    await writeFile(paths.rawFile('face', id), await character({ dx, dy, bodyW: 150, eyeColor, bodySeed }))
  }

  // 故意跑偏的一张：身体明显变宽 —— 归一后仍对不上，一致性报告应当点名它
  await writeFile(paths.rawFile('face', 'cry'), await character({ dx: 2, dy: 2, bodyW: 210, eyeColor: '#3a6ea5', bodySeed: 7 }))

  // 整体等比放大 4%（方舟实测约 3.4%）—— 应当被缩放归一修掉，且**不**被点名
  await writeFile(paths.rawFile('face', 'smug'), await character({ dx: 0, dy: 0, bodyW: 150, eyeColor: '#8a6d3b', scale: 1.04, bodySeed: 8 }))

  // 闭眼差分：只有眼睛变成横线，其余不动 —— 用于验证眼部区域自动检测
  await writeFile(paths.rawFile('eyes', 'normal'), await character({ dx: 1, dy: -2, bodyW: 150, eyeColor: '#222222', eyeOpen: 0 }))

  // 三档嘴型：只有嘴部张开程度不同 —— 用于验证嘴部区域自动检测
  const mouths: Array<[string, number]> = [
    ['closed', 0],
    ['half', 8],
    ['open', 18],
  ]
  for (const [id, open] of mouths) {
    await writeFile(paths.rawFile('mouth', id), await character({ dx: -2, dy: 3, bodyW: 150, eyeColor: '#222222', mouthOpen: open }))
  }

  console.log(`合成素材已写入 ${paths.raw}`)
  console.log(`共 ${1 + faces.length + 1 + 1 + 3} 张（${KINDS.join(' / ')} 三类齐全）`)
  console.log('\n下一步：npm run sprite:process:fixture')
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err))
  process.exitCode = 1
})
