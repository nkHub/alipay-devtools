"""发现支付宝小程序开发者工具（IDE）的运行状态与调试端点。

原理
----
IDE 是 Electron 应用，模拟器调试面板（`mini-ide-emulator-devtools-frontend`）的
渲染进程命令行里带着它要连接的调试端点：

    .../mini-ide-emulator-devtools-frontend/index.html
        ?ws=localhost:52177&env=kaitian&_token=<uuid>&project=<name>&projectId=<hash>

那个端点说的是标准 Chrome DevTools Protocol（CDP 1.3 over WebSocket），
`_token` 是必需鉴权参数（缺失会被以 `1003 illegal url` 断开）。

所以「发现」= 扫进程命令行解析出 (端口, token, 项目名)。
端口每次启动都可能变，因此每次连接前都应重新发现。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "小程序开发者工具"

# 常见安装路径（macOS / Windows）
DEFAULT_APP_PATHS = [
    "/Applications/小程序开发者工具.app",
    os.path.expanduser("~/Applications/小程序开发者工具.app"),
    r"C:\Program Files\小程序开发者工具",
]

_WS_RE = re.compile(r"ws(?:%3D|=)(?:localhost|127\.0\.0\.1)(?:%3A|:)(\d+)", re.I)
_TOKEN_RE = re.compile(r"_token(?:%3D|=)([0-9a-fA-F][0-9a-fA-F-]{7,})")
_PROJECT_RE = re.compile(r"project(?:%3D|=)([^&^\"'\s]+)")
_PROJECT_ID_RE = re.compile(r"projectId(?:%3D|=)([0-9a-f]{8,})", re.I)


@dataclass(frozen=True)
class Endpoint:
    """一个可用的 IDE 调试端点。

    有两种来源：

    * `via="DevToolsActivePort"` —— Electron 标准 browser 端点，**不需要 token**，
      只要 IDE 在跑就有（推荐，最稳）。
    * `via="process"` —— 从「模拟器调试」面板渲染进程的命令行里解析出的 IDE 自有端点，
      带 `_token`；面板没打开时就没有这个进程。
    """

    port: int
    token: str | None = None
    project: str | None = None
    project_id: str | None = None
    renderer_pid: int | None = None
    path: str = "/"
    host: str = "127.0.0.1"
    via: str = "process"

    @property
    def ws_url(self) -> str:
        base = f"ws://{self.host}:{self.port}{self.path}"
        return f"{base}?_token={self.token}" if self.token else base

    def as_dict(self) -> dict:
        return {
            "via": self.via,
            "port": self.port,
            "host": self.host,
            "path": self.path,
            "token": "有" if self.token else None,
            "project": self.project,
            "project_id": self.project_id,
            "renderer_pid": self.renderer_pid,
        }


def _support_dirs() -> list[Path]:
    """IDE 的 userData 目录（DevToolsActivePort 就写在这里）。"""
    home = Path.home()
    return [
        home / "Library/Application Support/小程序开发者工具",  # macOS
        Path(os.environ.get("APPDATA", "")) / "小程序开发者工具",  # Windows
        home / ".config/小程序开发者工具",                        # Linux
    ]


def active_port_endpoint(project: str | None = None) -> Endpoint | None:
    """读 Electron 写的 DevToolsActivePort，得到标准 browser 级 CDP 端点。

    文件两行：端口 + `/devtools/browser/<uuid>`。这条路径不需要 token，
    也不依赖「模拟器调试」面板是否打开，是最稳的入口。
    """
    for d in _support_dirs():
        f = d / "DevToolsActivePort"
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not lines or not lines[0].strip().isdigit():
            continue
        path = lines[1].strip() if len(lines) > 1 else ""
        if not path.startswith("/devtools/browser/"):
            continue
        return Endpoint(
            port=int(lines[0].strip()),
            project=Path(project).name if project else None,
            path=path,
            host="localhost",  # 该端点的 Host 校验只认 localhost 或 IP
            via="DevToolsActivePort",
        )
    return None


def _ps_lines() -> list[tuple[int, str]]:
    """返回 [(pid, args), ...]；失败时返回空列表。"""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,args="],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, args = line.partition(" ")
        try:
            rows.append((int(pid_str), args))
        except ValueError:
            continue
    return rows


def _classify(args: str) -> str:
    """按 argv[0] 的可执行文件名分类。

    不能用「命令行里有没有 Helper 字样」判断：IDE 主进程会把一大段内嵌环境 JSON
    （里面全是各 Helper.app 的路径）也塞进命令行，实测有 6 万字符，用子串判定
    会把主进程误判成 helper，`ide_running()` 因此假阴。
    """
    exe = args.split(" ", 1)[0]
    base = Path(exe).name
    if base == APP_NAME:
        return "main"
    if "crashpad" in base:
        return "crashpad"
    if "--type=renderer" in args:
        return "renderer"
    if "/Contents/Frameworks/" in args:
        return "helper"
    return "other"


def ide_processes() -> list[dict]:
    """列出 IDE 相关进程（主进程 + 渲染进程 + 辅助进程）。"""
    procs = []
    for pid, args in _ps_lines():
        if APP_NAME not in args and "mini-studio-core" not in args:
            continue
        procs.append({"pid": pid, "kind": _classify(args), "args": args})
    return procs


def ide_running() -> bool:
    return any(p["kind"] == "main" for p in ide_processes())


def app_path() -> str | None:
    """解析 IDE 安装路径：环境变量 → 探测常见路径 → 从进程命令行反推。"""
    env = os.environ.get("ALIPAY_IDE_APP_PATH")
    if env and Path(env).exists():
        return env

    for path in DEFAULT_APP_PATHS:
        if Path(path).exists():
            return path

    # 从进程命令行里切出 ".app" 之前的部分
    for proc in ide_processes():
        m = re.search(r"(/[^\s]*?\.app)/Contents/", proc["args"])
        if m and Path(m.group(1)).exists():
            return m.group(1)
        m = re.search(r"(C:\\[^\s]*?小程序开发者工具)", proc["args"])
        if m:
            return m.group(1)
    return None


def cli_path() -> str | None:
    """第三方 CLI（minidev）路径。"""
    env = os.environ.get("ALIPAY_MINIDEV_CLI")
    if env and Path(env).exists():
        return env
    return shutil.which("minidev")


def endpoints() -> list[Endpoint]:
    """列出可用调试端点，按可靠性排序（browser 端点优先）。"""
    found: list[Endpoint] = []

    browser = active_port_endpoint()
    if browser is not None:
        found.append(browser)

    for pid, args in _ps_lines():
        # 只认调试前端那条命令行，它同时带 token 与项目名
        if not (APP_NAME in args and "devtools" in args):
            continue
        m = _WS_RE.search(args)
        if not m:
            continue
        token = _TOKEN_RE.search(args)
        project = _PROJECT_RE.search(args)
        project_id = _PROJECT_ID_RE.search(args)
        ep = Endpoint(
            port=int(m.group(1)),
            token=token.group(1) if token else None,
            project=project.group(1) if project else None,
            project_id=project_id.group(1) if project_id else None,
            renderer_pid=pid,
            via="process",
        )
        if ep not in found:
            found.append(ep)

    # 环境变量可强制覆盖（调试或 IDE 结构变化时的逃生口）
    env_port = os.environ.get("ALIPAY_IDE_CDP_PORT")
    if env_port:
        found.insert(
            0,
            Endpoint(
                port=int(env_port),
                token=os.environ.get("ALIPAY_IDE_CDP_TOKEN"),
                project=os.environ.get("ALIPAY_IDE_PROJECT"),
                via="env",
            ),
        )
    return found


def pick_endpoint(project: str | None = None) -> Endpoint | None:
    """选一个端点；给了 project 就尽量按项目名（目录名）匹配。"""
    eps = endpoints()
    if not eps:
        return None
    if project:
        want = Path(project).name
        for ep in eps:
            if ep.project == want:
                return ep
    return eps[0]
