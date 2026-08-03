/**
 * 后处理：抠透明背景 → 对齐 → 出 manifest。
 *
 *   npm run sprite:process
 *   npx tsx scripts/sprite/process.ts --naive     # 没装 rembg 时的降级方案
 *   npx tsx scripts/sprite/process.ts --face 260,190,250,250   # 手填脸部区域
 *
 * 五官区域默认自动检测，但它依赖「五官差异密度显著高于身体」这个前提。
 * 模型做表情差分时常把整个角色重画一遍，噪声压过信号时检测必然失准 ——
 * 这时用 --face（必要时再加 --eyes / --mouth）手填画布坐标。
 * 坐标可以从预览页读，或直接裁一块出来对着看。
 *
 * ⚠ Windows 下 `npm run xxx -- --flag` 会被 npm 吞掉参数，带参数一律用 npx tsx 直调。
 *
 * 对齐是这一步的关键。AI 每次生成，角色在画布里都会漂几个像素，
 * 不校正的话运行时切表情角色会「跳」，一眼假。
 *
 * 顺带产出一份一致性报告：把每张图的包围盒跟底图比，
 * 差得多说明模型偷偷改了身体，比肉眼扫图靠谱得多。
 */
import { spawn } from 'node:child_process'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { basename, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import sharp from 'sharp'
import { BLINK_TARGETS, EXPRESSIONS, MOUTH_SHAPES } from './config.ts'
import { writeManifest, type Rect, type SpriteManifest } from './manifest.ts'
import { CharPaths, KINDS, type Kind } from './paths.ts'

const { values } = parseArgs({
  options: {
    name: { type: 'string', default: 'yueli' },
    naive: { type: 'boolean', default: false },
    padding: { type: 'string', default: '24' },
    'no-rescale': { type: 'boolean', default: false },
    'no-bake': { type: 'boolean', default: false },
    // 五官区域手动指定，形如 --face 260,190,250,250（画布坐标，对齐后的）。
    // 自动检测依赖「五官差异密度显著高于身体」，而模型做差分时会把整个角色
    // 重画一遍 —— 身体噪声压过信号时检测必然失准，这时只能手填。
    face: { type: 'string' },
    eyes: { type: 'string' },
    mouth: { type: 'string' },
  },
})

/** 解析 "x,y,w,h"。写错了就直接报错退出，别让脏坐标一路传到烘焙里。 */
function parseRect(spec: string | undefined, label: string): Rect | null {
  if (!spec) return null
  // PowerShell 会把裸的 a,b,c 当数组字面量，传给 npx 时逗号变空格，
  // 所以逗号和空格都当分隔符收 —— 否则 Windows 下不加引号必然解析失败
  const n = spec.split(/[,\s]+/).filter(Boolean).map(Number)
  if (n.length !== 4 || n.some((v) => !Number.isFinite(v) || v < 0) || n[2]! < 1 || n[3]! < 1) {
    throw new Error(`--${label} 格式错误：${spec}　应为 x,y,w,h（四个非负整数，宽高 ≥1）`)
  }
  return { x: Math.round(n[0]!), y: Math.round(n[1]!), width: Math.round(n[2]!), height: Math.round(n[3]!) }
}

interface BBox {
  left: number
  top: number
  width: number
  height: number
}

/** 扫 alpha 通道求非透明区域的包围盒。空图返回 null。 */
function alphaBBox(data: Buffer, width: number, height: number, channels: number): BBox | null {
  let minX = width
  let minY = height
  let maxX = -1
  let maxY = -1
  const alphaOffset = channels - 1

  for (let y = 0; y < height; y++) {
    const row = y * width * channels
    for (let x = 0; x < width; x++) {
      // 阈值取 8 而非 0：抠图边缘常留一圈几乎全透明的杂点，
      // 按 >0 算包围盒会把它们算进去，导致每张图的框大小不一
      if (data[row + x * channels + alphaOffset]! > 8) {
        if (x < minX) minX = x
        if (x > maxX) maxX = x
        if (y < minY) minY = y
        if (y > maxY) maxY = y
      }
    }
  }

  if (maxX < 0) return null
  return { left: minX, top: minY, width: maxX - minX + 1, height: maxY - minY + 1 }
}

/**
 * 求两张已对齐图之间的差异区域。
 *
 * 用来自动定位五官：`mouth/closed` 与 `mouth/open` 之间变了的地方就是嘴，
 * `face/x` 与 `eyes/x` 之间变了的地方就是眼睛。
 * 比让用户手工框选靠谱，也比按比例硬猜位置准。
 */
/**
 * 沿一个轴的投影直方图里，找出以峰值为中心的密集区间。
 *
 * 取「所有差异的包围盒」是错的：差分图的身体部分同样会被模型重画，
 * 散落的噪点会把框一路撑到全身。真正要的是差异最集中的那一块 ——
 * 从峰值出发向两侧扩张，密度掉到峰值的一定比例就停。
 */
function denseSpan(hist: number[], ratio = 0.2): { start: number; end: number } | null {
  let peak = 0
  let peakAt = -1
  for (let i = 0; i < hist.length; i++) {
    if (hist[i]! > peak) {
      peak = hist[i]!
      peakAt = i
    }
  }
  if (peakAt < 0 || peak === 0) return null

  const floor = peak * ratio
  let start = peakAt
  let end = peakAt
  // 允许跨过几行低谷再继续，否则眼睛和眉毛之间的空隙会把区间切断
  const GAP = 6
  let gap = 0
  while (start > 0) {
    if (hist[start - 1]! >= floor) {
      gap = 0
      start--
    } else if (gap < GAP) {
      gap++
      start--
    } else break
  }
  start = Math.min(start + gap, peakAt)
  gap = 0
  while (end < hist.length - 1) {
    if (hist[end + 1]! >= floor) {
      gap = 0
      end++
    } else if (gap < GAP) {
      gap++
      end++
    } else break
  }
  end = Math.max(end - gap, peakAt)
  return { start, end }
}

/**
 * @param band 只在这个纵向区间里找。五官只可能长在头上，
 *   而差分图的身体同样会被模型重画 —— 身体面积远大于眼睛，
 *   不加约束的话噪声会直接压过信号，把眼区检到裙子上。
 */
async function diffRegion(
  fileA: string,
  fileB: string,
  pad = 8,
  band?: { top: number; bottom: number },
  ratio = 0.45,
): Promise<Rect | null> {
  const [a, b] = await Promise.all([
    sharp(fileA).ensureAlpha().raw().toBuffer({ resolveWithObject: true }),
    sharp(fileB).ensureAlpha().raw().toBuffer({ resolveWithObject: true }),
  ])
  if (a.info.width !== b.info.width || a.info.height !== b.info.height) return null

  const { width, height, channels } = a.info
  const y0 = Math.max(0, band?.top ?? 0)
  const y1 = Math.min(height - 1, band?.bottom ?? height - 1)
  const rows = new Array<number>(height).fill(0)
  const cols = new Array<number>(width).fill(0)
  let total = 0

  for (let y = y0; y <= y1; y++) {
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * channels
      // 把 alpha 也算进去：闭眼/闭嘴处可能是从有内容变成透明
      const d =
        Math.abs(a.data[o]! - b.data[o]!) +
        Math.abs(a.data[o + 1]! - b.data[o + 1]!) +
        Math.abs(a.data[o + 2]! - b.data[o + 2]!) +
        Math.abs(a.data[o + 3]! - b.data[o + 3]!)
      if (d > 24) {
        rows[y]!++
        cols[x]!++
        total++
      }
    }
  }
  if (total === 0) return null

  const vSpan = denseSpan(rows, ratio)
  if (!vSpan) return null

  // 横向直方图只统计密集行内的像素 —— 否则身上的噪点仍会把左右边界撑开
  const cols2 = new Array<number>(width).fill(0)
  for (let y = vSpan.start; y <= vSpan.end; y++) {
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * channels
      const d =
        Math.abs(a.data[o]! - b.data[o]!) +
        Math.abs(a.data[o + 1]! - b.data[o + 1]!) +
        Math.abs(a.data[o + 2]! - b.data[o + 2]!) +
        Math.abs(a.data[o + 3]! - b.data[o + 3]!)
      if (d > 24) cols2[x]!++
    }
  }
  const hSpan = denseSpan(cols2, ratio)
  if (!hSpan) return null

  // 留一圈余量，避免抗锯齿边缘在合成时露出接缝
  const x = Math.max(0, hSpan.start - pad)
  const y = Math.max(0, vSpan.start - pad)
  return {
    x,
    y,
    width: Math.min(width - x, hSpan.end - hSpan.start + 1 + pad * 2),
    height: Math.min(height - y, vSpan.end - vSpan.start + 1 + pad * 2),
  }
}

