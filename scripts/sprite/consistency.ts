/**
 * 一致性体检：量化两张图之间的漂移，并说清楚**是什么性质的漂移**。
 *
 *   npx tsx scripts/sprite/consistency.ts <图A> <图B>
 *   npx tsx scripts/sprite/consistency.ts --name yueli        # 批量：底图 vs 所有表情
 *
 * 「差异 12% 但我看不出来」几乎总是同一个原因：整体位移。
 * 模型把角色整个挪了一两像素，肉眼完全无感，逐像素比却会把整条轮廓标红。
 * 这类问题 process 阶段的对齐本来就会修掉，不该拿来吓人 ——
 * 所以这里先估出位移并补偿，再报「补偿后」的差异，那才是真正救不回来的部分。
 */
import { readdir, readFile } from 'node:fs/promises'
import { basename, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import sharp from 'sharp'
import { CharPaths } from './paths.ts'

const { values, positionals } = parseArgs({
  options: {
    name: { type: 'string' },
    threshold: { type: 'string', default: '24' },
  },
  allowPositionals: true,
})

interface Loaded {
  data: Buffer
  w: number
  h: number
  ch: number
  box: { left: number; top: number; right: number; bottom: number }
}

/** 读图并求主体（非白）包围盒。生图都是纯白背景，按非白判定比 alpha 更直接。 */
async function load(file: string): Promise<Loaded> {
  const { data, info } = await sharp(await readFile(file)).ensureAlpha().raw().toBuffer({ resolveWithObject: true })
  const { width: w, height: h, channels: ch } = info

  let left = w
  let top = h
  let right = -1
  let bottom = -1
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * ch
      const opaque = ch < 4 || data[o + 3]! > 24
      if (opaque && (data[o]! < 240 || data[o + 1]! < 240 || data[o + 2]! < 240)) {
        if (x < left) left = x
        if (x > right) right = x
        if (y < top) top = y
        if (y > bottom) bottom = y
      }
    }
  }
  return { data, w, h, ch, box: { left, top, right, bottom } }
}

interface Report {
  rawPct: number
  alignedPct: number
  dx: number
  dy: number
  scale: number
  head: number
  torso: number
  legs: number
}

/**
 * 在 ±RANGE 像素内搜索使差异最小的整数偏移。
 *
 * 全分辨率搜索 25×25 次太慢，所以按 STEP 抽样像素来算代价 ——
 * 找偏移量不需要看每个像素，抽 1/64 已经足够稳定。
 */
function searchBestOffset(A: Loaded, B: Loaded, threshold: number): { dx: number; dy: number } {
  const RANGE = 12
  const STEP = 8
  const { w, h, ch } = A

  let best = { dx: 0, dy: 0, cost: Infinity }
  for (let oy = -RANGE; oy <= RANGE; oy++) {
    for (let ox = -RANGE; ox <= RANGE; ox++) {
      let cost = 0
      for (let y = 0; y < h; y += STEP) {
        const sy = y - oy
        if (sy < 0 || sy >= h) continue
        for (let x = 0; x < w; x += STEP) {
          const sx = x - ox
          if (sx < 0 || sx >= w) continue
          const o = (y * w + x) * ch
          const p = (sy * w + sx) * ch
          const d =
            Math.abs(A.data[o]! - B.data[p]!) + Math.abs(A.data[o + 1]! - B.data[p + 1]!) + Math.abs(A.data[o + 2]! - B.data[p + 2]!)
          if (d > threshold) cost++
        }
      }
      if (cost < best.cost) best = { dx: ox, dy: oy, cost }
    }
  }
  return { dx: best.dx, dy: best.dy }
}

function compare(A: Loaded, B: Loaded, threshold: number): Report {
  const { w, h, ch } = A

  // 位移估计：暴力搜索让差异最小的偏移量。
  //
  // 一开始用主体包围盒的边界差来估，在长发角色上完全失效 ——
  // 包围盒由发梢决定，而发梢每次重绘都不一样，拿它当参照点等于用噪声定位。
  // 直接搜真正的最优对齐才诚实：搜完仍然降不下去的差异，就是真的救不回来。
  const { dx, dy } = searchBestOffset(A, B, threshold)
  const scale = (B.box.bottom - B.box.top) / Math.max(1, A.box.bottom - A.box.top)

  const diffAt = (ox: number, oy: number) => {
    let changed = 0
    let counted = 0
    // 头/身/腿：按主体包围盒高度三等分，比按画布分准得多
    const bt = A.box.top
    const bh = Math.max(1, A.box.bottom - A.box.top)
    const seg = [0, 0, 0]
    const segTotal = [0, 0, 0]

    for (let y = 0; y < h; y++) {
      const sy = y - oy
      for (let x = 0; x < w; x++) {
        const sx = x - ox
        if (sx < 0 || sy < 0 || sx >= w || sy >= h) continue
        const o = (y * w + x) * ch
        const p = (sy * w + sx) * ch
        const d =
          Math.abs(A.data[o]! - B.data[p]!) + Math.abs(A.data[o + 1]! - B.data[p + 1]!) + Math.abs(A.data[o + 2]! - B.data[p + 2]!)

        counted++
        const band = y < bt ? -1 : Math.min(2, Math.floor(((y - bt) / bh) * 3))
        if (band >= 0) segTotal[band]!++
        if (d > threshold) {
          changed++
          if (band >= 0) seg[band]!++
        }
      }
    }
    const pct = (n: number, t: number) => (t ? (n / t) * 100 : 0)
    return {
      pct: pct(changed, counted),
      head: pct(seg[0]!, segTotal[0]!),
      torso: pct(seg[1]!, segTotal[1]!),
      legs: pct(seg[2]!, segTotal[2]!),
    }
  }

  const raw = diffAt(0, 0)
  const aligned = dx || dy ? diffAt(dx, dy) : raw

  return { rawPct: raw.pct, alignedPct: aligned.pct, dx, dy, scale, head: aligned.head, torso: aligned.torso, legs: aligned.legs }
}

