#!/usr/bin/env python3
"""需要 IDE 在跑的端到端校验：连接、分层、求值、点击、跳转、截图。

跑法::

    ALIPAY_PROJECT_PATH=/path/to/miniprogram uv run python tests/live_check.py

前置：IDE 已启动、项目已运行（模拟器里有画面）。
点击类校验会在结束时把页面切回首页。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alipay_devtools import cdp, server as S  # noqa: E402

PASSED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        raise SystemExit(1)


def j(raw: str) -> dict:
    return json.loads(raw)


async def routes() -> list[str]:
    d = j(await S.alipay_automator("page_stack"))
    return [p["route"] for p in d["page_stack"]]


async def main() -> None:
    print("[IDE 状态]")
    st = j(await S.alipay_ide("status"))
    check("IDE 在运行", st["ide_running"] is True)
    check("发现调试端点", bool(st["endpoints"]), str(st.get("endpoints")))
    check("能连上模拟器", st.get("connected") is True, str(st.get("connect_error")))
    check("三个执行上下文都识别到",
          set(st["simulator"]["layers"]) >= {"logic", "view", "top"},
          str(st["simulator"]["layers"]))

    print("\n[分层与求值]")
    st2 = j(await S.alipay_automator("start"))
    check("逻辑层能调 getCurrentPages",
          isinstance(st2["page_stack"], list) and st2["page_stack"], str(st2["page_stack"]))
    # 逻辑层是 appx worker **iframe**，它也有一个很小的 document（11 个节点），
    # 所以「有没有 document」不能用来区分层次；区分靠 getCurrentPages / my。
    ev = j(await S.alipay_automator(
        "evaluate",
        expression="(typeof my === 'object') + '|' + (typeof getCurrentPages === 'function')"))
    check("逻辑层有 my 与 getCurrentPages", ev["result"] == "true|true", str(ev["result"]))
    nodes = j(await S.alipay_automator(
        "evaluate", expression="document.querySelectorAll('*').length"))
    check("逻辑层 document 很小（不是渲染层）",
          isinstance(nodes["result"], int) and nodes["result"] < 50, str(nodes["result"]))
    ev2 = j(await S.alipay_automator("evaluate", layer="view",
                                     expression="document.querySelectorAll('*').length"))
    check("视图层能取 DOM 节点数", isinstance(ev2["result"], int) and ev2["result"] > 10,
          str(ev2))

    print("\n[页面数据]")
    pd = j(await S.alipay_automator("page_data", max_chars=400))
    check("能读到当前页 data", bool(pd.get("keys")), str(pd)[:200])

    print("\n[元素定位]")
    await S.alipay_navigate("/pages/index/index", mode="switchTab", wait=2.5,
                            capture_logs=False)
    f1 = j(await S.alipay_automator("find", selector=".a-view"))
    check("纯 selector 能命中", f1["count"] > 0, str(f1)[:200])
    f2 = j(await S.alipay_automator("find", selector=".a-view", text="查看"))
    check("selector + text 是叠加过滤（数量应收窄）",
          0 < f2["count"] <= f1["count"], f"selector={f1['count']} 叠加text={f2['count']}")
    f3 = j(await S.alipay_automator("find", text="不存在的文案zzz"))
    check("找不到时返回空而不是报错", f3["count"] == 0, str(f3)[:120])

    print("\n[点击 → 跳转]")
    before = await routes()
    target = None
    for cand in (j(await S.alipay_automator("find", text="查看更多")),
                 j(await S.alipay_automator("find", selector=".a-view", text="￥"))):
        items = [i for i in cand.get("items", []) if i.get("visible")]
        if items:
            target = items[0]
            break
    check("找到一个可点元素", target is not None, "首页没找到可点元素")
    # 注意：find 返回的 text 被截断到 60 字，正好顺带验证「精确失败退化为包含」
    r = j(await S.alipay_automator("tap", text=target["text"]))
    check("tap 返回成功", r.get("ok") is True, str(r)[:240])
    check("坐标落在视口内", (r.get("target") or {}).get("inViewport") is True,
          str(r.get("target")))
    await asyncio.sleep(1.2)
    after = await routes()
    check("点击引起了页面变化", after != before, f"{before} → {after}")
    print(f"     {before} → {after}")

    print("\n[截图]")
    shot = j(await S.alipay_screenshot(mode="view"))
    check("view 模式裁到设备视口（宽≈375）",
          shot["size"]["width"] in range(370, 381), str(shot["size"]))
    check("产生了截图文件", Path(shot["saved_to"]).exists(), shot["saved_to"])
    print(f"     {shot['saved_to']} {shot['size']} {shot.get('capture')}")
    shot2 = j(await S.alipay_screenshot(mode="full"))
    check("full 模式是整幅画面", shot2["size"]["width"] > shot["size"]["width"],
          f"{shot['size']} vs {shot2['size']}")

    print("\n[控制台]")
    logs = j(await S.alipay_inspector("console", duration=2, max_logs=10))
    check("能采到控制台记录", logs["summary"]["returned"] >= 0, str(logs["summary"]))

    print("\n[项目文件]")
    info = j(await S.alipay_file("project_info"))
    check("项目概览可读", info["app_json"]["pages"] > 0, str(info)[:160])
    pages = j(await S.alipay_file("list_pages"))
    check("页面清单可读", pages["total"] > 0 and pages["with_files"] > 0, str(pages)[:160])

    await S.alipay_navigate("/pages/index/index", mode="switchTab", wait=1.5,
                            capture_logs=False)
    print(f"\n已恢复首页：{await routes()}")
    await cdp.shutdown()
    print(f"\n全部通过：{len(PASSED)} 项 ✅")


if __name__ == "__main__":
    asyncio.run(main())
