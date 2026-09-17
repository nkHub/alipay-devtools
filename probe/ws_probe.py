#!/usr/bin/env python3
"""探针：验证支付宝小程序 IDE 模拟器调试端口(ws)的协议与鉴权。

用法:
    python3 ws_probe.py            # 自动发现端口与 token
    python3 ws_probe.py 52177      # 指定端口
"""

import asyncio
import json
import re
import subprocess
import sys

import websockets

APP_MARK = "小程序开发者工具"
# 渲染进程参数里带着调试前端 URL: ...?ws=localhost:PORT&env=kaitian&_token=XXX&...
WS_URL_RE = re.compile(r"ws(?:%3D|=)localhost(?:%3A|:)(\d+)", re.I)
TOKEN_RE = re.compile(r"_token(?:%3D|=)([0-9a-fA-F-]{8,})")


def discover() -> dict:
    """从 IDE 渲染进程命令行里解析出模拟器调试端口与 token。"""
    out = subprocess.run(
        ["ps", "-Ao", "args="], capture_output=True, text=True, errors="replace"
    ).stdout
    cands = []
    for line in out.splitlines():
        if "devtools-frontend" not in line or APP_MARK not in line:
            continue
        port = WS_URL_RE.search(line)
        token = TOKEN_RE.search(line)
        if not port:
            continue
        proj = re.search(r"project(?:%3D|=)([^&^\"']+)", line)
        cands.append(
            {
                "port": int(port.group(1)),
                "token": token.group(1) if token else None,
                "project": proj.group(1) if proj else None,
                "pid": (re.search(r"--renderer-client-id=(\d+)", line) or [None, None])[1],
            }
        )
    # 去重
    uniq, seen = [], set()
    for c in cands:
        if c["port"] in seen:
            continue
        seen.add(c["port"])
        uniq.append(c)
    return {"candidates": uniq}


async def try_connect(uri: str, label: str) -> None:
    print(f"\n{'='*72}\n▶ 尝试连接 {label}: {uri}\n{'='*72}")
    try:
        # proxy=None: macOS 系统级 SOCKS 代理会干扰 localhost 连接
        async with websockets.connect(
            uri, open_timeout=5, close_timeout=2, proxy=None
        ) as ws:
            print("✅ 握手成功")
            probes = [
                {"id": 1, "method": "Schema.getDomains"},
                {"id": 2, "method": "Browser.getVersion"},
                {"id": 3, "method": "Runtime.evaluate",
                 "params": {"expression": "1+1", "returnByValue": True}},
                {"id": 4, "method": "Runtime.enable"},
            ]
            for p in probes:
                await ws.send(json.dumps(p))
                print(f"  → {p['method']}")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                except asyncio.TimeoutError:
                    print("    ⏱ 无响应")
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    print(f"    ← 非 JSON: {str(raw)[:200]}")
                    continue
                if "result" in msg:
                    r = msg["result"]
                    s = json.dumps(r, ensure_ascii=False)[:400]
                    print(f"    ← ✅ {s}")
                elif "error" in msg:
                    print(f"    ← ❌ {json.dumps(msg['error'], ensure_ascii=False)[:300]}")
                else:
                    print(f"    ← (事件) {json.dumps(msg, ensure_ascii=False)[:200]}")

            # 抓一会儿事件，看有什么
            print("\n  ── 监听 2s 事件流 ──")
            deadline = asyncio.get_event_loop().time() + 2
            while True:
                left = deadline - asyncio.get_event_loop().time()
                if left <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=left)
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "method" in msg:
                    print(f"    · {msg['method']} {json.dumps(msg.get('params',{}), ensure_ascii=False)[:120]}")
    except Exception as e:
        print(f"❌ 失败: {type(e).__name__}: {e}")


async def main() -> None:
    info = discover()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if len(sys.argv) > 1:
        info["candidates"] = [{"port": int(sys.argv[1]), "token": None}]

    if not info["candidates"]:
        print("⚠️ 没发现候选端口——IDE 可能没打开项目")
        return

    c = info["candidates"][0]
    port, token = c["port"], c.get("token")
    await try_connect(f"ws://127.0.0.1:{port}", "根路径（无 token）")
    if token:
        await try_connect(f"ws://127.0.0.1:{port}/?_token={token}", "根路径 + token")


if __name__ == "__main__":
    asyncio.run(main())
