#!/usr/bin/env python3
"""探针2：摸清 CDP 端点的上下文结构、能力面（截图/控制台/网络/DOM）。

用法: python3 ws_probe2.py
"""

import asyncio
import json
import re
import subprocess

import websockets


def discover():
    out = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True,
                         errors="replace").stdout
    for line in out.splitlines():
        if "devtools-frontend" not in line or "小程序开发者工具" not in line:
            continue
        port = re.search(r"ws(?:%3D|=)localhost(?:%3A|:)(\d+)", line)
        token = re.search(r"_token(?:%3D|=)([0-9a-fA-F-]{8,})", line)
        if port:
            return int(port.group(1)), (token.group(1) if token else None)
    return None, None


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self.events = []

    async def send(self, method, **params):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        return mid

    async def call(self, method, timeout=5, **params):
        mid = await self.send(method, **params)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_event_loop().time()
            if left <= 0:
                return {"__timeout__": True, "method": method}
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except asyncio.TimeoutError:
                return {"__timeout__": True, "method": method}
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg
            if "method" in msg:
                self.events.append(msg)

    async def drain(self, seconds):
        deadline = asyncio.get_event_loop().time() + seconds
        while True:
            left = deadline - asyncio.get_event_loop().time()
            if left <= 0:
                return
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except asyncio.TimeoutError:
                return
            msg = json.loads(raw)
            if "method" in msg:
                self.events.append(msg)


def brief(obj, n=300):
    return json.dumps(obj, ensure_ascii=False)[:n]


JS_PROBES = {
    "my 全局": "typeof my",
    "getCurrentPages": "typeof getCurrentPages",
    "document": "typeof document",
    "window": "typeof window",
    "my.getSystemInfoSync": "JSON.stringify(my.getSystemInfoSync && my.getSystemInfoSync())",
    "页面栈": "JSON.stringify((getCurrentPages()||[]).map(p=>p.route||p.__route__))",
    "页面 data 键": "JSON.stringify(Object.keys((getCurrentPages()||[{data:{}}]).slice(-1)[0].data||{}))",
    "当前页面 DOM 节点数": "document.querySelectorAll('*').length",
    "axml 自定义标签": "JSON.stringify([...new Set([...document.querySelectorAll('*')].map(e=>e.tagName.toLowerCase()))].slice(0,25))",
    "my API 抽样": "JSON.stringify(Object.keys(my).filter(k=>/^(get|set|navigate|request|alert|show|hide)/.test(k)).slice(0,40))",
}


async def main():
    port, token = discover()
    if not port:
        print("⚠️ 未发现 IDE 调试端口")
        return
    uri = f"ws://127.0.0.1:{port}/?_token={token}"
    print(f"▶ {uri}\n")

    async with websockets.connect(uri, proxy=None, open_timeout=5, max_size=64 * 1024 * 1024) as ws:
        c = CDP(ws)

        # 1. 全部协议域
        r = await c.call("Schema.getDomains")
        names = [d["name"] for d in r.get("result", {}).get("domains", [])]
        print(f"【协议域 {len(names)} 个】\n  {', '.join(names)}\n")

        # 2. 打开各域，收集 executionContextCreated
        for m in ("Runtime.enable", "Log.enable", "Network.enable", "Page.enable",
                  "DOM.enable", "Debugger.enable", "Target.setDiscoverTargets"):
            try:
                await c.send(m, discover=True) if m.startswith("Target") else await c.send(m)
            except Exception as e:
                print(f"  {m} 发送失败 {e}")
        await c.drain(2.5)

        ctxs = [e["params"]["context"] for e in c.events
                if e["method"] == "Runtime.executionContextCreated"]
        print(f"【执行上下文 {len(ctxs)} 个】")
        for x in ctxs:
            print(f"  id={x.get('id')} name={x.get('name')!r} origin={x.get('origin')!r} "
                  f"aux={x.get('auxData')}")

        targets = [e for e in c.events if e["method"].startswith("Target.")]
        if targets:
            print("\n【Target 事件】")
            for e in targets[:6]:
                print(f"  {e['method']} {brief(e.get('params'), 220)}")

        # 3. JS 能力探测
        print("\n【JS 环境探测】")
        for label, expr in JS_PROBES.items():
            r = await c.call("Runtime.evaluate", timeout=4,
                             expression=expr, returnByValue=True, awaitPromise=True)
            res = r.get("result", {}).get("result", {})
            val = res.get("value", res.get("description", r.get("error") or r))
            print(f"  {label:22} = {str(val)[:180]}")

        # 4. 截图能力
        print("\n【截图能力】")
        for m in ("Page.captureScreenshot", "Page.getLayoutMetrics"):
            r = await c.call(m, timeout=6, **({"format": "png"} if "capture" in m else {}))
            if "result" in r:
                data = r["result"].get("data", "")
                print(f"  ✅ {m} 返回 {len(data)} 字符 base64")
            else:
                print(f"  ❌ {m} {brief(r.get('error') or r, 200)}")

        # 5. 控制台事件是否可达（手动 eval 一条 console.log 看能不能抓到）
        print("\n【控制台事件】")
        c.events.clear()
        await c.call("Runtime.evaluate", expression="console.log('__probe_marker__', 42)")
        await c.drain(1.5)
        for e in c.events:
            if e["method"] in ("Runtime.consoleAPICalled", "Log.entryAdded"):
                print(f"  ✅ {e['method']} {brief(e['params'], 220)}")
        if not any(e["method"] in ("Runtime.consoleAPICalled", "Log.entryAdded") for e in c.events):
            print("  ⚠️ 未捕获到 console 事件")

        print("\n【累计事件类型统计】")
        from collections import Counter
        for k, v in Counter(e["method"] for e in c.events).most_common(20):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
