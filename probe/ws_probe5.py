#!/usr/bin/env python3
"""探针5：挂载「Lyra Simulator」webview target，验证截图 / DOM / 点击 / 控制台能力。

这是 automator 类工具可行性的决定性测试。
"""

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


async def main():
    port, token = discover()
    uri = f"ws://127.0.0.1:{port}/?_token={token}"
    print(f"▶ {uri}\n")
    async with websockets.connect(uri, proxy=None, open_timeout=5,
                                  max_size=128 * 1024 * 1024) as ws:
        c = CDP(ws)
        r = await c.call("Target.getTargets")
        infos = r["result"]["targetInfos"]
        sim = [t for t in infos if "Simulator" in (t.get("title") or "")]
        if not sim:
            print("❌ 没找到 Lyra Simulator target")
            return
        target = sim[0]
        print(f"挂载 target: {target['title']} ({target['targetId']})\n")

        ar = await c.call("Target.attachToTarget", targetId=target["targetId"], flatten=True)
        sid = ar["result"]["sessionId"]
        print(f"sessionId = {sid}\n")

        for m in ("Page.enable", "Runtime.enable", "Log.enable", "DOM.enable",
                  "Network.enable", "Input.enable"):
            try:
                await c.send(m, session=sid)
            except Exception as e:
                print(f"  {m} 发送失败: {e}")
        await asyncio.sleep(1.0)

        print("【Page.getFrameTree / 帧结构】")
        r = await c.call("Page.getFrameTree", session=sid, timeout=6)
        def walk_frames(node, depth=0):
            fr = node.get("frame", {})
            print(f"  {'  '*depth}├ {fr.get('id','')[:12]} {str(fr.get('url'))[:100]}")
            for ch in node.get("childFrames", []) or []:
                walk_frames(ch, depth + 1)
        ft = r.get("result", {}).get("frameTree")
        if ft:
            walk_frames(ft)
        else:
            print(f"  ❌ {json.dumps(r)[:200]}")

        # 各执行上下文
        print("\n【执行上下文】")
        ctxs = [e["params"]["context"] for e in c.events
                if e["method"] == "Runtime.executionContextCreated"]
        for x in ctxs:
            print(f"  id={x.get('id')} name={x.get('name')!r} origin={str(x.get('origin'))[:60]!r} "
                  f"aux={json.dumps(x.get('auxData'), ensure_ascii=False)}")

        # 截图
        print("\n【截图】")
        r = await c.call("Page.captureScreenshot", session=sid, timeout=20, format="png")
        if "result" in r and r["result"].get("data"):
            data = base64.b64decode(r["result"]["data"])
            open("/tmp/sim_shot.png", "wb").write(data)
            print(f"  ✅ 截图成功 {len(data)} 字节 → /tmp/sim_shot.png")
            # PNG 尺寸
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
                print(f"     尺寸 {w}x{h}")
        else:
            print(f"  ❌ {json.dumps(r, ensure_ascii=False)[:300]}")

        # 模拟器容器 DOM
        print("\n【模拟器容器 DOM 探测】")
        probes = {
            "容器内 iframe 数": "document.querySelectorAll('iframe').length",
            "容器 innerHTML 长度": "(document.querySelector('#__lyra-simulator-container')||document.body).innerHTML.length",
            "渲染出来的自定义标签": "JSON.stringify([...new Set([...document.querySelectorAll('*')].map(e=>e.tagName.toLowerCase()).filter(t=>t.includes('-')))].slice(0,30))",
            "页面文本前 200": "(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,200)",
            "iframe 内文本": "JSON.stringify([...document.querySelectorAll('iframe')].map(f=>{try{return (f.contentDocument.body.innerText||'').replace(/\\s+/g,' ').slice(0,80)}catch(e){return 'X-origin'}}))",
        }
        for k, v in probes.items():
            r = await c.call("Runtime.evaluate", session=sid, timeout=6,
                             expression=v, returnByValue=True)
            val = r.get("result", {}).get("result", {}).get("value")
            print(f"  {k:20} = {str(val)[:250]}")

        print("\n【本会话收到的事件】")
        from collections import Counter
        print("  " + str(Counter(e["method"] for e in c.events).most_common(20)))


if __name__ == "__main__":
    asyncio.run(main())
