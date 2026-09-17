#!/usr/bin/env python3
"""不依赖 IDE 的自检：验证纯逻辑部分（裁剪、CLI 参数拼装、项目解析）。

跑法::

    uv run python tests/selfcheck.py

需要 IDE 在跑的那部分（CDP 通道）请用 `uv run alipay-devtools --doctor`。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alipay_devtools import cdp, minidev, project  # noqa: E402

PASSED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        sys.exit(1)


def png(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_crop() -> None:
    print("\n[cdp._crop]")
    data = png(790, 1690)
    out, note = cdp.CDPClient._crop(data, (10, 89, 375, 623), 1)
    check("按顶层坐标裁出可视区", (out, note) != (data, "") and "已裁出 375x623" == note, note)

    from PIL import Image

    check("裁剪结果尺寸正确", Image.open(io.BytesIO(out)).size == (375, 623))

    out2, note2 = cdp.CDPClient._crop(data, (600, 1500, 375, 623), 1)
    check("越界裁剪框会退回整帧并报原因", out2 == data and "超出画面" in note2, note2)

    out3, note3 = cdp.CDPClient._crop(b"not an image", (0, 0, 10, 10), 1)
    check("坏数据不抛异常，只报原因", out3 == b"not an image" and "裁剪失败" in note3, note3)


def test_scale() -> None:
    print("\n[dpr 缩放]")
    data = png(790, 1690)
    # dpr=2 时同一逻辑坐标要乘 2，超出画面就该报原因而不是裁错
    out, note = cdp.CDPClient._crop(data, (10, 89, 375, 623), 2)
    check("dpr=2 时按物理像素裁", "已裁出 750x1246" == note, note)


def test_build_argv() -> None:
    print("\n[minidev.build_argv]")
    argv = minidev.build_argv("build", "/tmp/proj", app_id="2021004156616335")
    check("build 带 project 与 app-id",
          "-p" in argv and "/tmp/proj" in argv and "-a" in argv and "2021004156616335" in argv,
          str(argv))
    check("build 默认加 machine-output", "--machine-output" in argv, str(argv))

    argv2 = minidev.build_argv("preview", "/tmp/proj", "2021", page="/pages/a/a",
                               auto_push=False)
    check("preview 带 page", "--page" in argv2 and "/pages/a/a" in argv2, str(argv2))
    check("布尔 False 转成 --no- 形式", "--no-auto-push" in argv2, str(argv2))

    try:
        minidev.build_argv("upload", "/tmp/proj", None)
        check("缺 app_id 时报错", False, "没抛异常")
    except ValueError as exc:
        check("缺 app_id 时报错", "app_id" in str(exc), str(exc))

    try:
        minidev.build_argv("不存在的动作")
        check("未知动作时报错", False, "没抛异常")
    except ValueError as exc:
        check("未知动作时报错", "支持" in str(exc), str(exc))


def test_project(tmp: Path) -> None:
    print("\n[project 解析]")
    (tmp / "app.json").write_text(json.dumps({
        "pages": ["pages/index/index", "pages/a/a"],
        "subPackages": [{"root": "package/x", "pages": ["y/y"]}],
        "tabBar": {"items": [{"pagePath": "pages/index/index", "name": "首页"}]},
    }), encoding="utf-8")
    (tmp / "pages" / "index").mkdir(parents=True)
    (tmp / "pages" / "index" / "index.axml").write_text("<view>hi</view>", encoding="utf-8")
    (tmp / "pages" / "index" / "index.js").write_text("Page({data:{a:1}})", encoding="utf-8")
    (tmp / "package" / "x" / "y").mkdir(parents=True)
    (tmp / "package" / "x" / "y" / "y.js").write_text("// x", encoding="utf-8")
    (tmp / "node_modules").mkdir()
    (tmp / "node_modules" / "junk.js").write_text("needle 不该被搜到", encoding="utf-8")
    (tmp / "mini.project.json").write_text('{"format":2}', encoding="utf-8")

    root = project.resolve_project(str(tmp))
    check("能解析出项目根", root == tmp.resolve(), str(root))

    pages = project.pages(root)
    routes = [p.route for p in pages]
    check("主包 + 分包页面都枚举到",
          routes == ["pages/index/index", "pages/a/a", "package/x/y/y"], str(routes))
    check("页面文件存在性判断正确",
          pages[0].files and ".axml" in pages[0].files and not pages[1].files,
          str([p.files for p in pages]))

    info = project.project_info(root)
    check("project_info 统计主包页数", info["app_json"]["pages"] == 2, str(info["app_json"]))
    check("project_info 带分包", info["app_json"]["sub_packages"][0]["root"] == "package/x")
    check("project_info 带 tabBar", info["app_json"]["tab_bar"][0]["name"] == "首页")

    page = project.read_page(root, "pages/index/index")
    check("read_page 只取存在的文件", set(page["files"]) == {"index.axml", "index.js"},
          str(page))
    check("read_page 报告缺失后缀", ".acss" in "".join(page["missing"]), str(page["missing"]))

    hits = project.search(root, "不该被搜到")
    check("search 跳过 node_modules", hits["hits"] == [], str(hits))
    hits2 = project.search(root, "needle|hi", glob="*.axml")
    check("search 命中项目内文件",
          len(hits2["hits"]) == 1 and hits2["hits"][0]["file"] == "pages/index/index.axml",
          str(hits2))

    try:
        project.safe_join(root, "../../etc/passwd")
        check("路径越界会被拦下", False, "没抛异常")
    except project.ProjectError:
        check("路径越界会被拦下", True)

    try:
        project.read_page(root, "pages/不存在/不存在")
        check("读不存在的页面会报错", False, "没抛异常")
    except project.ProjectError:
        check("读不存在的页面会报错", True)


def test_endpoint_url() -> None:
    print("\n[discovery.Endpoint]")
    ep = cdp.discovery.Endpoint(port=52177, token="abc-def")
    check("带 token 时拼进查询串",
          ep.ws_url == "ws://127.0.0.1:52177/?_token=abc-def", ep.ws_url)
    ep2 = cdp.discovery.Endpoint(port=1, token=None)
    check("无 token 时不带查询串", ep2.ws_url == "ws://127.0.0.1:1/", ep2.ws_url)


def main() -> None:
    test_crop()
    test_scale()
    test_build_argv()
    with tempfile.TemporaryDirectory() as td:
        test_project(Path(td))
    test_endpoint_url()
    print(f"\n全部通过：{len(PASSED)} 项 ✅")


if __name__ == "__main__":
    main()
