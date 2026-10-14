/** 字段来自 src/core/config/mcp_webui.py；tests/tools-fields.cjs 用 TS 编译器逐层对照。 */
export interface PluginTool {
  name: string
  description: string
  capabilities: string[]
  visibility: string
  timeout_ms: number
}
export interface PluginEntry {
  directory: string
  id: string
  name: string
  version: string
  description: string
  author: string
  source: string
  min_version: string
  max_version: string
  decision: string
  reason: string
  tools: PluginTool[]
  config_schema: Record<string, unknown>
  values: Record<string, unknown>
  secretSet: Record<string, boolean>
  configEditable: boolean
}
export interface PluginsResponse { plugins: PluginEntry[] }
export interface McpToolSettings {
  expose: boolean
  readonly: boolean
  alias: string
  description: string
  daily_limit: number
}
export interface McpServerSettings {
  name: string
  original_name: string
  enabled: boolean
  transport: 'http' | 'stdio'
  url: string
  api_key: string
  api_key_provider: string
  apiKeySet: boolean
  clear_api_key: boolean
  headers: Record<string, string>
  headerSet: Record<string, boolean>
  use_proxy: boolean
  command: string
  args: string[]
  env: Record<string, string>
  envSet: Record<string, boolean>
  cwd: string
  call_timeout_seconds: number
  tools: Record<string, McpToolSettings>
}
export interface McpConfigResponse {
  enabled: boolean
  servers: McpServerSettings[]
  providers: string[]
}
export interface McpDiscoveredTool {
  name: string
  title: string
  description: string
  input_schema: Record<string, unknown>
  read_only: boolean | null
}
export interface McpToolStatus extends McpDiscoveredTool {
  listed: boolean
  expose: boolean
  readonly: boolean
  alias: string
  override_description: string
  daily_limit: number
  used_today: number
  exposed: boolean
  reason: string
}
export interface McpChange { kind: string; tool: string; old_head: string; new_head: string }
export interface McpServerStatus {
  name: string
  state: string
  protocol_version: string
  server_name: string
  server_version: string
  last_error: string
  changes: McpChange[]
  tools: McpToolStatus[]
}
export interface McpStatusResponse { servers: McpServerStatus[] }
export interface McpTestResponse {
  ok: boolean
  protocol_version: string
  server_name: string
  server_version: string
  tools: McpDiscoveredTool[]
  error: string
}
