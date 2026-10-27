/**
 * 市场页面的纯展示规则：筛选目录、限制版本按钮、提取后端手动命令。
 * 市场页和工具页共同依赖本文件；不重新实现后端兼容性判定。
 */
import type { MarketPlugin, MarketVersion } from './types'

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
