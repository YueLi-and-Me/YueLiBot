/**
 * 月璃匿名安装统计的服务端，运行在 Cloudflare Workers 上，数据落在 D1。
 *
 * 对外只有三个路由与一个定时任务：
 *  - POST /register   下发 UUID 并落库，客户端首次启动时调用一次
 *  - POST /heartbeat  更新 last_seen 与三个上报字段
 *  - GET  /stats      特权读取聚合数据，供开发者的 /inst 命令绘图
 *  - Cron             每日写一行快照，折线图的历史维度全靠它
 *
 * 依赖关系：客户端在 src/core/runtime/telemetry.py，表结构在同目录
 * schema.sql，绑定与定时配置在 wrangler.toml。
 *
 * 用 JavaScript 而非 TypeScript：仓库 tsconfig 的 include 只列了
 * scripts / electron / webui / tests 四处，新目录不在其中。写成 .ts 会是
 * 「看着有类型、其实没进 typecheck」的状态，比纯 JS 更容易骗人。
 *
 * 隐私约束在代码层面的落地：全文不读取请求方 IP。注册限流用内存计数，
 * 不落库也不写日志——一旦为了限流把 IP 存下来，「表里没有 IP 列」这条
 * 结构约束就退化成一句口头承诺。
 */

/** 在线判定窗口：last_seen 落在最近 24 小时内算存活。单位毫秒。 */
const ONLINE_WINDOW_MS = 24 * 60 * 60 * 1000
/** 三个上报字段各自的字符数上限。超限截断而非拒绝：遥测不值得为一个畸形版本号丢掉整次心跳。 */
const FIELD_MAX_LENGTH = 64
/** /stats 返回的版本条目上限，其余合并为「其它」，避免图例被长尾撑爆。 */
const VERSION_TOP_N = 10
/** daily 序列的默认与最大天数，用于夹取 ?days= 参数。 */
const DEFAULT_DAYS = 30
const MAX_DAYS = 365
/** 同一限流桶在窗口内允许的注册次数，及窗口长度（毫秒）。 */
const REGISTER_LIMIT = 10
const REGISTER_WINDOW_MS = 60 * 60 * 1000

/**
 * 注册限流的内存计数表。键为 Cloudflare 机房标识，值为该窗口内的计数与重置时刻。
 *
 * 这是刻意做弱的限流：
 * - 现象：同一机房的高频注册会被挡下，跨机房或换网络即可绕过。
 * - 原因：严格限流需按 IP 计数并跨实例共享，那意味着 IP 必须落库或进 KV。
 * - 后果：装机量被小幅刷高的代价，远小于为此存下所有访问者 IP；不要为了
 *   「限得更准」把 IP 引入任何持久化路径。
 *
 * @type {Map<string, {count: number, resetAt: number}>}
 */
const registerBuckets = new Map()

/**
 * 判断本次注册请求是否应被限流。
 *
 * 计数保存在模块级 Map 中，随 Worker 实例回收而清空，不同机房各算各的。
 *
 * @param {Request} request 入站请求，仅读取 cf.colo。
 * @returns {boolean} 超出配额时为 true，调用方应返回 429。
 */
function rateLimited(request) {
  const key = request.cf?.colo ?? 'unknown'
  const now = Date.now()
  const bucket = registerBuckets.get(key)
  if (bucket === undefined || now > bucket.resetAt) {
    registerBuckets.set(key, { count: 1, resetAt: now + REGISTER_WINDOW_MS })
    return false
  }
  bucket.count += 1
  return bucket.count > REGISTER_LIMIT
}

/**
 * 把客户端上报的字段裁成可安全落库的字符串：剔除控制字符并截断到长度上限。
 *
 * 按码点而非字节遍历，中文版本号不会被切成半个字符。
 *
 * @param {unknown} value 客户端传来的原始值，非字符串一律视为缺失。
 * @returns {string} 长度不超过 FIELD_MAX_LENGTH 的字符串，缺失时为空串。
 */
function sanitize(value) {
  if (typeof value !== 'string') return ''
  let out = ''
  for (const ch of value) {
    const code = ch.codePointAt(0)
    if (code === undefined || code < 32 || code === 127) continue
    out += ch
    if (out.length >= FIELD_MAX_LENGTH) break
  }
  return out
}

