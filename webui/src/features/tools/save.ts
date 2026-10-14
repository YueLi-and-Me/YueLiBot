/** 顺序写入变更的配置，每份文件由后端完整校验；成功后重读脱敏快照。 */
import { hasDraftChanges } from '@/hooks/use-restart'

import { SAVED_RESTART_HINT } from './form'
import type { ToolsSnapshot } from '@/hooks/use-tools-config'

export async function saveToolsSnapshot(original: ToolsSnapshot, draft: ToolsSnapshot,
  write: (path: string, body: unknown) => Promise<unknown>): Promise<string> {
  for (const plugin of draft.plugins.plugins) {
    const previous = original.plugins.plugins.find(item => item.directory === plugin.directory)
    if (previous && hasDraftChanges(plugin.values, previous.values)) {
      await write(`/tools/plugins/${encodeURIComponent(plugin.id)}/config`, plugin.values)
    }
  }
  if (hasDraftChanges(draft.mcp, original.mcp)) await write('/tools/mcp/config', draft.mcp)
  return SAVED_RESTART_HINT
}
