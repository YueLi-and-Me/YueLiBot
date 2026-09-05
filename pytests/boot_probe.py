"""
端到端启动探针。该模块会拉起 uvicorn 子进程，
再通过 HTTP 和 WebSocket 验证真实启动链路：
  · 进程活着并在 stdout 宣告端口与 token
  · 启动早期打出人可读的 WebUI 入口框，监听后再打出就绪确认框
  · 无 token 的 /health 放行（存活探测用），带 token 也必须通
  · 只读端点能返回结构
  · WS 子协议握手成功，且能收到推送事件

进程内单元测试无法覆盖 uvicorn、鉴权中间件和 WebSocket 握手的组合行为，
因此本探针保留真实子进程和网络连接。

用法（仓库根目录下）：

    uv run python pytests/boot_probe.py            # 空库
    uv run python pytests/boot_probe.py --real-db  # 复制一份真实库再启动

不进四条门：它要拉真进程、占端口，跑一次十几秒。四条门绿了不代表它绿，
反过来也一样——它挂过一次就是启动链路真的坏了。
"""

from __future__ import annotations

import argparse
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

# 配置样板从既有测试里取，不在这里再抄一份。
# 现象：本探针原先自己写 `[llm] provider = "ark"` 的单文件 config.toml，
#       配置改成四文件目录之后它一启动就死在「不是配置目录」，而这条错误只有真跑它才看得见。
# 原因：探针不进四条门，自带的配置样板一旦与加载器分家，就没有任何东西会提醒它过期了。
# 后果：改动配置结构时，这里必须跟着一起红，不能再让它悄悄烂掉。
#
# 这里改的是 sys.path[0] 而不是往前插一项：以脚本方式运行时 sys.path[0] 是脚本所在的
# pytests/，而 pytests/ 下有个 platform 包，会把标准库的 platform 顶掉。
# 现象是 import 链深处莫名其妙地报 module 'platform' has no attribute
# 'python_implementation'（structlog → rich → attr 才用到它），排查起来完全指不到这里。
sys.path[0] = str(REPO)
from pytests.core.test_split_config import _write_split_config  # noqa: E402