/**
 * 构造 JSON 响应。
 *
 * @param {unknown} body 可被 JSON.stringify 序列化的对象。
 * @param {number} status HTTP 状态码，默认 200。
 * @returns {Response} 带 UTF-8 Content-Type 的响应。
 */
function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  })
}

/**
 * 首次注册：生成 UUID 并写入 installs 表。
 *
 * 不读取请求体。客户端本就不发送任何内容，读了等于凭空开一个可被塞入任意
 * 数据的入口；uuid 由服务端生成，客户端无从指定。
 *
 * @param {Request} request 入站请求。
 * @param {{DB: D1Database}} env Worker 绑定。
 * @returns {Promise<Response>} 200 携带新 uuid，或限流时的 429。
 */
async function handleRegister(request, env) {
  if (rateLimited(request)) return json({ error: 'rate_limited' }, 429)
  const uuid = crypto.randomUUID()
  const now = Date.now()
  await env.DB.prepare(
    'INSERT INTO installs (uuid, first_seen, last_seen) VALUES (?, ?, ?)',
  ).bind(uuid, now, now).run()
  return json({ uuid })
}

/**
 * 心跳：刷新 last_seen 并覆盖三个上报字段。
 *
 * UUID 不在表中时返回 403，客户端据此删除本地身份文件并在下一轮重新注册；
 * 这条路径在数据库被清空或重建后把老客户端拉回统计范围。
 *
 * @param {Request} request 入站请求，UUID 取自 Client-UUID 头。
 * @param {{DB: D1Database}} env Worker 绑定。
 * @returns {Promise<Response>} 204 无内容；缺少 UUID 头 400；未知 UUID 403。
 */
async function handleHeartbeat(request, env) {
  const uuid = request.headers.get('Client-UUID')
  if (!uuid) return json({ error: 'missing_uuid' }, 400)

  let body = {}
  try {
    body = await request.json()
  } catch {
    // 正文损坏不构成拒绝心跳的理由：三个字段全空仍能记录「这个安装还活着」，
    // 而这正是在线数唯一依赖的信息。
    body = {}
  }

  const result = await env.DB.prepare(
    'UPDATE installs SET last_seen = ?, app_version = ?, os_type = ?, python_version = ? WHERE uuid = ?',
  ).bind(
    Date.now(),
    sanitize(body.app_version),
    sanitize(body.os_type),
    sanitize(body.python_version),
    uuid,
  ).run()

  if (result.meta.changes === 0) return json({ error: 'unknown_uuid' }, 403)
  // 单向上报：响应体不回写任何指令或配置，心跳不是控制通道。
  return new Response(null, { status: 204 })
}

/**
 * 聚合读取，供开发者的 /inst 命令绘图。
 *
 * 鉴权失败一律返回同一个 401，不区分「服务端未配置令牌」与「令牌不匹配」，
 * 避免响应差异泄露服务端状态。
 *
 * @param {Request} request 入站请求，令牌取自 Authorization 头。
 * @param {{DB: D1Database, STATS_TOKEN?: string}} env Worker 绑定。
 * @returns {Promise<Response>} 200 携带聚合结果，或 401。
 */
async function handleStats(request, env) {
  const expected = env.STATS_TOKEN
  const provided = request.headers.get('Authorization')
  // 令牌未配置时同样拒绝：空令牌放行等于把聚合端点裸露在公网。
  if (!expected || provided !== 'Bearer ' + expected) {
    return json({ error: 'unauthorized' }, 401)
  }

  const url = new URL(request.url)
  const requested = Number.parseInt(url.searchParams.get('days') ?? '', 10)
  const days = Number.isFinite(requested)
    ? Math.min(Math.max(requested, 1), MAX_DAYS)
    : DEFAULT_DAYS

  const aliveSince = Date.now() - ONLINE_WINDOW_MS
  const installs = await env.DB.prepare('SELECT COUNT(*) AS n FROM installs').first('n')
  const online = await env.DB.prepare(
    'SELECT COUNT(*) AS n FROM installs WHERE last_seen > ?',
  ).bind(aliveSince).first('n')
  const versionRows = await env.DB.prepare(
    'SELECT app_version AS version, COUNT(*) AS count FROM installs WHERE last_seen > ? GROUP BY app_version ORDER BY count DESC',
  ).bind(aliveSince).all()

  const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 10)
  const dailyRows = await env.DB.prepare(
    'SELECT day, installs, online, versions FROM daily_stats WHERE day >= ? ORDER BY day ASC',
  ).bind(since).all()

  return json({
    installs: installs ?? 0,
    online: online ?? 0,
    versions: foldVersions(versionRows.results ?? []),
    daily: (dailyRows.results ?? []).map((row) => ({
      day: row.day,
      installs: row.installs,
      online: row.online,
      versions: parseVersions(row.versions),
    })),
  })
}