/**
 * 把每个表情的脸「烘焙」到同一具身体上。
 *
 * 实测：Seedream 5.0 Pro 做表情差分时，角色设计、姿势、服装都保住了，
 * 但布料褶皱、发丝走向、尾巴形态会整体重画一遍 —— 身体区域差异高达 18~20%。
 * 静态看完全看不出来（同款不同笔触），可一旦运行时整图切换表情，
 * 人眼对闪变极其敏感，整个身体会「滋啦」一下。
 *
 * 所以在这里就把身体统一掉：取底图作为唯一的身体，
 * 各表情只贡献脸部区域。运行时仍是简单的整图切换，但身体逐像素一致，零闪烁。
 *
 * 用羽化椭圆遮罩而不是硬矩形 —— 硬边会在重画的脸与固定身体的交界处露出接缝。
 */
async function bakeFace(baseFile: string, faceFile: string, region: Rect, feather: number): Promise<Buffer> {
  const { x, y, width, height } = region

  // 羽化遮罩：白色椭圆经高斯模糊，边缘平滑过渡到透明
  const mask = await sharp(
    Buffer.from(
      `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">
        <rect width="${width}" height="${height}" fill="black"/>
        <ellipse cx="${width / 2}" cy="${height / 2}" rx="${width / 2 - feather}" ry="${height / 2 - feather}" fill="white"/>
      </svg>`,
    ),
  )
    .blur(Math.max(1, feather / 2))
    .toColourspace('b-w')
    .raw()
    .toBuffer()

  // 取表情图的脸部区域，用遮罩替换其 alpha
  const face = await sharp(faceFile).extract({ left: x, top: y, width, height }).ensureAlpha().raw().toBuffer()

  for (let i = 0; i < width * height; i++) {
    // 原有 alpha 与遮罩相乘：抠图边缘之外的透明区保持透明，不会糊出一块方影
    face[i * 4 + 3] = Math.round((face[i * 4 + 3]! * mask[i]!) / 255)
  }

  const patch = await sharp(face, { raw: { width, height, channels: 4 } }).png().toBuffer()

  return sharp(baseFile)
    .composite([{ input: patch, left: x, top: y }])
    .png()
    .toBuffer()
}

