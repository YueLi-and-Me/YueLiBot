/**
 * 为观察窗口暴露只读诊断 IPC bridge。
 *
 * 渲染层只能请求快照和追踪数据，不能修改聊天、配置或平台状态；通道定义统一
 * 使用 electron/shared/ipc.ts。
 */
import { contextBridge, ipcRenderer } from 'electron'
import { IPC, type ObservabilityBridge } from '../shared/ipc.ts'

/** 观察面板是开发者的只读观察窗，不给渲染层任何状态修改能力。 */
const bridge: ObservabilityBridge = {
  read: () => ipcRenderer.invoke(IPC.Observability),
  readTrace: (since) => ipcRenderer.invoke(IPC.DebugTrace, since ?? 0),
  openSettings: () => ipcRenderer.send(IPC.OpenSettings),
}

contextBridge.exposeInMainWorld('observability', bridge)
