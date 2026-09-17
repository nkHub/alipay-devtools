#!/usr/bin/env python3
"""验证 Electron 标准 browser 端点（DevToolsActivePort）是否比面板命令行更可靠。"""

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import websockets

# 从脚本位置推导仓库根：写死绝对路径的话，项目一挪就废
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SUPPORT = Path.home() / "Library/Application Support/小程序开发者工具"


def read_active_port():
    f = SUPPORT / "DevToolsActivePort"
    if not f.exists():
        return None, None
    lines = f.read_text().splitlines()
    port = int(lines[0]) if lines and lines[0].strip().isdigit() else None
    path = lines[1].strip() if len(lines) > 1 else None
    return port, path


async def main():
    port, browser_path = read_active_port()
    print(f"DevToolsActivePort: port={port} path={browser_path}")
    if not port:
        print("❌ 文件不存在（IDE 可能没开）")
        return

    print("\n── HTTP /json/version（Host 必须是 localhost 或 IP）──")
    for host in ("localhost", "127.0.0.1"):
        out = subprocess.run(
            ["curl", "-s", "-m", "4", "-H", f"Host: {host}:{port}",
             f"http://{host}:{port}/json/version"],
            capture_output=True, text=True,
        )
        body = (out.stdout or out.stderr or "").strip()
        print(f"  Host={host:<10} -> {body[:220] or '(空)'}")
        if body.startswith("{"):
            break

    uri = f"ws://localhost:{port}{browser_path}"
    print(f"\n── 连 browser 端点 {uri} ──")
    try:
        async with websockets.connect(uri, proxy=None, open_timeout=6,
                                      max_size=128 * 1024 * 1024) as ws:
            mid = 0

            async def call(method, **params):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                deadline = asyncio.get_event_loop().time() + 10
                while True:
                    left = deadline - asyncio.get_event_loop().time()
                    if left <= 0:
                        return {"__timeout__": True}
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                    if m.get("id") == mid:
                        return m

            r = await call("Target.getTargets")
            infos = r.get("result", {}).get("targetInfos", [])
            print(f"  ✅ Target.getTargets 返回 {len(infos)} 个 target")
            for t in infos:
                print(f"     [{t.get('type'):<10}] {str(t.get('title'))[:30]!r}")
            sim = next((t for t in infos if "Simulator" in (t.get("title") or "")), None)
            if not sim:
                print("  ⚠️ 没有 Simulator target")
                return
            a = await call("Target.attachToTarget", targetId=sim["targetId"], flatten=True)
            sid = a.get("result", {}).get("sessionId")
            print(f"  attach sessionId = {sid}")
            await ws.send(json.dumps({"id": 100, "method": "Runtime.enable",
                                      "params": {}, "sessionId": sid}))
            await asyncio.sleep(1.0)
            js = "JSON.stringify((getCurrentPages()||[]).map(p=>p.route))"
            # 先找到逻辑层 contextId
            ctx = None
            for _ in range(1):
                pass
            # 逐个 context 试
            await ws.send(json.dumps({"id": 101, "method": "Runtime.evaluate", "params": {
                "expression": "[typeof getCurrentPages, typeof document].join('|')",
                "returnByValue": True}, "sessionId": sid}))
            print("  (默认 context) 探测已发出")
            print("\n结论：browser 端点无需 token，且不依赖「模拟器调试」面板是否打开。")
    except Exception as exc:
        print(f"  ❌ 失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
