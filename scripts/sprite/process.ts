/**
 * 精灵资源后处理模块：将生成阶段的原始角色图片转换为运行时可加载的透明素材。
 *
 * 所属模块：``scripts/sprite`` 资源生成工具链。
 * 核心职责：调用 rembg 或近白阈值方案移除背景，按主体包围盒统一缩放与锚点，
 * 通过像素差异定位眼睛和嘴部区域，将表情脸部合成到底图身体，并生成 ``manifest.json``。
 * 依赖关系：读取 ``config.ts`` 提供的素材类别，使用 ``paths.ts`` 解析中间目录和最终资源目录，
 * 使用 ``manifest.ts`` 写入运行时清单；图像解码、裁剪和编码依赖 sharp，模型抠图依赖外部 rembg 命令。
 *
 *   npm run sprite:process
 *   npx tsx scripts/sprite/process.ts --naive     # rembg 不可用时使用阈值抠图
 *   npx tsx scripts/sprite/process.ts --face 260,190,250,250   # 指定脸部区域
 *
 * 五官区域默认自动检测，但前提是五官差异密度显著高于身体。若差分素材同时
 * 重绘了大部分身体，自动检测结果会失真，此时使用 --face、--eyes 或 --mouth
 * 传入对齐后画布坐标。
 *
 * Windows 下带参数的脚本建议直接使用 npx tsx 调用，避免 npm 参数转发差异。
 *
 * 对齐是这一步的关键。生成接口可能使角色在画布中产生像素级位置偏移；若不校正，
 * 运行时切换表情会产生明显跳动。
 *
 * 同时生成一份一致性报告：将每张图的包围盒与底图比较，
 * 差异较大说明生成结果可能改变身体区域；数值报告比人工逐图检查更稳定。
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
    // 身体重绘噪声可能超过五官差异信号，导致自动检测失准；此时应使用手动坐标。
    face: { type: 'string' },
    eyes: { type: 'string' },
    mouth: { type: 'string' },
  },
})

/**
 * 将命令行传入的矩形字符串解析为画布坐标。
 *
 * @param spec 矩形文本，支持逗号或空白分隔的 ``x,y,width,height``；未提供时返回 ``null``。
 * @param label 参数名称，仅用于构造可定位的错误信息。
 * @returns {Rect | null} 非负坐标且宽高至少为 1 的矩形；参数为空时返回 ``null``。
 * @throws {Error} 参数不是四个有限数字，或坐标为负数、宽高小于 1 时抛出。
 * @remarks 函数会将坐标和尺寸四舍五入为整数，避免浮点值进入 sharp 的区域裁剪接口。
 */
