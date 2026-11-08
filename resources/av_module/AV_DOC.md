# AV_DOC — 模拟音视频通话模块（独立微服务）

> 目标：让 AI 能在 QQ / 微信里"真的"打语音和视频通话 —— 不逆向腾讯协议，
> 而是用**虚拟外设欺骗**（虚拟声卡 / 虚拟摄像头）+ **客户端自动化接管**（RPA 接听）实现。
>
> 本模块是**完全独立**的微服务：不 import、不修改主控台的 `agent.py` / `backend_server.py` /
> Tauri 代码，只通过 `http://127.0.0.1:5800` 的 HTTP 接口对外提供服务。

---

## 0. 目录结构

```
音视频接入模块/
├─ av_server.py              # 微服务入口（FastAPI，端口 5800，37 个路由）
├─ av_core/                  # 全部核心逻辑（可被主控台直接 import 复用）
│  ├─ config.py              # 配置（config.json + AV_* 环境变量）
│  ├─ winutil.py             # 提权/UAC、管理员检测、杀软检测、彩色控制台
│  ├─ drivers.py             # 驱动规格、检测（多重证据）、静默安装
│  ├─ fetch.py               # 驱动下载（多镜像）、解压、MANIFEST 生成
│  ├─ audio_io.py            # 设备枚举、WASAPI 回环录音、播放器
│  ├─ tts.py                 # edge-tts → SAPI 回退、WAV 缓存
│  ├─ stt.py                 # faster-whisper 懒加载、静音切句流式听写
│  ├─ video.py               # 虚拟摄像头推流、图片动效、字幕
│  ├─ rpa.py                 # uiautomation 监听通话弹窗 + 图像识别兜底点击
│  ├─ screen.py              # 截图/模板匹配/点击（DPI 感知，opencv + pyautogui）
│  ├─ pipeline.py            # 各模块编排（发出/收听/推流状态机）
│  └─ events.py              # 事件总线（SSE 推给 UI / 主控台）
├─ install_drivers.py        # 模块零：检测 + 下载 + 提权静默安装（CLI）
├─ drivers/                  # 驱动安装包仓库（打包时整个目录带走）
│  ├─ fetch_drivers.py       # 只下载不安装
│  ├─ MANIFEST.json          # 文件清单 + sha256 + 静默参数 + 许可
│  ├─ VBCABLE_Setup_x64.exe
│  └─ obs-virtualcam-2.0.5-Windows-installer.exe
├─ rpa/templates/            # 图像兜底的按钮模板（accept_*.png / hangup_*.png）
├─ selftest.py               # 模块一/二/三 命令行自测
├─ tools/                    # 开发期回归脚本（不影响运行）
│  ├─ stt_probe.py           # TTS→扬声器→回环→whisper 全链路自检（tiny）
│  ├─ stt_quality.py         # 固定句集 → 关键词命中率 + 字错率
│  ├─ probe_stream.py        # 流式听写端到端：连播句子 → 看切句是否完整
│  ├─ probe_listen.py        # 阻塞听写 / 流式听写 / RPA 三合一回归
│  ├─ probe_sse.py           # SSE 事件流 + 字幕回归
│  ├─ probe_api.py           # 打一遍关键接口：错误码/并发/帧率
│  ├─ check_ui.py            # 测试台静态自检：JS 语法/id 引用/接口对账
│  ├─ capture_template.py    # 截屏 → 裁出接听/挂断按钮模板 → 自检匹配
│  ├─ simulate_call.py       # 假来电窗口（没有真 QQ 也能测 RPA）
│  ├─ test_rpa_image.py      # 全自动验证：图像识别 → 真点击接听
│  └─ list_routes.py         # 列出全部路由
├─ av_server.py / run.bat    # 一键启动
├─ static/index.html         # 本地测试台（浏览器 UI，单文件无外部依赖）
├─ assets/                   # 推流用图片（人设图放这里）
├─ runtime/                  # 运行期产物：config 缓存、tts_cache、models、preview.jpg
├─ requirements.txt          # 全量依赖（含 faster-whisper，约 305MB）
└─ requirements-lite.txt     # 精简依赖（不含 STT，约 125MB）
```

---

## 1. 快速开始

### 1.1 一键启动（推荐）

```bat
run.bat
```

`run.bat` 会自动：①首次运行装依赖（清华镜像）②检查虚拟驱动 ③启动服务并打开浏览器
`http://127.0.0.1:5800`。

### 1.2 手动启动

```powershell
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python install_drivers.py --check          # 看虚拟设备装了没
python install_drivers.py --install        # 没装就装（会弹 UAC）
python av_server.py                        # 默认 127.0.0.1:5800
```

常用启动参数：

| 参数 | 说明 |
| --- | --- |
| `--host 127.0.0.1` | 监听地址；要给局域网/主控台远程调用可改 `0.0.0.0` |
| `--port 5800` | 端口（默认 5800，与主控台 5700 隔离） |
| `--token xxx` | 开启鉴权：所有 `/api/av/*` 需带 `x-av-token` 头或 `?token=` |
| `--rpa` | 启动时顺带开启自动接听线程 |
| `--open` | 启动后自动打开浏览器测试台 |
| `--log-level debug` | 日志级别 |

> 模型、TTS 缓存放 `runtime/`，删掉即可重置（不删 config.json）。

### 1.3 依赖与体积控制

| 文件 | 内容 | 体积 |
| --- | --- | --- |
| `requirements-lite.txt` | 发声 / 推流 / 驱动 / RPA（不含听写） | ≈ 80 MB |
| `requirements.txt` | 全量，含 `faster-whisper`(ctranslate2+onnxruntime+tokenizers) | ≈ 260 MB |
| 模型权重（首次自动下载） | `tiny` 75MB / `small` 480MB / `medium` 1.5GB | 落在 `runtime/models` |

听写不是必须功能：没装 `faster-whisper` 时服务照常启动，`/api/av/listen` 返回安装指引。

---

## 2. 模块零：虚拟驱动（最高优先级）

### 2.1 两个驱动分别解决什么

