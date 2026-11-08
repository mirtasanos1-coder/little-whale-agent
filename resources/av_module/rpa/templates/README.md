# RPA 按钮模板目录

图像识别兜底用的小图放这里，文件名决定用途：

| 前缀 | 含义 | 示例 |
| --- | --- | --- |
| `accept_*` | "接听"按钮 | `accept_qq.png`、`accept_wechat_voice.png` |
| `hangup_*` | "挂断"按钮 | `hangup_qq.png` |

规则：

* 只放**按钮本身**的小图（建议 40~120 像素见方，包含按钮边框但**不要**包含大片背景）。
* **有两种中任意一种就能工作**：只有 `accept_*` 就能自动接听；只有 `hangup_*` 就只能挂断。
  `auto_hangup=false`（默认）时 `hangup_*` 只用于 `POST /api/av/rpa/hangup`。
* 同一个按钮可以有多个版本（不同主题/皮肤/DPI），文件名用 `accept_xxx.png` 区分即可，全部都会被尝试。
* 匹配阈值在 `config.json` 的 `rpa.match_threshold`（默认 0.80）：认不出来就降到 0.70，
  误点别的按钮就升到 0.88。缩放容差是 `rpa.match_scales`（默认试 100%/85%/115% 三档）。
* `_snapshot.png` 是 `--snapshot` 生成的参考图，匹配时会自动忽略。

## 怎么截模板（三步）

```powershell
# 1. 让 QQ/微信 停在一个"正在响铃"的画面上，然后截图（窗口区域会自动裁出来）
python tools\capture_template.py --snapshot --window QQ

# 2. 用系统画图/任意看图软件打开 rpa\templates\_snapshot.png，
#    记下"接听"按钮左上角坐标和宽高（单位：像素），然后裁出来
python tools\capture_template.py --crop accept --region 320,180,64,64

# 3. 验证能不能认出来（只匹配、不点击）
python tools\capture_template.py --test --window QQ
```

`--test` 输出里 `score` 越接近 1.0 越可靠；`x/y` 是按钮中心点，也就是将来鼠标点击的位置。

## 没有真来电怎么练手

```powershell
python tools\simulate_call.py    # 开一个假来电窗口（绿"接听"/红"挂断"）
python tools\test_rpa_image.py   # 全自动：截模板 → 匹配 → 点接听 → 验证窗口收到点击
```

## 没有模板会怎样

`rpa.image_fallback=true` 但一张模板都没有时，`/api/av/rpa/status` 的 `mode` 会显示
`image-no-template`，日志里有一条提示，**不会**点击任何东西——不会误操作。
窗口最小化时也不会被截图匹配（Win32 坐标是 -32000）。

采集期间建议保持 `rpa.dry_run=true`，确认 `--test` 的坐标对了再关掉。
