/**
 * 声明渲染层使用的 Vite 环境变量类型。
 *
 * 角色素材目录由构建时环境变量选择；缺失时由渲染入口使用 fixture 默认值。
 */
/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 素材目录，默认 /character/fixture。换角色不用改代码。 */
  readonly VITE_CHARACTER?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