| 驱动 | 作用 | 安装后出现的设备 |
| --- | --- | --- |
| **VB-Audio Virtual Cable** | 电脑内部的"虚拟音频线" | 播放端 `CABLE Input`、录音端 `CABLE Output` |
| **OBS Virtual Camera** | 注册一个 DirectShow 虚拟摄像头 | 摄像头列表里的 `OBS Virtual Camera` |

### 2.2 声音是怎么进到对方耳朵里的

```
edge-tts 生成 PCM ──► 播放到 【CABLE Input】 ──(VB-Cable 内部环回)──► 【CABLE Output】
                                                                          │
                                            QQ/微信 的"麦克风"选 CABLE Output ┘
                                                                          ▼
                                                                      对方听到 AI
```

所以**只需要把 QQ/微信的麦克风设成 `CABLE Output`**（扬声器保持真实耳机/音箱，
模块用系统回环抓对方声音，见 §3.2）。视频同理：QQ/微信摄像头选 `OBS Virtual Camera`。

### 2.3 下载安装包

```powershell
python drivers/fetch_drivers.py            # 全部；等价于 install_drivers.py --download
python drivers/fetch_drivers.py --only vbcable
python drivers/fetch_drivers.py --manifest # 只重新生成 MANIFEST.json
```

- 下载源不写死：VB-Cable 走 `download.vb-audio.com` 官方直链（zip，自动解压出 x64 安装器）；
  OBS Virtual Camera 走 GitHub Releases，并按 `ghproxy.net` / `gh-proxy.com` /
  `mirror.ghproxy.com` 依次兜底（国内直连 GitHub 基本超时）。
- 支持断点式重试、`.part` 临时文件、大小/可执行头校验。
- 全部失败时会打印每个源的具体报错 + 官网手动下载地址。

### 2.4 静默安装（含 UAC 提权）

```powershell
python install_drivers.py --check          # 只检测（退出码 0=齐全 1=缺）
python install_drivers.py --download       # 只下载
python install_drivers.py --install        # 检测 → 缺则装（自动提权）
python install_drivers.py --install --yes  # 同上，不问确认
python install_drivers.py --install --force # 已装也重装（驱动坏了/升级用）
python install_drivers.py --json           # 机器可读，方便主控台调用
```

安装逻辑（`av_core/drivers.py::install_driver`）：

1. **检测是否已装**（多重证据，避免误判）：
   - 注册表 `MMDevices\Audio\{Render,Capture}` 里的友好名是否含 `CABLE`/`VB-Audio`
   - PnP 设备列表（`Get-PnpDevice`）是否含虚拟声卡/摄像头
   - 虚拟摄像头再叠加 CLSID `{A3FCE0F5-3493-419F-958A-ABA1250EC20B}` 与
     `pyvirtualcam` 实际开一次摄像头探测
2. 找安装包（本地 `drivers/` 优先；没有就自动先下载）。
3. 检查数字签名（`Get-AuthenticodeSignature`）：`hash_mismatch` 会**拒绝安装**。
4. 非管理员 → `ShellExecuteW(runas)` 提权一个**子进程**来做静默安装并等它退出
   （用户点"否"→ `ERROR_CANCELLED=1223`，脚本明确提示"被取消"，不假装成功）。
5. 静默参数：
   - VB-Cable：`VBCABLE_Setup_x64.exe -i -h`（`-i` 安装驱动、`-h` 静默）
   - OBS Virtual Camera：`obs-virtualcam-2.0.5-Windows-installer.exe /S`
6. **装后验证**：以 1s 间隔最多轮询 10 次设备是否出现 —— 以"设备真的出现"为准，
   不信安装器退出码（VB-Cable 即使成功也常返回 1，已在代码里注明）。
7. 失败时输出红字指引（见 §2.5），并保留安装日志。

### 2.5 被 360 / 火绒 / Defender 拦截怎么办

脚本内置 5 条红字指引，要点：

1. 弹窗出现"发现风险/是否允许"时选 **允许 / 添加信任 / 恢复并添加白名单**；
2. 360：`防护中心 → 信任区` 或 `木马查杀 → 恢复区 → 恢复并信任`；
3. 火绒：`防护中心 → 程序执行控制 → 信任`；
4. Defender：`Windows 安全中心 → 病毒和威胁防护 → 保护历史记录 → 允许`；
5. 仍不行就临时关闭实时防护 → 装完再打开。

同时会列出当前运行的安全软件（360tray / 360sd / zhudongfangyu / huorong / qqpcmgr /
avp / mcshield / msmpeng...），让你知道"到底是谁拦的"。

> 注意：**UAC 弹窗必须真人点"是"**，脚本无法绕过（也不应该绕过）。
> 被拒绝时输出的是"缺少管理员权限"，不会误报成杀软拦截。

### 2.6 驱动打包清单（打进行政安装包时用）

| 文件 | 大小 | SHA-256（当前内置版本） | 静默参数 | 许可 |
| --- | --- | --- | --- | --- |
| `drivers/VBCABLE_Setup_x64.exe` | 0.89 MB | `734c35dfa6d98f48782a451633ceb471166ec70d60482fd89a1123d0ee3c4f41` | `-i -h` | Donationware（VB-Audio） |
| `drivers/obs-virtualcam-2.0.5-Windows-installer.exe` | 0.82 MB | `aae3b5ff00d18c0268799c4ad8b58f985e9bc281a08806398d771e62930ad514` | `/S` | GPL-2.0（OBS Project / obs-virtual-cam） |

合计 **1.71 MB**（`drivers/MANIFEST.json` 里有完整记录，可直接喂给打包脚本）。

- 打包建议：把 `drivers/` 整目录塞进安装包，首次运行时后台执行
  `install_drivers.py --install --yes --json`，读 JSON 判断成功与否。
- VB-Cable 属 Donationware：个人/内部使用免费，**对外分发前请阅读授权**
  （<https://vb-audio.com/Cable/>）；若不能接受，可换成同样有虚拟线的
  VoiceMeeter 或自签名的 WDM 虚拟声卡。