function parseRect(spec: string | undefined, label: string): Rect | null {
  if (!spec) return null
  // 同时接受逗号和空格分隔，兼容 PowerShell 对未加引号参数的拆分行为。
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

/**
 * 扫描 RGBA 原始像素，计算有效透明度区域的最小包围盒。
 *
 * @param data 按行排列的原始像素缓冲区；像素步长由 ``channels`` 指定。
 * @param width 图像宽度，单位为像素，必须为正整数。
 * @param height 图像高度，单位为像素，必须为正整数。
 * @param channels 每个像素的通道数量；最后一个通道被视为 alpha 通道。
 * @returns {BBox | null} alpha 大于 8 的像素包围盒；没有有效像素时返回 ``null``。
 * @remarks 以固定 alpha 阈值排除几乎透明的抠图噪点；时间复杂度为 ``O(width * height)``。
 */
function alphaBBox(data: Buffer, width: number, height: number, channels: number): BBox | null {
  let minX = width
  let minY = height
  let maxX = -1
  let maxY = -1
  const alphaOffset = channels - 1

  for (let y = 0; y < height; y++) {
    const row = y * width * channels
    for (let x = 0; x < width; x++) {
      // 使用 8 而非 0 作为阈值：抠图边缘的近透明像素不应参与包围盒计算，
      // 否则边缘噪点会使不同素材的主体边界产生不必要的尺寸差异。
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
 * 沿一个轴的投影直方图里，找出以峰值为中心的密集区间。
 *
 * @param hist 单轴投影直方图；数组下标对应画布坐标，数值表示该坐标上的差异像素数。
 * @param ratio 密度下限与峰值的比例，默认 ``0.2``；值越大，结果区域越集中。
 * @returns {{start: number, end: number} | null} 密集区间的闭区间坐标；没有差异像素时返回 ``null``。
 * @remarks 函数允许跨越有限数量的低谷，以免眉毛与眼睛之间的空隙把同一五官拆成多个区域。
 *   时间复杂度为 ``O(hist.length)``。
 *
 * 直接取所有差异像素的包围盒会将身体重绘噪声一并纳入，导致结果覆盖整个角色。
 * 此处从差异峰值向两侧扩张，在密度低于峰值指定比例后停止，以保留信号最集中的区域。
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
  // 允许跨过有限低谷，避免眉毛与眼睛之间的间隙把同一五官拆分成多个区间。
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
 * 计算两张已对齐图像的差异密集区域，用于自动定位眼睛或嘴部。
 *
 * @param fileA 第一张 PNG 文件路径。
 * @param fileB 第二张 PNG 文件路径；两张图的尺寸必须一致。
 * @param pad 检测区域四周扩展的像素数，默认 ``8``，用于覆盖抗锯齿边缘。
 * @param band 可选的纵向搜索范围；``top`` 和 ``bottom`` 均为包含端点的画布坐标。
 * @param ratio 投影直方图密集区间的峰值比例阈值，默认 ``0.45``。
 * @returns {Promise<Rect | null>} 差异区域；尺寸不一致、没有有效差异或无法形成密集区间时返回 ``null``。
 * @throws {Error} 任一图片读取、解码或原始像素转换失败时抛出。
 * @remarks 函数只在 ``band`` 内统计纵向差异，避免身体重绘噪声超过头部五官信号；
 *   两次逐像素扫描的时间复杂度为 ``O(width * height)``，会占用两张原始 RGBA 图像的内存。
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
      // 将 alpha 纳入差异计算，以捕获闭眼或闭嘴区域由有内容变为透明的情况。
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

  // 横向直方图只统计密集行内的像素，避免身体重绘噪声扩大左右边界。
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

  // 预留边界余量，避免抗锯齿边缘在局部合成时产生接缝。
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
 * 将表情图的脸部区域合成到底图，统一所有表情的身体像素。
 *
 * 生成接口在表情差分中可能同步重绘服装、发丝或姿态；运行时直接切换整图时，
 * 这些非表情区域的像素差异会造成可见闪烁。因此以底图作为唯一身体来源，
 * 只保留表情图指定脸部区域的像素。
 *
 * @param baseFile 身体基准底图 PNG 路径。
 * @param faceFile 当前表情 PNG 路径；必须覆盖 ``region`` 指定的完整区域。
 * @param region 脸部区域的画布坐标，坐标和尺寸必须为正整数且位于两张图范围内。
 * @param feather 羽化半径，单位为像素；值越大，脸部与固定身体的过渡越平滑。
 * @returns {Promise<Buffer>} 合成后的 PNG 二进制数据。
 * @throws {Error} 图片读取、区域裁剪、遮罩生成或 PNG 编码失败时抛出。
 * @remarks 函数会将整张结果图编码为 PNG 并暂存于内存；椭圆羽化遮罩避免矩形边缘产生接缝。
 */
async function bakeFace(baseFile: string, faceFile: string, region: Rect, feather: number): Promise<Buffer> {
  const { x, y, width, height } = region

  // 使用高斯模糊的白色椭圆遮罩，使脸部边缘平滑过渡到固定身体。
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

  // 提取表情图的脸部区域，并用羽化遮罩约束其 alpha 通道。
  const face = await sharp(faceFile).extract({ left: x, top: y, width, height }).ensureAlpha().raw().toBuffer()

  for (let i = 0; i < width * height; i++) {
    // 保留原始 alpha 与遮罩的交集，避免透明背景被矩形区域重新带入合成结果。
    face[i * 4 + 3] = Math.round((face[i * 4 + 3]! * mask[i]!) / 255)
  }

  const patch = await sharp(face, { raw: { width, height, channels: 4 } }).png().toBuffer()

  return sharp(baseFile)
    .composite([{ input: patch, left: x, top: y }])
    .png()
    .toBuffer()
}

/**
 * 检查自动检测出的五官区域是否满足面积约束。
 *
 * @param r 待校验的检测矩形。
 * @param label 区域类别；``mouth`` 使用更严格的宽高上限。
 * @param canvasW 对齐后画布宽度，单位为像素。
 * @param charH 对齐后角色主体高度，单位为像素。
 * @returns {boolean} 区域同时满足对应类别的宽度和高度上限时返回 ``true``。
 * @remarks 差分图可能将身体重绘噪声误判为五官；拒绝过大区域可避免烘焙覆盖角色主体。
 */
function plausible(r: Rect, label: 'mouth' | 'eyes', canvasW: number, charH: number): boolean {
  const maxH = label === 'mouth' ? 0.08 : 0.1
  const maxW = label === 'mouth' ? 0.25 : 0.4
  return r.height <= charH * maxH && r.width <= canvasW * maxW
}

/**
 * 检查外部命令是否可启动并以成功状态退出。
 *
 * @param cmd 可执行文件名或路径。
 * @param args 传递给命令的参数列表，按进程启动顺序排列。
 * @returns {Promise<boolean>} 命令以退出码 ``0`` 结束时返回 ``true``；启动失败或退出码非零时返回 ``false``。
 * @remarks 函数吞掉子进程错误并将其转换为布尔结果，适用于能力探测；不会向调用方抛出启动异常。
 */
function which(cmd: string, args: string[]): Promise<boolean> {
  return new Promise((res) => {
    const p = spawn(cmd, args, { shell: process.platform === 'win32', stdio: 'ignore' })
    p.on('error', () => res(false))
    p.on('close', (code) => res(code === 0))
  })
}

/**
 * 运行外部图像处理命令，并等待其完成。
 *
 * @param cmd 可执行文件名或路径。
 * @param args 传递给命令的参数列表。
 * @returns {Promise<void>} 子进程以退出码 ``0`` 结束时完成。
 * @throws {Error} 子进程无法启动，或以非零退出码结束时抛出。
 * @remarks 子进程继承当前进程的标准输出，便于保留外部工具的诊断信息；调用方需等待 Promise 完成后继续处理。
 */
function run(cmd: string, args: string[]): Promise<void> {
  return new Promise((res, rej) => {
    const p = spawn(cmd, args, { shell: process.platform === 'win32', stdio: 'inherit' })
    p.on('error', rej)
    p.on('close', (code) => (code === 0 ? res() : rej(new Error(`${cmd} 退出码 ${code}`))))
  })
}

/**
 * 基于近白像素阈值移除背景的备用抠图方案。
 *
 * @param src 输入 PNG 文件路径。
 * @returns {Promise<Buffer>} 将近白像素 alpha 置零后重新编码的 PNG 数据。
 * @throws {Error} 输入图片读取、原始像素转换或 PNG 编码失败时抛出。
 * @remarks 该方案可能同时移除白色服装、高光和浅色头发，精度低于 ``isnet-anime``；
 *   函数会将整张图片解码到内存，时间复杂度为 ``O(width * height)``。
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

/**
 * 对底图和各类别素材执行透明背景抠图。
 *
 * @param paths 角色素材工作路径和输出路径。
 * @param naive 是否使用近白阈值备用方案；为 ``false`` 时要求 rembg 可执行。
 * @returns 所有图片处理完成后的 Promise。
 * @throws Error rembg 不可用、外部命令失败或图片读写失败。
 * @remarks 副作用：在最终素材目录写入透明 PNG；备用方案直接在进程内处理图像。
 */
async function cutoutAll(paths: CharPaths, naive: boolean): Promise<void> {
  if (naive) {
    console.log('  使用降级抠图（纯白阈值）—— 白色服装和高光可能同时被移除\n')
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

/**
 * 执行抠图、尺寸归一、局部烘焙和 manifest 生成。
 *
 * @returns {Promise<void>} 全部后处理步骤完成后的 Promise。
 * @throws {Error} 参数、外部抠图工具、图像数据或输出文件无效。
 * @remarks 副作用：读取工作目录素材，写入透明、对齐后的最终 PNG 和 ``manifest.json``。
 */
async function main(): Promise<void> {
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

  // 为每张图计算非透明包围盒和头顶中心锚点。
  const boxes = new Map<string, BBox>()
  for (const e of entries) {
    const { data, info } = await sharp(e.file).ensureAlpha().raw().toBuffer({ resolveWithObject: true })
    const box = alphaBBox(data, info.width, info.height, info.channels)
    if (!box) {
      console.log(`  ⚠ ${e.kind}/${e.id} 结果完全透明，跳过；请检查抠图是否移除了全部像素`)
      continue
    }
    boxes.set(`${e.kind}/${e.id}`, box)
  }

  const baseBox = boxes.get('base/base')
  if (!baseBox) throw new Error('底图 base.png 抠图后为空，无法继续对齐；请检查 raw/base.png 和抠图结果。')

  // 以底图主体高度为基准等比缩放，消除生成接口造成的整体尺寸偏差；高度是
  // 稳定锚点，宽度随比例同步调整。
  const scaleOf = new Map<string, number>()
  for (const [key, box] of boxes) {
    scaleOf.set(key, rescale ? baseBox.height / box.height : 1)
  }

  const scaledW = (key: string) => boxes.get(key)!.width * scaleOf.get(key)!
  const scaledH = (key: string) => boxes.get(key)!.height * scaleOf.get(key)!

  // 创建统一画布，并将所有素材的头顶中心对齐到同一锚点。
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
  // 比较缩放归一后的宽度差：整体比例偏差已在上一步消除，
  // 归一后仍存在的差异才可能表示模型改变了体型或姿势，需要重新生成素材。
  const tolW = Math.max(4, Math.round(baseBox.width * 0.02))
  const suspects = drift
    .map((d) => ({ key: d.key, residual: Math.round((baseBox.width + d.dw) * d.scale - baseBox.width), scale: d.scale }))
    .filter((d) => Math.abs(d.residual) > tolW)

  if (suspects.length) {
    console.log(`\n  ⚠ 以下 ${suspects.length} 张在缩放归一后轮廓仍不一致，可能存在体型或姿势变化：`)
    for (const s of suspects.sort((a, b) => Math.abs(b.residual) - Math.abs(a.residual))) {
      console.log(`      ${s.key}　残余宽差 ${s.residual > 0 ? '+' : ''}${s.residual}px`)
    }
    console.log('      请在预览页启用 diff 模式确认；若确有变化，应标记素材重新生成。')
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
  // 渲染层使用这些区域执行局部合成，避免切换整图时嘴部变化覆盖已有表情状态。
  const regions: NonNullable<SpriteManifest['regions']> = {}
  const manualEyes = parseRect(values.eyes, 'eyes')
  const manualMouth = parseRect(values.mouth, 'mouth')
  const manualFace = parseRect(values.face, 'face')

  // 五官位于头部。对齐后角色顶部固定在 anchorY，取上 25% 高度作为搜索带，
  // 以排除肩膀和胸口区域的重绘噪声，避免其影响嘴部差异检测。
  const headBand = { top: anchorY, bottom: anchorY + Math.round(maxH * 0.25) }

  if (manualMouth) {
    regions.mouth = manualMouth
  } else if (mouth.closed && mouth.open) {
    const r = await diffRegion(resolve(paths.root, mouth.closed), resolve(paths.root, mouth.open), 8, headBand)
    if (r && plausible(r, 'mouth', canvasW, maxH)) regions.mouth = r
    else if (r) console.log(`  ⚠ 嘴区检测结果不合理（${r.width}×${r.height}），已丢弃；可能受身体重绘噪声影响`)
  }

  if (manualEyes) {
    regions.eyes = manualEyes
  } else {
    const blinkPair = Object.entries(expressions).find(([, v]) => v.blink)
    if (blinkPair) {
      const [, v] = blinkPair
      const r = await diffRegion(resolve(paths.root, v.file), resolve(paths.root, v.blink!), 8, headBand)
      if (r && plausible(r, 'eyes', canvasW, maxH)) regions.eyes = r
      else if (r) console.log(`  ⚠ 眼区检测结果不合理（${r.width}×${r.height}），已丢弃；可能受身体重绘噪声影响`)
    }
  }

  // 正常布局中眼区应位于嘴区上方；若顺序相反，至少有一个检测结果不可信，
  // 因此删除眼区，避免错误坐标继续扩大烘焙范围。
  if (regions.eyes && regions.mouth && regions.eyes.y > regions.mouth.y) {
    console.log('  ⚠ 检测到眼区位置低于嘴区，五官检测不可信，已丢弃眼区。')
    delete regions.eyes
  }

  // 两块区域分别扩展了边界余量，脸部较小或五官较紧凑时可能发生重叠。
  // 重叠会使嘴型合成覆盖眼区下缘，因此按上下区域的中线切分重叠带。
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

  // --- 脸部烘焙：将所有表情统一到底图身体 ---
  // 烘焙只需要一个脸部矩形；优先使用 --face，未指定时由眼区与嘴区的并集推导。
  let face: Rect | null = manualFace
  if (!face && regions.eyes && regions.mouth) {
    const e = regions.eyes
    const m = regions.mouth
    // 脸部区域由眼区与嘴区的并集向外扩展，以覆盖腮红和脸颊阴影等相邻细节。
    const padX = Math.round(e.width * 0.28)
    const padY = Math.round(e.height * 1.1)
    const x = Math.max(0, Math.min(e.x, m.x) - padX)
    const y = Math.max(0, e.y - padY)
    const right = Math.max(e.x + e.width, m.x + m.width) + padX
    const bottom = Math.max(e.y + e.height, m.y + m.height) + padY

    face = {
      x,
      y,
      // 将尺寸限制为至少 1，避免检测误差产生非法尺寸并使 sharp 在裁剪阶段失败。
      width: Math.max(1, Math.min(canvasW - x, right - x)),
      height: Math.max(1, Math.min(canvasH - y, bottom - y)),
    }

    // 脸部区域不应覆盖大部分画布；超出该范围通常表示检测被身体重绘噪声偏移，
    // 继续烘焙会替换过多角色像素，因此放弃自动区域。
    if (face.height > canvasH * 0.55 || face.width > canvasW * 0.9) {
      console.log(
        `  ⚠ 脸部区域检测异常（${face.width}×${face.height}，占画布 ${((face.height / canvasH) * 100).toFixed(0)}% 高），已丢弃。`,
      )
      face = null
    }
  }

  if (bake && face) {
    // 手动坐标同样限制在画布范围内，避免越界导致 sharp 的 extract 操作失败。
    face.width = Math.max(1, Math.min(canvasW - face.x, face.width))
    face.height = Math.max(1, Math.min(canvasH - face.y, face.height))

    const feather = Math.max(6, Math.round(Math.min(face.width, face.height) * 0.12))

    // 所有表情都需要烘焙，包括 normal；只有底图 base.png 作为统一的身体像素来源。
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
    console.log('  ⚠ 没有可用的脸部区域，跳过烘焙；运行时切换表情可能出现身体闪烁。')
    console.log('     身体大面积重绘时自动检测可靠性不足，请使用 --face 手动指定画布坐标：')
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
