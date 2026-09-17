#!/usr/bin/env python3
"""探针3：确认 52177 端点到底是什么页面，以及页面内 iframe/子上下文结构。"""

import asyncio
import json
import re
import subprocess

import websockets


def discover():
    out = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True,
                         errors="replace").stdout
    res = []
    for line in out.splitlines():
        if "小程序开发者工具" not in line:
            continue
        for m in re.finditer(r"ws(?:%3D|=)(?:localhost|127\.0\.0\.1)(?:%3A|:)(\d+)", line):
            token = re.search(r"_token(?:%3D|=)([0-9a-fA-F-]{8,})", line)
            res.append((int(m.group(1)), token.group(1) if token else None))
    seen, out2 = set(), []
    for p, t in res:
        if p in seen:
            continue
        seen.add(p)
        out2.append((p, t))
    return out2


class CDP:
    def __init__(self, ws):
        self.ws, self._id, self.events = ws, 0, []

    async def send(self, method, **params):
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        return self._id

    async def call(self, method, timeout=4, **params):
        mid = await self.send(method, **params)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_event_loop().time()
            if left <= 0:
                return {"__timeout__": True}
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except asyncio.TimeoutError:
                return {"__timeout__": True}
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg
            if "method" in msg:
                self.events.append(msg)

    async def evaluate(self, expr, timeout=4, **kw):
        r = await self.call("Runtime.evaluate", timeout=timeout, expression=expr,
                            returnByValue=True, awaitPromise=True, **kw)
        res = r.get("result", {}).get("result", {})
        if res.get("value") is not None:
            return res["value"]
        if res.get("description") is not None:
            return res["description"]
        return f"<{(r.get('error') or r)}>"


INTROSPECT = {
    "URL": "location.href",
    "title": "document.title",
    "有 my?": "typeof my",
    "iframe 数": "document.querySelectorAll('iframe').length",
    "iframe src 列表": "JSON.stringify([...document.querySelectorAll('iframe')].map(f=>f.src||f.getAttribute('src')||'(about:blank)'))",
    "body 前 200": "document.body ? document.body.innerHTML.slice(0,200) : 'no body'",
    "全局键(含 miniprogram 表象)": "JSON.stringify(Object.keys(window).filter(k=>/mini|app|alipay|service|view|game|sumi|kaitian/i.test(k)).slice(0,40))",
    "webview 标签": "JSON.stringify([...document.querySelectorAll('webview,web-view,iframe')].map(e=>e.tagName))",
    "主脚本 src": "JSON.stringify([...document.scripts].map(s=>s.src).slice(0,8))",
}


async def main():
    cands = discover()
    print(f"【发现 {len(cands)} 个 ws 端点】 {cands}\n")
    if not cands:
        return
    port, token = cands[0]
    uri = f"ws://127.0.0.1:{port}/?_token={token}"
    print(f"▶ 连接 {uri}\n")
    async with websockets.connect(uri, proxy=None, open_timeout=5,
                                  max_size=64 * 1024 * 1024) as ws:
        c = CDP(ws)
        await c.send("Runtime.enable")
        await c.send("Target.setDiscoverTargets", discover=True)
        await c.send("Page.enable")
        await asyncio.sleep(0.5)

        for k, v in INTROSPECT.items():
            print(f"  {k:24} = {str(await c.evaluate(v))[:220]}")

        print("\n【Target.getTargets】")
        r = await c.call("Target.getTargets")
        print(f"  {json.dumps(r, ensure_ascii=False)[:900]}")

        print("\n【Target.getBrowserContexts / Page.getFrameTree】")
        print(f"  FrameTree: {json.dumps(await c.call('Page.getFrameTree'), ensure_ascii=False)[:600]}")

        print("\n【收到的事件类型】")
        from collections import Counter
        print("  " + str(Counter(e['method'] for e in c.events).most_common(25)))


if __name__ == "__main__":
    asyncio.run(main())