- 安装后通常需要**注销/重启**一次，虚拟声卡才会出现在所有软件里；OBS Virtual Camera
  不需要重启，但**正在运行的 QQ/微信必须重启**才能看到新摄像头。

---

## 3. 模块一：虚拟音频管道（语音通话）

### 3.1 输出：TTS → 虚拟麦克风

```
POST /api/av/speak {"text": "..."}  →  edge-tts 合成 mp3 → 解码 PCM
                                    →  重采样/混声道 →  写入【CABLE Input】
                                    →  QQ 从 CABLE Output 取到 → 对方听见
```

- 引擎：`edge`（微软在线音色，14 个中文音色，最自然）→ 失败自动回退 `sapi`
  （Windows 本地朗读，离线可用但机械）。
- 缓存：同一句 + 同一音色命中 `runtime/tts_cache/*.wav`，不重复联网。
- 打断：`interrupt=true`（默认）会先停掉上一条再播，避免两句话叠在一起。
- 没装 VB-Cable 时降级到系统默认扬声器播放（能听能测，但不会进对方麦克风），
  状态里 `audio.output.virtual_cable=false` 并给出 `note` 说明。

### 3.2 输入：抓"对方的声音"→ 文字

两种模式（`audio.input_mode`）：

| 模式 | 采集对象 | 适用 | 注意事项 |
| --- | --- | --- | --- |
| `loopback`（默认） | **系统默认播放设备**的 WASAPI 回环 | 推荐：QQ 扬声器保持耳机/音箱，不用改任何设置；无回声 | 需要 `soundcard`；若把系统输出静音则录不到 |
| `device` | 指定录音设备（如 `CABLE Output`） | 想把 QQ 的输出也导进虚拟线，做纯"虚拟全链路" | 需要用户在 QQ 里把扬声器改成 `CABLE Input`，容易形成回声 |

```
QQ 对方说话 → 系统扬声器（耳机）→ WASAPI 回环 → 静音检测 → 按静音切句 → faster-whisper → 文本
```

- 引擎：`faster-whisper`（CTranslate2），默认 `small` + `int8`，CPU 也能实时；
  首次会从**镜像 `hf-mirror.com`** 下载模型到 `runtime/models`（官方源在国内不通，
  代码里会自动切镜像，并用 `HF_HUB_DISABLE_XET=1` 绕开 Xet 存储的 401 问题）。
- 电平自适应：录音峰值 < 0.1 时自动放大到 0.5（最多 120×≈+41dB），解决"耳机音量小导致识别差"。
  本机 WASAPI 回环峰值实测只有 0.006~0.02，不放大基本识别不出来。
- **切句策略（流式，4 秒硬上限）**：不按固定时长硬切，而是找 **≥0.25s 的连续静音**作为句边界
  （`av_core/stt.py::_find_cut`）：有声不足 1.6s 继续攒；**连续说话最多 4s（`stt.stream_window_s`）
  强制切一次**，保证对讲机式通话不会长时间没反应。硬切会把一句话劈两半
  （"帮我把这条消息转。"+"将写给张三"），所以静音边界优先、4s 只是兜底。
  静音门限来自 `audio.silence_rms`(0.001) 与 `audio.silence_hang_ms`(250ms)，可按麦克风灵敏度微调。
- **识别质量实测**（本机 Ryzen + CPU int8，走 TTS→扬声器→回环→whisper 全链路）：

  | 模型 | 关键词命中 | 平均字错率 | 速度(RTF) | 模型体积 |
  | --- | --- | --- | --- | --- |
  | `tiny` | 2/4 | 高（低电平下明显乱码） | 0.12 | 75 MB |
  | `small`（默认） | **9/9** | **≈5.4%** | 0.43–0.50（实时） | 480 MB |

  3 句共 9 个关键词全中，标点/逗号有差异但语义完整，例如
  `"今天下午三点,我们在公司三楼会议室开个短会,讨论一下下个版本的发布计划。"`
  → 结论：**`small` 够用且能实时**；追求更低延迟/更小体积才降级 `tiny`（多数场景不建议）。

- **流式端到端实测**（TTS 连播、跑 `tools\probe_stream.py`）：
  - 静音充分时（句间 0.9s）：`你好 我是人工智能助手。` / `现在开始测试虚拟麦克风。` /
    `今天下午三点我们在三楼会议室开会。` / `讨论下个版本的发布计划。` /
    `帮我把这条消息转发给张三。` / `顺便问问他周末有没有空。` —— **6 段全是完整句子**，
    无截断、无串词（改静音切句之前同样音频会吐出 "帮我把这条条"、"将写写给张三"、
    "今天下午三点文…" 这类碎片）。
  - 碰到 4s 上限被强切时（如 6s 长句）：`今天下午三点我们再…` + `3楼会议室开会。`
    —— 文字没丢，只是句子被切成两段并各自出结果。强制切点会优先落在最后 1.2s 里
    "能量低于均值 60%" 的音节缝隙上，避免切在字中间导致模型脑补填充词
    （之前硬切在 4.0s 会出现"在这一步会议室开会"这类幻觉前缀）。
  - 代价：不足 1.6s 的短句要等下一个静音才出；连续不停说话最迟 `stt.stream_window_s`
    （默认 4s）出一段。想要更完整的整句就把该值调到 6~8（延迟变长），
    想要更快就调到 2~3（切碎概率上升）。

### 3.3 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/av/speak` | 文字 → 音频 → 推进虚拟麦克风 |
| POST | `/api/av/speak/stop` | 立即打断当前播放 |
| POST | `/api/av/test_tone` | 播一个提示音（不联网、不依赖 TTS 的链路自测） |
| POST | `/api/av/listen` | 录一段 → 转文字（阻塞到录完+转完） |
| POST | `/api/av/listen/start` | 开始**流式**听写（后台线程，SSE 推字幕） |
| POST | `/api/av/listen/stop` | 停止流式听写 |
| GET | `/api/av/listen/status` | 流式听写状态 + 已识别片段 |
| GET | `/api/av/devices` | 所有音频设备 + 是否检测到虚拟声卡 |
| GET | `/api/av/voices?locale=zh` | edge-tts 可用音色 |

