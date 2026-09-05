/**
 * 后端运行时令牌来源测试。
 *
 * 本模块属于 Electron 后端连接层与 Python 适配器边界的 Vitest 测试，
 * 通过读取关键源码确认 Electron 侧不再生成令牌，适配器改从 data/runtime/backend.json
 * 获取端口和凭证。测试不启动真实进程，因此只验证静态实现约束。
 *
 * 适配器插件化之后，原先单个入口文件承担的两件事拆成了两层，断言也随之分开：
 * 宿主（src/platforms/onebot11/__main__.py）只解析并注入运行时文件路径，
 * 具体读取凭证的是各协议端插件（adapters/<名>/plugin.py）。
 * 两个协议端后端互斥、同时只启用一个，但两者都必须满足同一条约束，因此逐个断言。
 */
import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'


/** 协议端适配器插件源码路径；两者互斥启用，但都必须从运行时文件取凭证。 */
const ADAPTER_PLUGIN_SOURCES = [
  'adapters/yueli-napcat-adapter/plugin.py',
  'adapters/yueli-snowluma-adapter/plugin.py',
]

describe('M5.1 后端 token 翻转', () => {
  it('后端连接层不再生成或注入 token', () => {
    const source = readFileSync('electron/main/python/backendLink.ts', 'utf8')

    expect(source).not.toContain('randomUUID')
    expect(source).not.toContain('YUELI_TOKEN:')
    expect(source).not.toContain("'--token'")
  })

  it('QQ 适配器从 backend.json 读取连接凭证', () => {
    const host = readFileSync('src/platforms/onebot11/__main__.py', 'utf8')

    // 宿主不接受 token 参数，只把运行时文件的位置传下去。
    expect(host).not.toContain("'--token'")
    expect(host).toContain("'--runtime-path'")

    for (const path of ADAPTER_PLUGIN_SOURCES) {
      const plugin = readFileSync(path, 'utf8')

      expect(plugin, path).toContain('read_backend_runtime')
      expect(plugin, path).not.toContain("'--token'")
    }
  })
})
