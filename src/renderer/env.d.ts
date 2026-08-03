/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 素材目录，默认 /character/fixture。换角色不用改代码。 */
  readonly VITE_CHARACTER?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
