#!/usr/bin/env python3
"""探针7：验证交互原语 —— 逻辑层导航 / 视图层 DOM 点击 / 坐标事件派发。

结束后会把模拟器恢复到首页，避免留下脏状态。
"""

import asyncio
import json
import re
import subprocess

import websockets

HOME = "pages/index/index"
TARGET_TAB = "pages/index/classify/classify"


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
        return None


async def main():
    port, token = discover()
    async with websockets.connect(f"ws://127.0.0.1:{port}/?_token={token}", proxy=None,
                                  open_timeout=5, max_size=128 * 1024 * 1024) as ws:
        c = CDP(ws)
        infos = (await c.call("Target.getTargets"))["result"]["targetInfos"]
        sim = next(t for t in infos if "Simulator" in (t.get("title") or ""))
        sid = (await c.call("Target.attachToTarget", targetId=sim["targetId"],
                            flatten=True))["result"]["sessionId"]
        for m in ("Runtime.enable", "Page.enable", "Log.enable"):
            await c.send(m, session=sid)
        await asyncio.sleep(1.2)

        ft = (await c.call("Page.getFrameTree", session=sid))["result"]["frameTree"]
        frames = {}
        def walk(n):
            frames[n["frame"]["id"]] = n["frame"].get("url", "")
            for ch in n.get("childFrames") or []:
                walk(ch)
        walk(ft)
        ctxs = [e["params"]["context"] for e in c.events
                if e["method"] == "Runtime.executionContextCreated"]
        logic = view = top = None
        for x in ctxs:
            if not x.get("auxData", {}).get("isDefault"):
                continue
            fid = x["auxData"].get("frameId", "")
            url = frames.get(fid, "")
            if "af-appx" in url:
                logic = x["id"]
            elif "61218" in url:
                view = x["id"]
            elif fid == ft["frame"]["id"]:
                top = x["id"]
        print(f"上下文: logic={logic} view={view} top={top}\n")

        async def stack():
            return await c.ev("JSON.stringify((getCurrentPages()||[]).map(p=>p.route))", ctx=logic)

        print(f"初始页面栈 = {await stack()}")

        # ── 1. 逻辑层导航 my.navigateTo ──
        print("\n【1. 逻辑层导航 my.navigateTo】")
        r = await c.ev(f"""new Promise(res=>{{
            my.navigateTo({{url:'/pages/member/member/member',
              success:()=>res('success'), fail:e=>res('fail: '+JSON.stringify(e))}});
        }})""", ctx=logic)
        await asyncio.sleep(1.5)
        print(f"  navigateTo 结果 = {r}")
        print(f"  导航后页面栈 = {await stack()}")
        # 回退
        await c.ev("new Promise(res=>my.navigateBack({complete:()=>res('ok')}))", ctx=logic)
        await asyncio.sleep(1.2)
        print(f"  navigateBack 后 = {await stack()}")

        # ── 2. 逻辑层 switchTab ──
        print("\n【2. 逻辑层 switchTab】")
        r = await c.ev(f"""new Promise(res=>{{
            my.switchTab({{url:'/{TARGET_TAB}',
              success:()=>res('success'), fail:e=>res('fail: '+JSON.stringify(e))}});
        }})""", ctx=logic)
        await asyncio.sleep(1.8)
        print(f"  switchTab 结果 = {r}")
        print(f"  切换后页面栈 = {await stack()}")
        await c.ev(f"new Promise(res=>my.switchTab({{url:'/{HOME}',complete:()=>res('ok')}}))", ctx=logic)
        await asyncio.sleep(1.2)
        print(f"  恢复首页 = {await stack()}")

        # ── 3. 视图层 DOM 点击（可点元素普查）──
        print("\n【3. 视图层可点元素普查】")
        survey = await c.ev("""(function(){
            const out=[];
            for(const el of document.querySelectorAll('*')){
                const t=(el.textContent||'').trim();
                if(!t || t.length>18) continue;
                if(el.querySelector('*') && el.children.length>3) continue;
                const r=el.getBoundingClientRect();
                if(r.width<8||r.height<8) continue;
                out.push({tag:el.tagName.toLowerCase(), text:t.slice(0,18),
                          x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});
                if(out.length>=14) break;
            }
            return JSON.stringify(out);
        })()""", ctx=view)
        items = json.loads(survey) if survey else []
        for it in items:
            print(f"  {it['tag']:<10} ({it['x']:>4},{it['y']:>4})  {it['text']!r}")

        # ── 4. 视图层 JS 合成点击 ──
        print("\n【4. 视图层 JS 合成点击首个可点元素】")
        if items:
            it = items[0]
            before = await c.ev("(document.body.innerText||'').length", ctx=view)
            res = await c.ev(f"""(function(){{
                const els=[...document.querySelectorAll('*')].filter(e=>
                    (e.textContent||'').trim()==={json.dumps(it['text'])});
                if(!els.length) return 'not found';
                const el=els[0];
                ['mousedown','mouseup','click'].forEach(t=>
                    el.dispatchEvent(new MouseEvent(t,{{bubbles:true,cancelable:true,view:window}})));
                return 'dispatched on '+el.tagName;
            }})()""", ctx=view)
            await asyncio.sleep(1.5)
            after = await c.ev("(document.body.innerText||'').length", ctx=view)
            print(f"  合成点击: {res}")
            print(f"  文本长度 {before} → {after}；页面栈 = {await stack()}")

        # ── 5. 坐标事件派发（top 坐标系）──
        print("\n【5. Input.dispatchMouseEvent 坐标派发】")
        # 取视图帧在父页面的偏移，把视图内坐标换算到 top 坐标
        off = await c.ev("""(function(){
            try{
                const fe=window.frameElement;
                if(!fe) return null;
                const r=fe.getBoundingClientRect();
                return JSON.stringify({ox:r.x, oy:r.y, w:r.width, h:r.height});
            }catch(e){ return 'ERR '+e.message }
        })()""", ctx=view)
        print(f"  视图帧偏移 = {off}")

        if items and off and off != "null" and not str(off).startswith("ERR"):
            o = json.loads(off)
            tx, ty = o["ox"] + items[0]["x"], o["oy"] + items[0]["y"]
            print(f"  top 坐标 = ({tx:.0f},{ty:.0f})")
            before_stack = await stack()
            await c.send("Input.dispatchMouseEvent", session=sid, type="mousePressed",
                         x=tx, y=ty, button="left", clickCount=1)
            await c.send("Input.dispatchMouseEvent", session=sid, type="mouseReleased",
                         x=tx, y=ty, button="left", clickCount=1)
            await asyncio.sleep(1.8)
            print(f"  派发后页面栈 {before_stack} → {await stack()}")

        # 收尾：回首页
        await c.ev(f"new Promise(res=>my.switchTab({{url:'/{HOME}',complete:()=>res('ok')}}))", ctx=logic)
        await asyncio.sleep(1.0)
        print(f"\n收尾页面栈 = {await stack()}")

        print("\n【本会话事件类型】")
        from collections import Counter
        print("  " + str(Counter(e["method"] for e in c.events).most_common(12)))


if __name__ == "__main__":
    asyncio.run(main())