```bash
# 让 AI 说话（对方能听到）
curl -X POST http://127.0.0.1:5800/api/av/speak ^
  -H "content-type: application/json" ^
  -d "{\"text\":\"你好呀，我现在用语音跟你聊\",\"voice\":\"zh-CN-XiaoxiaoNeural\"}"

# 听 6 秒，转文字
curl -X POST http://127.0.0.1:5800/api/av/listen ^
  -H "content-type: application/json" -d "{\"seconds\":6}"

# 开始 / 停止流式听写
curl -X POST http://127.0.0.1:5800/api/av/listen/start -d "{}" -H "content-type: application/json"
curl -X POST http://127.0.0.1:5800/api/av/listen/stop
```

`/api/av/speak` 返回示例：

```json
{
  "ok": true,
  "text": "你好呀，我现在用语音跟你聊",
  "engine": "edge",
  "voice": "zh-CN-XiaoxiaoNeural",
  "audio_seconds": 2.64,
  "synth_ms": 2441,
  "cached": false,
  "file": "runtime/tts_cache/377a020b04448ca12879.wav",
  "played": {
    "played": true, "seconds": 2.64, "interrupted": false,
    "device": "CABLE Input (VB-Audio Virtual Cable)", "virtual_cable": true
  },
  "total_ms": 5111
}
```

`/api/av/listen` 返回示例：

```json
{
  "ok": true,
  "text": "今天下午三点我们在公司三楼会议室开个短会",
  "language": "zh",
  "audio_seconds": 6.02,
  "rtf": 0.31,
  "segments": [{"start": 0.62, "end": 5.82, "text": "今天下午三点...", "no_speech_prob": 0.01}],
  "mode": "loopback",
  "device": "(系统默认播放设备)"
}
```

---

## 4. 模块二：虚拟摄像头推流（视频通话）

```
图片(人设图) → PIL 渲染帧（Ken Burns 缓动/呼吸/字幕） → pyvirtualcam → OBS Virtual Camera
                                                              ↓
                                              QQ/微信 摄像头选它 → 对方看到画面
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/av/stream/start` | 开始推流（不传图片则用 `assets/` 里的默认图/测试卡） |
| POST | `/api/av/stream_image` | 切换推流图片（本地路径 / http URL / base64 / data URI） |
| POST | `/api/av/stream/caption` | 底部字幕（例如把 AI 说的话同步显示） |
| POST | `/api/av/stream/stop` | 停止推流 |
| GET | `/api/av/preview.jpg` | 当前画面预览（JPEG，UI 每 0.8s 刷新） |

```bash
# 用本地人设图开推流
curl -X POST http://127.0.0.1:5800/api/av/stream/start ^
  -H "content-type: application/json" ^
  -d "{\"image\":\"assets/sample_avatar.png\",\"effect\":\"kenburns\"}"

# 换成另一张图 + 字幕
curl -X POST http://127.0.0.1:5800/api/av/stream_image ^
  -H "content-type: application/json" ^
  -d "{\"path\":\"assets/sample_avatar.png\",\"caption\":\"正在思考中...\"}"

# 换成内置测试卡
curl -X POST http://127.0.0.1:5800/api/av/stream_image ^
  -H "content-type: application/json" -d "{\"image\":\"placeholder\"}"
```

- 动效：`still`（静止）/ `kenburns`（缓慢推拉，默认）/ `pulse`（呼吸亮度）。
- 分辨率/帧率：`video.width/height/fps`（默认 1280×720@24）。
- **没有虚拟摄像头时自动 dry-run**：照样按帧率生成画面并更新 `/api/av/preview.jpg`，
  状态里 `video.dry_run=true`、`video.error`/`error_detail`/`error_hint` 说明原因，
  方便在没有 OBS 的机器上先把 UI 和业务跑通。
- 启动时若 `backend` 与驱动的实际后端不匹配（例如只装了 Unity Capture），
  会自动尝试 `obs`/`unitycapture`/自动选择三种组合，并把成功用的 backend 写进状态。

---

## 5. 模块三：客户端自动化接听（RPA）

`av_core/rpa.py` 用 `uiautomation` 轮询顶层窗口，匹配到 QQ/微信窗口后遍历按钮控件，
命中"接听/挂断"这类标签就 Invoke/Click。**取不到按钮时自动切图像识别**（§5.4）。

```
轮询(0.8s) → 窗口名/类名命中关键词 → 遍历控件(depth≤7) → 找按钮文本 ∈ accept_labels
          → 状态 ringing → 点击"接听" → 状态 in_call
          → 之后检测到"挂断" → 点击 → 状态 idle
      （控件树里一个按钮都没有 → mode 切 image → 截窗口区域 → 模板匹配 → pyautogui 点击）
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/av/rpa/start` | 启动监听（`{"dry_run": true}` 只记录不点击） |
| POST | `/api/av/rpa/stop` | 停止 |
| GET | `/api/av/rpa/status` | 状态机、`mode`(uia/image/…)、轮询次数、点击数、图像命中详情、最近 20 条日志 |
| GET | `/api/av/rpa/scan?depth=3` | **调参神器**：dump 当前所有窗口 + 控件名 + 模板状态 |
| GET | `/api/av/rpa/templates` | 模板目录/阈值/已有哪些模板/是否就绪 |
| POST | `/api/av/rpa/test_match` | **只匹配不点击**：验证模板能不能认出来、坐标对不对 |
| POST | `/api/av/rpa/snapshot` | 截当前 QQ/微信 窗口存成 `_snapshot.png`（窗口最小化则截全屏） |
| POST | `/api/av/rpa/hangup` | 显式挂断（先 UIA 再图像；不在通话中返回 409） |

默认词表（`config.json` 里改）：

```json
"rpa": {
  "window_keywords": ["QQ", "微信", "WeChat", "通话", "Call", "Voice", "Video"],
  "accept_labels": ["接听", "接听电话", "接听通话", "接受", "Accept", "Answer", "Accept call"],
  "hangup_labels": ["挂断", "挂断电话", "结束通话", "拒绝", "Hang up", "Hangup", "End call", "Decline"]
}
```

