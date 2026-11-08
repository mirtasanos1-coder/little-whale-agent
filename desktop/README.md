# 小鲸鱼 Agent · Tauri 桌面壳

这是 V1.2.0 前后端分离后的**前端壳**：只负责开窗口、把 `index.html` 显示出来、
把后端进程的生死管住。所有业务逻辑都在上一层的 `backend_server.py`（Python）里。

## 目录

```
src/                    前端资源（Tauri 的 frontendDist）
  index.html            原控制台（530KB，一张页面装完），只改了 3 处，见下表
  bridge.js             ★ 桥接层：window.pywebview.api.xxx() → POST /api/xxx
  vendor/tailwind.js    Tailwind Play CDN 的本地副本（断网也能跑）
  icons/                顶栏图标
src-tauri/
  src/lib.rs            拉起并监管 backend_server.exe
  tauri.conf.json       窗口尺寸 / resources 声明
  resources/            backend_server.exe 放在这里，打包时被塞进安装目录
```

## index.html 相对原版改了哪 3 处

| 位置 | 改动 | 原因 |
| --- | --- | --- |
| 第 7 行 | `cdn.tailwindcss.com` → `vendor/tailwind.js` | 桌面端不能依赖联网 |
| 第 8 行 | 插入 `<script src="bridge.js">` | 装桥接层 |
| `loadEvOptions();` | 包进 `setTimeout(…, 1500)` | 原来在接口就绪前就调后端，开界面必弹红字 |

## 开发

```bash
# 终端 A：只跑后端（改 Python 逻辑时只重启它）
cd ..
python backend_server.py --port 5701

# 终端 B：窗口（改前端自动刷新）
npm run tauri dev
```

`tauri dev` 时 `src-tauri/src/lib.rs` 找不到 `backend_server.exe` 会往上四级找
`backend_server.py`，用本机 Python 跑一份 —— 所以前端能连上。

不装 Tauri 也能验前端：

```bash
python -m http.server 8099 --directory src     # 然后开 http://127.0.0.1:8099/index.html
```

## 打包

```bash
cd ..
python build_backend.py       # 先出 backend_server.exe 并复制到 src-tauri/resources/
cd desktop
npm run tauri build           # 出安装包：src-tauri/target/release/bundle/
```

`npx tauri build` 也可以。`tauri.conf.json` 里 `bundle.resources` 声明了
`resources/backend_server.exe`，所以**必须先把后端打出来**，否则构建会报
"resource path doesn't exist"（这是故意的，防止发出去的包没有后端）。

## 端口约定

- 后端从 `5701` 起找空端口（`5700` 留给 agent.py 的 OneBot webhook，不能占）。
- 后端启动会打印 `[backend] listening on http://127.0.0.1:5701`；
  Rust 端解析这一行，通过 `invoke('backend_info')` 把 `base` 给前端。
- 前端三步兜底找端口：`backend_info` → 扫 5701~5715 → 默认 5701。

## Rust 依赖源

`desktop/.cargo/config.toml` 把 crates.io 换成了 rsproxy 镜像。国内网络下
`index.crates.io` 经常解析失败；想换回官方源，删掉这个文件即可。
