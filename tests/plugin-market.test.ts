/** 验证市场筛选、版本可选性与依赖命令提取，避免界面提供不可执行的操作。 */
import { describe, expect, it } from 'vitest'
import { filterPlugins, installable, manualCommand } from '../webui/src/features/plugin-market/view'
import type { MarketPlugin, MarketVersion } from '../webui/src/features/plugin-market/types'

describe('插件市场展示规则', () => {
  const plugins = [
    { id: 'test.search', name: '搜索', description: '查找公开信息', tags: ['搜索'] },
    { id: 'test.calendar', name: '日历', description: '安排日程', tags: ['效率'] },
  ] as MarketPlugin[]
  it('名称、说明和 id 均可搜索，分类同时生效', () => {
    expect(filterPlugins(plugins, 'SEARCH', '')).toHaveLength(1)
    expect(filterPlugins(plugins, '公开', '搜索')).toHaveLength(1)
    expect(filterPlugins(plugins, '搜索', '效率')).toHaveLength(0)
    expect(filterPlugins(plugins, '', '')).toHaveLength(2)
  })
  it('拒绝撤回和不兼容版本，允许兼容模式和明确强制兼容', () => {
    expect(installable({ yanked: true, decision: '正常' } as MarketVersion)).toBe(false)
    expect(installable({ yanked: false, decision: '不兼容' } as MarketVersion)).toBe(false)
    expect(installable({ yanked: false, decision: '兼容模式' } as MarketVersion)).toBe(true)
    expect(installable({ yanked: false, decision: '强制兼容' } as MarketVersion)).toBe(true)
  })
  it('复制依赖说明中的完整命令，未提供命令时不生成虚构命令', () => {
    const command = 'uv pip install --python "D:/python.exe" --constraint "D:/constraints.txt" "respx>=0.23"'
    expect(manualCommand(`依赖缺失\n错误原文\n${command}`)).toBe(command)
    expect(manualCommand('没有手动命令')).toBe('')
  })
})