/**
 * 检测结果的合理性闸门。
 *
 * 差分图的身体被整体重画时，denseSpan 会一路扩张到全身，吐出一个几百像素见方的
 * 「嘴区」。这种结果比没有更糟 —— 它会让烘焙把大半个角色换掉。宁可判失败，
 * 让用户用 --mouth / --eyes / --face 手填。
 */
function plausible(r: Rect, label: 'mouth' | 'eyes', canvasW: number, charH: number): boolean {
  const maxH = label === 'mouth' ? 0.08 : 0.1
  const maxW = label === 'mouth' ? 0.25 : 0.4
  return r.height <= charH * maxH && r.width <= canvasW * maxW
}

function which(cmd: string, args: string[]): Promise<boolean> {
  return new Promise((res) => {
    const p = spawn(cmd, args, { shell: process.platform === 'win32', stdio: 'ignore' })
    p.on('error', () => res(false))
    p.on('close', (code) => res(code === 0))
  })
}

function run(cmd: string, args: string[]): Promise<void> {
  return new Promise((res, rej) => {
    const p = spawn(cmd, args, { shell: process.platform === 'win32', stdio: 'inherit' })
    p.on('error', rej)
    p.on('close', (code) => (code === 0 ? res() : rej(new Error(`${cmd} 退出码 ${code}`))))
  })
}

/**
 * 白底去除的降级方案。只在没装 rembg 时用 ——
 * 它会把角色身上接近纯白的部分（白色衣服、高光、浅色头发）一起吃掉，
 * 效果明显不如 isnet-anime。
 */
