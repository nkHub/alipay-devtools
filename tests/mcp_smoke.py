#!/usr/bin/env python3
"""MCP 协议层冒烟：像客户端一样起服务、列工具、真调一个工具。

这层能查出「函数没错但 MCP 注册/签名有问题」的情况（工具名、schema、stdio 启动）。

跑法::

    ALIPAY_PROJECT_PATH=/path/to/miniprogram uv run python tests/mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "alipay_ide",
    "alipay_build",
    "alipay_automator",
    "alipay_inspector",
    "alipay_screenshot",
    "alipay_navigate",
    "alipay_file",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        raise SystemExit(1)


async def main() -> None:
    # SDK 的 env=None 不会继承父进程环境，必须显式把项目/appId 传下去
    # （这也是注册进 ZCode config.json 时要在 env 里写明的原因）
    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(ROOT), "alipay-devtools"],
        env={k: v for k, v in os.environ.items()
             if k in ("ALIPAY_PROJECT_PATH", "ALIPAY_APP_ID", "ALIPAY_SHOTS_DIR",
                      "ALIPAY_IDE_APP_PATH", "ALIPAY_MINIDEV_CLI",
                      "ALIPAY_IDE_CDP_PORT", "ALIPAY_IDE_CDP_TOKEN", "PATH", "HOME")},
    )
    print("[启动 stdio 服务]")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check("initialize 成功", bool(init.server_info.name), str(init))
            print(f"      server: {init.server_info.name} {init.server_info.version}")

            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            check(f"列出 {len(tools)} 个工具", len(tools) == 7, str(sorted(names)))
            check("工具名与设计一致", names == EXPECTED_TOOLS, str(sorted(names ^ EXPECTED_TOOLS)))

            for t in tools:
                props = (t.input_schema or {}).get("properties", {})
                check(f"{t.name} 有参数 schema", bool(props), str(t.input_schema)[:120])
                check(f"{t.name} 有描述", bool(t.description), t.name)

            print("\n[真调工具]")
            r = await session.call_tool("alipay_file", {"action": "project_info"})
            text = r.content[0].text if r.content else ""
            data = json.loads(text)
            check("alipay_file(project_info) 返回 ok", data.get("ok") is True, text[:200])
            check("能读到项目页数", data["app_json"]["pages"] > 0, text[:200])
            print(f"      项目: {data['root']}  页面数: {data['app_json']['pages']}")

            r2 = await session.call_tool("alipay_ide", {"action": "discover"})
            d2 = json.loads(r2.content[0].text)
            check("alipay_ide(discover) 返回 ok", d2.get("ok") is True, str(d2)[:200])
            print(f"      端点: {[e['via'] + ':' + str(e['port']) for e in d2['endpoints']]}")

            r3 = await session.call_tool("alipay_automator", {"action": "status"})
            d3 = json.loads(r3.content[0].text)
            if d3.get("ok"):
                print(f"      模拟器: {d3['simulator']['title']} 层: {sorted(d3['layers'])}")
                check("MCP 调用能驱动模拟器", "logic" in d3["layers"], str(d3)[:200])
            else:
                print(f"      ⚠️ 模拟器未就绪（IDE 没跑项目时属正常）: {d3.get('error')}")

    print("\nMCP 协议层冒烟全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
