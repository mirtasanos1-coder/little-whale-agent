"""列出 av_server 的全部路由（无需启动服务）。

用法: python tools/list_routes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import av_server as s  # noqa: E402

paths = []
for r in s.app.routes:
    methods = sorted(getattr(r, "methods", []) or [])
    if not methods:
        continue
    name = getattr(r, "name", "")
    summary = (getattr(r, "summary", "") or "").strip()
    paths.append((methods[0], r.path, name, summary))

for m, p, n, sm in sorted(paths, key=lambda x: x[1]):
    print(f"{m:6s} {p:34s} {n:22s} {sm}")
print(len(paths), "routes")
