#!/usr/bin/env python3
"""看清 devtools-frontend 进程命令行里的实际调试端点写法。"""

import re
import sys
from pathlib import Path

# 从脚本位置推导仓库根：写死绝对路径的话，项目一挪就废
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alipay_devtools import discovery  # noqa: E402

for pid, args in discovery._ps_lines():
    if "devtools-frontend" not in args:
        continue
    print(f"── pid {pid}  (命令行 {len(args)} 字)")
    m = re.search(r"webviewSrc=(\S{0,600})", args)
    if m:
        print("   webviewSrc:", m.group(1)[:560])
    else:
        print("   （没有 webviewSrc）")
    for pat in ("ws=", "ws%3D", "_token", "token", "endPoint", "port"):
        hits = [mm.start() for mm in re.finditer(re.escape(pat), args)][:2]
        for i, pos in enumerate(hits):
            print(f"   [{pat}] #{i}: …{args[max(0, pos - 60):pos + 100]}…")
    print()