使用建议：

1. 先 `dry_run: true` 跑一遍，看日志有没有"命中窗口/命中按钮"；
2. 没命中就用 `/api/av/rpa/scan` 看真实控件名，把词加进 `accept_labels`；
3. 接听冷却 5s、挂断冷却 8s，防止连点；
4. QQ NT / 微信新版本部分通话窗口是自绘 UI，UIA 拿不到按钮 → 自动走 §5.4 的图像识别。

### 5.4 图像识别兜底（UIA 拿不到按钮时）

自绘 UI（Electron/CEF 画的通话弹窗）在 UIA 里往往**只有窗口、没有按钮**（本机 QQ
实测 `controls=0`）。这时 `rpa.py` 会自动把 `mode` 切成 `image`：截窗口区域 →
用 `opencv` 模板匹配找按钮 → `pyautogui` 点它。

**状态机 / 热切换**：

| `rpa.status().mode` | 含义 |
| --- | --- |
| `uia` | 正常走控件树（优先） |
| `image` | 控件树里没按钮，已切图像识别（每轮 80~150ms） |
| `image-no-template` | 切了图像识别但 `rpa/templates/` 里没有模板图（**不会瞎点**） |
| `image-missing` | 没装 opencv/pyautogui（`pip install pyautogui opencv-python`） |

**三种模板，任选其一即可工作**（放 `rpa/templates/`，按文件名**前缀**分类）：

| 文件名 | 作用 | 必需性 |
| --- | --- | --- |
| `accept_*.png` | 来电时点"接听" | 至少要有这一张 |
| `hangup_*.png` | 通话中点"挂断" | 只有 `auto_hangup=true` 才用 |
| `_snapshot.png` | `--snapshot` 存的参考图 | 会被自动忽略 |

**采集三步**（推荐用内置工具，别自己瞎裁）：

```bash
# 1) 让 QQ/微信 出现通话弹窗（或先开假窗口练手：python tools\simulate_call.py）
# 2) 一键截图窗口
python tools\capture_template.py --snapshot --window 通话
# 3) 按提示裁出按钮（L,T,W,H 从 _snapshot.png 里量；窗口坐标和截图坐标已按 DPI 对齐）
python tools\capture_template.py --crop accept --region 380,540,120,44
# 4) 自检：能不能认出来（只匹配不点击）
python tools\capture_template.py --test --window 通话
```

也可以直接用接口：`POST /api/av/rpa/snapshot` → `POST /api/av/rpa/test_match`。

**本机实测（假窗口全自动回归）**：

```
python tools\test_rpa_image.py
① 启动假来电窗口… 窗口 [331,245,540,380] | 接听按钮 [516,429,170,62]
② 截屏并按颜色找到『接听』按钮，裁出模板… → rpa/templates/accept_simtest.png
③ 校验模板匹配坐标 命中 score=1.0 期望中心=(601,460) 实测中心=(601,460)
④ CallWatcher(dry_run=False) 模式=image 轮询=1 点击=1 图像点击=1 状态=in_call
✔ PASS：UIA 拿不到按钮 → 图像识别命中 → pyautogui 点击成功
```

**调参要点**：

- `rpa.match_threshold`（默认 0.80）：认不出来就降到 0.70~0.75；误认就提到 0.90。
- `rpa.match_scales`（默认 `[1.0, 0.85, 1.15]`）：DPI/窗口缩放变化时靠它兜住。
- 模板**只裁按钮本体**（含文字边缘、不要带大片背景），太小（<30×20）会误匹配。
- 窗口**最小化时不会截图匹配**（Win32 坐标是 -32000），也不会在 `image-no-template`
  状态下乱点——这是有意的安全设计。
- 屏幕缩放 125%/150% 也没问题：`av_core/screen.py` 启动即 `SetProcessDpiAwareness`，
  截图、匹配、点击三套坐标系一致（本机 175% 缩放实测命中偏差 0 像素）。
- **自动挂断默认关闭**：`auto_hangup=false`。打开后接听满 `hangup_grace_s`(15s) 才会点
  挂断——否则会把刚接起来的通话自己掐了。需要主动挂断请调 `POST /api/av/rpa/hangup`。

> 合规提醒：自动接听只是替你点按钮，不修改客户端、不碰协议。请只在自己账号、
> 自己设备上使用，遵守平台用户协议。

---

## 6. 通用接口与事件

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/av/health` | 存活探针（不查设备，最快） |
| GET | `/api/av/status` | 全量状态：音频输出/虚拟声卡/麦克风、TTS/STT、视频、RPA |
| GET | `/api/av/config` | 当前配置 |
| POST | `/api/av/config` | 部分更新配置（写回 config.json） |
| GET | `/api/av/events?since=0&limit=50` | 轮询事件 |
| GET | `/api/av/events/stream` | SSE 实时事件流（UI 用） |
| POST | `/api/av/drivers/check` | 驱动检测（JSON） |
| POST | `/api/av/drivers/install` | 远程安装（默认 **403**，需 `drivers.allow_remote_install=true`） |
| GET | `/docs` | Swagger UI（自动生成的完整接口文档） |

配置更新三种写法都支持：

```bash
curl -X POST http://127.0.0.1:5800/api/av/config -H "content-type: application/json" ^
  -d "{\"audio\":{\"out_gain\":0.8}}"
