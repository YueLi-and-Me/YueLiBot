/**
 * 批量生成表情差分。
 *
 *   npm run sprite:gen                                          # 生成缺失的部分（断点续跑）
 *   npx tsx scripts/sprite/gen.ts --force                       # 全部重跑
 *   npx tsx scripts/sprite/gen.ts --only face/shy,face/cry      # 只跑指定条目
 *   npx tsx scripts/sprite/gen.ts --concurrency 3
 *
 * ⚠ Windows 下 `npm run xxx -- --flag` 会被 npm 吞掉参数，带参数一律用 npx tsx 直调。
 *
 * 三个约束决定了这里的实现：
 *   · 图像 API 限流严，并发默认压到 2，靠 withRetry 扛 429
 *   · 中断后不能重复计费 —— state.json 记录已完成条目
 *   · 预览页标记为「漂了」的条目，重跑时要把具体问题回喂给模型（reinforce）
 */
import { readFile, writeFile } from 'node:fs/promises'
import { parseArgs } from 'node:util'
import {
  BLINK_TARGETS,
  EXPRESSIONS,
  MOUTH_SHAPES,
  editBlinkInstruction,
  editExpressionInstruction,
  editMouthInstruction,
  reinforce,
} from './config.ts'
import { readJson, writeJson, type GenState, type ReviewList } from './manifest.ts'
import { CharPaths, slug, type Kind } from './paths.ts'
import {
  createProvider,
  ProviderError,
  refineKind,
  setupProxy,
  withRetry,
  type FailureKind,
  type ImageProvider,
} from './providers/index.ts'

const { values } = parseArgs({
  options: {
    name: { type: 'string', default: 'yueli' },
    force: { type: 'boolean', default: false },
    only: { type: 'string' },
    concurrency: { type: 'string', default: '2' },
    model: { type: 'string' },
  },
})

interface Job {
  kind: Kind
  id: string
  label: string
  instruction: string
  /** 该条目基于哪张图编辑。face 基于底图；eyes 基于对应表情图；mouth 基于 normal。 */
  sourceOf: (paths: CharPaths) => string
}

function buildJobs(): Job[] {
  const jobs: Job[] = []

  for (const e of EXPRESSIONS) {
    jobs.push({
      kind: 'face',
      id: e.id,
      label: e.cn,
      instruction: editExpressionInstruction(e),
      sourceOf: (p) => p.rawBase,
    })
  }

  // 闭眼差分基于对应的表情图，而不是底图 —— 否则会把该表情的脸颊红晕、
  // 眉形一并还原成中性脸，眨一次眼整张脸都变了
  for (const id of BLINK_TARGETS) {
    const e = EXPRESSIONS.find((x) => x.id === id)
    if (!e) continue
    jobs.push({
      kind: 'eyes',
      id: e.id,
      label: `${e.cn}·闭眼`,
      instruction: editBlinkInstruction(e),
      sourceOf: (p) => p.rawFile('face', e.id),
    })
  }

  for (const m of MOUTH_SHAPES) {
    jobs.push({
      kind: 'mouth',
      id: m.id,
      label: `嘴型·${m.cn}`,
      instruction: editMouthInstruction(m),
      sourceOf: (p) => p.rawFile('face', 'normal'),
    })
  }

  return jobs
}

async function exists(file: string): Promise<boolean> {
  try {
    await readFile(file)
    return true
  } catch {
    return false
  }
}

/** 固定并发度的任务池。图像 API 限流严，开太大只会集体撞 429。 */
async function pool<T>(items: T[], limit: number, worker: (item: T) => Promise<void>): Promise<void> {
  let cursor = 0
  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (cursor < items.length) {
        const item = items[cursor++]!
        await worker(item)
      }
    }),
  )
}

