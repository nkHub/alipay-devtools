"""minidev CLI 封装 —— 支付宝小程序官方的构建/预览/上传命令行。

命令面（`minidev -h`，本机 v2.1.7）::

    build  clean-build-cache  setup-build  dev  preview  login
    remote-debug  pack  upload  audit  cancel-audit  ide  download-assets
    create  config  app

注意：`preview` / `upload` / `audit` / `cancel-audit` / `remote-debug` 的
`-a/--app-id` 是必填；`build` / `dev` / `pack` 可省。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import discovery

MAX_OUTPUT = 12000


@dataclass
class CommandResult:
    ok: bool
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "command": " ".join(self.command),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration, 2),
            "stdout": self.stdout,
            "stderr": self.stderr,
            **({"note": self.note} if self.note else {}),
        }


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    head, tail = text[: MAX_OUTPUT // 3], text[-(MAX_OUTPUT * 2 // 3):]
    return f"{head}\n…（中间省略 {len(text) - MAX_OUTPUT} 字）…\n{tail}"


# 动作 → (位置参数, {参数名: 旗标})
# 值为 True 的条目表示布尔旗标，可传 False 触发 --no-xxx。
ACTIONS: dict[str, dict[str, Any]] = {
    "build": {
        "cli": "build",
        "flags": {
            "output": "-o", "app_id": "-a", "project": "-p",
            "minify": "--minify", "typescript": "--typescript", "less": "--less",
            "compact": "--compact", "parallel": "--parallel",
            "source_map": "--source-map", "enable_split_chunk": "--enable-split-chunk",
            "enable_module_graph": "--enable-module-graph",
            "subpackage_only": "--subpackage-only", "cache_dir": "--cacheDir",
        },
        "machine_output": True,
    },
    "preview": {
        "cli": "preview",
        "flags": {
            "app_id": "-a", "project": "-p", "page": "--page",
            "page_query": "--page-query", "query": "--query", "scene": "--scene",
            "client_type": "-c", "skip_build": "-s", "auto_push": "--auto-push",
            "enable_hot_reboot": "--enable-hot-reboot",
            "ignore_http_domain_check": "--ignore-http-domain-check",
            "cache_dir": "--cacheDir",
        },
        "machine_output": True,
        "requires": ["app_id"],
    },
    "upload": {
        "cli": "upload",
        "flags": {
            "app_id": "-a", "project": "-p", "version": "-v",
            "version_description": "--version-description",
            "experience": "-e", "delete_version": "-d",
            "identity_key_path": "--identity-key-path",
        },
        "machine_output": True,
        "requires": ["app_id"],
    },
    "pack": {
        "cli": "pack",
        "flags": {"project": "-p", "dir": "--dir", "filename": "-f"},
        "machine_output": True,
    },
    "audit": {
        "cli": "audit",
        "flags": {
            "app_id": "-a", "version": "-v",
            "version_description": "--version-description",
            "version_screenshot": "--version-screenshot",
            "auto_publish": "--auto-publish",
            "test_account": "--test-account",
            "test_account_password": "--test-account-password",
            "identity_key_path": "--identity-key-path",
        },
        "machine_output": True,
        "requires": ["app_id", "version", "version_description"],
    },
    "cancel_audit": {
        "cli": "cancel-audit",
        "flags": {"app_id": "-a", "version": "-v", "identity_key_path": "--identity-key-path"},
        "machine_output": True,
        "requires": ["app_id", "version"],
    },
    "dev": {
        "cli": "dev",
        "flags": {
            "project": "-p", "app_id": "-a", "host": "--host", "port": "--port",
            "hmr": "--hmr", "output": "-o", "minify": "--minify",
            "typescript": "--typescript", "less": "--less", "compact": "--compact",
            "cache_dir": "--cacheDir",
        },
        "machine_output": True,
        "long_running": True,
    },
    "remote_debug": {
        "cli": "remote-debug",
        "flags": {
            "app_id": "-a", "project": "-p", "page": "--page",
            "page_query": "--page-query", "query": "--query", "scene": "--scene",
            "client_type": "-c", "auto_open_devtools": "--auto-open-devtools",
            "auto_push": "--auto-push", "enable_hot_reboot": "--enable-hot-reboot",
            "ignore_http_domain_check": "--ignore-http-domain-check",
        },
        "machine_output": True,
        "requires": ["app_id"],
        "long_running": True,
    },
    "ide": {
        "cli": "ide",
        "flags": {"project": "-p", "app_path": "--app-path", "lite": "--lite",
                  "project_type": "--project-type"},
        "machine_output": True,
    },
    "start_sim": {"cli": "ide", "subcommand": "start-sim", "flags": {}},
    "login": {"cli": "login", "flags": {"client_type": "-c"}},
    "create": {
        "cli": "create",
        "flags": {"name": "--name", "dir": "--dir", "yes": "--yes",
                  "stack": "--stack", "type": "--type"},
        "machine_output": True,
    },
    "download_assets": {
        "cli": "download-assets",
        "flags": {"clean_previous": "--clean-previous", "with_compiler": "--with-compiler"},
        "machine_output": True,
    },
    "setup_build": {
        "cli": "setup-build",
        "flags": {"game": "-g", "app_id": "-a"},
        "machine_output": True,
    },
    "clean_build_cache": {"cli": "clean-build-cache", "flags": {}, "machine_output": True},
    "config_get": {"cli": "config", "subcommand": "get", "index": ["config_name"]},
    "config_set": {"cli": "config", "subcommand": "set", "index": ["config_name", "value"]},
}


def build_argv(
    action: str,
    project: str | Path | None = None,
    app_id: str | None = None,
    *,
    machine_output: bool = True,
    extra_args: list[str] | None = None,
    **kwargs: Any,
) -> list[str]:
    """按动作拼出 minidev 参数表。"""
    spec = ACTIONS.get(action)
    if spec is None:
        raise ValueError(
            f"未知动作 {action!r}，支持：{', '.join(sorted(ACTIONS))}"
        )

    # 必填校验
    provided = {"app_id": app_id, **kwargs}
    missing = [name for name in spec.get("requires", []) if not provided.get(name)]
    if missing:
        raise ValueError(
            f"{action} 缺少必填参数：{', '.join(missing)}"
            "（可传 app_id，或设置环境变量 ALIPAY_APP_ID）"
        )

    argv = ["minidev", spec["cli"]]
    if "subcommand" in spec:
        argv.append(spec["subcommand"])

    for name in spec.get("index", []):
        value = kwargs.get(name)
        if value is None:
            raise ValueError(f"{action} 缺少位置参数：{name}")
        argv.append(str(value))

    flags = spec.get("flags", {})
    if project and "project" in flags:
        argv += [flags["project"], str(project)]
    if app_id and "app_id" in flags:
        argv += [flags["app_id"], str(app_id)]

    for key, value in kwargs.items():
        if key in spec.get("index", []) or value is None:
            continue
        flag = flags.get(key)
        if flag is None:
            continue
        if isinstance(value, bool):
            argv.append(flag if value else f"--no-{flag.lstrip('-')}")
        elif isinstance(value, (list, tuple)):
            for item in value:
                argv += [flag, str(item)]
        else:
            argv += [flag, str(value)]

    if machine_output and spec.get("machine_output"):
        argv.append("--machine-output")
    if extra_args:
        argv += list(extra_args)
    return argv


def run(
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = 300.0,
) -> CommandResult:
    """执行 minidev（把 argv[0] 换成真实路径）。"""
    cli = discovery.cli_path()
    if not cli:
        return CommandResult(
            ok=False,
            command=argv,
            exit_code=-1,
            stdout="",
            stderr="",
            duration=0.0,
            note="没找到 minidev。请先安装：npm i -g minidev",
        )

    real = [cli, *argv[1:]]
    started = time.time()
    try:
        proc = subprocess.run(
            real,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            ok=False,
            command=real,
            exit_code=-1,
            stdout=_truncate(exc.stdout or "" if isinstance(exc.stdout, str) else ""),
            stderr=_truncate(exc.stderr or "" if isinstance(exc.stderr, str) else ""),
            duration=time.time() - started,
            timed_out=True,
            note=f"超时 {timeout:.0f}s 被中断。若这是真机预览/调试，二维码需在真机侧扫码，"
                 "命令行会一直等待——属正常现象，可改用 IDE 内操作。",
        )
    except OSError as exc:
        return CommandResult(
            ok=False, command=real, exit_code=-1, stdout="", stderr=str(exc),
            duration=time.time() - started, note="无法启动 minidev",
        )

    return CommandResult(
        ok=proc.returncode == 0,
        command=real,
        exit_code=proc.returncode,
        stdout=_truncate(proc.stdout),
        stderr=_truncate(proc.stderr),
        duration=time.time() - started,
    )


def execute(action: str, **kwargs: Any) -> CommandResult:
    """一步到位：拼参数 + 执行。"""
    project = kwargs.pop("project", None)
    app_id = kwargs.pop("app_id", None)
    timeout = kwargs.pop("timeout", 300.0)
    machine_output = kwargs.pop("machine_output", True)
    extra_args = kwargs.pop("extra_args", None)

    argv = build_argv(
        action, project, app_id,
        machine_output=machine_output, extra_args=extra_args, **kwargs,
    )
    spec = ACTIONS.get(action, {})
    if spec.get("long_running"):
        # dev / remote-debug 是常驻进程，给短超时并说明
        timeout = min(timeout, 60.0)
    return run(argv, cwd=project, timeout=timeout)


def version() -> str | None:
    cli = discovery.cli_path()
    if not cli:
        return None
    try:
        out = subprocess.run(
            [cli, "-v"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None
