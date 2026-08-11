/**
 * 一致性检查：量化两张图之间的位移、缩放和局部像素差异。
 *
 *   npx tsx scripts/sprite/consistency.ts <图A> <图B>
 *   npx tsx scripts/sprite/consistency.ts --name yueli        # 批量：底图 vs 所有表情
 *
 * 先搜索最优整数位移并计算补偿后的差异，再分别报告头部、身体和腿部区域，
 * 使自动对齐可修复的偏差不会被误判为素材质量问题。
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

/**
 * 读取图像并计算非白主体的包围盒。
 *
 * @param file PNG/JPEG 等可由 sharp 解码的图像路径。
 * @returns {Promise<Loaded>} 原始像素、尺寸、通道数及主体包围盒。
 * @throws Error 文件不存在、格式无法解码或像素缓冲区读取失败时抛出。
 * @sideEffects 读取图像文件并在内存中创建原始像素缓冲区。
 */
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
 * @param A 基准图像及其像素数据，尺寸必须与 B 一致。
 * @param B 待比较图像及其像素数据，尺寸必须与 A 一致。
 * @param threshold 单像素 RGB 差异阈值，必须为非负数。
 * @returns { {dx: number, dy: number} } 使抽样差异成本最小的整数横纵偏移。
 * @sideEffects 仅执行 CPU 像素遍历，不修改输入图像。
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

/**
 * 计算两张图的原始差异、位移补偿差异和身体分区差异。
 *
 * @param A 基准图像及其主体包围盒。
 * @param B 待比较图像及其主体包围盒。
 * @param threshold 单像素 RGB 差异阈值，默认值由命令行解析器提供。
 * @returns 包含原始/补偿差异、位移、缩放和头身腿比例的报告。
 * @sideEffects 不修改输入 Buffer，仅执行 CPU 像素遍历。
 */
function compare(A: Loaded, B: Loaded, threshold: number): Report {
  const { w, h, ch } = A

  // 使用像素代价搜索最优位移，避免长发边界变化将包围盒噪声误认为整体位移。
  const { dx, dy } = searchBestOffset(A, B, threshold)
  const scale = (B.box.bottom - B.box.top) / Math.max(1, A.box.bottom - A.box.top)

  /**
   * 在指定位移补偿下统计总体和头、身、腿分区的像素差异率。
   *
   * @param ox 横向位移补偿，单位为像素。
   * @param oy 纵向位移补偿，单位为像素。
   * @returns {{pct: number; head: number; torso: number; legs: number}} 总体及各分区差异百分比。
   * @remarks 每次调用按完整画布遍历像素；比较过程只读输入缓冲区，分区边界依据基准图主体包围盒三等分。
   */
  const diffAt = (ox: number, oy: number): { pct: number; head: number; torso: number; legs: number } => {
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

/**
 * 将一致性报告转换为面向命令行的诊断结论。
 *
 * @param r 单张图的比较报告。
 * @returns 按严重程度和可操作性组织的中文诊断行列表。
 */
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

/**
 * 将单张图报告格式化为表格行。
 *
 * @param label 素材标识。
 * @param r 比较报告。
 * @returns 固定列宽的命令行文本。
 */
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

/**
 * 执行单图比较或指定角色目录的批量一致性检查。
 *
 * @returns 检查完成后的 Promise。
 * @throws Error 输入图像、目录或参数无效。
 */
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
