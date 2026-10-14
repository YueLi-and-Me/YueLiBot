/** WebUI 表单与宿主工具测试共用项目路径映射。 */
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  resolve: { alias: { '@': fileURLToPath(new URL('./webui/src', import.meta.url)) } },
  esbuild: { jsx: 'automatic' },
})
