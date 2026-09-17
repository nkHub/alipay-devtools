"""项目文件层 —— 读 app.json / 页面三件套 / 任意源码，供 alipay_file 工具使用。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# 小程序页面/组件的文件后缀
EXT_ORDER = [".axml", ".js", ".acss", ".json"]
SKIP_DIRS = {".git", "node_modules", ".mini-ide", "__pycache__", ".DS_Store"}
MAX_FILE_CHARS = 60000

_APPID_RE = re.compile(r"\b(20[0-9]{14})\b")


class ProjectError(RuntimeError):
    """项目路径解析或读取失败。"""


def resolve_project(project: str | Path | None = None) -> Path:
    """解析项目根目录：入参 → ALIPAY_PROJECT_PATH → 当前目录。"""
    for candidate in (project, os.environ.get("ALIPAY_PROJECT_PATH"), Path.cwd()):
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if (path / "app.json").exists() or (path / "mini.project.json").exists():
            return path

    if project:
        path = Path(project).expanduser().resolve()
        if path.is_dir():
            return path
    raise ProjectError(
        "找不到小程序项目目录（需要含 app.json 或 mini.project.json）。"
        "请显式传 project，或设置环境变量 ALIPAY_PROJECT_PATH。"
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_text(path: Path, limit: int = MAX_FILE_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ProjectError(f"读取失败 {path}: {exc}") from exc
    if len(text) > limit:
        return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"
    return text


def safe_join(root: Path, rel: str) -> Path:
    """把相对路径限制在项目根目录内。"""
    target = (root / rel).expanduser().resolve()
    if root != target and root not in target.parents:
        raise ProjectError(f"路径越界（只能在项目内）：{rel}")
    return target


@dataclass
class PageEntry:
    route: str
    files: dict[str, str]        # 后缀 -> 相对路径
    package: str | None = None   # 分包根，主包为 None

    def as_dict(self) -> dict:
        return {
            "route": self.route,
            "path": f"/{self.route}",
            "package": self.package,
            "files": self.files,
            "exists": bool(self.files),
        }


def app_config(root: Path) -> dict:
    return _read_json(root / "app.json")


def pages(root: Path) -> list[PageEntry]:
    """枚举 app.json 里声明的全部页面（含分包）。"""
    cfg = app_config(root)
    out: list[PageEntry] = []

    def collect(routes: list[str], package: str | None) -> None:
        for route in routes:
            base = root / route
            files = {
                ext: f"{route}{ext}"
                for ext in EXT_ORDER
                if (base.parent / f"{base.name}{ext}").exists()
            }
            out.append(PageEntry(route=route, files=files, package=package))

    collect([r for r in cfg.get("pages", []) if isinstance(r, str)], None)
    for pkg in cfg.get("subPackages", []) or cfg.get("subpackages", []) or []:
        if not isinstance(pkg, dict):
            continue
        prefix = (pkg.get("root") or "").strip("/")
        routes = [f"{prefix}/{r}".strip("/") for r in pkg.get("pages", []) if isinstance(r, str)]
        collect(routes, prefix)
    return out


def guess_app_id(root: Path) -> dict:
    """尽力推断 appId：环境变量 → 项目内 json → 源码硬编码。"""
    env = os.environ.get("ALIPAY_APP_ID")
    if env:
        return {"app_id": env, "source": "env:ALIPAY_APP_ID"}

    for name in ("mini.project.json", "project.config.json", "package.json"):
        cfg = _read_json(root / name)
        for key in ("appId", "app_id", "appid"):
            if isinstance(cfg.get(key), str) and cfg[key].strip():
                return {"app_id": cfg[key].strip(), "source": name}

    for path in root.glob("*.json"):
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        m = _APPID_RE.search(text)
        if m:
            return {"app_id": m.group(1), "source": path.name}

    return {"app_id": None, "source": None}


def project_info(root: Path) -> dict:
    cfg = app_config(root)
    mini = _read_json(root / "mini.project.json")
    pkg = _read_json(root / "package.json")
    page_list = pages(root)

    tab_bar = cfg.get("tabBar") or cfg.get("tabbar") or {}
    tabs = [
        {"name": t.get("name"), "pagePath": t.get("pagePath")}
        for t in (tab_bar.get("items") or tab_bar.get("list") or [])
        if isinstance(t, dict)
    ]

    return {
        "root": str(root),
        "app_id": guess_app_id(root),
        "app_json": {
            "pages": len(cfg.get("pages", [])),
            "sub_packages": [
                {"root": p.get("root"), "pages": len(p.get("pages", []))}
                for p in (cfg.get("subPackages") or cfg.get("subpackages") or [])
                if isinstance(p, dict)
            ],
            "tab_bar": tabs,
            "window": cfg.get("window"),
            "lazy_code_loading": cfg.get("lazyCodeLoading"),
        },
        "mini_project_json": mini,
        "package_json": {k: pkg.get(k) for k in ("name", "version", "scripts") if pkg.get(k)},
        "pages_found": sum(1 for p in page_list if p.files),
        "pages_missing_files": [p.route for p in page_list if not p.files][:20],
        "markers": {
            "antmove": (root / "__antmove").exists(),
            "typescript": (root / "tsconfig.json").exists(),
            "ci": (root / "mini-ci.config.js").exists(),
        },
    }


def read_page(root: Path, route: str, exts: list[str] | None = None) -> dict:
    route = route.strip("/")
    exts = exts or EXT_ORDER
    out: dict[str, str] = {}
    missing: list[str] = []
    for ext in exts:
        path = safe_join(root, f"{route}{ext}")
        if path.exists() and path.is_file():
            out[path.name] = _read_text(path)
        else:
            missing.append(f"{route}{ext}")
    if not out:
        raise ProjectError(
            f"页面不存在或没有对应文件：{route}（尝试过 {', '.join(missing)}）"
        )
    return {"route": route, "files": out, "missing": missing}


def read_file(root: Path, rel: str) -> dict:
    path = safe_join(root, rel)
    if not path.exists():
        raise ProjectError(f"文件不存在：{rel}")
    if path.is_dir():
        return {"path": rel, "entries": sorted(
            p.name + ("/" if p.is_dir() else "") for p in path.iterdir()
        )[:200]}
    return {"path": rel, "size": path.stat().st_size, "content": _read_text(path)}


def search(
    root: Path,
    pattern: str,
    *,
    glob: str = "*.{axml,js,acss,json,ts,less}",
    max_results: int = 60,
    context_chars: int = 160,
    ignore_case: bool = True,
) -> dict:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        raise ProjectError(f"正则不合法：{exc}") from exc

    # glob 里的大括号是常见写法，展开成多条
    patterns = [glob]
    if "{" in glob and "}" in glob:
        head, _, rest = glob.partition("{")
        body, _, tail = rest.partition("}")
        patterns = [f"{head}{opt}{tail}" for opt in body.split(",")]

    hits: list[dict] = []
    files_scanned = 0
    for pat in patterns:
        for path in sorted(root.rglob(pat)):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            files_scanned += 1
            rel = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append({
                        "file": rel,
                        "line": i,
                        "text": line.strip()[:context_chars],
                    })
                    if len(hits) >= max_results:
                        return {
                            "pattern": pattern, "files_scanned": files_scanned,
                            "hits": hits, "truncated": True,
                        }
    return {
        "pattern": pattern, "files_scanned": files_scanned,
        "hits": hits, "truncated": False,
    }
