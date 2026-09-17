#!/usr/bin/env python3
"""最小 asar 工具：列目录 / 提取文件（macOS Electron 应用解包用）。

用法:
    python3 asar.py list <app.asar> [filter]
    python3 asar.py cat  <app.asar> <inner/path>
    python3 asar.py grep <app.asar> <substring>      # 列包含该串的条目
"""

import json
import struct
import sys


def open_asar(path):
    f = open(path, "rb")
    head = f.read(8)
    if len(head) < 8:
        raise ValueError("不是 asar 文件")
    # Pickle 布局: [0:4]=4, [4:8]=header pickle 总长
    header_size = struct.unpack("<I", head[4:8])[0]
    pickle_buf = f.read(header_size)
    # pickle 内: [0:4]=payload 长度, [4:8]=字符串长度, [8:]=JSON
    json_size = struct.unpack("<I", pickle_buf[4:8])[0]
    raw = pickle_buf[8:8 + json_size]
    header = json.loads(raw.decode("utf-8"))
    data_base = 8 + header_size  # 文件数据起始
    return f, header, data_base


def walk(node, prefix=""):
    for name, meta in (node.get("files") or {}).items():
        p = f"{prefix}/{name}" if prefix else name
        if "files" in meta:
            yield from walk(meta, p)
        else:
            yield p, meta


def main():
    cmd = sys.argv[1]
    path = sys.argv[2]
    f, header, base = open_asar(path)

    if cmd == "list":
        flt = sys.argv[3] if len(sys.argv) > 3 else ""
        for p, meta in walk(header):
            if flt in p:
                print(f"{meta.get('size', 0):>10}  {p}")
    elif cmd == "cat":
        target = sys.argv[3].lstrip("/")
        for p, meta in walk(header):
            if p == target:
                f.seek(base + int(meta["offset"]))
                sys.stdout.write(f.read(int(meta["size"])).decode("utf-8", "replace"))
                return
        print(f"未找到: {target}", file=sys.stderr)
        sys.exit(1)
    elif cmd == "grep":
        import re
        sub = sys.argv[3]
        rx = re.compile(sub)
        for p, meta in walk(header):
            if int(meta.get("size", 0)) > 8_000_000:
                continue
            f.seek(base + int(meta["offset"]))
            try:
                text = f.read(int(meta["size"])).decode("utf-8", "replace")
            except Exception:
                continue
            if rx.search(text):
                hits = rx.findall(text)[:3]
                print(f"{p}  ({len(rx.findall(text))} 处) 样例={[h[:80] for h in hits]}")


if __name__ == "__main__":
    main()
