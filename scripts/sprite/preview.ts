/**
 * 素材预览与挑图。
 *
 *   npm run sprite:preview
 *
 * 一致性漂移肉眼扫不出来 —— 头发丝、衣褶、腰线的细微变化，
 * 单看一张图完全正常，切换时才发现在抖。
 * 所以这页的核心不是「把图列出来」，而是 diff：
 * 跟底图逐像素比，改动区域高亮，模型偷偷改了衣服立刻现形。
 *
 * 标记为不合格的条目写进 raw/review.json，
 * 下次 npm run sprite:gen 会带着具体问题描述重跑那几张。
 */
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'
import { parseArgs } from 'node:util'
import { BLINK_TARGETS, EXPRESSIONS, MOUTH_SHAPES } from './config.ts'
import { readJson, writeJson, type ReviewList } from './manifest.ts'
import { CharPaths } from './paths.ts'

const { values } = parseArgs({
  options: {
    name: { type: 'string', default: 'yueli' },
    port: { type: 'string', default: '5178' },
  },
})

const paths = new CharPaths(values.name!)
const PORT = Number(values.port) || 5178

interface Item {
  key: string
  kind: string
  id: string
  label: string
  url: string
}

function buildItems(): Item[] {
  const items: Item[] = []
  for (const e of EXPRESSIONS) {
    items.push({ key: `face/${e.id}`, kind: 'face', id: e.id, label: e.cn, url: `/raw/face/${e.id}.png` })
  }
  for (const id of BLINK_TARGETS) {
    const e = EXPRESSIONS.find((x) => x.id === id)
    if (e) items.push({ key: `eyes/${e.id}`, kind: 'eyes', id: e.id, label: `${e.cn}·闭眼`, url: `/raw/eyes/${e.id}.png` })
  }
  for (const m of MOUTH_SHAPES) {
    items.push({ key: `mouth/${m.id}`, kind: 'mouth', id: m.id, label: `嘴型·${m.cn}`, url: `/raw/mouth/${m.id}.png` })
  }
  return items
}

const MIME: Record<string, string> = { '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp' }

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://localhost:${PORT}`)

  try {
    if (url.pathname === '/') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
      return res.end(PAGE)
    }

    if (url.pathname === '/api/items') {
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' })
      return res.end(JSON.stringify({ name: values.name, items: buildItems() }))
    }

    if (url.pathname === '/api/review' && req.method === 'GET') {
      const review = await readJson<ReviewList>(paths.review, {})
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' })
      return res.end(JSON.stringify(review))
    }

    if (url.pathname === '/api/review' && req.method === 'POST') {
      const chunks: Buffer[] = []
      for await (const c of req) chunks.push(c as Buffer)
      const review = JSON.parse(Buffer.concat(chunks).toString('utf8')) as ReviewList
      await writeJson(paths.review, review)
      console.log(`  已保存重跑清单：${Object.keys(review).length} 项`)
      res.writeHead(200, { 'Content-Type': 'application/json' })
      return res.end('{"ok":true}')
    }

    if (url.pathname.startsWith('/raw/')) {
      // 路径穿越防护：解析后必须仍在 raw/ 目录内
      const target = resolve(paths.raw, url.pathname.slice('/raw/'.length))
      if (!target.startsWith(paths.raw + sep)) {
        res.writeHead(403)
        return res.end('forbidden')
      }
      const buf = await readFile(target)
      res.writeHead(200, { 'Content-Type': MIME[extname(target)] ?? 'application/octet-stream', 'Cache-Control': 'no-store' })
      return res.end(buf)
    }

    res.writeHead(404)
    res.end('not found')
  } catch (err) {
    // 图还没生成时 readFile 会失败，这是正常状态，不该刷屏
    const code = (err as NodeJS.ErrnoException)?.code
    res.writeHead(code === 'ENOENT' ? 404 : 500)
    res.end(code === 'ENOENT' ? 'not generated yet' : String(err))
  }
})

server.listen(PORT, () => {
  console.log(`\n  预览页 → http://localhost:${PORT}\n`)
  console.log('  快捷键：← → 切换　D 差异　F 闪烁　S 并排　X 标记不合格　Enter 保存')
  console.log('  标记后保存，再跑 npm run sprite:gen 会带强化指令重生成那几张。')
  console.log('\n  Ctrl-C 退出\n')
})

// ---------------------------------------------------------------------------

