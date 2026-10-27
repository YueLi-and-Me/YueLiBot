/**
 * 市场页面的纯展示规则：筛选目录、限制版本按钮、提取后端手动命令、解析商店一键安装链接。
 * 市场页和工具页共同依赖本文件；不重新实现后端兼容性判定。
 */
import type { MarketAction, MarketPlugin, MarketVersion } from './types'

/**
 * 名称、说明和 id 不区分大小写搜索；非空分类同时过滤。
 * @param plugins 后端已按来源合并的目录。
 * @param search 查询文本，空白表示全部。
 * @param tag 精确分类，空字符串表示全部。
 * @returns 保留原有顺序的匹配项。
 */
export function filterPlugins(plugins: MarketPlugin[], search: string, tag: string): MarketPlugin[] {
  const needle = search.trim().toLocaleLowerCase()
  return plugins.filter(item => (!tag || item.tags.includes(tag)) && `${item.name} ${item.description} ${item.id}`.toLocaleLowerCase().includes(needle))
}

/**
 * 撤回版本不可安装；兼容模式和用户明确开启的强制兼容仍可选择。
 * @param version 带后端兼容判定的版本。
 * @returns 该版本是否允许作为操作目标。
 */
export function installable(version: MarketVersion): boolean {
  return !version.yanked && version.decision !== '不兼容'
}

/**
 * 提取后端提供的完整命令，界面不自行拼接环境路径。
 * @param note 后端依赖说明原文。
 * @returns 完整单行命令；未提供时返回空字符串。
 */
export function manualCommand(note: string): string {
  return note.split('\n').find(line => line.startsWith('uv pip install ')) ?? ''
}

/**
 * 商店一键安装链接解析结果：没有链接参数、无法处理的原因，或待用户确认的操作。
 * 原因文字只取自面板自己的索引，不回显链接里的任何文本。
 */
export type InstallIntent =
  | { kind: 'none' }
  | { kind: 'error'; reason: string }
  | { kind: 'confirm'; action: Exclude<MarketAction, 'uninstall'>; pluginId: string; version: string }

/**
 * 解析商店「一键安装」带来的 `?install=<id>&version=<版本>`，只决定弹哪个确认框，不执行任何操作。
 *
 * 链接只用来定位插件与版本：插件名、说明、兼容判定全部取自面板已加载的索引，
 * 链接中的文字不进入界面。否则任何网页都能伪造一个链接，诱导部署者确认来路不明的内容。
 *
 * @param search 地址栏查询串，形如 `?install=a.b&version=1.0.0`。
 * @param plugins 面板从已配置索引源取得的插件目录。
 * @returns 无参数时为 none；插件或版本不存在、已撤回、不兼容、已是同版本时为 error；
 *   其余按安装状态返回安装、升级或交给市场管理的确认请求。
 */
export function readInstallIntent(search: string, plugins: MarketPlugin[]): InstallIntent {
  const params = new URLSearchParams(search)
  const id = params.get('install')
  const version = params.get('version')
  if (id === null && version === null) return { kind: 'none' }
  if (!id || !version) return { kind: 'error', reason: '链接缺少插件 ID 或版本号。' }
  const plugin = plugins.find(item => item.id === id)
  if (!plugin) return { kind: 'error', reason: '已配置的索引源里没有这个插件。' }
  const record = plugin.versions.find(item => item.version === version)
  if (!record) return { kind: 'error', reason: `索引里没有「${plugin.name}」的这个版本。` }
  if (record.yanked) return { kind: 'error', reason: `「${plugin.name}」${record.version} 已撤回，不能安装。` }
  if (!installable(record)) return { kind: 'error', reason: record.reason || `「${plugin.name}」${record.version} 与当前月璃版本不兼容。` }
  const target = { pluginId: plugin.id, version: record.version }
  if (plugin.installed.kind === '未装') return { kind: 'confirm', action: 'install', ...target }
  if (plugin.installed.kind === '市场安装') {
    if (plugin.installed.version === record.version) return { kind: 'error', reason: `「${plugin.name}」${record.version} 已经安装。` }
    return { kind: 'confirm', action: 'upgrade', ...target }
  }
  if (!plugin.installed.market_id_available) return { kind: 'error', reason: `已有手动安装的「${plugin.name}」，不能交给市场管理。` }
  return { kind: 'confirm', action: 'takeover', ...target }
}

/**
 * 去掉一键安装参数，保留其他查询参数；处理过的链接刷新后不再弹框。
 * @param search 地址栏查询串。
 * @returns 去掉 install 与 version 后的查询串，为空时返回空字符串。
 */
export function stripInstallParams(search: string): string {
  const params = new URLSearchParams(search)
  params.delete('install')
  params.delete('version')
  const rest = params.toString()
  return rest ? `?${rest}` : ''
}
