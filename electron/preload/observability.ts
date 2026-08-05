import { contextBridge, ipcRenderer } from 'electron'
import { IPC, type ObservabilityBridge } from '../shared/ipc.ts'

/** 观察面板是开发者的只读观察窗，不给渲染层任何状态修改能力。 */
const bridge: ObservabilityBridge = {
  read: () => ipcRenderer.invoke(IPC.Observability),
  readTrace: (since) => ipcRenderer.invoke(IPC.DebugTrace, since ?? 0),
  openSettings: () => ipcRenderer.send(IPC.OpenSettings),
}

contextBridge.exposeInMainWorld('observability', bridge)
