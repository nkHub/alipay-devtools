"""CDP over WebSocket 客户端 —— 支付宝小程序模拟器的调试/自动化通道。

实测结论（见 probe/ 目录下的探针脚本）
--------------------------------------
* 连上 IDE 的调试端点后需要 `Target.attachToTarget` 到标题含 `Simulator` 的
  webview target，那里面才是模拟器。
* 模拟器内部有三个我们关心的执行上下文：
    - 逻辑层  frame url 含 `af-appx`（支付宝 AFAppX worker）→ 有 `my` / `getCurrentPages`
    - 视图层  frame url 含 mini program 的 index.html  → 有 DOM
    - 顶层    模拟器外壳（设备框、tabBar 之外的外壳 DOM）
* `Runtime.evaluate` 在逻辑层可调 `my.*`；视图层 DOM 合成事件**不会**驱动小程序，
  必须用 `Input.dispatchMouseEvent` 派发真实事件，坐标要换算到顶层页面坐标系。
* `Page.captureScreenshot`（挂 Simulator target）可截模拟器画面。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets

from . import discovery

LOGIC = "logic"
VIEW = "view"
TOP = "top"

# 每个上下文探一次就能分层；这个探针同时能验证上下文是否还有效
_PROBE_EXPR = (
    "(function(){try{"
    "return JSON.stringify({fp:typeof getCurrentPages,my:typeof my,doc:typeof document})"
    "}catch(e){return 'ERR'}})()"
)


class CDPError(RuntimeError):
    """CDP 调用失败（协议错误或连接问题）。"""


class IDEUnavailable(CDPError):
    """IDE 没在跑，或找不到调试端点。"""


@dataclass
class SimulatorInfo:
    target_id: str
    title: str
    url: str


@dataclass
class _State:
    session_id: str | None = None
    simulator: SimulatorInfo | None = None
    contexts: dict[int, dict] = field(default_factory=dict)   # ctxId -> context 描述
    frame_urls: dict[str, str] = field(default_factory=dict)  # frameId -> url
    layers: dict[str, int] = field(default_factory=dict)      # layer -> ctxId
    root_frame_id: str | None = None
    endpoint: discovery.Endpoint | None = None


_NETWORK_KEEP = {
    "requestWillBeSent": ("request", "type", "documentURL"),
    "responseReceived": ("response", "type"),
    "loadingFailed": ("errorText", "type", "blockedReason"),
    "requestServedFromCache": ("requestId",),
    "loadingFinished": ("encodedDataLength",),
}


class CDPClient:
    """单条 CDP WebSocket 连接 + 一个 Simulator 会话。"""

    def __init__(self, events_maxlen: int = 4000) -> None:
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._send_lock = asyncio.Lock()
        self._shot_strategy: tuple[str, dict, bool] | None = None
        self._view_ctx: int | None = None
        self._view_route: str | None = None
        self.shot_info: dict[str, str] = {}
        self.state = _State()

        # 事件流：inspector 用
        self.events: deque[dict] = deque(maxlen=events_maxlen)
        self._event_seq = 0
        self._event_signal = asyncio.Event()

    # ── 连接 ────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def connect(self, project: str | None = None, *, force: bool = False) -> None:
        """发现端点并连接；已连接且端点未变则复用。"""
        endpoint = discovery.pick_endpoint(project)
        if endpoint is None:
            raise IDEUnavailable(
                "没有发现 IDE 调试端点。请确认「小程序开发者工具」已启动并打开了项目。"
            )
        if self.connected and not force and self.state.endpoint == endpoint:
            return

        await self.close()
        try:
            # proxy=None：macOS 系统级 SOCKS 代理会被 websockets 自动读取，必须显式关闭
            self._ws = await websockets.connect(
                endpoint.ws_url,
                proxy=None,
                open_timeout=8,
                close_timeout=2,
                max_size=128 * 1024 * 1024,
                ping_interval=None,
            )
        except TypeError:
            # 老版本 websockets 没有 proxy 参数
            self._ws = await websockets.connect(
                endpoint.ws_url,
                open_timeout=8,
                close_timeout=2,
                max_size=128 * 1024 * 1024,
                ping_interval=None,
            )
        except Exception as exc:  # noqa: BLE001 — 统一转成可读错误
            raise IDEUnavailable(
                f"连接 IDE 调试端口失败({endpoint.port}): {exc}. "
                "端口可能已随 IDE 重启变化，请重试。"
            ) from exc

        self.state = _State(endpoint=endpoint)
        self._reader = asyncio.create_task(self._read_loop())
        await self.attach_simulator()

    async def close(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self._shot_strategy = None
        self._view_ctx = None
        self._view_route = None
        self.shot_info = {}
        self.state = _State()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 连接断了，让后续调用重新发现
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(IDEUnavailable("调试连接已断开"))
            self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid is not None:
            fut = self._pending.pop(mid, None)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        method = msg.get("method")
        if not method:
            return
        params = msg.get("params") or {}
        self._track(method, params)

        self._event_seq += 1
        self.events.append(
            {
                "seq": self._event_seq,
                "t": time.time(),
                "method": method,
                "session": msg.get("sessionId"),
                "params": self._trim(method, params),
            }
        )
        self._event_signal.set()

    @staticmethod
    def _trim(method: str, params: dict) -> dict:
        """网络类事件量大，只留摘要，避免事件缓冲区吃掉内存。"""
        if method.startswith("Network."):
            ev = method.split(".", 1)[1]
            keep = _NETWORK_KEEP.get(ev)
            if keep is None:
                return {}
            out = {k: params.get(k) for k in keep if k in params}
            resp = params.get("response") or {}
            if resp:
                out["response"] = {
                    "url": resp.get("url"),
                    "status": resp.get("status"),
                    "mimeType": resp.get("mimeType"),
                }
            req = params.get("request") or {}
            if req:
                out["request"] = {"url": req.get("url"), "method": req.get("method")}
            return out
        return params

    def _invalidate_layers(self) -> None:
        """上下文/帧变了，分层缓存作废（view 每次按路由重算，这里一并清掉）。"""
        self.state.layers.pop(LOGIC, None)
        self.state.layers.pop(TOP, None)
        self.state.layers.pop(VIEW, None)
        self._view_ctx = None

    def _track(self, method: str, params: dict) -> None:
        """维护上下文/帧映射，供分层定位使用。"""
        if method == "Runtime.executionContextCreated":
            ctx = params.get("context") or {}
            if ctx.get("id") is not None:
                self.state.contexts[ctx["id"]] = ctx
                self._invalidate_layers()
        elif method == "Runtime.executionContextDestroyed":
            self.state.contexts.pop(params.get("executionContextId"), None)
            self._invalidate_layers()
        elif method == "Runtime.executionContextsCleared":
            self.state.contexts.clear()
            self._invalidate_layers()
        elif method == "Page.frameNavigated":
            fr = params.get("frame") or {}
            if fr.get("id"):
                self.state.frame_urls[fr["id"]] = fr.get("url") or ""
                self._view_ctx = None

    # ── 收发 ────────────────────────────────────────────────────────────
    async def send(self, method: str, session: str | None = None, **params: Any) -> int:
        """发一条不等待返回的命令（事件派发用）。"""
        if self._ws is None:
            raise IDEUnavailable("尚未连接 IDE")
        self._next_id += 1
        mid = self._next_id
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        async with self._send_lock:
            await self._ws.send(json.dumps(msg))
        return mid

    async def call(
        self,
        method: str,
        session: str | None = None,
        timeout: float = 15.0,
        **params: Any,
    ) -> dict:
        """发命令并等返回；协议错误抛 CDPError。"""
        if self._ws is None:
            raise IDEUnavailable("尚未连接 IDE")
        self._next_id += 1
        mid = self._next_id
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params}
        if session:
            msg["sessionId"] = session

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        async with self._send_lock:
            await self._ws.send(json.dumps(msg))
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(mid, None)
            raise CDPError(f"{method} 超时({timeout}s)，该命令在当前上下文可能不受支持") from exc
        finally:
            self._pending.pop(mid, None)

        if "error" in reply:
            err = reply["error"]
            raise CDPError(f"{method} 失败: {err.get('message') or err}")
        return reply.get("result", {})

    async def try_call(self, method: str, session: str | None = None, **params: Any) -> dict:
        """可失败的 call：出错返回空字典（用于可选能力，如 Network.enable）。"""
        try:
            return await self.call(method, session=session, **params)
        except CDPError:
            return {}

    # ── Simulator 会话 ──────────────────────────────────────────────────
    async def attach_simulator(self) -> SimulatorInfo:
        targets = (await self.call("Target.getTargets")).get("targetInfos", [])
        sim = next((t for t in targets if "Simulator" in (t.get("title") or "")), None)
        if sim is None:
            titles = ", ".join(repr(t.get("title")) for t in targets[:8])
            raise IDEUnavailable(
                f"没找到模拟器 target（需要 IDE 里打开并运行着项目）。现有 target: {titles}"
            )

        res = await self.call("Target.attachToTarget", targetId=sim["targetId"], flatten=True)
        self.state.session_id = res.get("sessionId")
        self.state.simulator = SimulatorInfo(
            target_id=sim["targetId"],
            title=sim.get("title") or "",
            url=sim.get("url") or "",
        )
        for method in ("Runtime.enable", "Page.enable", "Log.enable", "DOM.enable"):
            await self.try_call(method, session=self.state.session_id)
        await self.load_frames()
        return self.state.simulator

    async def load_frames(self) -> None:
        sid = self.state.session_id
        tree = (await self.try_call("Page.getFrameTree", session=sid)).get("frameTree")
        if not tree:
            return

        def walk(node: dict) -> None:
            fr = node.get("frame") or {}
            if fr.get("id"):
                self.state.frame_urls[fr["id"]] = fr.get("url") or ""
            for child in node.get("childFrames") or []:
                walk(child)

        walk(tree)
        self.state.root_frame_id = (tree.get("frame") or {}).get("id")

    # ── 执行上下文分层 ──────────────────────────────────────────────────
    async def _probe_context(self, ctx_id: int) -> dict | None:
        try:
            res = await self.call(
                "Runtime.evaluate",
                session=self.state.session_id,
                contextId=ctx_id,
                expression=_PROBE_EXPR,
                returnByValue=True,
                timeout=5,
            )
        except CDPError:
            return None
        raw = (res.get("result") or {}).get("value")
        if not isinstance(raw, str) or raw == "ERR":
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def resolve_layers(self, *, refresh: bool = False) -> dict[str, int]:
        """定位 logic / top 两层。

        判定靠实测而不是 URL 假设：能调 `getCurrentPages`、有 `my` 的那个上下文就是逻辑层。
        view 层不在这里定，由 `view_context()` 按当前路由动态选（见那里的说明）。
        """
        if refresh:
            self.state.layers.pop(LOGIC, None)
            self.state.layers.pop(TOP, None)
            self._view_ctx = None
        elif self.state.layers and LOGIC in self.state.layers:
            return dict(self.state.layers)

        def defaults() -> list[dict]:
            # 只认 default 上下文：isolated world 里没有小程序的 API
            return [
                ctx for ctx in self.state.contexts.values()
                if (ctx.get("auxData") or {}).get("isDefault", True)
            ]

        candidates = defaults()
        if not candidates:
            await self.try_call("Runtime.enable", session=self.state.session_id)
            await asyncio.sleep(0.4)
            candidates = defaults()

        root = self.state.root_frame_id
        for ctx in candidates:
            cid = ctx.get("id")
            if cid is None:
                continue
            frame_id = (ctx.get("auxData") or {}).get("frameId")
            if root and frame_id == root:
                self.state.layers.setdefault(TOP, cid)
                continue
            info = await self._probe_context(cid)
            if not info:
                continue
            if info.get("fp") == "function" or info.get("my") == "object":
                self.state.layers.setdefault(LOGIC, cid)

        return dict(self.state.layers)

    async def current_route(self) -> str:
        """逻辑层里的当前页面路由（不带前导斜杠）。"""
        raw = await self.try_evaluate(
            "(function(){var ps=getCurrentPages()||[];var p=ps[ps.length-1];"
            "return p?(p.route||p.__route__||''):''})()",
            LOGIC,
        )
        return str(raw or "").strip("/")

    async def view_context(self, *, refresh: bool = False) -> int | None:
        """当前页面的视图层上下文。

        坑：支付宝模拟器会为**每个页面实例**保留一个视图帧——页面栈里退到后台的页面
        帧依然活着（实测同时存在 3 个渲染过的页面帧）。所以不能按上下文顺序挑，
        必须拿逻辑层的当前路由去匹配帧 URL，否则会查到已经不在前台的旧页面 DOM 上。
        """
        route = await self.current_route()
        if not refresh and self._view_ctx is not None and self._view_route == route:
            return self._view_ctx

        layers = await self.resolve_layers()
        top, logic = layers.get(TOP), layers.get(LOGIC)

        candidates: list[tuple[int, int, str]] = []
        for cid, ctx in self.state.contexts.items():
            aux = ctx.get("auxData") or {}
            if not aux.get("isDefault", True) or cid in (top, logic):
                continue
            url = self.state.frame_urls.get(aux.get("frameId") or "", "")
            if route and route in url:
                score = 3
            elif "index.html" in url:
                score = 1
            else:
                score = 0
            candidates.append((score, cid, url))

        if not candidates:
            return None

        best = max(c[0] for c in candidates)
        tied = [c for c in candidates if c[0] == best]
        if len(tied) == 1:
            chosen: int | None = tied[0][1]
        else:
            # 路由对不上（帧 URL 还没更新）时，退回看节点数：渲染出页面的那个节点最多
            counts = []
            for _score, cid, _url in tied:
                n = await self._raw_eval(cid, "document.querySelectorAll('*').length", timeout=5)
                counts.append((n if isinstance(n, int) else -1, cid))
            counts.sort(reverse=True)
            chosen = counts[0][1] if counts else None

        self._view_ctx, self._view_route = chosen, route
        if chosen is not None:
            self.state.layers[VIEW] = chosen
        return chosen

    async def context_id(self, layer: str) -> int | None:
        if layer == VIEW:
            return await self.view_context()
        layers = await self.resolve_layers()
        return layers.get(layer)

    # ── 求值 ────────────────────────────────────────────────────────────
    async def _raw_eval(
        self,
        ctx_id: int,
        expression: str,
        *,
        timeout: float = 15.0,
        await_promise: bool = True,
    ) -> Any:
        """在指定 contextId 求值（不负责分层解析）。"""
        res = await self.call(
            "Runtime.evaluate",
            session=self.state.session_id,
            contextId=ctx_id,
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            text = (
                (detail.get("exception") or {}).get("description")
                or detail.get("text")
                or "未知异常"
            )
            raise CDPError(f"求值抛异常: {text}")
        return (res.get("result") or {}).get("value")

    async def evaluate(
        self,
        expression: str,
        layer: str = LOGIC,
        *,
        timeout: float = 15.0,
        await_promise: bool = True,
    ) -> Any:
        """在指定层求值，返回 CDP 的 value（不可序列化的对象会得到 None）。"""
        for attempt in (0, 1):
            ctx = await self.context_id(layer)
            if ctx is None:
                if attempt:
                    raise CDPError(f"找不到「{layer}」层执行上下文（模拟器可能未运行）")
                await self.load_frames()
                await asyncio.sleep(0.5)
                continue
            try:
                return await self._raw_eval(
                    ctx, expression, timeout=timeout, await_promise=await_promise
                )
            except CDPError as exc:
                if attempt == 0 and (
                    "Cannot find context" in str(exc) or "超时" in str(exc)
                ):
                    if layer == VIEW:
                        await self.view_context(refresh=True)
                    else:
                        await self.resolve_layers(refresh=True)
                    continue
                raise

        raise CDPError(f"无法在「{layer}」层求值")

    async def evaluate_json(self, expression: str, layer: str = LOGIC, **kw: Any) -> Any:
        """求值并解析 JSON 字符串（表达式需自行 JSON.stringify）。"""
        raw = await self.evaluate(expression, layer, **kw)
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except ValueError:
                return raw
        return raw

    # ── 坐标换算 ────────────────────────────────────────────────────────
    _OFFSET_EXPR = (
        "(function(){try{"
        "var fe=window.frameElement; if(!fe) return null;"
        "var r=fe.getBoundingClientRect();"
        "return JSON.stringify({ox:r.x,oy:r.y,w:r.width,h:r.height,"
        "dpr:window.devicePixelRatio||1})"
        "}catch(e){return null}})()"
    )

    async def view_offset(self) -> dict:
        """视图层坐标系 → 顶层页面坐标系 的偏移。"""
        if (vid := await self.context_id(VIEW)) is not None:
            raw = await self.evaluate(self._OFFSET_EXPR, VIEW)
            if raw:
                try:
                    off = json.loads(raw)
                    if off:
                        return off
                except (ValueError, TypeError):
                    pass

        # 兜底：在顶层遍历 iframe，按 URL 关键字命中视图帧
        js = """(function(){
            try{
                var vids = %s;
                for (var f of document.querySelectorAll('iframe')) {
                    try{
                        var href = f.contentWindow.location.href || '';
                        if (vids.some(function(k){return href.indexOf(k) >= 0})){
                            var r=f.getBoundingClientRect();
                            return JSON.stringify({ox:r.x,oy:r.y,w:r.width,h:r.height,
                                dpr:window.devicePixelRatio||1});
                        }
                    }catch(e){}
                }
            }catch(e){}
            return null;
        })()""" % json.dumps(
            [u for u in self.state.frame_urls.values() if "index.html" in u][:5]
        )
        raw = await self.try_evaluate(js, TOP)
        if raw:
            try:
                off = json.loads(raw)
                if off:
                    return off
            except (ValueError, TypeError):
                pass
        return {"ox": 0.0, "oy": 0.0, "w": None, "h": None, "dpr": 1}

    async def try_evaluate(self, expression: str, layer: str) -> Any:
        try:
            return await self.evaluate(expression, layer)
        except CDPError:
            return None

    # ── 元素定位 / 点击 / 输入 ──────────────────────────────────────────
    # 注意：getBoundingClientRect() 给的就是**视口相对**坐标，别再叠加 scrollY——
    # 之前多加了滚动量，导致屏幕外的元素被算出"看起来合法"的坐标，点了没反应。
    _FIND_JS = """(function(){
  var sel = %(sel)s, txt = %(txt)s, exact_only = %(exact_only)s, siv = %(siv)s;
  var norm = function(s){ return (s || '').replace(/\\s+/g, ' ').trim(); };
  var pool = sel ? [].slice.call(document.querySelectorAll(sel))
                 : [].slice.call(document.querySelectorAll('*'));
  var hits = pool;
  if (txt !== null) {
    // selector 与 text 是叠加关系：先按选择器收窄，再按文本过滤
    hits = pool.filter(function(e){ return norm(e.textContent) === txt; });
    if (!hits.length && !exact_only) {
      // 精确匹配不到就退回“包含”：真实文案常带前后缀（如【2026春季限定】xxx）
      hits = pool.filter(function(e){ return norm(e.textContent).indexOf(txt) >= 0; });
    }
    // 只留最具体的节点：排掉同样含这段文本的祖先
    hits = hits.filter(function(e){
      return !hits.some(function(o){ return o !== e && e.contains(o); });
    });
  }
  var vh = function(){ return window.innerHeight || document.documentElement.clientHeight; };
  var centerInView = function(r){
    var cy = r.y + r.height / 2;
    return cy >= 0 && cy <= vh();
  };
  // 把元素弄进视口：先 scrollIntoView，不行就找最近的可滚动祖先手动补一次。
  // 小程序的页面滚动经常挂在内部容器上，光滚 window 是没用的。
  // 关键：滚动可能是动画的（scroll-behavior:smooth 或 scrollIntoView 默认行为），
  // 同步读坐标会抓到“滚动途中”的瞬时位置——实测据此算出的点击点最终停在 y=-1。
  // 所以这里临时关掉平滑滚动，并等两帧再量。
  var jumpScroll = function(target, delta){
    var prev = '';
    try {
      prev = target === window ? document.documentElement.style.scrollBehavior
                               : target.style.scrollBehavior;
      if (target === window) document.documentElement.style.scrollBehavior = 'auto';
      else target.style.scrollBehavior = 'auto';
      if (target === window) target.scrollBy(0, delta);
      else target.scrollTop += delta;
    } catch (_) {}
    try {
      if (target === window) document.documentElement.style.scrollBehavior = prev;
      else target.style.scrollBehavior = prev;
    } catch (_) {}
  };
  var ensureInView = function(e){
    var how = 'scrollIntoView';
    try { e.scrollIntoView({block: 'center', behavior: 'instant'}); }
    catch (_) { try { e.scrollIntoView(); } catch (__) {} }
    var r = e.getBoundingClientRect();
    if (centerInView(r)) return how;
    var cy = r.y + r.height / 2;
    var need = cy < 0 ? cy - 40 : cy - vh() + 40;
    var sc = e.parentElement, host = null;
    while (sc) {
      if (sc.scrollHeight > sc.clientHeight + 1) { host = sc; break; }
      sc = sc.parentElement;
    }
    jumpScroll(host || window, need);
    return host ? 'scrollAncestor' : 'scrollWindow';
  };
  var measure = function(e){
    var r = e.getBoundingClientRect();
    var cs = window.getComputedStyle(e);
    return {
      tag: e.tagName.toLowerCase(),
      text: norm(e.textContent).slice(0, 60),
      cls: (e.className && e.className.toString ? e.className.toString() : '').slice(0, 60),
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height),
      visible: r.width > 0 && r.height > 0 &&
        cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0',
      inViewport: centerInView(r) && r.left >= 0
    };
  };
  var out = hits.map(measure);
  var scrolled = null;
  if (siv) {
    // 屏幕外的元素先弄进视口再重新量坐标，否则点击会落在别处。
    // ensureInView 用的是瞬时滚动，所以这里同步复量就是准的。
    for (var i = 0; i < hits.length; i++) {
      if (out[i].visible && !out[i].inViewport) {
        scrolled = ensureInView(hits[i]);
        out = hits.map(measure);
        break;
      }
    }
  }
  return JSON.stringify({
    count: out.length, items: out.slice(0, 30),
    viewport: {w: window.innerWidth, h: vh()}, scrolled: scrolled
  });
})()"""

    async def find_elements(
        self,
        selector: str | None = None,
        text: str | None = None,
        *,
        strict: bool = False,
        layer: str = VIEW,
        scroll_into_view: bool = False,
    ) -> dict:
        """在视图层找元素。

        `selector` 与 `text` 可叠加（先按选择器收窄，再按文本过滤）。
        `text` 默认先精确、失败退化为包含；`strict=True` 只允许精确。
        返回 `{"count", "items", "viewport"}`。
        """
        js = self._FIND_JS % {
            "sel": json.dumps(selector) if selector else "null",
            "txt": json.dumps(text) if text else "null",
            "exact_only": "true" if strict else "false",
            "siv": "true" if scroll_into_view else "false",
        }
        raw = await self.evaluate_json(js, layer)
        if not isinstance(raw, dict):
            return {"count": 0, "items": [], "viewport": {}}
        raw.setdefault("count", len(raw.get("items") or []))
        raw.setdefault("items", [])
        raw.setdefault("viewport", {})
        return raw

    async def tap(
        self,
        *,
        selector: str | None = None,
        text: str | None = None,
        index: int = 0,
        x: float | None = None,
        y: float | None = None,
        layer: str = VIEW,
        mode: str = "mouse",
    ) -> dict:
        """点击。

        坐标一律按「视图层坐标系」给（即小程序 375x623 那套视口 CSS px），
        内部换算成顶层页面坐标后派发真实事件。按 selector/text 定位时会自动把
        屏幕外的元素滚进可视区再点（否则点击会落在别处）。
        """
        info: dict[str, Any] = {"requested": {"selector": selector, "text": text}}

        if x is None or y is None:
            if not selector and not text:
                raise CDPError("需要提供 selector / text，或显式坐标 x,y")

            # 页面里可能有自动轮播/动画（实测首页 swiper 的 data 带 autoplay），
            # 元素会自己漂走，一次「滚→复量」不够。所以 Python 侧重试几轮，
            # 注意别在页面里等帧——requestAnimationFrame 在后台帧根本不触发。
            hit: dict | None = None
            found: dict = {}
            for attempt in range(3):
                found = await self.find_elements(
                    selector, text, layer=layer, scroll_into_view=True
                )
                matches = found.get("items") or []
                info["viewport"] = found.get("viewport")
                info["attempts"] = attempt + 1
                if not matches:
                    await asyncio.sleep(0.4)
                    continue
                if len(matches) > 1:
                    info["ambiguous"] = len(matches)
                cand = matches[index if index < len(matches) else 0]
                if not cand.get("visible"):
                    raise CDPError(
                        f"元素存在但不可见（{cand.get('tag')}.{cand.get('cls')!r}），"
                        "可能藏在折叠区或弹层后面。"
                    )
                if cand.get("inViewport"):
                    hit = cand
                    break
                await asyncio.sleep(0.4)

            if not found.get("items"):
                raise CDPError(
                    f"视图层没找到元素：selector={selector!r} text={text!r}。"
                    "可用 alipay_automator(action='find') 先确认选择器。"
                )
            if hit is None:
                # 弄不进视口就拒绝点击：盲点一个视口外的坐标，会点到那里恰好存在的
                # 别的元素上（实测会误触发 tabBar 跳转），比直接报错误导得多。
                vp = found.get("viewport") or {}
                off = (found["items"][0] if found.get("items") else {}) or {}
                raise CDPError(
                    f"元素 {off.get('tag')!r}.{off.get('cls')!r} 试过 "
                    f"{found.get('scrolled')} 仍进不了视口"
                    f"（点击点 y={off.get('y')}，视口高 {vp.get('h')}）。"
                    "页面可能在自动轮播导致坐标漂移，"
                    "可先 alipay_automator(action='scroll') 或用 alipay_navigate 跳路由。"
                )

            x, y = hit["x"], hit["y"]
            info["target"] = hit
            info["scrolled"] = found.get("scrolled")
            # 再确认一次那个点上到底是谁，避免被遮罩/浮层吃掉
            info["hits"] = await self.evaluate_json(
                "(function(){var e=document.elementFromPoint("
                + str(x) + "," + str(y) + ");"
                "return JSON.stringify(e?{tag:e.tagName.toLowerCase(),"
                "cls:String(e.className||'').slice(0,40),"
                "text:(e.textContent||'').replace(/\\s+/g,' ').trim().slice(0,40)}:null)})()",
                layer,
            )

        off = await self.view_offset()
        tx, ty = x + float(off.get("ox") or 0), y + float(off.get("oy") or 0)
        info["point"] = {"view": {"x": x, "y": y}, "top": {"x": tx, "y": ty}, "offset": off}

        sid = self.state.session_id
        if mode == "touch":
            await self.send(
                "Input.dispatchTouchEvent", session=sid, type="touchStart",
                touchPoints=[{"x": tx, "y": ty, "radiusX": 2, "radiusY": 2, "force": 1}],
            )
            await asyncio.sleep(0.05)
            await self.send(
                "Input.dispatchTouchEvent", session=sid, type="touchEnd", touchPoints=[]
            )
        else:
            await self.send(
                "Input.dispatchMouseEvent", session=sid, type="mouseMoved",
                x=tx, y=ty, button="none",
            )
            await asyncio.sleep(0.02)
            await self.send(
                "Input.dispatchMouseEvent", session=sid, type="mousePressed",
                x=tx, y=ty, button="left", clickCount=1,
            )
            await asyncio.sleep(0.05)
            await self.send(
                "Input.dispatchMouseEvent", session=sid, type="mouseReleased",
                x=tx, y=ty, button="left", clickCount=1,
            )
        info["ok"] = True
        return info

    async def type_text(
        self,
        value: str,
        *,
        selector: str | None = None,
        text: str | None = None,
        x: float | None = None,
        y: float | None = None,
        clear_first: bool = False,
    ) -> dict:
        """往输入框里打字：先点聚焦，再走真实输入事件（insertText）。"""
        tapped = await self.tap(selector=selector, text=text, x=x, y=y, mode="mouse")
        await asyncio.sleep(0.25)
        sid = self.state.session_id
        if clear_first:
            # 全选后覆盖输入
            for _ in range(3):
                await self.send(
                    "Input.dispatchKeyEvent", session=sid, type="keyDown",
                    key="Backspace", code="Backspace", windowsVirtualKeyCode=8,
                    nativeVirtualKeyCode=8,
                )
                await self.send(
                    "Input.dispatchKeyEvent", session=sid, type="keyUp",
                    key="Backspace", code="Backspace", windowsVirtualKeyCode=8,
                    nativeVirtualKeyCode=8,
                )
        await self.call("Input.insertText", session=sid, text=value)
        await asyncio.sleep(0.15)
        return {"ok": True, "tapped": tapped.get("target") or tapped.get("point"), "value": value}

    async def scroll(
        self, *, delta_y: float, delta_x: float = 0, x: float = 187, y: float = 400
    ) -> dict:
        off = await self.view_offset()
        tx = x + float(off.get("ox") or 0)
        ty = y + float(off.get("oy") or 0)
        await self.send(
            "Input.dispatchMouseEvent", session=self.state.session_id, type="mouseWheel",
            x=tx, y=ty, deltaX=delta_x, deltaY=delta_y,
        )
        await asyncio.sleep(0.3)
        return {"ok": True, "delta_y": delta_y}

    # ── 截图 ────────────────────────────────────────────────────────────
    async def _wait_events(
        self, methods: set[str], since: int, timeout: float
    ) -> list[dict]:
        """等 since 之后出现的某类事件。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            hits = self.events_since(since, methods)
            if hits:
                return hits
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return []
            self._event_signal.clear()
            try:
                await asyncio.wait_for(self._event_signal.wait(), timeout=min(left, 0.5))
            except asyncio.TimeoutError:
                pass

    async def _screenshot_screencast(
        self, *, format: str = "png", quality: int = 80, timeout: float = 10.0
    ) -> bytes:
        """走 `Page.startScreencast` 取一帧。

        这是模拟器截图的主力通路：由渲染器主动出帧，**不要求 target 当前可见**。
        而 `Page.captureScreenshot` 走合成器表面，模拟器被遮挡时会一直不返回。
        """
        sid = self.state.session_id
        start = self._event_seq
        await self.call(
            "Page.startScreencast", session=sid,
            format="jpeg" if format == "jpeg" else "png",
            quality=quality, everyNthFrame=1,
        )
        try:
            frames = await self._wait_events({"Page.screencastFrame"}, start, timeout)
            if not frames:
                raise CDPError(f"screencast {timeout:.0f}s 内没有推来帧")
            params = frames[-1].get("params") or {}
            data = params.get("data")
            if not data:
                raise CDPError("screencast 帧里没有图像数据")
            # 不回 ack 渲染器就不再推下一帧
            await self.try_call(
                "Page.screencastFrameAck", session=sid, sessionId=params.get("sessionId")
            )
            return base64.b64decode(data)
        finally:
            await self.try_call("Page.stopScreencast", session=sid)

    @staticmethod
    def _crop(data: bytes, box: tuple[float, float, float, float], scale: float) -> tuple[bytes, str]:
        """按顶层坐标裁出小程序可视区 → (图像, 说明)。

        screencast 给的是整个模拟器画面的帧，要小图必须自己裁。裁剪失败不致命，
        但原因一定要暴露出来——静默退回整帧会让人误以为 view 模式坏了。
        """
        try:
            import io

            from PIL import Image
        except ImportError:
            return data, "没装 pillow，退回整帧"
        try:
            img = Image.open(io.BytesIO(data))
            x, y, w, h = box
            box_px = (
                round(x * scale), round(y * scale),
                round((x + w) * scale), round((y + h) * scale),
            )
            if box_px[2] > img.width or box_px[3] > img.height:
                return data, (
                    f"裁剪框 {box_px} 超出画面 {img.width}x{img.height}，退回整帧"
                    "（多半是 dpr/坐标基准不一致）"
                )
            crop = img.crop(box_px)
            buf = io.BytesIO()
            crop.save(buf, format=img.format or "PNG")
            return buf.getvalue(), f"已裁出 {crop.width}x{crop.height}"
        except Exception as exc:  # noqa: BLE001
            return data, f"裁剪失败({type(exc).__name__}: {exc})，退回整帧"

    async def screenshot(
        self,
        *,
        to_view: bool = False,
        format: str = "png",
        quality: int | None = None,
        timeout: float = 12.0,
        from_surface: bool | None = None,
    ) -> bytes:
        """截图。

        两条通路，按可靠性排序：

        1. **screencast**（主力）—— 渲染器主动推帧，target 被遮挡也能出图。
        2. **`Page.captureScreenshot`** —— 走合成器表面，只在 target 当前可见时才可靠；
           实测模拟器这个 webview 在被遮挡时会一直不返回。这里保留为兜底，
           并把成功过的调用形态记住，下次优先。
        """
        off = await self.view_offset() if to_view else None
        scale = float((off or {}).get("dpr") or 1) or 1
        has_clip = bool(off and off.get("w") and off.get("h"))
        clip = (
            {
                "x": float(off.get("ox") or 0),
                "y": float(off.get("oy") or 0),
                "width": float(off.get("w")),
                "height": float(off.get("h")),
                "scale": 1,
            }
            if has_clip
            else None
        )

        errors: list[str] = []

        # 通路 1：screencast
        if from_surface is None:
            try:
                data = await self._screenshot_screencast(
                    format=format, quality=quality or 80, timeout=min(timeout, 10.0)
                )
                note = "无需裁剪"
                if to_view:
                    if clip:
                        data, note = self._crop(
                            data, (clip["x"], clip["y"], clip["width"], clip["height"]), scale
                        )
                    else:
                        note = "拿不到视图区尺寸，退回整帧"
                self.shot_info = {"via": "screencast", "crop": note}
                return data
            except CDPError as exc:
                errors.append(f"screencast: {exc}")

        # 通路 2：captureScreenshot（策略链兜底）
        base: dict[str, Any] = {"format": format}
        if quality and format == "jpeg":
            base["quality"] = quality
        if clip:
            base["clip"] = clip

        strategies: list[tuple[str, dict, bool]] = []
        if self._shot_strategy:
            strategies.append(self._shot_strategy)
        if from_surface is None:
            strategies += [
                ("nonsurface", {"fromSurface": False}, False),
                ("default", {}, False),
            ]
        else:
            strategies.append(("pinned", {"fromSurface": from_surface}, False))
        strategies.append(("bring", {"fromSurface": False}, True))

        seen: set[str] = set()
        sid = self.state.session_id
        for name, extra, bring in strategies:
            key = f"{name}:{sorted(extra.items())}"
            if key in seen:
                continue
            seen.add(key)
            try:
                if bring:
                    await self.try_call("Page.bringToFront", session=sid)
                    await asyncio.sleep(0.5)
                res = await self.call(
                    "Page.captureScreenshot", session=sid, timeout=timeout,
                    **{**base, **extra},
                )
            except CDPError as exc:
                errors.append(f"{name}: {exc}")
                continue
            data = res.get("data")
            if data:
                self._shot_strategy = (name, extra, bring)
                self.shot_info = {
                    "via": f"captureScreenshot/{name}",
                    "crop": "由 clip 参数裁出" if clip else "无需裁剪",
                }
                return base64.b64decode(data)

        raise CDPError("截图失败。" + " | ".join(errors) +
                       "（两条通路都不通时，把 IDE 窗口切到前台再试）")

    # ── 事件流 ──────────────────────────────────────────────────────────
    def events_since(self, seq: int, methods: set[str] | None = None) -> list[dict]:
        out = [e for e in self.events if e["seq"] > seq]
        if methods:
            out = [e for e in out if e["method"] in methods]
        return out

    @property
    def event_seq(self) -> int:
        return self._event_seq

    async def collect_events(
        self, duration: float, methods: set[str] | None = None, *, limit: int = 300
    ) -> tuple[list[dict], int]:
        """在当前事件基础上再收集 duration 秒，返回 (新事件, 当前 seq)。"""
        start = self._event_seq
        deadline = asyncio.get_running_loop().time() + duration
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                break
            self._event_signal.clear()
            try:
                await asyncio.wait_for(self._event_signal.wait(), timeout=left)
            except asyncio.TimeoutError:
                break
        return self.events_since(start, methods)[:limit], self._event_seq


# ── 进程级单例：跨工具调用复用同一连接 ──────────────────────────────────
_client: CDPClient | None = None
_client_lock = asyncio.Lock()


async def get_client(project: str | None = None, *, force: bool = False) -> CDPClient:
    """拿到已连接（必要时重连）的客户端。"""
    global _client
    async with _client_lock:
        if _client is None:
            _client = CDPClient()
        try:
            await _client.connect(project, force=force)
        except IDEUnavailable:
            if not force:
                # 端点可能是过期缓存，强制重新发现一次
                await _client.connect(project, force=True)
            else:
                raise
        return _client


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
