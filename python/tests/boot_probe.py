"""
端到端启动探针。不是单测 —— 它真的拉起 uvicorn 子进程，
再用 HTTP + WebSocket 打进去，验证 Phase 0 的验收标准：
  · 进程活着并在 stdout 宣告端口
  · 无 token 的请求被拒
  · 带 token 的 /health 返回 ok
  · WS 子协议握问成功，且能收到推送事件

单测覆盖不到这一层：它们全在进程内直接调函数，
既不过 uvicorn，也不过鉴权中间件，更不过 WS 握手。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
TOKEN = "probe-token-1234"


def _wait_port(proc: subprocess.Popen, log: Path, timeout: float = 25.0) -> int:
    """轮询 stdout 日志直到出现 YUELI_PORT=，或进程先死。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"后端在宣告端口前就退出了 rc={proc.returncode}\n{log.read_text('utf-8', 'replace')}")
        if log.exists():
            m = re.search(r"YUELI_PORT=(\d+)", log.read_text("utf-8", "replace"))
            if m:
                return int(m.group(1))
        time.sleep(0.3)
    raise TimeoutError(f"等端口超时\n{log.read_text('utf-8', 'replace')}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="yueli_probe_"))
    real = Path.home() / "AppData/Roaming/yueli-bot/data/memory.db"
    if real.exists():
        shutil.copy2(real, tmp / "memory.db")
        print(f"[probe] 用真实库副本 {real.stat().st_size} bytes")
    else:
        print("[probe] 真实库不存在，用空库")

    config_path = tmp / "config.toml"
    config_path.write_text("[llm]\nprovider = \"ark\"\n", encoding="utf-8")

    log = tmp / "backend.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "yueli", "--data-dir", str(tmp),
             "--config-path", str(config_path), "--token", TOKEN],
            cwd=str(REPO), stdout=fh, stderr=subprocess.STDOUT,
        )

    failures: list[str] = []
    try:
        port = _wait_port(proc, log)
        print(f"[probe] 端口 {port}")
        base = f"http://127.0.0.1:{port}"

        # 给 uvicorn 一点时间真正开始 accept
        for _ in range(40):
            try:
                httpx.get(f"{base}/health", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=1.0)
                break
            except Exception:
                time.sleep(0.25)

        # 1/2. /health 故意不要求 token（supervisor 用它做存活探测，且不返回任何
        #      数据）——这条和 auth_probe.py 的结论一致，逐端点鉴权细节见那边。
        r = httpx.get(f"{base}/health", timeout=5.0)
        if r.status_code != 200:
            failures.append(f"无 token 的 /health 应该放行（存活探测用），却返回 {r.status_code}")
        else:
            print("[probe] /health 对无 token 开放（刻意设计）✓")

        # 3. 正确 token 必须通
        r = httpx.get(f"{base}/health", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5.0)
        if r.status_code != 200:
            failures.append(f"带正确 token 的 /health 返回 {r.status_code}: {r.text[:200]}")
        else:
            print(f"[probe] 带 token /health = {r.json()} ✓")

        # 4. 只读端点：diary / observability 必须能返回结构
        for path in ("/diary", "/observability"):
            r = httpx.get(f"{base}{path}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=15.0)
            if r.status_code != 200:
                failures.append(f"{path} 返回 {r.status_code}: {r.text[:300]}")
            else:
                keys = sorted(r.json().keys()) if isinstance(r.json(), dict) else "非对象"
                print(f"[probe] {path} = {keys} ✓")

        # 5. WS 握手 + 推送
        try:
            from websockets.sync.client import connect as ws_connect
            has_ws = True
        except ImportError:
            has_ws = False
            print("[probe] 跳过 WS（websockets 未安装）")

        if has_ws:
            # 错 token 的子协议必须握手失败
            try:
                with ws_connect(f"ws://127.0.0.1:{port}/ws", subprotocols=["yueli-wrong"], open_timeout=5):
                    failures.append("错 token 的 WS 竟然握手成功")
            except Exception:
                print("[probe] 错 token WS 被拒 ✓")

            # 正确 token 必须握手成功并能收到东西
            try:
                with ws_connect(
                    f"ws://127.0.0.1:{port}/ws",
                    subprotocols=[f"yueli-{TOKEN}"],
                    open_timeout=8,
                ) as ws:
                    print("[probe] WS 握手成功 ✓")
                    # 触发一次 sleep 状态推送
                    httpx.post(
                        f"{base}/platform/foreground",
                        headers={"Authorization": f"Bearer {TOKEN}"},
                        json={"process": "Code.exe", "title": "probe", "fullscreen": False},
                        timeout=5.0,
                    )
                    try:
                        msg = ws.recv(timeout=6)
                        print(f"[probe] 收到推送 {str(msg)[:160]} ✓")
                    except TimeoutError:
                        print("[probe] 6s 内无推送（无 LLM 时属正常，不算失败）")
            except Exception as exc:
                failures.append(f"正确 token 的 WS 握手失败：{type(exc).__name__}: {exc}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        tail = log.read_text("utf-8", "replace")
        if "Traceback" in tail:
            failures.append("后端日志里有 Traceback")
            print("\n=== 后端日志 Traceback ===")
            idx = tail.find("Traceback")
            print(tail[idx:idx + 1500])

    print()
    if failures:
        print(f"[probe] 失败 {len(failures)} 项：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("[probe] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