/**
 * 把版本行折叠到前 VERSION_TOP_N 条，其余合并为「其它」。
 *
 * @param {Array<{version: string, count: number}>} rows 已按 count 降序排列的版本行。
 * @returns {Array<{version: string, count: number}>} 至多 VERSION_TOP_N + 1 条。
 */
function foldVersions(rows) {
  if (rows.length <= VERSION_TOP_N) return rows
  const head = rows.slice(0, VERSION_TOP_N)
  const rest = rows.slice(VERSION_TOP_N).reduce((sum, row) => sum + row.count, 0)
  return head.concat([{ version: '其它', count: rest }])
}

/**
 * 解析每日快照中的版本 JSON，损坏数据按空构成处理。
 *
 * 快照是历史数据，无法重算；一行坏掉不应让整条 /stats 失败。
 *
 * @param {string} raw daily_stats.versions 落库的 JSON 文本。
 * @returns {Record<string, number>} 版本号到存活实例数的映射。
 */
function parseVersions(raw) {
  try {
    const parsed = JSON.parse(raw)
    return parsed !== null && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

/**
 * 写入当日快照，由 Cron 在 UTC 00:05 触发。
 *
 * versions 只统计存活实例，与 /stats 的口径一致：装过一次再没开过的实例若
 * 计入，版本分布会变成一堆永不下降的线，而这张图要回答的是「谁还在用」。
 * 同日重复触发按 day 主键覆盖，补跑不会产生重复行。
 *
 * @param {{DB: D1Database}} env Worker 绑定。
 * @returns {Promise<void>}
 */
async function writeDailySnapshot(env) {
  const aliveSince = Date.now() - ONLINE_WINDOW_MS
  const day = new Date().toISOString().slice(0, 10)
  const installs = await env.DB.prepare('SELECT COUNT(*) AS n FROM installs').first('n')
  const online = await env.DB.prepare(
    'SELECT COUNT(*) AS n FROM installs WHERE last_seen > ?',
  ).bind(aliveSince).first('n')
  const rows = await env.DB.prepare(
    'SELECT app_version AS version, COUNT(*) AS count FROM installs WHERE last_seen > ? GROUP BY app_version',
  ).bind(aliveSince).all()

  const versions = {}
  for (const row of rows.results ?? []) {
    if (row.version) versions[row.version] = row.count
  }

  await env.DB.prepare(
    'INSERT INTO daily_stats (day, installs, online, versions) VALUES (?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET installs = excluded.installs, online = excluded.online, versions = excluded.versions',
  ).bind(day, installs ?? 0, online ?? 0, JSON.stringify(versions)).run()
}

export default {
  /**
   * HTTP 入口，按方法与路径分派到三个处理函数。
   *
   * @param {Request} request 入站请求。
   * @param {{DB: D1Database, STATS_TOKEN?: string}} env Worker 绑定。
   * @returns {Promise<Response>} 路由未命中时为 404。
   */
  async fetch(request, env) {
    const { pathname } = new URL(request.url)
    if (request.method === 'POST' && pathname === '/register') {
      return handleRegister(request, env)
    }
    if (request.method === 'POST' && pathname === '/heartbeat') {
      return handleHeartbeat(request, env)
    }
    if (request.method === 'GET' && pathname === '/stats') {
      return handleStats(request, env)
    }
    return new Response('Not Found', { status: 404 })
  },

  /**
   * Cron 入口，触发表达式见 wrangler.toml 的 triggers.crons。
   *
   * @param {ScheduledController} _controller 触发信息；当前只有一条 Cron 表达式，无需区分。
   * @param {{DB: D1Database}} env Worker 绑定。
   * @returns {Promise<void>}
   */
  async scheduled(_controller, env) {
    await writeDailySnapshot(env)
  },
}
