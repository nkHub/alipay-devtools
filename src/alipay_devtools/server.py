"""alipay-devtools-mcp：支付宝小程序开发者工具的 MCP 服务。

对标微信侧的 `wechat-devtools-mcp`，提供 7 个聚合工具：

    alipay_ide           IDE 生命周期与端点发现
    alipay_build         构建 / 预览 / 上传 / 打包 / 提审 / DevServer
    alipay_automator     模拟器驱动：求值、点击、输入、页面栈、页面数据、存储
    alipay_inspector     控制台、异常、网络事件采集
    alipay_screenshot    模拟器截图
    alipay_navigate      跳转页面并采集该过程的日志
    alipay_file          项目文件读取与检索
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any

try:  # mcp 2.x：FastMCP 更名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer  # type: ignore[no-redef]

from . import __version__
from . import cdp, discovery, minidev
from . import project as proj  # 工具参数名 project 会遮蔽同名模块

try:  # mcp 2.x 支持报版本号；1.x 的 FastMCP 没这个参数
    mcp = _MCPServer("alipay-devtools", version=__version__)
except TypeError:
    mcp = _MCPServer("alipay-devtools")

CONSOLE_METHODS = {
    "Runtime.consoleAPICalled",
    "Runtime.exceptionThrown",
    "Log.entryAdded",
    "Log.consoleAPICalled",
}


# ── 通用工具函数 ────────────────────────────────────────────────────────
def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2)


def _err(exc: Exception, hint: str | None = None) -> str:
    payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _png_size(data: bytes) -> tuple[int | None, int | None]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return None, None


def _shots_dir() -> Path:
    """截图默认存这里，而不是项目目录——不污染业务仓库的 git status。"""
    env = os.environ.get("ALIPAY_SHOTS_DIR")
    return Path(env).expanduser() if env else Path.home() / ".alipay-devtools" / "screenshots"


def _render_remote_arg(arg: dict) -> Any:
    if "value" in arg:
        return arg["value"]
    if "description" in arg:
        return arg["description"]
    if arg.get("subtype") == "null":
        return None
    return f"<{arg.get('type') or 'unknown'}>"


def _console_records(events: list[dict]) -> list[dict]:
    """把 CDP 事件整理成好读的控制台记录。"""
    out: list[dict] = []
    for ev in events:
        method, params = ev["method"], ev.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            out.append({
                "kind": "console",
                "level": params.get("type"),
                "text": " ".join(
                    str(_render_remote_arg(a)) for a in (params.get("args") or [])
                ),
                "t": ev["t"],
            })
        elif method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails") or {}
            out.append({
                "kind": "exception",
                "level": "error",
                "text": (d.get("exception") or {}).get("description")
                        or d.get("text") or "未知异常",
                "url": d.get("url"),
                "line": d.get("lineNumber"),
                "stack": ((d.get("stackTrace") or {}).get("callFrames") or [{}])[:5],
                "t": ev["t"],
            })
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            out.append({
                "kind": "log",
                "level": entry.get("level"),
                "source": entry.get("source"),
                "text": entry.get("text"),
                "url": entry.get("url"),
                "line": entry.get("lineNumber"),
                "t": ev["t"],
            })
        elif method == "Tiny.Data.dataChanged":
            out.append({"kind": "dataChanged", "level": "info",
                        "text": json.dumps(params, ensure_ascii=False)[:400], "t": ev["t"]})
    return out


def _network_records(events: list[dict]) -> list[dict]:
    out = []
    for ev in events:
        m, p = ev["method"], ev.get("params") or {}
        if m == "Network.requestWillBeSent":
            req = p.get("request") or {}
            out.append({"kind": "request", "method": req.get("method"),
                        "url": req.get("url"), "type": p.get("type"), "t": ev["t"]})
        elif m == "Network.responseReceived":
            resp = p.get("response") or {}
            out.append({"kind": "response", "status": resp.get("status"),
                        "url": resp.get("url"), "mime": resp.get("mimeType"),
                        "type": p.get("type"), "t": ev["t"]})
        elif m == "Network.loadingFailed":
            out.append({"kind": "failed", "url": (p.get("request") or {}).get("url"),
                        "error": p.get("errorText"), "t": ev["t"]})
    return out


# ── 1. alipay_ide ───────────────────────────────────────────────────────
@mcp.tool()
async def alipay_ide(
    action: str,
    project: str | None = None,
    app_path: str | None = None,
    timeout: float = 180.0,
) -> str:
    """支付宝小程序 IDE（小程序开发者工具）的生命周期与调试端点管理。

    Args:
        action:
            - "status": IDE 是否在跑、端点端口、打开的项目、minidev 版本
            - "discover": 列出全部调试端点（多项目/多窗口时有用）
            - "open": 启动 IDE 并打开 project（等价 `minidev ide -p <project>`）
            - "start_sim": 展示 IDE 模拟器（`minidev ide start-sim`）
            - "login": 走 minidev 授权登录（会输出二维码/等待扫码）
            - "quit": 结束 IDE 主进程
        project: 项目根目录；缺省用环境变量 ALIPAY_PROJECT_PATH 或当前目录
        app_path: IDE 安装路径，缺省自动探测
        timeout: open/login 等等待秒数
    """
    try:
        if action == "discover":
            return _ok({
                "ide_running": discovery.ide_running(),
                "app_path": discovery.app_path(),
                "minidev": {
                    "path": discovery.cli_path(),
                    "version": minidev.version(),
                },
                "endpoints": [e.as_dict() for e in discovery.endpoints()],
            })

        if action == "status":
            eps = discovery.endpoints()
            payload: dict[str, Any] = {
                "ide_running": discovery.ide_running(),
                "app_path": discovery.app_path(),
                "minidev": {"path": discovery.cli_path(), "version": minidev.version()},
                "endpoints": [e.as_dict() for e in eps],
            }
            if eps:
                try:
                    client = await cdp.get_client(project)
                    await client.view_context()  # view 层按当前路由动态选，先解析再汇报
                    layers = await client.resolve_layers()
                    payload["connected"] = True
                    payload["simulator"] = {
                        "title": client.state.simulator.title if client.state.simulator else None,
                        "target_id": client.state.simulator.target_id if client.state.simulator else None,
                        "layers": {k: v for k, v in layers.items()},
                        "contexts": len(client.state.contexts),
                    }
                except cdp.CDPError as exc:
                    payload["connected"] = False
                    payload["connect_error"] = str(exc)
            return _ok(payload)

        if action == "open":
            root = proj.resolve_project(project)
            argv = minidev.build_argv("ide", root, app_path=app_path)
            return _ok({"result": minidev.run(argv, timeout=timeout).as_dict(),
                        "note": "IDE 启动后需在模拟器里运行项目，自动化通道才会出现调试端点。"})

        if action == "start_sim":
            return _ok(minidev.execute("start_sim", project=project, timeout=timeout).as_dict())

        if action == "login":
            return _ok(minidev.execute("login", project=project, timeout=timeout).as_dict())

        if action == "quit":
            killed = []
            for proc in discovery.ide_processes():
                if proc["kind"] != "main":
                    continue
                try:
                    import os
                    import signal

                    os.kill(proc["pid"], signal.SIGTERM)
                    killed.append(proc["pid"])
                except OSError as exc:
                    return _err(exc, "结束 IDE 主进程失败")
            await cdp.shutdown()
            return _ok({"terminated_pids": killed})

        return _err(ValueError(action), "action 需为 status/discover/open/start_sim/login/quit")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# ── 2. alipay_build ─────────────────────────────────────────────────────
@mcp.tool()
async def alipay_build(
    action: str,
    project: str | None = None,
    app_id: str | None = None,
    timeout: float = 600.0,
    version: str | None = None,
    version_description: str | None = None,
    page: str | None = None,
    page_query: str | None = None,
    query: str | None = None,
    client_type: str | None = None,
    output: str | None = None,
    extra_flags: dict | None = None,
) -> str:
    """调用官方 minidev CLI 做构建、预览、上传、打包、提审等。

    Args:
        action:
            - "build": 编译小程序（`minidev build`），不传 app_id 也能跑
            - "preview": 真机预览，产出二维码（需要 app_id）
            - "upload": 上传版本（需要 app_id、version）
            - "pack": 打包源码 zip
            - "audit": 提审（需要 app_id、version、version_description）
            - "cancel_audit": 取消提审（需要 app_id、version）
            - "dev": 启动本地 DevServer（常驻进程，60s 后中断并返回已产出的日志）
            - "remote_debug": 真机调试（需要 app_id）
            - "setup_build": 初始化构建环境
            - "clean_cache": 清构建缓存
            - "download_assets": 下载离线资源包（可配 extra_flags={"with_compiler": True}）
            - "create": 新建项目（配 extra_flags={"name": ..., "stack": "native"}）
        project: 项目根目录
        app_id: 小程序 appId；缺省读 ALIPAY_APP_ID 或从项目配置推断
        version / version_description: upload、audit 用
        page / page_query / query: preview、remote_debug 的入口页与参数
        client_type: 端类型（alipay / amap / genie ...），缺省 alipay
        output: build 的产物目录
        extra_flags: 透传其它 minidev 旗标，键用下划线命名（如 {"minify": True}）
    """
    try:
        root = proj.resolve_project(project)
        if not app_id:
            app_id = proj.guess_app_id(root).get("app_id")

        mapping = {
            "build": "build",
            "preview": "preview",
            "upload": "upload",
            "pack": "pack",
            "audit": "audit",
            "cancel_audit": "cancel_audit",
            "dev": "dev",
            "remote_debug": "remote_debug",
            "setup_build": "setup_build",
            "clean_cache": "clean_build_cache",
            "download_assets": "download_assets",
            "create": "create",
        }
        if action not in mapping:
            return _err(ValueError(action), f"action 需为 {', '.join(mapping)}")

        kwargs: dict[str, Any] = dict(extra_flags or {})
        for key, value in (
            ("version", version),
            ("version_description", version_description),
            ("page", page),
            ("page_query", page_query),
            ("query", query),
            ("output", output),
        ):
            if value is not None:
                kwargs[key] = value
        if client_type is None and action in {"preview", "upload", "remote_debug", "audit"}:
            kwargs.setdefault("client_type", "alipay")

        result = minidev.execute(
            mapping[action],
            project=str(root),
            app_id=app_id,
            timeout=timeout,
            **kwargs,
        )
        return _ok({"action": action, "result": result.as_dict()})
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "用 alipay_ide(action='status') 可先确认 minidev 是否可用")


# ── 3. alipay_automator ─────────────────────────────────────────────────
_JS_PAGE_STACK = """
JSON.stringify((getCurrentPages() || []).map(function (p, i) {
  return {
    index: i,
    route: p.route || p.__route__ || p.__pagePath__ || null,
    depth: i + 1,
    is_current: i === (getCurrentPages().length - 1)
  };
}))
"""

_JS_CURRENT_PAGE = "(getCurrentPages() || []).slice(-1)[0]"


@mcp.tool()
async def alipay_automator(
    action: str,
    project: str | None = None,
    layer: str | None = None,
    expression: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    index: int = 0,
    x: float | None = None,
    y: float | None = None,
    value: str | None = None,
    data: dict | None = None,
    method: str | None = None,
    args: list | None = None,
    route: str | None = None,
    key: str | None = None,
    delta_y: float = 300.0,
    max_chars: int = 4000,
) -> str:
    """驱动 IDE 模拟器里的小程序：求值、查找元素、点击、输入、页面栈、页面数据、存储。

    坐标说明：x/y 一律用**视图层坐标系**（iPhone 模拟器里就是 375x623 那套 CSS px），
    内部会自动换算成顶层页面坐标派发真实事件。视图层内 JS 合成事件不会驱动小程序，
    所以点击一律走真实输入事件。

    Args:
        action:
            - "start": 连接并报告模拟器/分层/坐标偏移自检
            - "status": 同 start，但连接已建立时更轻量
            - "evaluate": 在 layer 求值，返回 JSON 化的结果
            - "find": 按 selector / text 找元素，返回标签、文本、坐标、可见性
            - "tap": 点击（selector/text/index 或直接 x,y）
            - "input": 往输入框打字（先点击聚焦，再 insertText；clear_first 用 extra 传）
            - "scroll": 在视图层滚轮滚动（delta_y 正数向下）
            - "page_stack": 当前页面栈（路由数组）
            - "page_data": 当前页面 data（max_chars 截断）
            - "set_data": 调当前页 setData（data 为要合并的对象）
            - "call_method": 调当前页方法（method + args）
            - "navigate": 页面跳转（route；用 extra 里的 mode 指定 switchTab/redirectTo/reLaunch）
            - "system_info": my.getSystemInfoSync()
            - "storage": my.getStorageSync（可选 key）
            - "set_storage": my.setStorageSync（key + value，value 按 JSON 解析）
        layer: 求值层。不传时按动作取默认值：evaluate→logic（能调 my.*），
               find/tap/input→view（有 DOM）/ top（模拟器外壳）
        expression: evaluate 用的 JS 表达式
        selector / text / index: find 与 tap 的定位方式
        data / method / args / route / key / value: 各 action 的入参
    """
    try:
        client = await cdp.get_client(project)

        if action in {"start", "status"}:
            # 先解析视图层（view_offset 内部会按当前路由选上下文），再汇报分层
            offset = await client.view_offset()
            layers = await client.resolve_layers(refresh=(action == "start"))
            await client.view_context()
            layers = await client.resolve_layers()
            stack = await client.try_evaluate(_JS_PAGE_STACK, cdp.LOGIC)
            return _ok({
                "simulator": {
                    "title": client.state.simulator.title if client.state.simulator else None,
                    "target_id": client.state.simulator.target_id if client.state.simulator else None,
                    "endpoint_port": client.state.endpoint.port if client.state.endpoint else None,
                },
                "layers": layers,
                "contexts": sorted(client.state.contexts.keys()),
                "frames": client.state.frame_urls,
                "view_offset": offset,
                "page_stack": json.loads(stack) if stack else None,
                "hint": "分层缺失时：确认 IDE 里项目已运行（模拟器显示页面），再 action='start' 重试。",
            })

        if action == "evaluate":
            if not expression:
                return _err(ValueError("evaluate 需要 expression"))
            use_layer = layer or cdp.LOGIC
            val = await client.evaluate(expression, use_layer)
            return _ok({"layer": use_layer, "result": val})

        if action == "find":
            found = await client.find_elements(
                selector, text, layer=layer or cdp.VIEW,
                scroll_into_view=bool((data or {}).get("scroll_into_view")),
            )
            return _ok(found)

        if action == "tap":
            result = await client.tap(
                selector=selector, text=text, index=index, x=x, y=y,
                layer=layer or cdp.VIEW,
            )
            await asyncio.sleep(0.6)
            stack = await client.try_evaluate(_JS_PAGE_STACK, cdp.LOGIC)
            result["page_stack_after"] = json.loads(stack) if stack else None
            return _ok(result)

        if action == "input":
            if value is None:
                return _err(ValueError("input 需要 value"))
            clear = bool((data or {}).get("clear_first"))
            result = await client.type_text(
                value, selector=selector, text=text, x=x, y=y, clear_first=clear
            )
            return _ok(result)

        if action == "scroll":
            return _ok(await client.scroll(delta_y=delta_y))

        if action == "page_stack":
            raw = await client.evaluate(_JS_PAGE_STACK, cdp.LOGIC)
            return _ok({"page_stack": json.loads(raw) if raw else None})

        if action == "page_data":
            route_expr = f"(({_JS_CURRENT_PAGE})||{{}}).route || null"
            route_val = await client.try_evaluate(route_expr, cdp.LOGIC)
            raw = await client.evaluate(
                f"JSON.stringify((({_JS_CURRENT_PAGE})||{{}}).data || {{}})", cdp.LOGIC
            )
            payload = json.loads(raw) if raw else {}
            trimmed = json.dumps(payload, ensure_ascii=False)
            truncated = len(trimmed) > max_chars
            return _ok({
                "route": route_val,
                "keys": list(payload.keys()) if isinstance(payload, dict) else None,
                "data": payload if not truncated else None,
                "raw": trimmed[:max_chars] if truncated else None,
                "truncated": truncated,
            })

        if action == "set_data":
            if not data:
                return _err(ValueError("set_data 需要 data"))
            payload = json.dumps(data, ensure_ascii=False)
            js = (
                "(function(){var p=(getCurrentPages()||[]).slice(-1)[0];"
                "if(!p||!p.setData) return 'no-page';"
                "p.setData(" + payload + ");"
                "return JSON.stringify({ok:true,keys:Object.keys(" + payload + ")});"
                "})()"
            )
            raw = await client.evaluate(js, cdp.LOGIC)
            if raw == "no-page":
                return _err(proj.ProjectError("当前没有可用页面（或页面无 setData）"))
            await asyncio.sleep(0.5)
            return _ok({"outcome": json.loads(raw) if raw else None,
                        "hint": "数据已合并进当前页；可用 alipay_screenshot 复核渲染结果。"})

        if action == "call_method":
            if not method:
                return _err(ValueError("call_method 需要 method"))
            call_args = json.dumps(args or [], ensure_ascii=False)
            js = (
                f"(function(){{var p=({_JS_CURRENT_PAGE})||{{}};"
                f"if(typeof p[{json.dumps(method)}]!=='function') "
                f"return JSON.stringify({{error:'方法不存在:'+{json.dumps(method)}}});"
                f"var r=p[{json.dumps(method)}].apply(p,{call_args});"
                "return JSON.stringify({result: (r===undefined?null:r)})})()"
            )
            raw = await client.evaluate(js, cdp.LOGIC)
            return _ok({"method": method, "outcome": json.loads(raw) if raw else None})

        if action == "navigate":
            if not route:
                return _err(ValueError("navigate 需要 route，如 /pages/index/index"))
            mode = str((data or {}).get("mode") or "auto")
            url = route if route.startswith("/") else f"/{route}"
            if mode == "auto":
                mode = "switchTab" if _is_tab_page(client, project, url) else "navigateTo"
            js = (
                "new Promise(function(res){"
                f"my.{mode}({{url:{json.dumps(url)},"
                "success:function(){res('success')},"
                "fail:function(e){res('fail:'+(e&&e.errorMessage||JSON.stringify(e)))}})"
                "})"
            )
            outcome = await client.evaluate(js, cdp.LOGIC)
            await asyncio.sleep(1.2)
            stack = await client.try_evaluate(_JS_PAGE_STACK, cdp.LOGIC)
            return _ok({
                "url": url, "mode": mode, "outcome": outcome,
                "page_stack": json.loads(stack) if stack else None,
            })

        if action == "system_info":
            raw = await client.evaluate("JSON.stringify(my.getSystemInfoSync())", cdp.LOGIC)
            return _ok({"system_info": json.loads(raw) if raw else None})

        if action == "storage":
            if key:
                js = f"JSON.stringify(my.getStorageSync({{key:{json.dumps(key)}}}) || null)"
            else:
                # Alipay 的 getStorageSync() 不传参不会返回全部数据，得先拿 key 列表再逐个读
                js = (
                    "(function(){var out={keys:[],values:{}};"
                    "try{var info=my.getStorageInfoSync()||{};"
                    "out.keys=info.keys||[];out.currentSize=info.currentSize;"
                    "out.limitSize=info.limitSize;}catch(e){out.error=String(e)}"
                    "out.keys.forEach(function(k){"
                    "try{out.values[k]=my.getStorageSync({key:k}).data}catch(e){}});"
                    "return JSON.stringify(out)})()"
                )
            raw = await client.evaluate(js, cdp.LOGIC)
            return _ok({"storage": json.loads(raw) if raw else None})

        if action == "set_storage":
            if not key:
                return _err(ValueError("set_storage 需要 key"))
            parsed: Any = value
            try:
                parsed = json.loads(value) if value is not None else None
            except (ValueError, TypeError):
                pass
            js = (
                f"my.setStorageSync({{key:{json.dumps(key)},"
                f"data:{json.dumps(parsed, ensure_ascii=False)}}});'ok'"
            )
            await client.evaluate(js, cdp.LOGIC)
            return _ok({"set": {"key": key, "data": parsed}})

        return _err(ValueError(action),
                    "action 需为 start/status/evaluate/find/tap/input/scroll/page_stack/"
                    "page_data/set_data/call_method/navigate/system_info/storage/set_storage")
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "IDE 里项目需处于运行状态；端口会随 IDE 重启变化，直接重试即可重新发现。")


def _is_tab_page(client: cdp.CDPClient, project_arg: str | None, url: str) -> bool:
    """判断目标路由是否 tabBar 页面（tabBar 页只能用 switchTab 打开）。"""
    try:
        root = proj.resolve_project(project_arg)
        cfg = proj.app_config(root)
        tab = cfg.get("tabBar") or cfg.get("tabbar") or {}
        paths = {
            (t.get("pagePath") or "").strip("/")
            for t in (tab.get("items") or tab.get("list") or [])
            if isinstance(t, dict)
        }
        return url.strip("/") in paths
    except Exception:  # noqa: BLE001
        return False


# ── 4. alipay_inspector ─────────────────────────────────────────────────
@mcp.tool()
async def alipay_inspector(
    action: str = "all",
    project: str | None = None,
    duration: float = 6.0,
    detail_level: str = "concise",
    max_logs: int = 80,
    level: str | None = None,
    keyword: str | None = None,
    include_network: bool = False,
) -> str:
    """采集模拟器运行时日志：控制台输出、JS 异常、网络请求。

    事件监听是**连上之后**才开始生效的，页面加载早期触发的日志抓不到——
    要抓启动期日志，先用 action='start' 打开监听，再 alipay_automator(action='navigate')
    触发重载，然后 action='all' 取结果。

    Args:
        action: "console"（控制台+异常）/ "exceptions" / "network" / "all" / "clear"
        duration: 采集时长（秒）。已发生的事件会一并返回，因此 0 表示只看已有缓冲
        detail_level: "concise" 只返回过滤后的记录；"full" 附带原始 CDP 事件
        max_logs: 返回条数上限
        level: 只看某个级别，如 "error" / "warn" / "log"
        keyword: 只保留文本里含该关键词的记录
        include_network: console/all 时是否连带网络摘要
    """
    try:
        client = await cdp.get_client(project)
        if action == "clear":
            client.events.clear()
            return _ok({"cleared": True})

        await client.try_call("Runtime.enable", session=client.state.session_id)
        await client.try_call("Log.enable", session=client.state.session_id)
        if action in {"network", "all"} or include_network:
            await client.try_call("Network.enable", session=client.state.session_id)

        if duration > 0:
            await client.collect_events(duration)

        want_console = action in {"console", "exceptions", "all"}
        want_network = action in {"network", "all"} or include_network

        records: list[dict] = []
        if want_console:
            methods = {"Runtime.exceptionThrown"} if action == "exceptions" else CONSOLE_METHODS
            recs = _console_records(client.events_since(0, methods))
            if action == "exceptions":
                recs = [r for r in recs if r["kind"] == "exception"]
            records += recs
        if want_network:
            records += _network_records(client.events_since(
                0, {"Network.requestWillBeSent", "Network.responseReceived", "Network.loadingFailed"}
            ))

        if level:
            records = [r for r in records if (r.get("level") or "") == level]
        if keyword:
            records = [r for r in records if keyword in str(r.get("text") or r.get("url") or "")]

        records = records[-max_logs:]
        summary: dict[str, Any] = {"returned": len(records)}
        for r in records:
            key = r.get("level") or r.get("kind")
            summary[key] = summary.get(key, 0) + 1

        payload: dict[str, Any] = {"action": action, "summary": summary, "records": records}
        if detail_level == "full":
            payload["raw_events"] = [e for e in client.events][-max_logs:]
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "确认 IDE 里模拟器正在运行。")


# ── 5. alipay_screenshot ────────────────────────────────────────────────
@mcp.tool()
async def alipay_screenshot(
    project: str | None = None,
    mode: str = "view",
    path: str | None = None,
    return_base64: bool = False,
) -> str:
    """截取模拟器画面。

    Args:
        mode: "view" 只截小程序可视区（375x623 那块，日常看页面用这个）；
              "full" 截整个模拟器窗口（含设备框、状态栏）
        path: 保存路径；缺省存到 ~/.alipay-devtools/screenshots/<时间戳>.png
              （用 ALIPAY_SHOTS_DIR 可改；刻意不落在项目里，免得污染 git status）
        return_base64: 是否同时返回 base64（体积大，默认关）
    """
    try:
        client = await cdp.get_client(project)
        # 确保 Network 之外的基础域已开；观感更稳
        await client.try_call("Page.enable", session=client.state.session_id)
        data = await client.screenshot(to_view=(mode == "view"))

        if path:
            target = Path(path).expanduser()
        else:
            target = _shots_dir() / f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        width, height = _png_size(data)

        payload: dict[str, Any] = {
            "saved_to": str(target),
            "bytes": len(data),
            "size": {"width": width, "height": height},
            "mode": mode,
            # via/crop 让“走了哪条通路、有没有裁成功”可见，避免静默退回整帧看不出来
            "capture": client.shot_info,
        }
        if return_base64:
            import base64

            payload["base64"] = base64.b64encode(data).decode()
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "mode='view' 依赖视图层上下文，可先用 alipay_automator(action='start') 自检。")


# ── 6. alipay_navigate ──────────────────────────────────────────────────
@mcp.tool()
async def alipay_navigate(
    route: str,
    project: str | None = None,
    query: str | None = None,
    mode: str = "auto",
    wait: float = 2.5,
    capture_logs: bool = True,
    screenshot: bool = False,
) -> str:
    """跳转到指定页面，并采集跳转过程（onLoad/onShow 阶段）的日志。

    Args:
        route: 页面路由，如 /pages/index/index
        query: 查询串，如 "goods_id=6890&from=home"
        mode: "auto"（tabBar 页自动用 switchTab）/ "switchTab" / "navigateTo"
              / "redirectTo" / "reLaunch" / "navigateBack"
        wait: 跳转后等待秒数（页面渲染 + 数据请求）
        capture_logs: 是否返回这段时间的控制台/异常记录
        screenshot: 是否同时存一张图并返回路径
    """
    try:
        client = await cdp.get_client(project)
        await client.try_call("Runtime.enable", session=client.state.session_id)
        await client.try_call("Log.enable", session=client.state.session_id)

        url = route if route.startswith("/") else f"/{route}"
        if query:
            url = f"{url}?{query.lstrip('?')}"

        if mode == "auto":
            mode = "switchTab" if _is_tab_page(client, project, url) else "navigateTo"
        if mode in {"navigateBack", "reLaunch"}:
            js = f"new Promise(function(r){{my.{mode}({{url:{json.dumps(url)},complete:function(){r('done')}}})}})"
        else:
            js = (
                "new Promise(function(res){"
                f"my.{mode}({{url:{json.dumps(url)},"
                "success:function(){res('success')},"
                "fail:function(e){res('fail:'+(e&&e.errorMessage||JSON.stringify(e)))}})"
                "})"
            )
        outcome = await client.evaluate(js, cdp.LOGIC)
        await asyncio.sleep(max(0.0, wait))

        stack = await client.try_evaluate(_JS_PAGE_STACK, cdp.LOGIC)
        data_raw = await client.evaluate(
            f"JSON.stringify((({_JS_CURRENT_PAGE})||{{}}).data || {{}})", cdp.LOGIC
        )
        page_data = json.loads(data_raw) if data_raw else {}

        payload: dict[str, Any] = {
            "url": url,
            "mode": mode,
            "outcome": outcome,
            "page_stack": json.loads(stack) if stack else None,
            "page_data_keys": list(page_data.keys()) if isinstance(page_data, dict) else None,
        }

        if capture_logs:
            logs = _console_records(client.events_since(0, CONSOLE_METHODS))
            payload["console"] = logs[-40:]
            payload["console_count"] = len(logs)

        if screenshot:
            data = await client.screenshot(to_view=True)
            target = (
                _shots_dir()
                / f"nav-{route.strip('/').replace('/', '_')}-{time.strftime('%H%M%S')}.png"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            payload["screenshot"] = str(target)
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "路由需存在于 app.json 的 pages 中；可用 alipay_file(action='list_pages') 查。")


# ── 7. alipay_file ──────────────────────────────────────────────────────
@mcp.tool()
async def alipay_file(
    action: str,
    project: str | None = None,
    route: str | None = None,
    path: str | None = None,
    pattern: str | None = None,
    glob: str = "*.{axml,js,acss,json}",
    max_results: int = 60,
    exts: list | None = None,
) -> str:
    """读小程序项目文件：项目概览、页面清单、页面源码、任意文件、全局检索。

    Args:
        action: "project_info" / "list_pages" / "read_page" / "read_file" / "search"
        route: read_page 用，如 pages/index/index
        path: read_file 用，项目内相对路径
        pattern: search 用的正则
        glob: search 的文件匹配，如 "*.js" 或 "*.{axml,acss}"
        exts: read_page 时只取指定后缀，如 [".axml", ".js"]
        max_results: search 返回上限
    """
    try:
        root = proj.resolve_project(project)

        if action == "project_info":
            return _ok(proj.project_info(root))

        if action == "list_pages":
            items = [p.as_dict() for p in proj.pages(root)]
            return _ok({
                "total": len(items),
                "with_files": sum(1 for i in items if i["exists"]),
                "pages": items,
            })

        if action == "read_page":
            if not route:
                return _err(ValueError("read_page 需要 route"))
            return _ok(proj.read_page(root, route, exts))

        if action == "read_file":
            if not path:
                return _err(ValueError("read_file 需要 path"))
            return _ok(proj.read_file(root, path))

        if action == "search":
            if not pattern:
                return _err(ValueError("search 需要 pattern"))
            return _ok(proj.search(root, pattern, glob=glob, max_results=max_results))

        return _err(ValueError(action),
                    "action 需为 project_info/list_pages/read_page/read_file/search")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# ── 启动 ────────────────────────────────────────────────────────────────
def _doctor() -> int:
    """不开 MCP 传输，只打印端到端自检结果（排查环境用）。"""
    print("=== alipay-devtools doctor ===")
    print(f"minidev        : {discovery.cli_path()}  ({minidev.version()})")
    print(f"IDE app        : {discovery.app_path()}")
    print(f"IDE running    : {discovery.ide_running()}")
    try:
        root = proj.resolve_project(None)
        print(f"project        : {root}")
        print(f"app_id         : {proj.guess_app_id(root)}")
    except Exception as exc:  # noqa: BLE001
        print(f"project        : ⚠️ {exc}")

    eps = discovery.endpoints()
    print(f"endpoints      : {len(eps)}")
    for ep in eps:
        print(f"  - port={ep.port} project={ep.project} token={'有' if ep.token else '无'}")
    if not eps:
        print("  （没有端点：确认 IDE 里项目已运行、模拟器有画面）")
        return 1

    async def probe() -> int:
        try:
            client = await cdp.get_client(None)
            layers = await client.resolve_layers(refresh=True)
            await client.view_context(refresh=True)
            layers = await client.resolve_layers()
            offset = await client.view_offset()
            stack = await client.try_evaluate(_JS_PAGE_STACK, cdp.LOGIC)
            print(f"simulator      : {client.state.simulator.title}")
            print(f"layers         : {layers}")
            print(f"view_offset    : {offset}")
            print(f"page_stack     : {stack}")
            data = await client.screenshot(to_view=True)
            print(f"screenshot     : {len(data)} bytes, size={_png_size(data)}")
            await cdp.shutdown()
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ CDP 自检失败: {exc}")
            return 1

    return asyncio.run(probe())


def main() -> None:
    parser = argparse.ArgumentParser(prog="alipay-devtools", description=__doc__)
    parser.add_argument("--doctor", action="store_true", help="只做环境自检，不启动 MCP")
    args = parser.parse_args()

    if args.doctor:
        sys.exit(_doctor())

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