curl ... -d "{\"section\":\"audio\",\"values\":{\"out_gain\":0.8}}"
```

事件类型：`server`(boot/shutdown)、`audio`(tts/speak/speak_stop/warn/listen)、
`video`(start/image/camera_open/camera_missing/stop/error)、
`rpa`(start/stop/state/accept/accepting/accepted/hangup/hungup，带 `how=uia|image` 与 `score`)、
`config`(patch)、`drivers`(…)。SSE 每条形如 `data: {"seq":17,"ts":...,"kind":"video",...}`。

---

## 7. 配置说明（config.json / 环境变量）

`config.json` 首次启动自动生成，字段都有默认值；**环境变量优先级更高**（方便容器/打包）。

| 配置项 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `server.port` | `AV_PORT` | 5800 | 端口 |
| `server.token` | `AV_TOKEN` | 空 | 非空则 `/api/av/*` 需鉴权 |
| `audio.output_device` | `AV_OUTPUT_DEVICE` | `CABLE Input` | 虚拟声卡播放端（子串匹配） |
| `audio.fallback_output_device` | `AV_FALLBACK_OUTPUT_DEVICE` | 空 | 找不到虚拟声卡时用哪个 |
| `audio.input_mode` | `AV_INPUT_MODE` | `loopback` | `loopback` / `device` |
| `audio.input_device` | `AV_INPUT_DEVICE` | `CABLE Output` | `device` 模式的录音设备 |
| `audio.loopback_device` | `AV_LOOPBACK_DEVICE` | 空 | 回环抓哪个输出（空=系统默认） |
| `audio.out_gain` | `AV_OUT_GAIN` | 1.0 | 输出增益 |
| `audio.silence_rms` | - | 0.001 | 静音判定 RMS 阈值（越低越灵敏） |
| `audio.silence_hang_ms` | - | 250 | 静音持续多久算一句结束（流式切句用） |
| `tts.engine` | `AV_TTS_ENGINE` | `edge` | `edge` / `sapi` |
| `tts.voice` | `AV_TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | 音色 |
| `tts.rate/volume/pitch` | `AV_TTS_RATE`… | `+0%`/`+0%`/`+0Hz` | 语速/音量/音调 |
| `tts.interrupt` | - | true | 新句子打断旧句子 |
| `stt.model` | `AV_STT_MODEL` | `small` | `tiny`/`base`/`small`/`medium` |
| `stt.device` | `AV_STT_DEVICE` | `cpu` | `cpu`/`cuda` |
| `stt.compute_type` | `AV_STT_COMPUTE` | `int8` | `int8`/`float16`… |
| `stt.language` | `AV_STT_LANG` | `zh` | 识别语言 |
| `stt.hf_endpoint` | `AV_HF_ENDPOINT` | `https://hf-mirror.com` | 模型下载源 |
| `stt.download_root` | `AV_STT_ROOT` | `runtime/models` | 模型目录 |
| `stt.stream_window_s` | `AV_STT_WINDOW` | 4.0 | 流式听写的**强制切句上限**（秒） |
| `video.width/height/fps` | `AV_VIDEO_WIDTH/HEIGHT/FPS` | 1280/720/24 | 推流分辨率帧率 |
| `video.backend` | `AV_VIDEO_BACKEND` | `obs` | `obs`/`unitycapture`/`akvcam` |
| `video.effect` | `AV_VIDEO_EFFECT` | `kenburns` | `still`/`kenburns`/`pulse` |
| `rpa.enabled` | `AV_RPA_ENABLED` | true | 是否允许 RPA |
| `rpa.dry_run` | `AV_RPA_DRY_RUN` | false | 只记录不点击 |
| `rpa.image_fallback` | `AV_RPA_IMAGE_FALLBACK` | true | UIA 取不到按钮时切图像识别 |
| `rpa.auto_hangup` | `AV_RPA_AUTO_HANGUP` | **false** | 接听满宽限期后自动点挂断（默认关，避免掐断通话） |
| `rpa.template_dir` | `AV_RPA_TEMPLATE_DIR` | `rpa/templates` | 按钮模板目录 |
| `rpa.match_threshold` | `AV_RPA_THRESHOLD` | 0.80 | 模板匹配阈值 |
| `drivers.allow_remote_install` | `AV_ALLOW_REMOTE_INSTALL` | false | 允许 HTTP 触发驱动安装 |

例：临时换音色 + 换模型，不改文件：

```powershell
$env:AV_TTS_VOICE="zh-CN-YunxiNeural"; $env:AV_STT_MODEL="tiny"; python av_server.py
```

---

## 8. 本地测试台 & CLI 自测

### 8.1 浏览器测试台

服务启动后打开 <http://127.0.0.1:5800>（`static/index.html`，单文件、无 CDN 依赖）：

- **发声**：文本框 + 音色下拉 + 测试音，实时看 `played.device` 是不是 `CABLE Input`；
- **收听**：录 N 秒或开流式，字幕实时冒出来（走 SSE）；
- **推流**：左边预览实时刷新，右边可切本地图 / 上传图片 / 改字幕 / 换动效；
- **驱动**：一键检测，显示每个驱动的证据、安装包、签名状态；
- **RPA**：启动/停止 + dry-run 开关 + 窗口扫描 + 图像识别测试 + 一键截图存模板；

  没有真 QQ 来电也能完整验证：一个终端 `python tools\simulate_call.py` 开假来电窗口，
  另一个终端 `python tools\test_rpa_image.py` 全自动验证"图像识别 → 点击接听"。
- 顶栏 5 个状态灯每 3 秒刷新，事件流水在底部。

### 8.2 命令行自测（无 QQ 参与）

```powershell
python selftest.py                # 环境体检 + 设备 + 驱动 + 结论
python selftest.py --tone         # 播提示音到虚拟麦克风
python selftest.py --speak "你好"  # TTS 发声
python selftest.py --listen 6     # 录 6 秒回环并转写
python selftest.py --video 5      # 推流 5 秒（无摄像头自动 dry-run）+ 存预览
python selftest.py --image assets/sample_avatar.png --video 5
python selftest.py --rpa-scan     # dump 窗口/控件，调 RPA 词表

# 回归脚本（开发用，需先起服务）
python tools\stt_probe.py         # 链路通不通（tiny，秒级）
python tools\stt_quality.py small # 识别质量打分（首次下 480MB 模型）
python tools\probe_stream.py      # 流式听写：3 句长句，看切句是否完整
python tools\probe_stream.py --short   # 流式听写：6 句短句
python tools\probe_listen.py      # 阻塞听写 + 流式听写 + RPA 三合一
python tools\probe_sse.py         # SSE 事件流 + 字幕
python tools\probe_api.py         # 接口回归：错误码/并发/帧率
python tools\check_ui.py          # 测试台自检：JS 语法/id 引用/接口对账
python tools\list_routes.py       # 37 条路由清单

# RPA 图像兜底（不需要真来电）
python tools\simulate_call.py                 # 开假来电窗口（保持前台）
python tools\capture_template.py --snapshot --window 模拟来电
python tools\capture_template.py --crop accept --region L,T,W,H
python tools\capture_template.py --test --window 模拟来电
python tools\test_rpa_image.py                # 一条命令全自动验证
python tools\capture_template.py --bench      # 模板匹配性能自检（本机 83ms/帧）
```

---

## 9. 主控台集成（不改现有代码）

任何语言/进程都能调 HTTP。主控台侧只需：

```python
import requests

AV = "http://127.0.0.1:5800"
HEAD = {"x-av-token": ""}          # 没设 token 就留空

# AI 说话（对方听到）
requests.post(f"{AV}/api/av/speak", json={"text": "你好呀"}, headers=HEAD, timeout=120)

# 对方说话 → 文字（给 agent 当输入）
r = requests.post(f"{AV}/api/av/listen", json={"seconds": 6}, headers=HEAD, timeout=180)
user_text = r.json()["text"]

# 视频：切到人设图
requests.post(f"{AV}/api/av/stream_image",
              json={"path": "assets/persona.png", "caption": "AI 正在思考…"}, headers=HEAD)
```

建议的集成方式（三选一）：

1. **旁挂进程**：主控台启动时 `subprocess.Popen([sys.executable, "av_server.py"])`，
   退出时 kill；
2. **直接 import**：`from av_core.pipeline import AVPipeline`（同机同 venv），
   不占端口、不启 uvicorn；
3. **编排**：主控台只做业务，把"该说话了/该看画面了"作为 HTTP 调过来。

健康检查/降级：

```python
try:
    requests.get(f"{AV}/api/av/health", timeout=2).raise_for_status()
except Exception:
    ...  # 模块没起来：走纯文字流程，不影响主控台
```

---

## 10. 排错 FAQ

| 现象 | 原因 / 处理 |
| --- | --- |
| 状态里 `virtual_cable: false` | 没装 VB-Cable（`install_drivers.py --install`），或装完没重启；此时声音从扬声器外放，对方听不到 |
| 对方听不到，但本机能听到 | QQ/微信 **麦克风**没选 `CABLE Output`；或选了 `CABLE Output` 但我们的 `output_device` 不是 `CABLE Input`（看 `/api/av/status` 的 `audio.output`） |
| 装驱动后设备列表没有 | VB-Cable 需要注销/重启；QQ/微信 需要重启 |
| `listen` 一直转不出字 | ①没装 faster-whisper ②模型没下载（看 `stt.error`，可 `AV_HF_ENDPOINT` 换镜像/手动放模型）③系统输出静音了（回环录制依赖它） |
| 识别结果很差 | 换 `AV_STT_MODEL=small`（默认就是）以上；确认音量（录音峰值应 > 0.05）；中文场景别用 `tiny` |
| 推流 `dry_run: true` | 没装 OBS Virtual Camera 或装了没重启 QQ；`video.error_detail` 里有具体原因 |
| QQ/微信看不到虚拟摄像头 | 摄像头列表缓存，重启 QQ/微信；或驱动安装后注销一次 |
| `pyvirtualcam` 报 `incompatible constructor arguments` | 旧版把 `device` 传给 `obs` 后端导致，本模块已自动重试不带 `device` |
| 端口 5800 被占 | `--port 5801` 或 `netstat -ano | findstr :5800` 找占用进程 |
| 360 报毒/拦截 | 见 §2.5；把 `drivers/` 和 `python.exe` 加白名单 |
| 说一句话要等 2-3 秒 | edge-tts 联网合成；可预生成常用语句缓存，或改 `tts.engine=sapi`（离线但机械） |
| RPA 不点接听 | ①`dry_run` 还开着 ②`mode=image-no-template`（去 §5.4 采模板）③窗口最小化（不会被匹配） |
| 图像识别认不出按钮 | `POST /api/av/rpa/test_match` 看 score；低了降 `rpa.match_threshold`，或按本机 DPI 重截模板 |
| 图像识别点错地方 | 模板裁太大/带背景，只裁按钮本体；把 `match_threshold` 提到 0.85~0.9 |

### 已知限制

- **RPA 优先走 UIA 控件树**：自绘 UI（部分 QQ NT 版本）取不到按钮时**自动降级到图像识别**
  （§5.4，本机已用假窗口端到端验证 score=1.0 → 点击成功）；两条路都不通才需要手工介入。
  图像方案对客户端换皮肤/改字号敏感，模板需重截。
- **不是"真协议"**：一切建立在客户端 UI 自动化 + 虚拟设备上，客户端改版/换皮肤都可能影响；
  它不抢麦、不注入进程，所以反向也意味着"无法绕过平台对自动化的检测策略"。
- **单机单会话**：一个进程一套虚拟设备，同时只能应付一个通话。
- **音频独占**：如果 QQ 里开了"独占模式"，虚拟声卡可能被占用导致我们写不进去。
- 视频目前是"图片 + 动效 + 字幕"，还没有接实时数字人/口型同步（可后续用
  MuseTalk/SadTalker 之类把帧源换成生成模型，接口不变）。
- **流式听写有延迟下限**：靠静音切句保证句子完整，所以不足 1.6s 的短句要等下一个
  停顿才出结果；对方连续不停说话时最迟 4s（`stt.stream_window_s`）强制切一刀
  （可能切开长句）。想要更低延迟可以把该值调到 2~3s（代价是整句被切碎概率上升）。
- **识别质量与电平强相关**：本机回环峰值只有 0.006~0.02，靠自动增益补救；如果把系统
  音量调到很低，识别率会明显下降。通话对方音量本身就很小时同理。

---

## 11. 开发者备忘

```powershell
# 只测后端库（不起服务）
python -c "from av_core import audio_io, video, rpa; print(audio_io.describe_devices())"

# 后端库全量编译检查
python -m compileall -q av_core av_server.py install_drivers.py selftest.py

# 看接口（服务起来后）
start http://127.0.0.1:5800/docs
```

关键实现位置：

| 功能 | 文件 |
| --- | --- |
| 提权/UAC/杀软检测 | `av_core/winutil.py` |
| 驱动检测与静默安装 | `av_core/drivers.py` |
| 驱动下载与校验 | `av_core/fetch.py` |
| WASAPI 回环 / 播放器 | `av_core/audio_io.py` |
| TTS（edge→sapi） | `av_core/tts.py` |
| STT（faster-whisper + 镜像 + 自动增益 + 静音切句） | `av_core/stt.py` |
| 推流与动效 | `av_core/video.py` |
| 自动接听（UIA + 图像兜底） | `av_core/rpa.py` |
| 截图/模板匹配/点击（DPI 感知） | `av_core/screen.py` |
| 按钮模板采集 | `tools/capture_template.py` |
| 假来电窗口 / 图像兜底全自动验证 | `tools/simulate_call.py` / `tools/test_rpa_image.py` |
| 编排 | `av_core/pipeline.py` |

---

## 12. 打包成 exe（用户机器不需要 Python）

对接主控台交付时，用户机器上不一定有 Python。本模块可以打成**单个 `av_server.exe`**
（PyInstaller onefile，约 **137MB**，内置 Python 3.11 + 全部运行时），由主控台的
`av_bridge.py` **优先拉起**，只有找不到 exe 时才退回 `python av_server.py`。

### 12.1 怎么打

主控台仓库里有 `build_av_exe.py`（`python build_av_exe.py`，85~110 秒），
`python 打包发布.py` 会在打后端之前自动调用它。产物两处一份：

```
resources\av_module\av_server.exe        ← Tauri 会把它带进安装包
dist\av_exe\av_server.exe                ← 单独分发
```

打包内容：`--noconsole --onefile`、`--collect-all sounddevice/miniaudio/pyvirtualcam/faster_whisper/ctranslate2`、
`--add-data static/assets/rpa`、隐藏导入 uvicorn 全套（loops/protocols/lifespan）+ av_core 全 13 个模块、
排除 `matplotlib/pandas/scipy/torch/tensorflow/onnxruntime/transformers` 等。
版本信息写进 exe（`VERSIONINFO` 1.3.2），避免防火墙提示里显示成 Python。

### 12.2 冻结后路径怎么变的（`av_core/config.py`）

| 变量 | 源码运行 | 打包后 |
| --- | --- | --- |
| `ROOT` | 模块目录 | `_MEIPASS`（只读临时解包目录：static/assets/rpa 模板在这儿） |
| `DATA_ROOT` | `ROOT` | `%AV_MODULE_HOME%` → 缺省 `%LOCALAPPDATA%\小鲸鱼\av_module` |
| `RUNTIME_DIR`（模型/TTS 缓存/预览/日志） | `ROOT/runtime` | `DATA_ROOT/runtime` |
| `rpa.template_dir` | 相对模块目录 | 自动改成 `<DATA_ROOT>/rpa/templates`（首次启动把包内模板复制过去） |

主控台 `av_bridge.start()` 会传 `AV_MODULE_HOME=<控制台数据目录>\av_module`，
所以绿色版整个文件夹换台机器也能带走配置和模型。

### 12.3 无控制台的两个坑（已在代码里处理）

1. `--noconsole` 时 `sys.stdout/stderr is None`，`print()` 会直接抛异常 →
   `av_server.py::_ensure_console()` 把两者重定向到 `runtime/av_server_console.log`。
2. onefile 是"引导进程 + 真服务进程"两层：`taskkill /PID <引导进程> /F` 之后服务进程可能
   还活着几百毫秒 → `av_bridge.stop()` 先 `terminate`，再轮询最多 4 秒，仍未退就
   `taskkill /T /F` 整棵树（否则 5800 会被一直占着）。

### 12.4 打包版的降级点

| 功能 | 打包版行为 |
| --- | --- |
| TTS | `edge-tts` 已内置（联网），`sapi` 走系统 COM，`tts.engine=sapi` 离线可用 |
| STT | `faster-whisper` + `ctranslate2` 已内置；**模型仍要下载**（首次 ~480MB，`hf-mirror.com`），落到 `DATA_ROOT/runtime/models` |
| VAD 过滤 | **不带 `onnxruntime`**（省 60MB）→ `stt.vad=false`，自动退回纯转写，不影响识别 |
| 虚拟摄像头 | `pyvirtualcam` 内置，仍要装 OBS Virtual Camera 驱动（需用户点 UAC） |
| 虚拟声卡 | 仍要装 VB-Cable；没装时自动外放到系统默认设备（面板标"降级"） |
| RPA | `uiautomation`/`comtypes`/`cv2`/`pyautogui` 全内置，与源码版一致 |

### 12.5 验收（本机实测）

```powershell
# 1) 无 Python 也能跑：清空 PATH/PYTHONHOME，工作目录换到 System32
$env:PATH="C:\Windows\System32;C:\Windows"; $env:PYTHONHOME=""; $env:PYTHONPATH=""
Start-Process .\resources\av_module\av_server.exe -ArgumentList "--port","5802"
Invoke-RestMethod http://127.0.0.1:5802/api/av/health
# → {"ok":true,"service":"av-module","version":"1.0.0",...}（实测 1 秒内起来）

# 2) 桥接认的是 exe
python -c "import av_bridge as b; print(b.AVBridge().server_exe(), b.AVBridge()._launcher())"
# → D:\...\resources\av_module\av_server.exe exe
```

冻结后实测通过：`/status`（tts edge+sapi 均 available、`stt.vad=false`）、`/speak`（sapi 3.2s 真出声）、
`/rpa/status`（`available=true`，`mode` 随窗口状态在 `none`/`image-no-template` 间切换）、
`/stream/start`（无摄像头→dry-run，`fps_actual=24.0`）、`/listen`（1.5s 音频 → whisper 正常返回，无 VAD 报错）。
