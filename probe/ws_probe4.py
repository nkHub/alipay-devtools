#!/usr/bin/env python3
"""探针4：枚举 IDE 的全部 CDP target，逐个挂载，找到小程序逻辑层/视图层上下文。"""

import asyncio
import json
import re
import subprocess

import websockets


def discover():
    out = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True,
                         errors="replace").stdout
    seen, res = set(), []
    for line in out.splitlines():
        if "小程序开发者工具" not in line:
            continue
        for m in re.finditer(r"ws(?:%3D|=)(?:localhost|127\.0\.0\.1)(?:%3A|:)(\d+)", line):
            p = int(m.group(1))
            if p in seen:
                continue
            seen.add(p)
            t = re.search(r"_token(?:%3D|=)([0-9a-fA-F-]{8,})", line)
            res.append((p, t.group(1) if t else None))
    return res


class CDP:
    def __init__(self, ws):
        self.ws, self._id, self.events = ws, 0, []

    async def send(self, method, session=None, **params):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        await self.ws.send(json.dumps(msg))
        return self._id

    async def call(self, method, session=None, timeout=5, **params):
        mid = await self.send(method, session=session, **params)
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


SHORT = re.compile(r"^file:///Applications/[^/]*/")


def shorten(u, n=110):
    u = SHORT.sub("file://…/", u or "")
    return u[:n]


async def main():
    for port, token in discover():
        uri = f"ws://127.0.0.1:{port}/?_token={token}"
        try:
            async with websockets.connect(uri, proxy=None, open_timeout=3,
                                          max_size=64 * 1024 * 1024) as ws:
                c = CDP(ws)
                r = await c.call("Target.getTargets", timeout=5)
                infos = r.get("result", {}).get("targetInfos")
                if infos is None:
                    print(f"\n── port {port}: 无 Target 域 ({json.dumps(r)[:120]})")
                    continue
                print(f"\n{'='*78}\n端口 {port}：{len(infos)} 个 target\n{'='*78}")
                for t in infos:
                    print(f"  [{t.get('type'):<12}] {t.get('title','')[:36]:<38} {shorten(t.get('url'))}")

                # 对每个 page/worker target 试挂载，探测是不是小程序上下文
                print(f"\n  ── 逐个挂载探测 Runtime 上下文 ──")
                for t in infos:
                    if t.get("type") not in ("page", "iframe", "worker",
                                             "service_worker", "shared_worker", "webview"):
                        continue
                    ar = await c.call("Target.attachToTarget", timeout=5,
                                      targetId=t["targetId"], flatten=True)
                    sid = ar.get("result", {}).get("sessionId")
                    if not sid:
                        print(f"  ✗ 挂载失败 {t['title'][:28]}: {json.dumps(ar)[:120]}")
                        continue
                    await c.send("Runtime.enable", session=sid)
                    r1 = await c.call("Runtime.evaluate", session=sid, timeout=4,
                                      expression="[typeof my, typeof getCurrentPages, "
                                                 "typeof document, document.title].join('|')",
                                      returnByValue=True)
                    val = r1.get("result", {}).get("result", {}).get("value")
                    print(f"  {'✅' if val else '·'} {t.get('title','')[:30]:<32} "
                          f"type={t.get('type'):<10} => {val if val else json.dumps(r1)[:90]}")
                    if val and "function" in str(val):
                        # 疑似小程序逻辑层，深挖
                        for expr, label in [
                            ("JSON.stringify(my.getSystemInfoSync())", "systemInfo"),
                            ("JSON.stringify((getCurrentPages()||[]).map(p=>p.route||p.__route__))", "页面栈"),
                            ("typeof my.getStorageSync==='function'", "storage"),
                        ]:
                            r2 = await c.call("Runtime.evaluate", session=sid, timeout=4,
                                              expression=expr, returnByValue=True)
                            v2 = r2.get("result", {}).get("result", {}).get("value")
                            print(f"        {label} = {str(v2)[:200]}")
        except Exception as e:
            print(f"\n── port {port} 连接失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
