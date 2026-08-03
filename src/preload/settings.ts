import { contextBridge, ipcRenderer } from 'electron'
import { IPC, type SettingsBridge, type YueliConfig } from '../shared/ipc.ts'

/** 设置窗口的 preload：读/写配置 + 重启后端，仅此三个通道，没有其它写入面。 */
const bridge: SettingsBridge = {
  read: () => ipcRenderer.invoke(IPC.ReadConfig) as Promise<YueliConfig>,
  save: (config) => ipcRenderer.invoke(IPC.SaveConfig, config) as Promise<{ ok: boolean; error?: string }>,
  restartBackend: () => ipcRenderer.send(IPC.RestartBackend),
}

contextBridge.exposeInMainWorld('settings', bridge)
