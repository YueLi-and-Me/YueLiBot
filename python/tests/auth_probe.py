"""
只验一件事：没有 token 时，每个端点分别返回什么。

上一轮 boot_probe 只测了 /health，就得出「HTTP 鉴权没生效」的结论，
那是过度概括。这个探针逐个端点测，用事实说话。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
REAL_DB = Path(os.environ["APPDATA"]) / "yueli-bot" / "data" / "memory.db"

# (method, path, 需要的 body)
ENDPOINTS = [
    ("GET", "/health", None),
    ("GET", "/diary", None),
    ("GET", "/observability", None),
    ("POST", "/chat/send", {"text": "探针"}),
    ("POST", "/chat/interrupt", None),
    ("POST", "/platform/foreground", {"process": "probe.exe"}),
    ("GET", "/debug/trace", None),
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="yueli_auth_"))
    if REAL_DB.exists():
        shutil.copy2(REAL_DB, tmp / "memory.db")

    config_path = tmp / "config.toml"
    config_path.write_text("[llm]\nprovider = \"ark\"\n", encoding="utf-8")

    token = "auth-probe-token-xyz"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [PY, "-m", "yueli", "--data-dir", str(tmp),
         "--config-path", str(config_path), "--token", token],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )

    port = None
    deadline = time.time() + 30
    try:
        while time.time() < deadline and port is None:
            line = proc.stdout.readline()
            if not line:
                break
            m = re.search(r"YUELI_PORT=(\d+)", line)
            if m:
                port = int(m.group(1))
        if port is None:
            print("[auth] 后端未启动")
            return 1

        base = f"http://127.0.0.1:{port}"
        for _ in range(40):
            try:
                httpx.get(f"{base}/health", timeout=1.0)
                break
            except Exception:
                time.sleep(0.25)

        print(f"[auth] 端口 {port}\n")
        print(f"{'端点':<28} {'无 token':<12} {'错 token':<12} {'对 token'}")
        print("-" * 66)

        leaks = []
        for method, path, body in ENDPOINTS:
            row = []
            for label, hdr in (
                ("none", {}),
                ("wrong", {"Authorization": "Bearer totally-wrong"}),
                ("right", {"Authorization": f"Bearer {token}"}),
            ):
                try:
                    if method == "GET":
                        r = httpx.get(f"{base}{path}", headers=hdr, timeout=10.0)
                    else:
                        r = httpx.post(f"{base}{path}", headers=hdr, json=body or {}, timeout=10.0)
                    row.append(r.status_code)
                except Exception as exc:
                    row.append(type(exc).__name__)

            print(f"{path:<28} {str(row[0]):<12} {str(row[1]):<12} {row[2]}")

            # /health 故意开放（supervisor 存活探测用），其余必须 401
            if path != "/health":
                if row[0] != 401:
                    leaks.append(f"{path} 无 token 返回 {row[0]}，应为 401")
                if row[1] != 401:
                    leaks.append(f"{path} 错 token 返回 {row[1]}，应为 401")

        print()
        if leaks:
            print(f"[auth] 真实泄漏 {len(leaks)} 项：")
            for l in leaks:
                print(f"  ✗ {l}")
            return 1

        print("[auth] 除 /health 外全部要求 token ✓")
        print("[auth] /health 开放是刻意设计：supervisor 用它做存活探测，且不返回任何数据")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