const PAGE = /* html */ `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>素材预览</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#14151a;color:#e6e7ea;font:14px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;border-bottom:1px solid #282a33;position:sticky;top:0;background:#14151a;z-index:10;flex-wrap:wrap}
h1{font-size:15px;font-weight:600}
.spacer{flex:1}
button{background:#22242c;color:#e6e7ea;border:1px solid #34363f;border-radius:7px;padding:6px 12px;font:inherit;cursor:pointer}
button:hover{background:#2b2d36}
button.on{background:#4a6cf7;border-color:#4a6cf7;color:#fff}
button.bad{background:#c0392b;border-color:#c0392b;color:#fff}
.hint{color:#8b8e99;font-size:12px}
main{display:grid;grid-template-columns:220px 1fr;height:calc(100vh - 53px)}
nav{overflow-y:auto;border-right:1px solid #282a33;padding:8px}
.group{color:#8b8e99;font-size:11px;text-transform:uppercase;letter-spacing:.08em;padding:12px 10px 6px}
.row{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:7px;cursor:pointer}
.row:hover{background:#1d1f26}
.row.sel{background:#4a6cf7;color:#fff}
.row .dot{width:7px;height:7px;border-radius:50%;flex:none;background:#3a3d47}
.row.marked .dot{background:#e74c3c}
.row.missing{opacity:.4}
.row .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
section{display:flex;flex-direction:column;overflow:hidden}
.stage{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:20px;
  background-image:linear-gradient(45deg,#1a1b21 25%,transparent 25%),linear-gradient(-45deg,#1a1b21 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#1a1b21 75%),linear-gradient(-45deg,transparent 75%,#1a1b21 75%);
  background-size:20px 20px;background-position:0 0,0 10px,10px -10px,-10px 0}
.stage img,.stage canvas{max-width:100%;max-height:100%;object-fit:contain;image-rendering:auto}
.side{display:flex;gap:16px;align-items:center;justify-content:center;height:100%}
.side figure{display:flex;flex-direction:column;align-items:center;gap:6px;max-height:100%}
.side figcaption{color:#8b8e99;font-size:12px}
.side img{max-height:calc(100% - 24px)}
footer{border-top:1px solid #282a33;padding:10px 20px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
select{background:#22242c;color:#e6e7ea;border:1px solid #34363f;border-radius:7px;padding:6px 10px;font:inherit}
.empty{color:#8b8e99;text-align:center;padding:40px}
</style></head><body>
<header>
  <h1>素材预览</h1>
  <button id="m-normal" class="on">原图</button>
  <button id="m-diff">差异 D</button>
  <button id="m-flicker">闪烁 F</button>
  <button id="m-side">并排 S</button>
  <label class="hint" id="thWrap" style="display:none">
    灵敏度 <input id="th" type="range" min="8" max="120" value="24" style="vertical-align:middle;width:90px">
    <span id="thVal">24</span>
  </label>
  <label class="hint" id="alignWrap" style="display:none">
    <input id="align" type="checkbox" checked style="vertical-align:middle"> 补偿位移
  </label>
  <span class="spacer"></span>
  <span class="hint" id="stat"></span>
  <button id="save">保存重跑清单 ⏎</button>
</header>
<main>
  <nav id="nav"></nav>
  <section>
    <div class="stage" id="stage"><div class="empty">载入中…</div></div>
    <footer>
      <button id="mark">标记不合格 X</button>
      <select id="reason">
        <option value="改动了服装或衣褶">改动了服装或衣褶</option>
        <option value="改动了发型或头发细节">改动了发型或头发细节</option>
        <option value="改动了姿势、手臂或手的位置">改动了姿势、手臂或手的位置</option>
        <option value="角色在画面中的位置或大小变了">角色位置/大小变了</option>
        <option value="表情画得不对，不符合要求">表情不对</option>
        <option value="画风与原图不一致">画风不一致</option>
        <option value="出现了多余的文字、水印或背景元素">出现多余元素</option>
      </select>
      <span class="hint" id="cur"></span>
    </footer>
  </section>
</main>
<script>
const S = { items: [], review: {}, idx: 0, mode: 'normal', flickerOn: false, timer: null, missing: new Set() }
const $ = (id) => document.getElementById(id)
const BASE_URL = '/raw/base.png'

async function boot() {
  const meta = await (await fetch('/api/items')).json()
  S.items = meta.items
  S.review = await (await fetch('/api/review')).json()
  // 探测哪些图还没生成，侧栏里灰掉，避免点进去一片空白
  await Promise.all(S.items.map(async (it) => {
    const r = await fetch(it.url, { method: 'HEAD' })
    if (!r.ok) S.missing.add(it.key)
  }))
  renderNav(); show(0)
}

function renderNav() {
  const groups = { face: '表情', eyes: '闭眼差分', mouth: '嘴型' }
  let html = ''
  for (const [kind, title] of Object.entries(groups)) {
    const rows = S.items.filter((i) => i.kind === kind)
    if (!rows.length) continue
    html += '<div class="group">' + title + '</div>'
    for (const it of rows) {
      const i = S.items.indexOf(it)
      const cls = ['row', i === S.idx ? 'sel' : '', S.review[it.key] ? 'marked' : '', S.missing.has(it.key) ? 'missing' : ''].join(' ')
      html += '<div class="' + cls + '" data-i="' + i + '"><span class="dot"></span><span class="nm">' + it.label + '</span></div>'
    }
  }
  $('nav').innerHTML = html
  for (const el of $('nav').querySelectorAll('.row')) el.onclick = () => show(+el.dataset.i)
  const marked = Object.keys(S.review).length
  $('stat').textContent = marked ? marked + ' 项已标记待重跑' : '未标记任何问题'
}

const load = (src) => new Promise((res, rej) => { const im = new Image(); im.onload = () => res(im); im.onerror = rej; im.src = src })

/** 取一张图的像素数据 + 主体（非白）包围盒。包围盒用来估计整体位移。 */
function pixelsOf(img, w, h) {
  const c = document.createElement('canvas'); c.width = w; c.height = h
  const ctx = c.getContext('2d', { willReadFrequently: true })
  ctx.drawImage(img, 0, 0, w, h)
  const d = ctx.getImageData(0, 0, w, h)
  let left = w, top = h, right = -1, bottom = -1
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4
      // 非白即主体。生图都是纯白背景，这比 alpha 更直接
      if (d.data[o] < 240 || d.data[o+1] < 240 || d.data[o+2] < 240) {
        if (x < left) left = x; if (x > right) right = x
        if (y < top) top = y; if (y > bottom) bottom = y
      }
    }
  }
  return { data: d.data, box: { left, top, right, bottom } }
}

/**
 * 差异热力图。
 *
 * 关键在于回答「这些差异是什么性质」，而不只是「有多少」：
 *
 *  · 整体位移 —— 模型把角色整个挪了一两像素。肉眼完全无感，
 *    逐像素比却会把整条轮廓标红，动辄十几个百分点。
 *    这类问题 sprite:process 的对齐本来就会修掉，不该拿来吓人。
 *    所以先按主体包围盒估出位移量，补偿之后再比。
 *
 *  · 按幅度分级上色 —— 二值化的红色会让「轻微重绘」和「整块换掉」
 *    看起来一模一样。暗蓝=细微，亮红=剧烈，一眼分得清。
 *
 *  · 分区统计 —— 头/身/腿三段各自的差异占比。
 *    改表情本就该只有头部变，身体一有动静立刻暴露。
 */
async function renderDiff(url) {
  const [a, b] = await Promise.all([load(BASE_URL), load(url)])
  const w = a.naturalWidth, h = a.naturalHeight
  const A = pixelsOf(a, w, h), B = pixelsOf(b, w, h)

  const compensate = $('align').checked
  const dx = compensate ? Math.round(((B.box.left + B.box.right) - (A.box.left + A.box.right)) / 2) : 0
  const dy = compensate ? B.box.top - A.box.top : 0
  const th = +$('th').value

  const out = new ImageData(w, h)
  let changed = 0, counted = 0
  // 头/身/腿：按主体包围盒的高度三等分，比按画布分准得多
  const bt = A.box.top, bh = Math.max(1, A.box.bottom - A.box.top)
  const seg = [0, 0, 0], segTotal = [0, 0, 0]

  for (let y = 0; y < h; y++) {
    const sy = y - dy
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4
      const sx = x - dx
      const grey = (A.data[o] + A.data[o+1] + A.data[o+2]) / 3 * 0.2

      if (sx < 0 || sy < 0 || sx >= w || sy >= h) {
        out.data[o] = out.data[o+1] = out.data[o+2] = grey; out.data[o+3] = 255
        continue
      }
      const p = (sy * w + sx) * 4
      const d = Math.abs(A.data[o] - B.data[p]) + Math.abs(A.data[o+1] - B.data[p+1]) + Math.abs(A.data[o+2] - B.data[p+2])

      counted++
      const band = y < bt ? -1 : Math.min(2, Math.floor(((y - bt) / bh) * 3))
      if (band >= 0) segTotal[band]++

      if (d > th) {
        changed++
        if (band >= 0) seg[band]++
        // 幅度 → 冷暖：细微差异偏蓝且暗，剧烈差异偏红且亮
        const t = Math.min(1, (d - th) / 180)
        out.data[o]   = Math.round(60 + t * 195)
        out.data[o+1] = Math.round(30 + t * 40)
        out.data[o+2] = Math.round(150 - t * 110)
        out.data[o+3] = 255
      } else {
        out.data[o] = out.data[o+1] = out.data[o+2] = grey; out.data[o+3] = 255
      }
    }
  }

  const c = document.createElement('canvas'); c.width = w; c.height = h
  c.getContext('2d').putImageData(out, 0, 0)

  const pct = (changed / counted * 100)
  const p = (i) => segTotal[i] ? (seg[i] / segTotal[i] * 100).toFixed(1) + '%' : '—'
  const shifted = dx || dy

  let verdict
  if (pct < 3) verdict = '✓ 差异很小'
  else if (+p(1).replace('%','') > 12 || +p(2).replace('%','') > 12) verdict = '⚠ 身体或腿部改动明显 —— 这是真问题，标记重跑'
  else verdict = '○ 差异集中在头部，符合预期'

  $('cur').textContent =
    S.items[S.idx].key +
    '　差异 ' + pct.toFixed(1) + '%' +
    (shifted ? '（已补偿位移 ' + dx + ',' + dy + 'px）' : '') +
    '　头 ' + p(0) + ' / 身 ' + p(1) + ' / 腿 ' + p(2) +
    '　' + verdict

  return c
}

async function show(i) {
  S.idx = (i + S.items.length) % S.items.length
  const it = S.items[S.idx]
  renderNav()
  clearInterval(S.timer); S.timer = null
  const stage = $('stage')

  if (S.missing.has(it.key)) {
    stage.innerHTML = '<div class="empty">这张还没生成<br><br>跑 npm run sprite:gen 补上</div>'
    $('cur').textContent = it.key
    return
  }

  $('cur').textContent = it.key + (S.review[it.key] ? '　已标记：' + S.review[it.key] : '')

  if (S.mode === 'diff') { stage.replaceChildren(await renderDiff(it.url)); return }

  if (S.mode === 'side') {
    stage.innerHTML = '<div class="side">' +
      '<figure><img src="' + BASE_URL + '"><figcaption>底图</figcaption></figure>' +
      '<figure><img src="' + it.url + '"><figcaption>' + it.label + '</figcaption></figure></div>'
    return
  }

  if (S.mode === 'flicker') {
    // A/B 快速交替 —— 位置偏移在闪烁下极其显眼，静态对比反而看不出来
    const img = new Image(); img.src = it.url
    stage.replaceChildren(img)
    S.flickerOn = false
    S.timer = setInterval(() => { S.flickerOn = !S.flickerOn; img.src = S.flickerOn ? BASE_URL : it.url }, 450)
    return
  }

  const img = new Image(); img.src = it.url
  stage.replaceChildren(img)
}

function setMode(m) {
  S.mode = m
  for (const k of ['normal', 'diff', 'flicker', 'side']) $('m-' + k).classList.toggle('on', k === m)
  // 灵敏度和位移补偿只在差异模式下有意义，别占着地方
  $('thWrap').style.display = m === 'diff' ? '' : 'none'
  $('alignWrap').style.display = m === 'diff' ? '' : 'none'
  show(S.idx)
}

function toggleMark() {
  const it = S.items[S.idx]
  if (S.review[it.key]) delete S.review[it.key]
  else S.review[it.key] = $('reason').value
  renderNav(); show(S.idx)
}

async function save() {
  await fetch('/api/review', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(S.review) })
  const n = Object.keys(S.review).length
  $('stat').textContent = n ? '已保存 ' + n + ' 项 —— 去跑 npm run sprite:gen' : '已保存（清单为空）'
}

$('m-normal').onclick = () => setMode('normal')
$('m-diff').onclick = () => setMode('diff')
$('m-flicker').onclick = () => setMode('flicker')
$('m-side').onclick = () => setMode('side')
$('mark').onclick = toggleMark
$('save').onclick = save
$('th').oninput = () => { $('thVal').textContent = $('th').value; if (S.mode === 'diff') show(S.idx) }
$('align').onchange = () => { if (S.mode === 'diff') show(S.idx) }

addEventListener('keydown', (e) => {
  if (e.target.tagName === 'SELECT') return
  const k = e.key.toLowerCase()
  if (k === 'arrowright' || k === 'arrowdown') { e.preventDefault(); show(S.idx + 1) }
  else if (k === 'arrowleft' || k === 'arrowup') { e.preventDefault(); show(S.idx - 1) }
  else if (k === 'd') setMode(S.mode === 'diff' ? 'normal' : 'diff')
  else if (k === 'f') setMode(S.mode === 'flicker' ? 'normal' : 'flicker')
  else if (k === 's') setMode(S.mode === 'side' ? 'normal' : 'side')
  else if (k === 'x') toggleMark()
  else if (e.key === 'Enter') save()
})

boot()
</script></body></html>`