def _wait_runtime(proc: subprocess.Popen, log: Path, timeout: float = 25.0) -> tuple[int, str]:
    """轮询日志直到端口与 token 都已公告，或进程先退出。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"后端在宣告端口前就退出了 rc={proc.returncode}\n{log.read_text('utf-8', 'replace')}")
        if log.exists():
            output = log.read_text("utf-8", "replace")
            port_match = re.search(r"YUELI_PORT=(\d+)", output)
            token_match = re.search(r"YUELI_TOKEN=([0-9a-f]{64})", output)
            if port_match and token_match:
                return int(port_match.group(1)), token_match.group(1)
        time.sleep(0.3)
    raise TimeoutError(f"等端口超时\n{log.read_text('utf-8', 'replace')}")


def _check_webui_entry(output: str, port: int, token: str, runtime_path: Path) -> list[str]:
    """校验启动顶部入口框和监听后的 WebUI 就绪框。

    :param output: 后端 stdout 全文。
    :param port: 实际监听端口。
    :param token: 本次进程的认证 token。
    :param runtime_path: 运行时凭据文件路径。

    :return: 失败描述列表；全部通过时为空。

    入口框是给人看的，不带 ``YUELI_`` 前缀——supervisor 只吞协议行，其余原样转发。
    启动顶部先显示“正在初始化”的地址与 token；监听建立后再显示“WebUI 已就绪”，
    这样用户既能尽早复制入口，也不会把预告误判为已经可访问。
    """
    failures: list[str] = []
    expected = [
        f"WebUI 观察面板：http://127.0.0.1:{port}",
        f"登录 token：{token}",
        f"token 每次启动重新生成，也可从 {runtime_path} 读取",
    ]
    for line in expected:
        if line not in output:
            failures.append(f"stdout 里没有这一行：{line}")
    ready_index = output.find("YUELI_READY=1")
    entry_index = output.find(expected[0])
    if ready_index >= 0 and entry_index >= 0 and entry_index > ready_index:
        failures.append("WebUI 入口框没有出现在启动顶部，用户要等到后端完成后才能找到")
    ready_entry = "╭─ WebUI 已就绪 "
    ready_entry_index = output.find(ready_entry)
    if ready_index >= 0 and ready_entry_index >= 0 and ready_entry_index < ready_index:
        failures.append("WebUI 已就绪框出现在 YUELI_READY=1 之前")
    if ready_index >= 0 and ready_entry_index < 0:
        failures.append("stdout 里没有 WebUI 已就绪框")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="月璃后端启动探针")
    parser.add_argument(
        "--real-db",
        action="store_true",
        help="复制一份真实 memory.db 再启动；默认用空库，避免把全部对话留在临时目录",
    )
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="yueli_probe_"))
    data_dir = tmp / "data"
    data_dir.mkdir()

    if args.real_db:
        # 数据目录随 runtimePaths 落在仓库下的 data/，不再是漫游目录。
        real = REPO / "data" / "memory.db"
        if real.exists():
            shutil.copy2(real, data_dir / "memory.db")
            print(f"[probe] 用真实库副本 {real.stat().st_size} bytes")
        else:
            print(f"[probe] 指定了 --real-db 但 {real} 不存在，改用空库")
    else:
        print("[probe] 用空库（--real-db 可改为复制真实库）")

    config_dir = tmp / "config"
    _write_split_config(config_dir)
    runtime_path = data_dir / "runtime" / "backend.json"

    log = tmp / "backend.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            # 端口交给系统分配：写死 7999 会在桌宠已经开着的时候直接撞端口退出。
            [sys.executable, "bot.py", "--data-dir", str(data_dir),
             "--config-path", str(config_dir), "--port", "0"],
            cwd=str(REPO), stdout=fh, stderr=subprocess.STDOUT,
        )

    failures: list[str] = []
    try:
        port, token = _wait_runtime(proc, log)
        print(f"[probe] 端口 {port}")
        base = f"http://127.0.0.1:{port}"

        # 给 uvicorn 一点时间真正开始 accept
        for _ in range(40):
            try:
                httpx.get(f"{base}/health", headers={"Authorization": f"Bearer {token}"}, timeout=1.0)
                break
            except Exception:
                time.sleep(0.25)

        # 1/2. /health 不要求 token，供 Supervisor 做存活探测，且不返回任何敏感
        #      数据）——这条和 auth_probe.py 的结论一致，逐端点鉴权细节见那边。
        r = httpx.get(f"{base}/health", timeout=5.0)
        if r.status_code != 200:
            failures.append(f"无 token 的 /health 应该放行（存活探测用），却返回 {r.status_code}")
        else:
            print("[probe] /health 对无 token 开放（刻意设计）✓")

        # 3. 正确 token 必须通
        r = httpx.get(f"{base}/health", headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
        if r.status_code != 200:
            failures.append(f"带正确 token 的 /health 返回 {r.status_code}: {r.text[:200]}")
        else:
            print(f"[probe] 带 token /health = {r.json()} ✓")

        # 4. 只读端点：diary / streams / observability 必须能返回结构。
        #    observability 自 M1.4 分区之后按 stream 取，streamId 必填；
        #    这里从 /streams 现查而不是写死 1，顺带把 /streams 也验了。
        auth_header = {"Authorization": f"Bearer {token}"}
        desktop_stream_id: int | None = None
        r = httpx.get(f"{base}/streams", headers=auth_header, timeout=15.0)
        if r.status_code != 200:
            failures.append(f"/streams 返回 {r.status_code}: {r.text[:300]}")
        else:
            listed = r.json().get("streams", [])
            desktop_stream_id = next(
                (item["id"] for item in listed if item.get("platform") == "desktop"),
                None,
            )
            print(f"[probe] /streams = {len(listed)} 个，desktop={desktop_stream_id} ✓")
            if desktop_stream_id is None:
                failures.append("/streams 里没有 desktop 分支，观察面板将无 stream 可选")

        probes = [("/diary", {})]
        if desktop_stream_id is not None:
            probes.append(("/observability", {"streamId": desktop_stream_id}))
        for path, params in probes:
            r = httpx.get(f"{base}{path}", headers=auth_header, params=params, timeout=15.0)
            if r.status_code != 200:
                failures.append(f"{path} 返回 {r.status_code}: {r.text[:300]}")
            else:
                keys = sorted(r.json().keys()) if isinstance(r.json(), dict) else "非对象"
                print(f"[probe] {path} = {keys} ✓")

        # 5. WebUI 入口框和就绪确认：人要靠它打开面板，只有真跑一次才验得到
        entry_failures = _check_webui_entry(
            log.read_text("utf-8", "replace"), port, token, runtime_path,
        )
        failures.extend(entry_failures)
        if not entry_failures:
            print("[probe] WebUI 入口框齐全且早于就绪确认 ✓")

        # 6. WS 握手 + 推送
        try:
            from websockets.sync.client import connect as ws_connect
            has_ws = True
        except ImportError:
            has_ws = False
            print("[probe] 跳过 WS（websockets 未安装）")

        if has_ws:
            # M2.3 起 `client` 是必填查询参数，缺了会被直接拒；
            # 少了它，下面那条「正确 token 也连不上」会被误读成鉴权坏了。
            ws_url = f"ws://127.0.0.1:{port}/ws?client=desktop"

            # 错 token 的子协议必须握手失败
            try:
                with ws_connect(ws_url, subprotocols=["yueli-wrong"], open_timeout=5):
                    failures.append("错 token 的 WS 竟然握手成功")
            except Exception:
                print("[probe] 错 token WS 被拒 ✓")

            # 缺 client 参数同样必须被拒，否则分支隔离形同虚设
            try:
                with ws_connect(
                    f"ws://127.0.0.1:{port}/ws",
                    subprotocols=[f"yueli-{token}"],
                    open_timeout=5,
                ):
                    failures.append("缺 client 参数的 WS 竟然握手成功")
            except Exception:
                print("[probe] 缺 client 参数的 WS 被拒 ✓")

            # 正确 token 必须握手成功并能收到东西
            try:
                with ws_connect(
                    ws_url,
                    subprotocols=[f"yueli-{token}"],
                    open_timeout=8,
                ) as ws:
                    print("[probe] WS 握手成功 ✓")
                    # 触发一次 sleep 状态推送
                    httpx.post(
                        f"{base}/platform/foreground",
                        headers={"Authorization": f"Bearer {token}"},
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
        # 失败时保留现场：临时目录里有完整后端日志和刚才那个数据目录。
        print(f"[probe] 失败 {len(failures)} 项，现场保留在 {tmp}：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    # token 是当前进程的主凭据，成功路径不留副本在临时目录里。
    shutil.rmtree(tmp, ignore_errors=True)
    print("[probe] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