async function naiveCutout(src: string): Promise<Buffer> {
  const { data, info } = await sharp(src).ensureAlpha().raw().toBuffer({ resolveWithObject: true })
  const { width, height, channels } = info

  for (let i = 0; i < width * height; i++) {
    const o = i * channels
    const r = data[o]!
    const g = data[o + 1]!
    const b = data[o + 2]!
    if (r > 242 && g > 242 && b > 242) data[o + 3] = 0
  }

  return sharp(data, { raw: { width, height, channels } }).png().toBuffer()
}

async function cutoutAll(paths: CharPaths, naive: boolean): Promise<void> {
  if (naive) {
    console.log('  使用降级抠图（纯白阈值）—— 白色服装和高光可能被吃掉\n')
    for (const kind of KINDS) {
      const dir = resolve(paths.raw, kind)
      let files: string[] = []
      try {
        files = (await readdir(dir)).filter((f) => f.endsWith('.png'))
      } catch {
        continue
      }
      for (const f of files) {
        await writeFile(resolve(paths.root, kind, f), await naiveCutout(resolve(dir, f)))
      }
    }
    await writeFile(paths.base, await naiveCutout(paths.rawBase))
    return
  }

  const hasRembg = await which('rembg', ['--help'])
  if (!hasRembg) {
    throw new Error(
      [
        '没找到 rembg。它负责抠透明背景，动漫立绘用 isnet-anime 模型效果远好于任何阈值方案。',
        '',
        '  安装（已确认本机有 Python 3.12）：',
        '    pip install "rembg[cli]" onnxruntime',
        '',
        '  首次运行会自动下载模型权重（约 170MB），需要能访问 GitHub。',
        '  实在装不上就先用降级方案：npm run sprite:process -- --naive',
      ].join('\n'),
    )
  }

  // 批处理模式：模型只加载一次，比逐张调用快一个数量级
  for (const kind of KINDS) {
    const inDir = resolve(paths.raw, kind)
    try {
      const files = (await readdir(inDir)).filter((f) => f.endsWith('.png'))
      if (files.length === 0) continue
    } catch {
      continue
    }
    console.log(`  抠图 ${kind}/ …`)
    await run('rembg', ['p', '-m', 'isnet-anime', inDir, resolve(paths.root, kind)])
  }

  console.log('  抠图 base.png …')
  await run('rembg', ['i', '-m', 'isnet-anime', paths.rawBase, paths.base])
}

interface Entry {
  kind: Kind | 'base'
  id: string
  file: string
}