function verdict(r: Report): string[] {
  const lines: string[] = []

  const shiftExplained = r.rawPct - r.alignedPct
  if (shiftExplained > 2) {
    lines.push(
      `· 原始差异 ${r.rawPct.toFixed(1)}% 里有 ${shiftExplained.toFixed(1)} 个百分点纯粹来自整体位移（${r.dx}, ${r.dy}px）。`,
    )
    lines.push('  这部分 sprite:process 的对齐会自动修掉，不用管，也不该据此重跑。')
  }

  if (Math.abs(r.scale - 1) > 0.01) {
    lines.push(`· 整体缩放变了 ${((r.scale - 1) * 100).toFixed(1)}% —— process 的缩放归一会按底图高度修正。`)
  }

  if (r.torso > 12 || r.legs > 12) {
    lines.push(`· ⚠ 身体 ${r.torso.toFixed(1)}% / 腿部 ${r.legs.toFixed(1)}% 改动明显。`)
    lines.push('  改表情不该动这些地方，这是真问题 —— 到预览页标记重跑。')
  } else if (r.head > r.torso * 2.5) {
    lines.push(`· ✓ 差异集中在头部（头 ${r.head.toFixed(1)}% vs 身 ${r.torso.toFixed(1)}%），符合预期。`)
  }

  if (r.alignedPct < 3) lines.push('· ✓ 补偿位移后差异很小，这张可以用。')
  else if (r.alignedPct < 10 && r.torso <= 12 && r.legs <= 12) {
    lines.push('· ○ 补偿后仍有中等差异，但集中在该变的地方。开预览页 diff 目视确认一眼即可。')
  }

  if (lines.length === 0) lines.push('· 没有检出明显问题。')
  return lines
}

function row(label: string, r: Report): string {
  const flag = r.torso > 12 || r.legs > 12 ? '⚠' : r.alignedPct < 3 ? '✓' : '○'
  return (
    `  ${flag} ${label.padEnd(22)}` +
    `原始 ${r.rawPct.toFixed(1).padStart(5)}%  ` +
    `补偿后 ${r.alignedPct.toFixed(1).padStart(5)}%  ` +
    `位移 ${`${r.dx},${r.dy}`.padStart(7)}px  ` +
    `头/身/腿 ${r.head.toFixed(1)}/${r.torso.toFixed(1)}/${r.legs.toFixed(1)}%`
  )
}

async function main() {
  const threshold = Number(values.threshold) || 24

  // --- 批量模式：底图 vs 所有已生成的表情 ---
  if (values.name) {
    const paths = new CharPaths(values.name)
    const A = await load(paths.rawBase)
    const dir = resolve(paths.raw, 'face')

    let files: string[] = []
    try {
      files = (await readdir(dir)).filter((f) => f.endsWith('.png')).sort()
    } catch {
      throw new Error(`还没有生成任何表情：${dir}`)
    }
    if (!files.length) throw new Error(`还没有生成任何表情：${dir}`)

    console.log('─'.repeat(96))
    console.log(`  一致性体检　角色 ${values.name}　基准 base.png　阈值 ${threshold}`)
    console.log('─'.repeat(96))

    const reports: Array<[string, Report]> = []
    for (const f of files) {
      const B = await load(resolve(dir, f))
      const r = compare(A, B, threshold)
      reports.push([`face/${basename(f, '.png')}`, r])
      console.log(row(`face/${basename(f, '.png')}`, r))
    }

    const worst = reports.reduce((a, b) => (b[1].torso + b[1].legs > a[1].torso + a[1].legs ? b : a))
    console.log('')
    console.log(`  最需要关注：${worst[0]}`)
    for (const l of verdict(worst[1])) console.log(`  ${l}`)
    return
  }

  // --- 单对模式 ---
  const [fileA, fileB] = positionals
  if (!fileA || !fileB) {
    throw new Error('用法：\n  npx tsx scripts/sprite/consistency.ts <图A> <图B>\n  npx tsx scripts/sprite/consistency.ts --name yueli')
  }

  const A = await load(fileA)
  const B = await load(fileB)
  const r = compare(A, B, threshold)

  console.log('─'.repeat(72))
  console.log('  一致性体检')
  console.log('─'.repeat(72))
  console.log(`  A: ${basename(fileA)}　${A.w}×${A.h}`)
  console.log(`  B: ${basename(fileB)}　${B.w}×${B.h}`)
  console.log('')
  console.log(`  原始差异      ${r.rawPct.toFixed(1)}%`)
  console.log(`  整体位移      ${r.dx}, ${r.dy} px`)
  console.log(`  整体缩放      ${((r.scale - 1) * 100).toFixed(1)}%`)
  console.log(`  补偿后差异    ${r.alignedPct.toFixed(1)}%　← 这个才是真正救不回来的部分`)
  console.log(`  分区（补偿后）头 ${r.head.toFixed(1)}%　身 ${r.torso.toFixed(1)}%　腿 ${r.legs.toFixed(1)}%`)
  console.log('')
  for (const l of verdict(r)) console.log(`  ${l}`)
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err))
  process.exitCode = 1
})