async function runPhase(
  label: string,
  jobs: Job[],
  ctx: { paths: CharPaths; provider: ImageProvider; state: GenState; review: ReviewList; concurrency: number },
): Promise<{ ok: number; skipped: number; failed: string[]; kinds: Set<FailureKind> }> {
  const { paths, provider, state, review, concurrency } = ctx
  const failed: string[] = []
  const kinds = new Set<FailureKind>()
  let ok = 0
  let skipped = 0

  const pending: Job[] = []
  for (const job of jobs) {
    const key = slug(job.kind, job.id)
    const needsRedo = values.force || key in review
    if (!needsRedo && state.done[key] && (await exists(paths.rawFile(job.kind, job.id)))) {
      skipped++
      continue
    }
    pending.push(job)
  }

  if (pending.length === 0) {
    console.log(`  ${label}：全部已完成，跳过 ${skipped} 项`)
    return { ok, skipped, failed, kinds }
  }

  console.log(`  ${label}：待生成 ${pending.length} 项${skipped ? `（已完成 ${skipped} 项跳过）` : ''}`)

  await pool(pending, concurrency, async (job) => {
    const key = slug(job.kind, job.id)
    const src = job.sourceOf(paths)

    let base: Buffer
    try {
      base = await readFile(src)
    } catch {
      failed.push(`${key}（缺少源图 ${src}）`)
      console.log(`    ✗ ${job.label} —— 源图不存在，先把它依赖的那张跑出来`)
      return
    }

    // 预览页标记过的，把具体问题回喂给模型；比单纯换 seed 重跑有效得多
    const complaint = review[key]
    const instruction = complaint ? reinforce(job.instruction, complaint) : job.instruction

    let attempts = 0
    try {
      const img = await withRetry(
        () => {
          attempts++
          return provider.edit({ base, instruction })
        },
        { onRetry: (n, delay) => console.log(`    ↻ ${job.label} 限流，${Math.round(delay / 1000)}s 后第 ${n} 次重试`) },
      )
      await writeFile(paths.rawFile(job.kind, job.id), img)
      state.done[key] = { at: new Date().toISOString(), bytes: img.length, attempts }
      delete review[key]
      ok++
      console.log(`    ✓ ${job.label}　${(img.length / 1024).toFixed(0)} KB`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      // detail 里才有服务端的真实原因，只报 "HTTP 400" 等于没报
      const detail = err instanceof ProviderError ? err.detail : undefined
      if (err instanceof ProviderError) kinds.add(err.kind)
      failed.push(`${key}（${msg}）`)
      console.log(`    ✗ ${job.label} —— ${msg}`)
      if (detail) console.log(`       ${detail.slice(0, 300)}`)
    }

    // 每完成一项就落盘，中途 Ctrl-C 也不会丢进度
    await writeJson(paths.state, state)
    await writeJson(paths.review, review)
  })

  return { ok, skipped, failed, kinds }
}

async function main() {
  const paths = new CharPaths(values.name!)
  const concurrency = Math.max(1, Math.min(6, Number(values.concurrency) || 2))

  if (!(await exists(paths.rawBase))) {
    throw new Error(
      [
        '还没有定稿底图。先走这两步：',
        '  npx tsx scripts/sprite/base.ts --ref <参考图> --desc "<一句角色描述>"',
        '  npx tsx scripts/sprite/base.ts --pick <编号>',
      ].join('\n'),
    )
  }

  setupProxy()
  const provider = createProvider({ model: values.model })
  await paths.ensureRawDirs()

  const state = await readJson<GenState>(paths.state, { done: {} })
  const review = await readJson<ReviewList>(paths.review, {})

  let jobs = buildJobs()
  if (values.only) {
    // PowerShell 会把裸的 a,b 当数组字面量，传给 npx 时逗号变空格，
    // 所以逗号和空格都当分隔符收 —— 否则 Windows 下不加引号必然匹配不上
    const wanted = new Set(values.only.split(/[,\s]+/).filter(Boolean))
    jobs = jobs.filter((j) => wanted.has(slug(j.kind, j.id)))
    if (jobs.length === 0) throw new Error(`--only 没匹配到任何条目：${values.only}`)
  }

  const reviewCount = Object.keys(review).length
  console.log(`provider ${provider.name} / ${provider.model}　并发 ${concurrency}`)
  if (reviewCount) console.log(`预览页标记了 ${reviewCount} 项需要重跑，将带强化指令重生成`)
  console.log('')

  const ctx = { paths, provider, state, review, concurrency }

  // 分两阶段：eyes 依赖对应的 face 图、mouth 依赖 face/normal，
  // 必须等 face 全部落盘后再跑
  const faceJobs = jobs.filter((j) => j.kind === 'face')
  const derivedJobs = jobs.filter((j) => j.kind !== 'face')

  const empty = { ok: 0, skipped: 0, failed: [] as string[], kinds: new Set<FailureKind>() }
  const r1 = faceJobs.length ? await runPhase('[1/2] 表情', faceJobs, ctx) : empty
  const r2 = derivedJobs.length ? await runPhase('[2/2] 闭眼与嘴型', derivedJobs, ctx) : empty

  const failed = [...r1.failed, ...r2.failed]
  const kinds = new Set([...r1.kinds, ...r2.kinds])
  console.log('')
  console.log(`完成 ${r1.ok + r2.ok} 项，跳过 ${r1.skipped + r2.skipped} 项，失败 ${failed.length} 项`)

  if (!failed.length) {
    console.log('\n下一步：npm run sprite:preview　—— 逐张 diff 检查一致性，挑出漂了的标记重跑')
    return
  }

  console.log('\n失败清单（重跑本命令会自动补，已完成的不会重复计费）：')
  for (const f of failed) console.log(`  · ${f}`)

  // 内容拦截是唯一「重跑一百遍也没用」的失败：底图本身过不了审，
  // 所有差分都基于它，只能回去换底图
  if (kinds.has('blocked')) {
    console.log('\n★ 出现内容审核拦截。这类失败重跑无效 —— 差分全部基于同一张底图，底图过不了审就全过不了。')
    console.log('  常见原因：底图角色衣着过少、或生成时角色描述太模糊导致模型画出了人体基础模板。')
    console.log('  解法：重新跑底图，在 --desc 里写清楚服装，并务必带上 --ref 参考图：')
    console.log('    npx tsx scripts/sprite/base.ts --ref <参考图> --desc "……，穿着<具体服装描述>"')
  }
  if (kinds.has('quota')) {
    console.log('\n· 有条目撞到限流。降低并发再跑：npx tsx scripts/sprite/gen.ts --concurrency 1')
  }
}

main().catch((err) => {
  const detail = err instanceof ProviderError ? err.detail : undefined
  console.error(`\n✗ ${err instanceof Error ? err.message : String(err)}`)
  if (detail) console.error(`  详情：${detail}`)
  if (err instanceof ProviderError) {
    console.error(`  分类：${refineKind(err.kind, detail)}　—— 详细排查跑 npm run sprite:diagnose`)
  }
  process.exitCode = 1
})
