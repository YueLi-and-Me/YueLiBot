/** 验证市场筛选、版本可选性、依赖命令提取与商店一键安装链接，避免界面提供不可执行的操作。 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { filterPlugins, installable, manualCommand, readInstallIntent, stripInstallParams } from '../webui/src/features/plugin-market/view'
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

describe('商店一键安装链接', () => {
  const version = (value: string, extra: Partial<MarketVersion> = {}) => ({ version: value, yanked: false, decision: '正常', reason: '', ...extra }) as MarketVersion
  const plugin = (installed: Partial<MarketPlugin['installed']> = {}) => ({
    id: 'yueli.web-search', name: '联网搜索',
    versions: [version('0.2.5'), version('0.2.4', { yanked: true }), version('0.3.0', { decision: '不兼容', reason: '需要月璃 0.3.0 及以上' })],
    installed: { kind: '未装', version: '', upgrade_to: null, market_id_available: false, ...installed },
  }) as MarketPlugin
  it('没有链接参数时什么都不做，未安装的插件只给出安装确认请求', () => {
    expect(readInstallIntent('', [plugin()])).toEqual({ kind: 'none' })
    expect(readInstallIntent('?install=yueli.web-search&version=0.2.5', [plugin()]))
      .toEqual({ kind: 'confirm', action: 'install', pluginId: 'yueli.web-search', version: '0.2.5' })
  })
  it('插件不在索引、缺参数、版本不存在、撤回或不兼容时给出原因，且不回显链接里的文字', () => {
    const cases = ['?install=<b>伪造插件</b>&version=0.2.5', '?install=yueli.web-search', '?install=yueli.web-search&version=请联系客服',
      '?install=yueli.web-search&version=0.2.4', '?install=yueli.web-search&version=0.3.0']
    const reasons = cases.map(search => readInstallIntent(search, [plugin()]))
    expect(reasons.every(item => item.kind === 'error')).toBe(true)
    for (const item of reasons) {
      const reason = item.kind === 'error' ? item.reason : ''
      expect(reason).not.toMatch(/伪造|客服|<b>/)
    }
    expect(reasons[4]).toEqual({ kind: 'error', reason: '需要月璃 0.3.0 及以上' })
  })
  it('已安装同 id 时按安装方式改为升级或交给市场管理，同版本不再弹框', () => {
    const link = '?install=yueli.web-search&version=0.2.5'
    expect(readInstallIntent(link, [plugin({ kind: '市场安装', version: '0.2.4' })])).toMatchObject({ kind: 'confirm', action: 'upgrade' })
    expect(readInstallIntent(link, [plugin({ kind: '市场安装', version: '0.2.5' })]).kind).toBe('error')
    expect(readInstallIntent(link, [plugin({ kind: '手动安装', version: '0.2.5', market_id_available: true })])).toMatchObject({ kind: 'confirm', action: 'takeover' })
    expect(readInstallIntent(link, [plugin({ kind: '手动安装', version: '0.2.5', market_id_available: false })]).kind).toBe('error')
  })
  it('处理后去掉链接参数并保留其他查询参数', () => {
    expect(stripInstallParams('?install=a.b&version=1.0.0')).toBe('')
    expect(stripInstallParams('?tab=x&install=a.b&version=1.0.0')).toBe('?tab=x')
  })
  it('登录页原地渲染且不改写地址栏，链接参数留到登录完成后处理', () => {
    const source = (path: string) => readFileSync(new URL(`../webui/src/${path}`, import.meta.url), 'utf8')
    expect(source('app/App.tsx')).toContain('if (!authenticated) return <LoginScreen />')
    for (const path of ['features/auth/LoginScreen.tsx', 'hooks/use-auth.tsx']) {
      expect(source(path)).not.toMatch(/useNavigate|navigate\(|history\.|location\.(href|replace|assign)/)
    }
  })
})
