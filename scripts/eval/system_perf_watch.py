"""系统级性能监测：定位「打开/切换到 WebUI 时整机卡顿死机」的进程级元凶。

WebUI 页内探针（perf-probe.ts）已证明前端不是瓶颈，怀疑对象转向进程层面：
Python 后端、Electron 桌面端、浏览器在页面切换时刻的资源竞争。本脚本以 2 秒
间隔采样整机与关键进程的 CPU / 内存 / GPU，持续追加写入 CSV；整机卡死重启后，
CSV 里最后几行就是死机前的现场。

用法::

    python scripts/eval/system_perf_watch.py            # 默认写到 data/logs/system-perf.csv
    python scripts/eval/system_perf_watch.py --out out.csv --interval 2

Ctrl+C 停止。CSV 追加模式，可跨重启累计；列为::

    ts, cpu_total, mem_used_mb, mem_pct, gpu_util, gpu_mem_mb,
    python_cpu, python_mem_mb, electron_cpu, electron_mem_mb,
    chrome_cpu, chrome_mem_mb, top_process

依赖：psutil；GPU 列依赖 nvidia-smi（无 N 卡时恒为 -1，不影响其余列）。
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

import psutil

# 关注的进程名关键字（小写匹配）；chrome 同时覆盖 msedge 等 Chromium 系浏览器。
WATCHED = {
    'python': ('python', 'pythonw', 'uv'),
    'electron': ('electron', 'yueli'),
    'chrome': ('chrome', 'msedge', 'brave', 'chromium'),
}


def gpu_sample() -> tuple[int, int]:
    """读取 N 卡整机利用率与显存占用；无 nvidia-smi 时返回 (-1, -1)。

    :return: ``(GPU 利用率百分比, 显存占用 MB)``。
    """
    try:
        out = subprocess.run(
            [
                'nvidia-smi',
                '--query-gpu=utilization.gpu,memory.used',
                '--format=csv,noheader,nounits',
            ],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        util, mem = out.splitlines()[0].split(',')
        return int(util), int(mem)
    except Exception:
        return -1, -1


def group_processes() -> dict[str, tuple[float, float]]:
    """按关键字聚合各进程组的 CPU 百分比与内存占用（MB）。

    :return: 组名到 ``(cpu_percent, mem_mb)`` 的映射；``_top`` 键记录当前 CPU
        最高的进程名（排除 System Idle）。
    :remarks 排除脚本自身 PID，避免每秒全量遍历进程的自开销污染 python 组计数。
    """
    cpu: dict[str, float] = defaultdict(float)
    mem: dict[str, float] = defaultdict(float)
    top_name, top_cpu = '', 0.0
    own_pid = os.getpid()
    for proc in psutil.process_iter(['name', 'cpu_percent', 'memory_info']):
        try:
            if proc.pid == own_pid:
                continue
            name = (proc.info['name'] or '').lower()
            proc_cpu = proc.info['cpu_percent'] or 0.0
            proc_mem = (proc.info['memory_info'].rss if proc.info['memory_info'] else 0) / 1048576
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for group, keywords in WATCHED.items():
            if any(keyword in name for keyword in keywords):
                cpu[group] += proc_cpu
                mem[group] += proc_mem
                break
        # System Idle Process 的 cpu_percent 无意义，不参与 top 排名。
        if proc_cpu > top_cpu and 'idle' not in name:
            top_name, top_cpu = proc.info['name'] or '?', proc_cpu
    result = {group: (cpu[group], mem[group]) for group in WATCHED}
    result['_top'] = (top_cpu, 0.0)
    result['_top_name'] = (0.0, 0.0)
    result['_top_name_str'] = top_name  # type: ignore[assignment]
    return result


def main() -> None:
    """按间隔采样并追加写 CSV；首行写入表头，Ctrl+C 结束。

    :raises SystemExit: 参数解析失败时由 argparse 抛出。
    :remarks nvidia-smi 子进程启动较慢（Windows 上 0.5~1s+），GPU 列每 3 个采样
        周期才刷新一次，其余周期沿用旧值，保证整机/进程列接近设定间隔。
    """
    parser = argparse.ArgumentParser(description='系统级性能采样（死机取证）')
    parser.add_argument('--out', default='data/logs/system-perf.csv', help='CSV 输出路径')
    parser.add_argument('--interval', type=float, default=2.0, help='采样间隔秒数，默认 2')
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    is_new = not os.path.exists(args.out)

    # 首轮 cpu_percent 调用建立基线（返回 0），预热一次避免首行全零。
    psutil.cpu_percent()
    for proc in psutil.process_iter(['name']):
        try:
            proc.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    print(f'采样间隔 {args.interval}s，写入 {args.out}，Ctrl+C 停止')
    with open(args.out, 'a', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow([
                'ts', 'cpu_total', 'mem_used_mb', 'mem_pct', 'gpu_util', 'gpu_mem_mb',
                'python_cpu', 'python_mem_mb', 'electron_cpu', 'electron_mem_mb',
                'chrome_cpu', 'chrome_mem_mb', 'top_process',
            ])
            handle.flush()
        cycle = 0
        gpu_util, gpu_mem = -1, -1
        while True:
            time.sleep(args.interval)
            now = datetime.now()
            groups = group_processes()
            # GPU 列每 3 个周期刷新一次，摊薄 nvidia-smi 子进程启动开销。
            if cycle % 3 == 0:
                gpu_util, gpu_mem = gpu_sample()
            cycle += 1
            memory = psutil.virtual_memory()
            top_name = groups.pop('_top_name_str')
            groups.pop('_top')
            groups.pop('_top_name')
            writer.writerow([
                now.isoformat(timespec='seconds'),
                round(psutil.cpu_percent(), 1),
                round(memory.used / 1048576),
                round(memory.percent, 1),
                gpu_util, gpu_mem,
                round(groups['python'][0], 1), round(groups['python'][1]),
                round(groups['electron'][0], 1), round(groups['electron'][1]),
                round(groups['chrome'][0], 1), round(groups['chrome'][1]),
                top_name,
            ])
            handle.flush()  # 每行落盘：整机卡死时最后几行就是现场


if __name__ == '__main__':
    sys.exit(main())
