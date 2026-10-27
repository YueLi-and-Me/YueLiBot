/**
 * 市场响应契约，与后端 plugin_market.webui 序列化字段保持一致。
 * 页面、操作组件和 usePluginMarket 共用这些类型，不包含第三方 HTML。
 */
export interface MarketSource { name: string; index_url: string }
export interface MarketVersion {
  version: string
  commit: string
  host_application: { min_version: string; max_version: string }
  python_dependencies: string[]
  downloads: string[]
  sha256: string
  size: number
  published_at: string
  yanked: boolean
  decision: string
  reason: string
  downloads_count: number | null
}
export interface MarketPlugin {
  id: string
  name: string
  description: string
  author: string
  license: string
  repository: string
  tags: string[]
  source: MarketSource
  versions: MarketVersion[]
  installed: { kind: '未装' | '市场安装' | '手动安装'; version: string; upgrade_to: string | null; market_id_available: boolean }
  downloads_count: number | null
  store_url: string | null
}
export interface MarketSnapshot {
  plugins: MarketPlugin[]
  sources: MarketSource[]
  conflicts: { id: string; kept: string; ignored: string }[]
  errors: { source: string; index_url: string; reason: string }[]
  auto_install: boolean
  backup_root: string
}
export interface MarketResult { ok: boolean; detail: string; restart_required: boolean; dependency_note: string }
export type MarketAction = 'install' | 'upgrade' | 'uninstall' | 'takeover'