async function main() {
  const paths = new CharPaths(values.name!)
  const pad = Math.max(0, Number(values.padding) || 0)
  const rescale = !values['no-rescale']
  const bake = !values['no-bake']

  await paths.ensureOutDirs()

  console.log('[1/3] 抠透明背景')
  await cutoutAll(paths, values.naive!)

  // --- 收集所有产物 ---
  const entries: Entry[] = [{ kind: 'base', id: 'base', file: paths.base }]
  for (const kind of KINDS) {
    let files: string[] = []
    try {
      files = (await readdir(resolve(paths.root, kind))).filter((f) => f.endsWith('.png'))
    } catch {
      continue
    }
    for (const f of files) entries.push({ kind, id: basename(f, '.png'), file: resolve(paths.root, kind, f) })
  }

  console.log(`\n[2/3] 对齐 ${entries.length} 张`)

  // --- 求每张图的包围盒与锚点（头顶中心）---
  const boxes = new Map<string, BBox>()
  for (const e of entries) {
    const { data, info } = await sharp(e.file).ensureAlpha().raw().toBuffer({ resolveWithObject: true })
    const box = alphaBBox(data, info.width, info.height, info.channels)
    if (!box) {
      console.log(`  ⚠ ${e.kind}/${e.id} 抠完全透明，跳过 —— 多半是抠图把整张图吃了`)
      continue
    }
    boxes.set(`${e.kind}/${e.id}`, box)
  }

  const baseBox = boxes.get('base/base')
  if (!baseBox) throw new Error('底图 base.png 抠图后是空的，后续没法对齐。检查 raw/base.png 和抠图结果。')

  // --- 缩放归一 ---
  // 实测方舟做一次指令编辑，角色整体会放大约 3%。只对齐位置的话，
  // 切表情时角色会「一大一小」地呼吸。这里以底图高度为基准做等比缩放：
  // 脸变了但身高不该变，所以按高度归一是安全的，宽度随之等比跟随。
  const scaleOf = new Map<string, number>()
  for (const [key, box] of boxes) {
    scaleOf.set(key, rescale ? baseBox.height / box.height : 1)
  }

  const scaledW = (key: string) => boxes.get(key)!.width * scaleOf.get(key)!
  const scaledH = (key: string) => boxes.get(key)!.height * scaleOf.get(key)!

  // --- 统一画布：锚点定在头顶中心 ---
  const keys = [...boxes.keys()]
  const halfW = Math.max(...keys.map((k) => scaledW(k) / 2))
  const maxH = Math.max(...keys.map((k) => scaledH(k)))
  const canvasW = Math.ceil(halfW * 2 + pad * 2)
  const canvasH = Math.ceil(maxH + pad * 2)
  const anchorX = Math.round(canvasW / 2)
  const anchorY = pad

  const drift: Array<{ key: string; dw: number; dh: number; scale: number }> = []

  for (const e of entries) {
    const key = `${e.kind}/${e.id}`
    const box = boxes.get(key)
    if (!box) continue

    const s = scaleOf.get(key)!
    const tw = Math.max(1, Math.round(box.width * s))
    const th = Math.max(1, Math.round(box.height * s))

    let crop = sharp(e.file).extract({ left: box.left, top: box.top, width: box.width, height: box.height })
    if (tw !== box.width || th !== box.height) {
      crop = crop.resize(tw, th, { fit: 'fill', kernel: 'lanczos3' })
    }
    const cropped = await crop.png().toBuffer()

    const out = await sharp({
      create: { width: canvasW, height: canvasH, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 0 } },
    })
      .composite([{ input: cropped, left: Math.round(anchorX - tw / 2), top: anchorY }])
      .png()
      .toBuffer()

    await writeFile(e.file, out)

    if (key !== 'base/base') {
      drift.push({ key, dw: box.width - baseBox.width, dh: box.height - baseBox.height, scale: s })
    }
  }

  console.log(`  画布 ${canvasW}×${canvasH}，锚点 (${anchorX}, ${anchorY})`)

  if (rescale) {
    const corrected = drift.filter((d) => Math.abs(d.scale - 1) > 0.005)
    if (corrected.length) {
      const worst = corrected.reduce((a, b) => (Math.abs(b.scale - 1) > Math.abs(a.scale - 1) ? b : a))
      console.log(`  缩放归一：修正 ${corrected.length} 张，最大 ${((worst.scale - 1) * 100).toFixed(1)}%（${worst.key}）`)
    } else {
      console.log('  缩放归一：所有图尺度一致，无需修正')
    }
  }

  // --- 一致性报告 ---
  // 关键：比较的是**缩放归一之后**的宽度差。
  // 整体等比放大已经被上一步修掉了，若归一后宽度仍然对不上，
  // 才说明模型真的改了体型或姿势 —— 这种是修不掉的，只能重跑。
  const tolW = Math.max(4, Math.round(baseBox.width * 0.02))
  const suspects = drift
    .map((d) => ({ key: d.key, residual: Math.round((baseBox.width + d.dw) * d.scale - baseBox.width), scale: d.scale }))
    .filter((d) => Math.abs(d.residual) > tolW)

  if (suspects.length) {
    console.log(`\n  ⚠ 以下 ${suspects.length} 张在缩放归一后轮廓仍对不上，模型八成改了体型或姿势：`)
    for (const s of suspects.sort((a, b) => Math.abs(b.residual) - Math.abs(a.residual))) {
      console.log(`      ${s.key}　残余宽差 ${s.residual > 0 ? '+' : ''}${s.residual}px`)
    }
    console.log('      到预览页开 diff 模式确认，确实漂了就标记重跑 —— 这种缩放救不回来。')
  } else {
    console.log('  ✓ 缩放归一后所有图轮廓一致，没有检测到体型改动')
  }

  // --- manifest ---
  console.log('\n[3/3] 生成 manifest')

  const expressions: SpriteManifest['expressions'] = {}
  for (const e of EXPRESSIONS) {
    if (!boxes.has(`face/${e.id}`)) continue
    const entry: { file: string; blink?: string } = { file: `face/${e.id}.png` }
    if ((BLINK_TARGETS as readonly string[]).includes(e.id) && boxes.has(`eyes/${e.id}`)) {
      entry.blink = `eyes/${e.id}.png`
    }
    expressions[e.id] = entry
  }

  const mouth: SpriteManifest['mouth'] = {}
  for (const m of MOUTH_SHAPES) {
    if (boxes.has(`mouth/${m.id}`)) mouth[m.id] = `mouth/${m.id}.png`
  }

  // --- 五官区域：手动指定优先，否则自动检测 ---
  // 渲染层靠它做局部合成，而不是整图切换 —— 后者会让角色一开口表情就退回平静脸
  const regions: NonNullable<SpriteManifest['regions']> = {}
  const manualEyes = parseRect(values.eyes, 'eyes')
  const manualMouth = parseRect(values.mouth, 'mouth')
  const manualFace = parseRect(values.face, 'face')

  // 五官只可能在头部。对齐后角色顶部固定在 anchorY，
  // 取上 25% 高度作为搜索带 —— 原来的 40% 会把肩膀和胸口一起纳入，
  // 而差分图的身体同样被重画，那部分噪声足以压过嘴部信号
  const headBand = { top: anchorY, bottom: anchorY + Math.round(maxH * 0.25) }

  if (manualMouth) {
    regions.mouth = manualMouth
  } else if (mouth.closed && mouth.open) {
    const r = await diffRegion(resolve(paths.root, mouth.closed), resolve(paths.root, mouth.open), 8, headBand)
    if (r && plausible(r, 'mouth', canvasW, maxH)) regions.mouth = r
    else if (r) console.log(`  ⚠ 嘴区检测结果不合理（${r.width}×${r.height}），已丢弃 —— 身体被大面积重绘带偏了检测`)
  }

  if (manualEyes) {
    regions.eyes = manualEyes
  } else {
    const blinkPair = Object.entries(expressions).find(([, v]) => v.blink)
    if (blinkPair) {
      const [, v] = blinkPair
      const r = await diffRegion(resolve(paths.root, v.file), resolve(paths.root, v.blink!), 8, headBand)
      if (r && plausible(r, 'eyes', canvasW, maxH)) regions.eyes = r
      else if (r) console.log(`  ⚠ 眼区检测结果不合理（${r.width}×${r.height}），已丢弃 —— 同上`)
    }
  }

  // 眼在上嘴在下是解剖学保证的。反了说明至少有一个检歪了，
  // 继续往下算只会把烘焙区域搞成一团糟，不如老实报出来
  if (regions.eyes && regions.mouth && regions.eyes.y > regions.mouth.y) {
    console.log('  ⚠ 检测到眼区位置低于嘴区，五官检测不可信，已丢弃眼区。')
    delete regions.eyes
  }

  // 两块区域各自留了一圈 padding，脸小或五官紧凑时会叠在一起。
  // 一旦重叠，合成嘴型就会连带把眼睛下缘覆盖成 normal 脸的样子 —— 眨眼当场被抹掉。
  // 眼在上嘴在下是解剖学保证的，取重叠带中线一刀切开即可。
  if (regions.eyes && regions.mouth) {
    const e = regions.eyes
    const m = regions.mouth
    const overlap = Math.min(e.y + e.height, m.y + m.height) - Math.max(e.y, m.y)
    if (overlap > 0 && e.y < m.y) {
      const boundary = Math.round((e.y + e.height + m.y) / 2)
      e.height = Math.max(1, boundary - e.y)
      m.height = Math.max(1, m.y + m.height - boundary)
      m.y = boundary
      console.log(`  五官区域重叠 ${overlap}px，已在 y=${boundary} 处切开`)
    }
  }

  // --- 脸部烘焙：把所有表情统一到同一具身体上 ---
  // 烘焙只需要一个脸部矩形。--face 直接给定；没给才从眼区 ∪ 嘴区推。
  let face: Rect | null = manualFace
  if (!face && regions.eyes && regions.mouth) {
    const e = regions.eyes
    const m = regions.mouth
    // 脸部区域 = 眼区 ∪ 嘴区，再向外扩 —— 腮红、脸颊阴影都在这两块之外
    const padX = Math.round(e.width * 0.28)
    const padY = Math.round(e.height * 1.1)
    const x = Math.max(0, Math.min(e.x, m.x) - padX)
    const y = Math.max(0, e.y - padY)
    const right = Math.max(e.x + e.width, m.x + m.width) + padX
    const bottom = Math.max(e.y + e.height, m.y + m.height) + padY

    face = {
      x,
      y,
      // 必须钳到 ≥1：五官检测偶尔会失准（比如嘴区被判到眼区上方），
      // 算出负数尺寸会让 sharp 直接抛错，整条管线在最后一步崩掉
      width: Math.max(1, Math.min(canvasW - x, right - x)),
      height: Math.max(1, Math.min(canvasH - y, bottom - y)),
    }

    // 脸不该占到半个身子。超了说明区域检测被身体重绘噪点带偏了，
    // 这时烘焙会把大半个角色一起换掉，比不烘焙更糟 —— 宁可跳过
    if (face.height > canvasH * 0.55 || face.width > canvasW * 0.9) {
      console.log(
        `  ⚠ 脸部区域检测异常（${face.width}×${face.height}，占画布 ${((face.height / canvasH) * 100).toFixed(0)}% 高），已丢弃。`,
      )
      face = null
    }
  }

  if (bake && face) {
    // 手填的坐标也要钳进画布，越界会让 sharp 在 extract 时直接抛错
    face.width = Math.max(1, Math.min(canvasW - face.x, face.width))
    face.height = Math.max(1, Math.min(canvasH - face.y, face.height))

    const feather = Math.max(6, Math.round(Math.min(face.width, face.height) * 0.12))

    // 每个表情都要烘焙，包括 normal —— 它的身体同样是模型重画的，
    // 只有底图 base.png 本身是那具唯一的身体
    let baked = 0
    for (const id of Object.keys(expressions)) {
      const file = resolve(paths.root, expressions[id]!.file)
      await writeFile(file, await bakeFace(paths.base, file, face, feather))
      baked++
    }
    regions.face = face
    console.log(
      `  脸部烘焙${manualFace ? '（脸区手动指定）' : ''}：${baked} 个表情已统一到底图身体，` +
        `脸区 ${face.width}×${face.height} @ (${face.x}, ${face.y})，羽化 ${feather}px`,
    )
  } else if (bake) {
    console.log('  ⚠ 没有可用的脸部区域，跳过烘焙 —— 运行时切表情整个身体都会闪。')
    console.log('     自动检测在身体被大面积重绘的素材上不可靠，用 --face 手填画布坐标即可绕过：')
    console.log('       npx tsx scripts/sprite/process.ts --face x,y,宽,高')
  }

  const manifest: SpriteManifest = {
    version: 1,
    name: values.name!,
    generatedAt: new Date().toISOString(),
    canvas: { width: canvasW, height: canvasH },
    anchor: { x: anchorX, y: anchorY },
    base: 'base.png',
    expressions,
    mouth,
    ...(Object.keys(regions).length ? { regions } : {}),
  }
  await writeManifest(paths, manifest)

  console.log(`  表情 ${Object.keys(expressions).length} 个（含闭眼差分 ${Object.values(expressions).filter((e) => e.blink).length} 个），嘴型 ${Object.keys(mouth).length} 个`)
  const fmt = (r?: Rect) => (r ? `${r.width}×${r.height} @ (${r.x}, ${r.y})` : '未检出')
  console.log(`  五官区域：嘴 ${fmt(regions.mouth)}　眼 ${fmt(regions.eyes)}`)
  console.log(`  → ${paths.manifest}`)
  console.log('\n素材就绪。renderer 侧的 SpriteCharacterView 直接读这个 manifest 即可。')
}

main().catch((err) => {
  console.error(`\n✗ ${err instanceof Error ? err.message : String(err)}`)
  process.exitCode = 1
})
