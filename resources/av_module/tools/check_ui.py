"""测试台 UI 静态自检（不需要浏览器）。

做三件事：
1. 抽出 static/index.html 里的内联 <script>，用 node --check 做语法检查；
2. 校验 JS 里 getElementById/$()/querySelector 引用的 id 在 HTML 里都存在；
3. 校验前端调用的 /api/av/* 在后端 av_server 路由表里都存在。

用法：python tools/check_ui.py [node路径]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

NODE_DEFAULT = r"C:\Users\32807\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"


def extract_scripts(html: str) -> list[str]:
    out = []
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
        if "src=" in m.group(1).lower():
            continue
        out.append(m.group(2))
    return out


def main() -> int:
    node = sys.argv[1] if len(sys.argv) > 1 else NODE_DEFAULT
    html_path = os.path.join(ROOT, "static", "index.html")
    html = open(html_path, encoding="utf-8").read()
    scripts = extract_scripts(html)
    js = "\n;\n".join(scripts)
    print(f"index.html: {len(html)} 字符 | 内联 script 块: {len(scripts)}")

    ok = True
    if os.path.isfile(node):
        fd, tmp = tempfile.mkstemp(suffix=".js")
        os.close(fd)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(js)
            res = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            print("node --check:", "OK" if res.returncode == 0 else "FAILED")
            if res.returncode != 0:
                ok = False
                print(res.stderr.strip()[:800])
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    else:
        print(f"node 不可用（{node}），跳过语法检查")

    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js_ids = set(re.findall(r'getElementById\(\s*[\'"]([^\'"]+)[\'"]', js))
    js_ids |= set(re.findall(r'\$\(\s*[\'"]([^\'"\s]+)[\'"]\s*\)', js))
    js_ids |= set(re.findall(r'querySelector(?:All)?\(\s*[\'"]#([^\'"\s]+)[\'"]', js))
    missing = sorted(js_ids - html_ids)
    print(f"HTML id: {len(html_ids)} 个 | JS 直接引用: {len(js_ids)} 个")
    print("JS 引用但 HTML 缺失:", missing or "无")
    if missing:
        ok = False

    api_calls = sorted(set(re.findall(r'[\'"`](/api/av/[a-z_/]+)', js)))
    import av_server  # noqa: E402

    routes = {getattr(r, "path", "") for r in av_server.app.routes}
    bad = [a for a in api_calls if a not in routes]
    print(f"前端调用接口: {len(api_calls)} 个 | 后端缺失: {bad or '无'}")
    if bad:
        ok = False
    print("\n结论:", "PASS" if ok else "有问题，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
