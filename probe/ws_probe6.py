#!/usr/bin/env python3
"""探针6：验证逻辑层(appx worker 上下文)与视图层能力，并实测点击派发。"""

import asyncio
import base64
import json
import re
import subprocess

import websockets


def discover():
    out = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True,
                         errors="replace").stdout
    for line in out.splitlines():
        if "小程序开发者工具" in line:
            m = re.search(r"ws(?:%3D|=)(?:localhost|127\.0\.0\.1)(?:%3A|:)(\d+)", line)
            t = re.search(r"_token(?:%3D|=)([0-9a-fA-F-]{8,})", line)
            if m:
                return int(m.group(1)), (t.group(1) if t else None)
    return None, None


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

    async def call(self, method, session=None, timeout=8, **params):
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

    async def ev(self, expr, ctx=None, session=None, timeout=6):
        r = await self.call("Runtime.evaluate", session=session, timeout=timeout,
                            expression=expr, returnByValue=True, awaitPromise=True,
                            **({"contextId": ctx} if ctx else {}))
        res = r.get("result", {}).get("result", {})
        if res.get("value") is not None:
            return res["value"]
        if res.get("description"):
            return res["description"]
        return f"<{json.dumps(r, ensure_ascii=False)[:200]}>"


LOGIC_PROBES = {
    "my": "typeof my",
    "getCurrentPages": "typeof getCurrentPages",
    "getApp": "typeof getApp",
    "页面栈": "JSON.stringify((getCurrentPages()||[]).map(p=>p.route||p.__route__))",
    "当前页 data 键": "JSON.stringify(Object.keys((getCurrentPages()||[{data:{}}]).slice(-1)[0].data||{}).slice(0,20))",
    "systemInfo": "(function(){try{return JSON.stringify(my.getSystemInfoSync())}catch(e){return 'ERR '+e.message}})()",
    "storage 数": "(function(){try{return JSON.stringify(Object.keys(my.getStorageSync()||{}).length)}catch(e){return 'ERR '+e.message}})()",
    "navigateTo 可用": "(function(){try{return typeof my.navigateTo}catch(e){return 'ERR'}})()",
    "request 可用": "(function(){try{return typeof my.request}catch(e){return 'ERR'}})()",
}


async def main():
    port, token = discover()
    async with websockets.connect(f"ws://127.0.0.1:{port}/?_token={token}", proxy=None,
                                  open_timeout=5, max_size=128 * 1024 * 1024) as ws:
        c = CDP(ws)
        infos = (await c.call("Target.getTargets"))["result"]["targetInfos"]
        sim = next(t for t in infos if "Simulator" in (t.get("title") or ""))
        sid = (await c.call("Target.attachToTarget", targetId=sim["targetId"],
                            flatten=True))["result"]["sessionId"]
        for m in ("Runtime.enable", "Page.enable", "Log.enable", "DOM.enable",
                  "Network.enable", "Debugger.enable"):
            await c.send(m, session=sid)
        await asyncio.sleep(1.2)

        # 帧 → 上下文映射
        ft = (await c.call("Page.getFrameTree", session=sid))["result"]["frameTree"]
        frames = {}
        def walk(n):
            frames[n["frame"]["id"]] = n["frame"].get("url", "")
            for ch in n.get("childFrames") or []:
                walk(ch)
        walk(ft)
        ctxs = [e["params"]["context"] for e in c.events
                if e["method"] == "Runtime.executionContextCreated"]

        print("【帧 ↔ 上下文 映射】")
        logic_ctx = view_ctx = None
        for x in ctxs:
            if not x.get("auxData", {}).get("isDefault"):
                continue
            url = frames.get(x["auxData"].get("frameId", ""), "?")
            kind = ("逻辑层" if "af-appx" in url else
                    "视图层" if "61218" in url or "appx" in url else "其他")
            print(f"  ctx={x['id']:<3} [{kind}] {url[:88]}")
            if "af-appx" in url and logic_ctx is None:
                logic_ctx = x["id"]
            if ("61218" in url or "index.html" in url) and view_ctx is None:
                view_ctx = x["id"]

        if logic_ctx:
            print(f"\n【逻辑层能力探测 ctx={logic_ctx}】")
            for k, v in LOGIC_PROBES.items():
                print(f"  {k:16} = {str(await c.ev(v, ctx=logic_ctx))[:200]}")
        else:
            print("\n⚠️ 未识别出逻辑层上下文")

        # 视图层 DOM 与样式
        print(f"\n【视图层探测 ctx={view_ctx}】")
        for k, v in {
            "自定义标签": "JSON.stringify([...new Set([...document.querySelectorAll('*')].map(e=>e.tagName.toLowerCase()))].slice(0,30))",
            "节点总数": "document.querySelectorAll('*').length",
            "可见文本前 150": "(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,150)",
        }.items():
            print(f"  {k:14} = {str(await c.ev(v, ctx=view_ctx))[:280]}")

        # 截图裁到模拟器可视区
        print("\n【截图(带 clip)】")
        r = await c.call("Page.captureScreenshot", session=sid, timeout=20, format="png")
        if r.get("result", {}).get("data"):
            data = base64.b64decode(r["result"]["data"])
            open("/tmp/sim_shot2.png", "wb").write(data)
            print(f"  ✅ {len(data)} 字节 /tmp/sim_shot2.png")

        # 点击实测：点模拟器里的“分类”tab
        print("\n【点击派发实测】")
        get_rect = """(function(){
            const els=[...document.querySelectorAll('*')];
            const hit=els.filter(e=>e.children.length===0 && (e.textContent||'').trim()==='分类');
            if(!hit.length) return null;
            const r=hit[0].getBoundingClientRect();
            return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});
        })()"""
        rect = await c.ev(get_rect, ctx=view_ctx)
        print(f"  “分类”元素位置 = {rect}")
        if rect and rect != "null":
            p = json.loads(rect)
            await c.send("Input.dispatchMouseEvent", session=sid, type="mousePressed",
                         x=p["x"], y=p["y"], button="left", clickCount=1)
            await c.send("Input.dispatchMouseEvent", session=sid, type="mouseReleased",
                         x=p["x"], y=p["y"], button="left", clickCount=1)
            await asyncio.sleep(1.5)
            after = await c.ev("(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,120)", ctx=view_ctx)
            path = await c.ev("(document.body.innerText||'').match(/页面路径\\s*(\\S+)/)?.[1] || '未知'", ctx=view_ctx)
            print(f"  点击后页面路径 = {path}")
            print(f"  点击后文本     = {str(after)[:150]}")


if __name__ == "__main__":
    asyncio.run(main())
