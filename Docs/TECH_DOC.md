# 小鲸鱼 QQ Agent 技术文档

>本地部署的 QQ聊天机器人 · LM Studio / Ollama /云端 API + NapCat + PyWebView
> **文档版本**：V2.0.2（2026-11-08）
>上一版：2026-11-08（V2.0.1：开源包补上 core/）
>上上版：2026-11-08（V2.0.0：agent.py 拆成 core/ 九个模块）
>上上版：2026-11-06（V1.9.25：NapCat 启动误判修正与真唱合成）
>
> **版本历史**
> - V2.0.2 —— 点歌链路四修：`_share_video_target()` 加安静闸门 `share_video_idle_min`、`sing._lyrics_netease()` 音乐库取词 + `preview()` 先验词再清嗓子、`tool_web_search` 参数修正 + `_filter_relevant()` 相关性过滤、`ANTI_BOT_RULES` 与 `_TOOL_FAIL_SEEN` 双层防复读
> - V2.0.1 —— 打包修复：`step_source_zip()` 的 `DIRS` 补 `core`、根目录跳过 `_` 打头临时脚本、自检从「数条目」升级为「agent.py 与 core/ 每个模块必须都在」
> - V2.0.0 —— **架构重构**：`agent.py` 12145 → 6515 行，按职责拆成 `core/` 九个模块（`globals` / `utils` / `sender` / `prompt_builder` / `llm_client` / `tasks` / `extract` / `qzone_flow` / `pipeline`）；跨模块裸名字用 `_SYNC_NAMES` + `_sync()` 现取，保住「配置即时生效」与「测试可就地替换」两条老性质；顶部保留兼容层，`agent.xxx` 调用点零改动
> - V1.9.25 —— NapCat 启动误判修正（`launch_log_errors(after=)` 只扫本次启动、不再因 `token_dead` 提前 break、新增 `account_kicked` 判定、`qq_client_uins()` 靠数据目录活跃度识别被官方 QQ 占用的号）+ 唱歌换真唱（`sing.py::synth_sing` 拆字变调/颤音/numpy 伴奏、解码用 `miniaudio`、`_synth_read_plain` 保底、`build_backend.py` 加 `miniaudio` hidden-import —— 注意不是 spec）
> - V1.9.24 —— 记忆提取四修：`_ask_text(raw=True)` 绕开 `clean_reply` 的「记住:」剥壳、`EXTRACT_PROMPT` 六类扩写 + ★ 强制条款、端点冷却 `_EP_COOLDOWN`、`bg_slot_ok(ns=)` 命名空间与 `_MEM_SLOT` 槽位分离；新增 `_parse_memory_block` 宽容解析与 `time_point_context` 钟点换算
> - V1.9.23 —— 提取链路跟聊天对齐：`_feature_targets(feature)`（分工优先，否则 `_chain_targets()`）、`_extract_text()` 统一入口走 `_ask_text`（带顺延/加预算/去 effort 兜底）、`extract_and_store` / `extract_events` / 滚动摘要改用 `_extract_text`
> - V1.9.22 —— 回评论状态持久化：`load_state()`/`save_state()` 带 `feed` 段、`_feed_state()` 改读写模块级 `state`、`feed_comment_round()` 在评论循环开启时只入库不发送、`qzone_interact.comment()` 发完回读 `comment_list(include_mine=True)` 补 commentid、跳过自己发的「回复 @…」评论
> - V1.9.21 —— 语义记忆不再吃「连发合并」包装文案（`_mem_only = _mem_raw or raw_text` 传给 `semantic.add` / `extract_and_store` / `extract_events` / `bump_affinity_for`）、`semantic.purge_wrapped()` + `_purge_once()` 一次性清扫历史行、评论降级私聊 `no_split=True` 整条发
> - V1.9.20 —— 评论链路整体换底盘：`qzone.COMMENT_VIA_NAPCAT=False`、`qzone.comment_web()` 走 `qzone_interact.comment()`（`emotion_cgi_re_feeds` + `commentId`）、`agent._comment_at_text()` 统一「回复 @某某 ：」、`agent._deliver_comment_reply_ex()` 返回 `(ok, how, attempted)` 并做三重去重（`mem.get_comment` 前置查状态 / `_CMT_SENT` 5 分钟缓存 / 发送后无条件 `mark_comment`）、`moment_comments.remote_id` 新列
> - V1.9.19 —— 歌词层加散文打分（`_prose_score` / `_looks_like_prose`）、唱歌改由模型说收尾话（`_sing_perform` 返回 `{ok,lines,engine,note,msg}`）、主动问候/查岗/追问都带最近真实对话（`_recent_dialog` / `_greeting_repeats` / `_GREET_REPEAT`）
> - V1.9.18 —— 启动不再因官方 QQ 在跑而拒绝（删 v1.6.2 硬拦）、conflict 记忆（`mark_uin_conflict` / `conflict_uins` / `_CONFLICT_TTL=1800`）、`qq_login_takeover()` 只在用户点按钮时 `close_qq=True`、`launch()` 内 `_qrp()` 只认本次启动后的二维码
> - V1.9.17 —— 登录链路接上 NapCat WebUI（`napcat_webui.py`）、二维码内嵌 `data:image/png` + `QR_TTL_SEC=150` 倒计时自动换码、多号快速登录（`qq_login_pick`）、`qr_png()` 带 `age`/`sha1`
> - V1.9.16 —— 记忆写入与提示词分离（`_memory_text`）、自省前缀去重（`_reflect_ctx_line`）、小程序卡片读 `desc` 当标题（`_is_platform_name`）
> - V1.9.15 —— 聊天接上 function calling（`CHAT_TOOLS` / `_chat_with_tools` / `_run_chat_tool`）、事后自省带最近三轮真实对话（`_reflect_context` / `SKIP`）、`ANTI_BOT_RULES`
> - V1.9.13 —— 连发消息防抖重写（每条重置 `threading.Timer`）、合并格式带编号与媒体占位
> - V1.9.12 —— 收消息防抖合并（`_debounce_push/_debounce_wait/_debounce_flush`，`merge_delay_sec` 默认 2.5 秒）、`reply` 段查出原话（`_MSG_TEXT_CACHE` + `【你引用了历史消息：…】`）、`send_to_session(remember=True)`、发图换头像的意图提示
> - V1.9.11.2 —— 人设热切换改成真重启（`restart_agent`：stop → wait → start，返回 `restarted`/`pid`）、动手模式每次现读人设（`_agent_run_system`）、日记/永久记忆去 CQ 码（`diary._clean_cq`）
> - V1.9.9 —— 说说评论回声三层拦、`ignore_silence` 直达通道、清单只签程序本体
> - V1.9.7 —— 信任模型修正：清单必须含正在运行的主程序（取消 full_manifest 放宽）、多份清单必须全部通过、`_manifest_rels` 兼容 resources 布局、恢复工具先验签再动手、guard.seal 存 32+16 指纹
> - V1.9.6 —— 安装包误判「被篡改」修复：清单按覆盖范围区分全量/resources-only，候选清单逐个尝试，新增 `--verify-self` 无界面自检
> - V1.9.5 —— 三处防护真生效：沙箱拦截挪位、换锁检测、旧密钥表去明文；主程序子目录拦截
> - V1.9.4 —— 增量包 `_update_tmp` 备齐 + `Move-Item` 原子换 + `Invoke-Rollback`（rc 8/9）、`signing.verify()` release 缺 sig 即 tampered、`guard.seal` 独立密钥 + `guard_audit.log` + `set_guard`、`_load_shards()` 就地展开 + `_mask_of()`、`sandbox.py` 受限模式、`xor1:` → DPAPI、`V2_SUNSET_VERSION`
> - V1.9.3 —— 密钥构建期派生并切成 8 片异或（源码/机器码里无整串钥匙）、4 把诱饵 + 4 个陷阱环境变量 → 假盐、`binding=False` 让诱饵可见、`_self_in_package()` 堵子目录自检欺骗、增量包 `scripts` 哈希 + ps1 自校验、`guard_sig` 让五个防护开关关不掉
> - V1.9.2 —— 主题 `<style>` 挂 body、`_relaxClipped()` 放开截断、验签路径闭环（`VERIFIED_BACKEND`）、增量包走本机主程序验签、构建期 SECRET 编进 `.pyd`、旧胶囊导入警告
> - V1.9.1 —— 外壳验签（ed25519-dalek）、清单进 resources 且缺清单即拦、增量包签名 + `--verify-delta`、BUILD_SECRET 每版滚动 + 多盐迁移、胶囊口令透传
> - V1.9.0 —— 口令胶囊（scrypt+AES-GCM v3）、Ed25519 清单签名、强制 API Token、CSP 明确策略 + `safeUrl()`、Cython 编译四模块
> - V1.8.12 —— 真唱三档引擎与自动降级、多音字规则、自我操作、情感三档与情感画像
> - V1.8.16 —— 预设 v2（主题+特效+布局）、`langs/` 进包 + `i18n_builtin_en` 兜底、翻译空白归一化
> - V1.8.15 —— 情感中枢保存抢修（签名 + 关键字传参）、歌词提取三路并进、预设命中率提示
> - V1.8.11 —— 预设/追问/主动关心接线修复、base 自愈、队列落盘、增量包一键安装
> - V1.8.10 —— `loadSettings()` 拆 `_loadSettingsBody()` + `try/finally`（闸门不再被异常永久关死）；`call()` 闸门最多等 4 秒；`savePreset`/`delPreset` 写 `localStorage['xj_layout_presets']` 镜像，`applyPreset` 用 `_layoutSig()`/`_layoutDiffCount()` 如实报告挪了几张；`#swStickerGenSel` 以响应真实值回填（`dataset.saving` 防回灌）；`agent.py` 新增追问模块（`followup_mark/clear/loop`、`followup.json`）；`bridge.js` `pingBase`/`invalidateBase`/`rpcOnce`+重试/20 秒心跳。
> - V1.8.9 —— 卡片身份改 `data-card-id`（43 张，全局唯一）；`applyCardOrder()` 物理重排（`removeChild` 全摘 → `insertBefore` 插回）+ `overflowAnchor='none'` + 无条件写回 `scrollTop` + `blur()` activeElement；新增 `lock_layout`；`tts_prob` / `spark_allow_plain` 修正
> - V1.8.8 —— `_ndef(v,d)` 消灭假值陷阱（72 处，44 键 `LEGIT_ZERO` 白名单）；外观状态机 `STORE=_store()` / `renderAll()` / `commitTheme|commitFx|commitLook` + `_seqTheme|_seqFx|_seqLook`；`resetLook()` 清落盘值+CSS 变量+dataset+节点
> - V1.8.7 —— 启动探活 `/api/health` → `/api/ping`（`SPAWN_PING_MS = 5000` / `SPAWN_PING_TRIES = 10`）；`_health_plugins()` 缓存 + `_health_warmer()` 45 秒预热，冷启动 `return {}` 不阻塞；`last_boot_error()` 把真实报错塞进原生框；`_fnum_live(key, default)`（0 是合法值）；`isSettingsLoaded` 保存闸门。
> - V1.8.6 —— 排版预设改成 DOM 顺序即真相；应用预设前后保 `scrollTop` + `overflow-anchor`
> - V1.8.5 —— 语音与文字互斥（语音发出去就不补文字）；快路由默认关 + 连着聊不接管 + 模型复核；主动关心 `sec<=60` 无条件发、`_loop_log` 探针；说说评论转私聊兜底；`step_delta_zip` 增量包。
> - V1.8.4 —— 鼠标指针（`ui/ui-cursor.css` 三态 PNG base64，禁用 JS 跟随）；设置项保存守卫（`SG_AUTO` 表 → 替用户点真按钮，防止自拼 key 静默失效；`SG_DIRTY` + 呼吸高亮 + 离开拦截）；记忆多账号隔离（`memory_accounts()` 五处汇聚 + `get_memory_data(user_id=)` 只查私聊 + 缓存先清后拉）；`napcat_proc.py`（判据收紧 + Job Object `KILL_ON_JOB_CLOSE` + 提权清理）
> - V1.8.3 —— 删掉 `#titlebar` 独立横条（三颗窗口按钮搬进 `<header>` 的 `.tb-actions`，id 不变；拖动改用 `header[data-tauri-drag-region]`；`calc(100vh - 133px)` 去掉）；`napcat_mgr.launch()` 加 `takeover` 参数，只在用户亲手点的 3 个入口传 `True`，自动拉起的 2 条路径一个都不带
> - V1.8.2 ——无边框从「配置写了但运行时被改回去」修到根上（默认值三处 +启动先问 `get_decorations`真实状态 +老配置带 `_frameless_migrated`标记迁移）；自绘标题栏改 SVG描边图标 + `data-state`双态图标 +悬停/按下微动效 +呼吸点 +渐变底线；`PROVIDERS`新增 `"ollama"`并把本地/云端判据统一成 `LOCAL_KEYS = ("local", "ollama")`（8处）；`token_est`加「记忆」组两行（图谱提取 +150 /图谱注入 +100）
> - V1.8.1 —— 记忆查看新增「关系网」子页（统计条 + `A →(谓语)→ B` 表格 +「立即补建」，`Api` 342→344）；QA 遗留清零（新闻里混备案号、公告版本脱节、「暂不确定卸载」、审计哈希漏 4 个字段且查不出尾部删行）
> - V1.8.0 —— 知识图谱记忆（kg_entities / kg_relations +多跳检索 +跟着记忆与胶囊导出导入）、自定义音色（gpt_sovits / cosyvoice / fish / openai / simple，失败静默降级 edge-tts）
> - V1.1.2 —— 回复超时 60s + 后台提取闸门 + 去重只认「已发」 + 群聊补回带历史 + 会话聚合 + 待办来源/提示词 + 界面定宽 + 启动过场
> - v1 —— 基础收发消息 + SQLite 记忆
> - v2 —— 修复 400 / 模型切换 / JSON 人设
> - v3 —— 活人感：私聊补回、引用回复、自动说说、表情包
> - v4 —— 设置中心（主题/图标/自启）、日记与永久记忆、群聊绑定、在线更新、公告栏、@ 上下文修复
> - v5 —— 永久记忆「新事实顶掉旧事实」、记忆三档策略、云端 API 接入 + 用量看板 + 超限拦截、NapCat 自动检测/一键安装、公告弹窗 + 通知中心、日记自由编辑、记忆导出/导入/重编译、卸载保留数据
> - v6 —— 背景自定义（可扩展）、多语言（可外部扩展）、主动关心计划（场景感知）、说说配图 + 评论回复、生图表情包、用量明细、记忆多格式导入导出、所有「打开目录」修复、云端模型切换修复
> - v7 —— 模型来源重做（本地/云端双开关 + 优先级链 + 故障顺延）、拉取模型改成真下拉、浏览动态（点赞/评论/追评）、记忆目录 `memory/`、主动关心计划与主动回复可视化管理、临时⇄永久记忆互转、背景换图即时生效
> - v8 —— 主动回复重做（静默门槛可配）、好感度系统、英文界面修复、公告堆叠、说说条数自定义、日记升级、生图走当前模型链路、一键卸载
> - v8.5/V1.1.0 —— 见「V1.1.0 技术变更」
> - **V1.1.1** —— 见下一节「V1.1.1 技术变更」
> - **V1.2.0** —— 前后端分离：无头后端 `backend_server.py`（FastAPI）、`app_ui.py` 剥掉 GUI、环境自适应探测、依赖安装向导后端（见下一节）
> - **V1.3.2** —— QA 整改：三道安全闸（Origin 白名单/强制 JSON/一次性票据）、配置热更新（70 个派生常量重算）、音视频模块随包分发（见文首新章）
> - **V1.4.0** —— 时光胶囊（`.spark` 打包/搬家/融合）、Computer Use Agent（ReAct + 4 个工具 + 动作账本 + 急停）、主动打电话护栏（见文首新章）
>
> 二次开发请看 `开发者指南.md`（模块原理、怎么改、怎么构建打包）。


### V1.4.0：时光胶囊、Computer Use Agent、主动打电话

#### 1. 三个新模块在进程里的位置

```
backend_server.exe（FastAPI，5701）
  ├─ app_ui.py            Api 层：新增 spark_* / agent_* 共 17 个方法
  ├─ spark.py             时光胶囊（纯文件 + SQLite，不依赖界面）
  ├─ computer_use.py      Agent 工具集 + agent_actions 账本 + 急停
  ├─ qq_call.py           主动打电话护栏，调 av_bridge
  └─ agent.py             ReAct 主循环（run_agent_task / agent_wanted）
        └─ av_bridge → resources\av_module\av_server.exe（5800）→ av_core.rpa.call_now
```

#### 2. 时光胶囊（`spark.py`）

| 步骤 | 做什么 | 关键点 |
|---|---|---|
| 收集 | `mem_transfer.export_data()` + `personas.get_card()` + `list_faces()` + 配置白名单 | `personas.json` 只有简卡，**正文必须按 key 取全卡** |
| 打包 | 临时目录 → `zipfile.ZIP_DEFLATED` | 必须有 `manifest.json`；`format_version` 高版本直接拒绝 |
| 导入 replace | 先 `mem_transfer.export_to_file()` 备份 → `import_data(mode="replace")` → 人设 → 配置 | 配置只在 replace 时写回（merge 不动你的本机设置） |
| 导入 merge | `import_data(mode="merge")` | 记忆按唯一键去重，不会加两遍 |

配置白名单 = 前缀 `tts_/affinity_/agent_/humanize_/quote_/care_plan_/memory_/draw_/moment_/route_memo_/imagegen_/web_search/game_`，
**排除** `provider_keys`、`cloud_api_key`、`imagegen_api_key`、`codeword*`、`data_dir`、`web_port`。

#### 3. Computer Use Agent

工具表（`computer_use.TOOLS` + `qq_call.TOOLS`）：

| 工具 | 开关 | 参数 | 说明 |
|---|---|---|---|
| `web_search` | `agent_web` | `query, limit` | **先 HTTP 并发抓（bing/cnbing/ddg，单 6s、总 9s），拿不到才开浏览器**（总预算 16s）；v1.5.2 收尾把顺序反过来，实测 52s → 6.4s |
| `open_url` | `agent_web` | `url` | 有浏览器就用浏览器抓标题与首屏文字；没有就退 HTTP 读正文 |
| `play_game` | `agent_keyboard` | `action(click/move/type/key/scroll/screenshot)` | pyautogui；中文用剪贴板粘贴（`_paste_text`） |
| `execute_code` | `agent_code` | `code` | AST 白名单：禁 `import os/sys/subprocess`、`open`、`eval/exec`、`while`、双下划线属性 |
| `set_config` | `agent_code` | `key, value` | 让模型代开控制台开关（`_SETTABLE` 白名单：生图/生图方式/假装打字/拆条/引用/关心计划/说说/好感度/语音） |
| `voice_call` / `video_call` | `agent_call_enabled` | `name` | 见第 4 节 |
| `hangup_call` | 同上 | — | RPA 点挂断 |

ReAct 协议（`agent.py`）：

```
模型每步只输出一行：
  ACTION: 工具名 {"参数": ...}
或
  FINAL: 给用户的话
```

- 解析用 `json.JSONDecoder().raw_decode`，所以参数里有花括号也不会断；
- 每步结果回灌成「第 N 步 工具 → 结果」，连续两次失败会追加「换个法子」提示；
- 每步记录里带 `items`（搜到几条）与 `via`（走的哪条路），界面/测试都能看出真假；
- 走满 `max_agent_steps`（默认 10）强制要 `FINAL`，防止无限绕；
- 危险工具（键鼠/代码/打电话）走 `approval_bus`：后端挂起等前端弹窗，**逐个动作确认**，
  拒绝或 120 秒超时抛 `DangerDenied` 终止整条任务（v1.5.2 起没有「全部放行」的捷径）。

动作账本 `agent_actions`（`<数据目录>/agent/agent.db`）：
`id, ts, kind, action, args, result, ok, ms, screenshot, session`。
前端「Agent」页 40 条滚动列表 + 截图 data URL 预览（后端只允许读 `agent_shots/`）。

急停：`EmergencyStop` + `stop_now()`；守护线程用 `GetAsyncKeyState` 轮询 `Ctrl+Alt+K`（不装驱动、不要管理员），
触发后所有 `tool_*` 立即返回「已被紧急停止」。

#### 4. 主动打电话（`qq_call.py`）

```
dial(kind, name)
  → guard()：总开关 → 时段（支持 22→9 跨零点；start==end 视为「只那一小时」）→ 每小时配额
  → online_check()：NapCat get_friend_list 的 online 字段；拿不到就 known=False 放行但记日志
  → ensure_av()：微服务没起就真 BRIDGE.start()
  → POST /api/av/rpa/call {kind, name}（av_server 走 av_core.rpa.call_now）
  → _mark_called()：写 call_state.json（最近一小时的拨打时间戳）
```

`hangup()` 走 `/api/av/rpa/hangup`；`on_incoming()` 只在事件是「未接」且 `agent_call_autocallback` 开时才回拨。

#### 5. 怎么验证（不进界面也能验）

```powershell
python tests\test_v140_agent.py     # 24 项：参数落盘、真开 Chromium 搜索、沙箱、急停
python tests\test_v132_qc.py        # 7 项：版本单一来源、热更新、好感度、记住:、备份、av exe、密钥加密
python D:\临时文件\_t_spark.py      # 24 项：胶囊导出→空目录 replace 还原→merge 不重复→坏包不崩
```

---

### V1.3.2 QA 整改：三道安全闸、配置热更新、随包音视频模块

QA 报告里 13 个 BUG，这一版连同集成时撞出的 5 个一起修完。挑有技术含量的记三件事。

#### 1. 后端的三道安全闸（BUG-03 / 04 / 05 / 06）

V1.3.1 之前的攻击链很直白：任意网页 `fetch` 到 `127.0.0.1:5701/api/run_uninstall`
→ 因为 CORS 通配，能读到 `/api/health` 里的 `backend.confirm_token` 明文
→ 带上 `X-Confirm-Action` 重放 → **真卸载**。

现在三层同时上：

| 闸 | 位置 | 规则 |
|---|---|---|
| Origin 白名单 | `@app.middleware("http") _security_mw` | 只认 `tauri://localhost`、`https?://tauri.localhost`、`http://(127.0.0.1|localhost):<web_port>`（网页版控制台，端口按配置动态取）。带别的 Origin 一律 403 `bad_origin`；**没有 Origin 却标了 `Sec-Fetch-Site: cross-site`** 才 403 `cross_site`（V1.3.2 踩过：无条件拒 `cross-site` 会把 `tauri.localhost` 页面对 `127.0.0.1:57xx` 的请求全打死，因为 WebView2 判的是「站点」不是「主机」） |
| Content-Type 限制 | 同上 | 非 GET/HEAD/OPTIONS 必须 `application/json`，否则 415 —— 挡掉「表单跨站提交」这条经典路径（`text/plain` 也能表单提交，所以只看 Origin 不够） |
| 一次性票据 | `POST /api/confirm/ticket` + `_consume_ticket()` | 破坏性方法（`run_uninstall`/`apply_update`/`rollback_update`/`memory_rebuild_run`/`*_import*`）必须带 `X-Confirm-Ticket`。票据 30 秒过期，**绑定方法名 + Origin + 客户端 IP**，`pop` 即焚，重放无效。`/api/health` 里再也拿不到任何长效口令，只有 `confirm_via: "ticket"` |

另外两道：
- **免责协议**（BUG-03）：`_disclaimer_block()` 在通用分发最外层，`disclaimer_accepted != True` 时除豁免集（`get_disclaimer`/`accept_disclaimer`/`get_about`/`ping`/`health`/`methods`/`confirm_ticket`）和 `get_*` 只读接口，一律 403 `disclaimer_required`。为什么保留 `get_*`：界面要靠 `get_status` 才能把「同意」弹窗画出来，全封会锁死软件。
- **密钥与暗号**（BUG-05/06）：`set_cloud_key` 写盘前 `security.encrypt_secret()`（DPAPI，`dpapi:` 前缀），日志只打 `mask_secret()`；启动时 `security_migrate_plaintext_keys()` 把老明文就地加密。管理员暗号改成「连错 5 次锁 10 分钟 + 1/2/5/30 秒退避」，计数存在配置里（重启不清零），`set_codeword` 响应不再回显明文。

> 验收脚本 `_t_sec1.py` 用 urllib 打真实 HTTP，27 项断言全过（含「票据与方法名不匹配则失效」「锁定期间正确暗号也不放行」）。

#### 2. 配置热更新：从「改完要重启」到「下一句话就生效」（BUG-01）

历史包袱：`agent.py` 顶层有 70 个 `常量 = _CFG.get("key", 默认)`，import 时算一次，之后再也没人刷新它。
表现为「界面上改了、文件里也写了、程序里还是旧的」，必须重启 Agent。

做法（不重构成依赖注入，风险太大）：

```python
def _live_all():
    # 文件指纹变了才真读配置
    if _stamp_changed():
        cfg = _load_config()
        _reload_constants(cfg)      # ← 新增：把 70 个派生常量按新配置重算
        _cache = cfg
    return _cache
```

`_reload_constants(cfg)` 用 `globals()` 就地赋值，保留原来的推导式（`HUMANIZE_DELAY` 的排序、`CHECKIN_*` 的夹取、`providers.resolve()` 派生的 `LM_BASE/LM_MODEL/LM_API_KEY` 都在里面，行为完全等价）。
高频开关（`humanize_enabled`/`quote_enabled`/`care_plan_enabled`/`affinity_enabled`）不进全局量，改成每条消息 `_live_cfg(...)` 实时读 —— 这几个改得最快、也最需要「立刻」。

**防回归**：验收脚本用 `ast` 扫出所有「import 期由 `_CFG` 派生的顶层名字」，断言 `_reload_constants` 源码里都有 `g["名字"]`，缺一个就失败。以后谁加配置常量忘了注册，测试直接红。

#### 3. 音视频模块随包分发（第四阶段）

**第一版只带了 0.62MB 的源码，用户机器上没有 Python 就用不了 —— 验收时被驳回（原话「绝对不能要求用户电脑上装 Python」），所以改成打独立 exe。**

`build_av.py`（PyInstaller `--onefile`）产出 **`resources/av_module/av_server.exe`，145.2MB**，自带 Python 运行时与全部依赖：

| 类别 | 打进包里的 |
|---|---|
| Web | `fastapi` / `starlette` / `pydantic` / `uvicorn`（含 loops/protocols/lifespan 子模块）+ `h11` / `httptools` / `wsproto` / `websockets` |
| 语音合成 | `edge_tts`（+ `edge_tts.communicate` / `drm`）、`aiohttp` |
| 语音识别 | `faster_whisper` / `ctranslate2` / `tokenizers` / `onnxruntime`（+ `capi.onnxruntime_pybind11_state`）、`numpy` |
| 音频设备 | `sounddevice` / `soundcard` / `miniaudio` |
| 屏幕与自动化 | `PIL`（Image/ImageDraw/ImageFont）、`pyvirtualcam`、`uiautomation`、`comtypes`、`pyautogui` |
| 数据 | `static/`、`assets/`、`rpa/`、`drivers/` 用 `--add-data` 打进同一结构（`config.ROOT` 在 frozen 时指向 `_MEIPASS`） |

`cv2`（112MB）默认**不打**：代码里图像兜底是 lazy import，缺了自动退回 RPA 模板匹配（`/api/av/rpa/templates` 返回 `image_fallback=true`）；
需要的话 `python build_av.py --with-cv` 重打；只要最小体积 `--lite`（不带 STT / cv2）。

- 编排：`打包发布.py` 的顺序是 `step_av_exe()` → `step_backend()` → `sync_av_module()` → `tauri build`；
  `build_av.py` 会先比 mtime，`av_server.exe` 比源码新就跳过重打（打一次 2~3 分钟），拷完自动删掉 `build/`、`dist/` 中间件。
- **不要重复塞**：`build_backend.py` 检测到 exe 存在就**不再** `--add-data resources/av_module`，否则同一个 145MB 会在安装包里躺两份。
- `av_bridge.server_exe()` 的顺序：配置 `av_module_dir` → 环境变量 → exe 同级 → 数据目录 → `_MEIPASS`；
  找到 `av_server.exe` 就 `Popen([exe, "--host", "127.0.0.1", "--port", 5800])`，`cwd`=模块目录、`AV_MODULE_HOME`=`数据目录/av_module`（模型/模板/缓存都落用户目录，不写 Program Files）；
  找不到 exe 才退回 `python av_server.py`。
- 绿色版 / 安装包都带 `resources/av_module/`，用户能直接看到、也能自己换版本。
- 回收：spawn 成功后 `_assign_killjob(proc)`（`CreateJobObjectW` + `KILL_ON_JOB_CLOSE`）—— PyInstaller onefile 是「引导进程 + 真身」两个进程，Windows 的 `TerminateProcess`/`taskkill /F` **不跑 Python 的 `finally`**，只靠 `terminate()` 一定留孤儿；Job 对象在进程退出时由内核统一收掉整棵树。

实测：exe 冷启动 2 秒起服务；`/api/av/health` 200、`/api/av/status` 200（枚举输出设备）、`/api/av/voices` 200（edge-tts 在线音色）、`/api/av/rpa/templates` 200、`/api/av/listen/status` 200、`/api/av/events` 200；
PyInstaller 的 `PYZ-00.toc` 里 `faster_whisper`/`ctranslate2`/`onnxruntime`/`soundcard`/`uiautomation`/`pyvirtualcam`/`edge_tts`/`miniaudio` 全部 FOUND。


### V1.3.0 「能用」补丁：几个卡了用户很久的硬骨头

这一版不动架构，只修「写好了但跑不起来」的地方。挑四个有技术含量的记下来。

#### 1. NapCat 在 Shell 独立版上永远起不来

Shell 版（`NapCat.Shell.Windows.Node.zip`）自带 QQ 内核，启动入口是同目录的 `index.js`：
它在 `spawn` 之前会往环境里塞 `NAPCAT_WRAPPER_PATH` / `NAPCAT_QQ_PACKAGE_INFO_PATH` /
`NAPCAT_QQ_VERSION_CONFIG_PATH` / `NAPCAT_DISABLE_PIPE`，然后才去 `require` 内核。

所以正确姿势只有一条：**`node <NapCat 目录>\index.js`**，且不覆盖它的环境。
之前直接 `node napcat\napcat.mjs`，少了那几个变量，内核路径就靠猜，猜错即
`ENOENT … resources\app\versions\<ver>\package.json` → Worker 3 次崩 → 主进程退出。

还有一条老守卫要拆：V1.1.2 写的「本机 QQ 在跑但 3000 不通 → 拒绝启动」是给
「NapCat 依赖本机 QQ」的场景准备的，对 Shell 版等于无条件拒绝。现在用
`is_shell_build()` 判定后跳过，并将 stdout/stderr 落到 `napcat_launch.log`，
二维码就绪（`napcat\cache\qrcode.png` 出现）即视为启动成功。

#### 2. 前端资源什么时候才会被重新装箱

Tauri 的 `frontendDist` 是构建期嵌进二进制的。改 `src/*.js|html` 后直接 `cargo build`
**不会**重新嵌入（它认为 dist 没变，仍打印 `Finished`）。可靠做法：

```powershell
(Get-Item desktop\src-tauri\src\lib.rs).LastWriteTime = Get-Date
cd desktop; npm run tauri build        # 或 cargo build（debug）后再 native 跑
```

#### 3. 首启弹窗必须串成一条链，且不许阻塞 boot

弹窗层级：`#bootMask`（z-95）> `#popupMask`（z-40）> `#discMask`（z-70）。
把「等用户点同意」的 Promise 塞进 boot 的 `Promise.all` 会永久卡住加载层（用户看到的是
「请稍等噢」）。正确拆法：

```
boot()                      → 立刻 resolve，把弹窗链旁挂
  └─ first-run.js           → 免责协议(__DISCLAIMER_STATE) → 性能模式(__firstRunGate)
       └─ setup-wizard.js   → __setupGate（向导关掉才 resolve）
            └─ 公告         → Promise.race([__setupGate, 15min])
```

「性能模式问过没问过」要记在**后端配置**（`perf_mode_asked`），不能只记 localStorage ——
localStorage 跟 WebView2 用户目录走，换数据目录就会重复弹。

#### 4. 无边框 / 托盘为什么必须走 Rust

窗口是 `decorations:false` 建出来的，前端只能改 CSS class，真正的边框由 Win32 管。
所以这两个开关做成 Tauri 命令：

| 前端调用 | Rust 命令 | 作用 |
|---|---|---|
| `call('set_frameless', true)` | `set_decorations(window, flag)` | 当场上/下系统标题栏 |
| `call('tray_start')` | `set_tray(enabled=true)` | 建托盘图标（`tray-icon` feature） |
| 点 ✕ | `CloseRequested` | 托盘开着 → `prevent_close()` + `hide()`；否则 `stop_backend()` |

坑：`get_status` 里把 `frameless` 写死 `true` 会被 4 秒一次的轮询反复应用，
同时后端没就绪时会拿到 `undefined` 被当成 `false` → 反而加上标题栏。
规矩：**拿不到明确布尔值就别动手**，重试到拿到为止（现在最多 8 秒）。

#### 5. 七类记忆各自独立备份：格式与去重规则

`mem_transfer.py` 里有一张 `KINDS` 表，把「一类记忆」映射到「若干张表」，
所有导出/导入都走这张表，所以加一类只需要写一行：

| key | 名字 | 表 |
|---|---|---|
| `perm` | 永久记忆 | `perm_memory` |
| `temp` | 临时记忆（对话与摘要） | `messages` / `chat_log` / `summaries` / `memories` |
| `event` | 事件与关心计划 | `user_events` / `care_plans` |
| `affinity` | 好感度与关系 | `affinity` |
| `diary` | 日记 | `diary` |
| `reflect` | 自省与情绪 | `inner_thoughts` / `think_log` / `emotion_log` |
| `learn` | 学习清单 | 独立库 `learn.db` 的 `learn_items` |

- **导出**：`export_kind(mem, kind, fmt)` 产出 `{format:"xiaojingyu-memory-kind", kind, tables, counts, total}`，
  落到 `<数据目录>/memory_export/<kind>/`（`fmt="txt"` 时是人看的纯文本，明确**不可再导入**）；
- **导入**：`import_kind(mem, kind, path, mode)` 先校验「文件里到底有没有这一类」——
  拿永久记忆的备份去导日记会直接被拒（`这个文件里没有「日记」的数据`），不会污染表；
- **去重**：有主键的表按 key 判重（`{"key": "id"}`）；自省/情绪这类没有稳定主键的用
  `{"key": None}`，按「整行字段完全相同」判重，所以同一份备份导两次不会翻倍；
- **learn 例外**：它在自己的 `learn.db` 里，`_import_learn` 单独处理；
  **注意新库还没有 `learn_items` 表**，导入前要先 `_learn._conn()` 触发建表。

#### 6. 自省的参数都是从配置读的

`subconscious.py` 里所有「数值」都能在界面上改，别再写死：

| 配置键 | 默认 | 作用 |
|---|---|---|
| `subconscious_enabled` | true | 关掉就整条线程空转 |
| `subconscious_hour` / `subconscious_minute` | 3 / 0 | 每天几点几分反思（`run_loop` 每 10 分钟醒一次，命中小时且分钟够了才跑） |
| `subconscious_count` | 3 | 每次写几条（1~8，提示词里的「写 N 条」会跟着变） |
| `memory_decay_enabled` | false | 记忆模糊化总开关 |
| `memory_decay_days` | 180 | 多久没被提起算「老记忆」 |
| `memory_decay_max` | 10 | 每次最多压几条 |
| `memory_decay_len` | 15 | 压到多少字以内 |

- 手动跑：`subconscious.daily_reflection(mem, ask=..., force=True)` —— `force=True` 才绕过
  「今天已经反思过」的限制；
- `decay_memories` 以前从没拿到 `ask`（模型调用），所以永远「压缩了 0 条」；
  现在由 `app_ui._reflect_ask()` 包装 `agent.ask_model_raw` 传进去。

### V1.2.0 前后端分离：无头后端 + Tauri 前端

**一句话**：Python 退成「只提供 HTTP/WebSocket 的无头服务」，界面挪到 Tauri（Rust + Web），
这样 Windows / macOS / Linux 能共用一套前端，也不会再出现 WebView2 宿主层白屏那类问题。

#### 1. 进程结构

```
Tauri 壳（Rust）            ← 窗口、托盘、单实例、生命周期
   │  启动子进程 + 读 stdout 拿到端口
   ▼
backend_server.py           ← FastAPI + uvicorn，只监听 127.0.0.1
   │  业务全在 app_ui.Api / agent.py / memory.py（这些文件核心逻辑没动）
   ▼
agent.py（--agent 子进程）  ← QQ 收发、记忆、说说、主动关心…
```

- `app_ui.py` 不再 `import webview`，也没有窗口、托盘、DWM 标题栏这些东西了。
- `web_server.py`（V1.1.x 的 webview 兜底页）保留但不再是主路径，`backend_server.py` 是新的唯一后端入口。
- 端口：默认从 **5700 起**探，但 **5700 保留给 `agent.py:87-88` 的 OneBot 备用 webhook**
  （`WEBHOOK_HOST=127.0.0.1` / `WEBHOOK_PORT=5700`，`agent.py` 里绑这条），所以实际一般拿到 5701、5702…
  **5709 也保留**：那是 `agent.py` 的单实例锁端口（`INSTANCE_LOCK_PORT`，V1.2.1 起从 5701 挪过来）。
  后端抢了锁端口的话，Agent 会误判「已有另一个实例在跑」然后自己退出 —— 打包实测踩过，别再改回去。
  启动时打印契约行，Rust 端解析这一行拿端口：

  ```
  [backend] listening on http://127.0.0.1:5701
  [backend] health: http://127.0.0.1:5701/api/health　logs: ws://127.0.0.1:5701/ws/logs
  ```

#### 2. 环境自适应机制（换了机器/换了安装位置都能自己找东西）

所有会变的东西都集中在 `backend_server.py` 顶部几个函数里，**没有一个写死的绝对路径**：

| 要找的东西 | 探测顺序 | 代码 |
|---|---|---|
| 数据目录 | `QQ_AGENT_DATA_DIR` → 冻结时 exe 同级 → 源码同级 | `app_ui.DATA_DIR` |
| 运行目录 | `DATA_DIR/runtime`（首次启动自动建 `napcat/ cache/ models/ python/`） | `_ensure_runtime_dirs()` |
| Node | `PATH` → `%ProgramFiles%\nodejs` → `%ProgramW6432%\nodejs` → `%LOCALAPPDATA%\Programs\nodejs` → `runtime/node/node.exe` | `_node_status()` |
| pip 解释器 | `runtime/python/python.exe` → `PATH` 上的 `python` / `python3` → `sys.executable`（仅非冻结） | `_pip_python()` |
| NapCat 目录 | 配置里的 `napcat_dir` → 常见安装路径 → `runtime/napcat` | `napcat_mgr.find_install_dir()` |
| 端口 | 5700 起逐个 `bind` 试，占满则 `bind(0)` 让系统给 | `find_free_port()` |

为什么要这么多兜底：从桌面图标启动的程序，继承的 `PATH` 常常**没有** `nodejs`
（实测就遇到过「机器上明明装了 Node，服务里 `shutil.which('node')` 返回空」）。
同理 `pip`：打包版用的是 embeddable Python，`sys.executable` 不一定是能装包的那个，
所以我们优先用随包带的 `runtime/python/python.exe`。

#### 3. API 契约

**统一返回**：`{"ok": true, ...}` / `{"ok": false, "msg": "..."}`，任何方法都不会 500 拖崩服务。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 后端状态 + 当前模型 + 插件状态 + 是否需要安装向导（结构见下） |
| GET | `/api/ping` | 最轻的存活检查 |
| GET | `/api/methods` | 当前可调用的方法名（268 个） |
| GET | `/api/env/check` | 向导用：缺什么、能不能一键装、手动装的话去哪下 |
| GET | `/api/env/paths` | `runtime/` 下各个目录的绝对路径 |
| POST | `/api/env/install_tts` | 后台 `pip install edge-tts`（返回 `job` id） |
| POST | `/api/env/install_napcat` | 后台下载+解压 NapCat，装完自动写 `onebot11` 配置 |
| POST | `/api/env/set_napcat_dir` | 入口 B：用户自己装的，把路径填进来（会校验里面像不像 NapCat） |
| GET | `/api/env/job/{id}` | 查安装进度（`status/pct/msg/lines`） |
| GET | `/api/logs/tail?lines=` | 日志轮询兜底（返回 `lines` + `pos`） |
| WS | `/ws/logs` | 日志增量推送 |
| POST | `/api/{method}` | **通用分发**：`app_ui.Api` 的方法名直接当路径，如 `/api/start_agent` |
| GET | `/api/{method}` | 同上，给浏览器里手点用 |

`/api/health` 的结构（前端照着渲染就行）：

```json
{
  "ok": true,
  "backend": {"status": "running", "version": "1.2.0", "host": "127.0.0.1", "port": 5701,
              "url": "http://127.0.0.1:5701", "pid": 1234, "python": "3.11.9",
              "frozen": false, "data_dir": "...", "runtime_dir": ".../runtime",
              "log_file": ".../agent.log", "uptime_sec": 12.3, "methods": 268},
  "agent":  {"running": false, "pid": null, "webhook": "http://127.0.0.1:5700"},
  "app":    {"name": "小鲸鱼 Agent 控制台", "version": "1.1.6", "version_tag": "",
             "subtitle": "...", "ui": "tauri"},
  "model":  {"provider": "cloud", "provider_name": "云端 API", "model": "deepseek-v4.1-flash",
             "base": "https://api.21r.cn/v1", "is_cloud": true, "ready": true},
  "plugins": {
    "napcat": {"installed": true, "running": false, "http_ok": false, "ws_ok": false,
               "dir": ".../napcat", "config_ok": true, "webui": "http://127.0.0.1:6099/webui"},
    "tts":    {"installed": false, "enabled": true},
    "node":   {"installed": true, "path": "...", "version": "v26.12.1"}
  },
  "needs_setup": true,
  "setup_missing": ["语音引擎（edge-tts）"],
  "ts": 1792897197
}
```

`POST /api/{method}` 的 body 支持四种写法，前端怎么写都能用：

```js
fetch(base + '/api/start_agent', {method:'POST'})                              // 无参数
fetch(base + '/api/chat_probe',  {method:'POST', body: JSON.stringify({args:["在吗", 30]})})
fetch(base + '/api/chat_probe',  {method:'POST', body: JSON.stringify({text:"在吗", timeout:30})})
fetch(base + '/api/chat_probe',  {method:'POST', body: JSON.stringify(["在吗", 30])})
```

参数名对不上时会按顺序当位置参数传（所以位置和名字随便写一个就行）；
`TypeError` 会自动退回「不带参数调用」。开发者想加接口：**在 `app_ui.Api` 里加个方法就自动出现在
`/api/{method}` 里**，不用改后端（`getattr(api, method)` 动态转发）。
黑名单：`main` / `_run_agent_mode` / `restart_app` / `_hard_exit`（这几个会自己重启进程或退出，别从网页调）。

#### 4. `WS /ws/logs` 协议

```
连上   → {"type":"hello","file":"...agent.log","pos":1459173,"version":"1.2.0","ts":...}
有新增 → {"type":"log","lines":["[2026-10-25 10:54:53] [INFO] ...", ...],"pos":1467417,"ts":...}
出错   → {"type":"error","msg":"..."}
```

- 参数：`?tail=8000`（连上先回溯多少字节当历史）、`?from_start=1`（从头读，调试用）、`?path=...`（换文件）。
- 服务端每 0.4 秒 `read(pos)` 一次，**文件变小就当成轮转、从 0 重读**，不会因为日志被清空而卡死。
- 断线重连时把上次的 `pos` 通过 `?tail=0` + 自己过滤即可（`pos` 字段就是给这件事用的）。

#### 5. 依赖安装向导（双入口机制的「后端一半」）

首次启动前端拿 `/api/health` 看 `needs_setup` / `setup_missing`，然后弹窗给两个按钮：

- **A 一键极速安装**：`POST /api/env/install_tts` 或 `POST /api/env/install_napcat`，
  拿 `job` id 轮询 `/api/env/job/{id}` 显示进度。NapCat 装到 `runtime/napcat/`，
  装完 `napcat_mgr.write_onebot_config()` 自动写 `config/onebot11.json`（3000 HTTP + 3001 WS，都绑 127.0.0.1），
  并把 WebUI 地址/token 一起返回，引导用户去扫码。
- **B 我自己安装**：`GET /api/env/check` 里每个 item 都带 `manual_hint`（官方下载地址 / 命令），
  装好之后把路径 `POST /api/env/set_napcat_dir` 回来，或直接把 `runtime/napcat/` 解开。

前端把 `GET /api/env/paths` 返回的 `runtime_dir` 当成**所有动态下载与缓存的根目录**。

#### 6. 这一版从 `app_ui.py` 删掉了什么（8471 行 → 7598 行）

| 删掉/改掉 | 说明 |
|---|---|
| `_win_native` / `_win_hwnd` / `apply_dark_titlebar` / `apply_window_mode` / `_style_ok` / `enforce_window_mode` | Win32 标题栏与 DWM 深色，归 Tauri 管 |
| `_tray_image` / `start_tray` / `stop_tray` | 托盘挪到 Rust `tauri-plugin-tray` |
| `_activation_ping` / `_activation_serve` | 单实例挪到 Rust（`tauri-plugin-single-instance`） |
| `main()` 里的 `webview.create_window` / `webview.start` / 事件绑定 / P0-5 的 WebView2 看门狗 | 整个 GUI 入口不复存在；`main()` 现在直接 `import backend_server; backend_server.main()` |
| `Api.win_min(7670)` / `win_toggle_max` / `win_close` / `tray_start` / `win_state` / `win_resize` | 改成兼容占位，统一返回 `{"ok":true,"headless":true,"msg":"V1.2.0 起窗口由 Tauri 控制"}`，老前端调用不会炸 |
| `Api.pick_file` / `add_persona_face` / 旧 `_pick_dir` 里的 `webview.create_file_dialog` | 换成 **tkinter** 的 `filedialog`（`_pick_file` / `_pick_dir`）；前端也可以直接把路径传进来，不弹系统对话框 |
| 关于页里的 `import pywebview` | 删掉，`info["ui"]="tauri"` |
| `quit_app()` | 不再 `window.destroy()`，改成置 `_QUIT["flag"]` + `stop_agent()`，进程由 Rust 端结束 |

#### 7. 打包与桌面端（任务四/五，已落地）

目录约定（前后端在同一个仓库里，前端壳是 `desktop/`）：

```
小鲸鱼/
├─ backend_server.py        后端入口（FastAPI + uvicorn）
├─ build_backend.py         把后端打成 backend_server.exe
├─ 打包后端.bat             第 1 步：跑 build_backend.py
├─ 打包桌面端.bat           第 2 步：npm run tauri build
├─ dist/backend_server.exe  PyInstaller 产物
└─ desktop/                 Tauri 壳（Vanilla JS 模板，没有打包器）
   ├─ src/
   │  ├─ index.html         原控制台（几乎原样，只改了 3 行，见下）
   │  ├─ bridge.js          ★ 桥接层：pywebview → HTTP/WS
   │  ├─ vendor/tailwind.js Tailwind Play CDN 的本地副本（断网也能用）
   │  └─ icons/             顶栏图标
   └─ src-tauri/
      ├─ src/lib.rs         拉起后端、解析端口、关窗杀进程树
      ├─ tauri.conf.json    窗口 + resources
      └─ resources/backend_server.exe   打包时被塞进安装目录
```

**打后端**（`python build_backend.py` 或双击 `打包后端.bat`）：

```bash
python -m PyInstaller --noconfirm --clean --name backend_server \
  --onefile --console \                     # 必须 console：Rust 要读 stdout 的端口行
  --collect-submodules uvicorn \
  --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on \
  --exclude-module matplotlib --exclude-module numpy --exclude-module pandas \
  backend_server.py
```

产物 18.1MB（实测）。脚本跑完会自动复制到 `desktop/src-tauri/resources/`。
注意：`build_backend.py` 里那串 `HIDDEN` 不能省 —— uvicorn/fastapi 大量用字符串
import，PyInstaller 静态分析看不出来，漏了的表现是「能启动、一收请求就 500」。

**`tauri.conf.json` 关键三项**：

```json
"build": { "frontendDist": "../src" },
"app": { "withGlobalTauri": true, "windows": [{ "label": "main", "width": 1180, "height": 820,
          "minWidth": 960, "minHeight": 640, "theme": "Dark" }] },
"bundle": { "resources": ["resources/backend_server.exe"] }
```

`withGlobalTauri: true` 是桥接层用 `window.__TAURI__.core.invoke('backend_info')`
拿端口的前提，删了它前端就只能靠扫端口猜。

**Rust 端（`src-tauri/src/lib.rs`）做了什么**：

1. `run()` 里**先同步** `spawn_backend()`，再 `tauri::Builder` —— 窗口一出现后端就已经在跑，
   前端不用等；等端口最多 45 秒（超时也不卡死，界面会提示连不上）。
2. 后端探测顺序：`<exe目录>/backend_server.exe` → `<exe目录>/resources/backend_server.exe`
   → `<exe目录>/_up_/backend_server.exe` → 往上四级找 `backend_server.py`（`tauri dev` 用）。
3. `Command` 带 `--port 0`（后端自己找空端口）+ `CREATE_NO_WINDOW`（不弹黑窗口），
   抽干 stdout/stderr（**必须一直读，否则管道写满后端会卡死**），从
   `[backend] listening on http://127.0.0.1:57xx` 里抠出端口。
4. 前端通过 `invoke('backend_info')` → `{ok, base, pid, version}` 拿到 `base`。
5. `on_window_event(CloseRequested)` + `RunEvent::ExitRequested` 都调 `stop_backend()`，
   Windows 下用 `taskkill /PID <pid> /T /F` 杀**整棵进程树**（后端还会拉起 `agent.py`，
   只 kill 父进程会留下孤儿）。另有 `backend_restart` 命令供前端重启后端。

**打包桌面端**（`打包桌面端.bat` 或手动）：

```bash
cd desktop
npm install                 # 首次；国内建议 --registry=https://registry.npmmirror.com
npm run tauri build         # Windows → NSIS 安装包（tauri.conf.json 里 targets 已设为 ["nsis"]）
# 产物：desktop/src-tauri/target/release/bundle/nsis/小鲸鱼_<版本>_x64-setup.exe
```

**NSIS 下载超时怎么破**（第一次打包必踩）：`tauri build` 会自己去 GitHub 下 `nsis-3.13.zip`，
国内经常 `failed to bundle project: timeout: global`。两种解法：

1. 手动喂给它：把 `nsis-3.13.zip`（可用 `https://ghfast.top/<官方url>` 加速）解压后，
   **让 `makensis.exe` 直接位于 `%LOCALAPPDATA%\tauri\NSIS\`** 根目录（不是套一层 `nsis-3.13\`）。
   下次 `tauri build` 看到它就不再下载；缺失的 `nsis_tauri_utils.dll` 它会自己补。
2. 只出「绿色版」：`npm run tauri build -- --no-bundle`，产物是 `target/release/小鲸鱼.exe`，
   把 `resources/backend_server.exe` 放到它旁边的 `resources\` 里即可运行（安装包就是把这个目录打个包）。

`mainBinaryName: "小鲸鱼"` 决定安装后的 exe 名字；不设的话会跟着 crate 名变成 `desktop.exe`。
`productName` 决定安装包文件名与开始菜单项。

三平台：Windows 直接 `npm run tauri build`；macOS 在 mac 上跑同一条命令（出 .dmg/.app）；
Linux 出 .deb/.AppImage/.rpm。跨平台不支持交叉编译 —— **要哪个平台的包就在哪个平台打包**。
Rust 依赖走 rsproxy 镜像（见 `desktop/.cargo/config.toml`），删掉该文件即回官方源。

**开发调试**：

```bash
python backend_server.py --port 5701        # 终端 A：后端（热改后端逻辑时只重启它）
cd desktop && npm run tauri dev             # 终端 B：前端（窗口改了自动刷新）
```


#### 8. 前端桥接层 `desktop/src/bridge.js`（任务三）

原来 `index.html` 里 200 多处 `call('方法名', …)` 全都走 `window.pywebview.api.方法名()`，
那是 pywebview 注入的桥。Tauri 里没有 pywebview，所以**不改这些调用点**，而是给
`window.pywebview.api` 挂一个 `Proxy`：任何属性被访问都返回一个函数，调用时变成一次
`POST http://127.0.0.1:<port>/api/<方法名>`，body 是 `{"args":[...]}`。前端一行都不用改。

```
index.html  call('get_status')
               ↓  API[method](...args)
        window.pywebview.api.get_status  ← bridge.js 的 Proxy
               ↓
        POST /api/get_status  {"args": []}
               ↓
        backend_server.py  call_method()  →  app_ui.Api().get_status()
```

端口怎么来（三步兜底）：① `window.__TAURI__` 在就 `invoke('backend_info')`；
② 扫 5701→5715→5700 的 `/api/ping`；③ 都不行就用默认 5701（后续调用会给出明确错误）。

日志：不再靠 `get_logs` 每秒轮询，而是连 `ws://127.0.0.1:<port>/ws/logs?from_start=1`
增量收，本地维护一份文本缓冲；前端照旧调 `get_logs(offset)`，桥接层用缓冲里的增量回答
（`{ok, text, offset, reset}`，语义和原来完全一致，所以 `appendLog/pollLogs` 不用改）。

破坏性操作：`run_uninstall` / `apply_update` / `rollback_update` / `memory_rebuild_run` /
各类 `*import*` 必须先拿到 `/api/health` 里的 `backend.confirm_token`，以
`X-Confirm-Action` 头发回去；外部网页因为 CORS 读不到 health，敲地址栏也没用（GET 一律不带令牌）。

**对 `index.html` 的全部改动只有 3 处**（其余 530KB 原封不动）：

| 位置 | 改动 | 为什么 |
| --- | --- | --- |
| 第 7 行 | `https://cdn.tailwindcss.com` → `vendor/tailwind.js` | 桌面端断网时不能变成白板；Play CDN 的 JS 本身是浏览器端 JIT，存成本地文件也照跑 |
| 第 8 行 | 插入 `<script src="bridge.js">` | 装桥接层 |
| `loadEvOptions();`（原顶层调用） | 包进 `setTimeout(…, 1500)` | 原来在接口就绪前就调后端，一开界面右下角必弹红字「Python 接口尚未就绪」 |

**不装 Tauri 也能验前端**（调试很快）：

```bash
python backend_server.py --port 5701 &
python -m http.server 8099 --directory desktop/src
# 浏览器打开 http://127.0.0.1:8099/index.html
```

CORS 白名单允许 `http://127.0.0.1:<任意端口>`，所以这样打开能正常调后端。
（`file://` 打开不行 —— 源是 `null`，会被 CORS 挡掉，这是故意的。）


#### 9. 首次启动依赖安装向导 `desktop/src/setup-wizard.js`（任务六）

**要解决的事**：装完软件第一次打开，缺 NapCat（收不到 QQ 消息）和 edge-tts（只能打字）。
老版本只在设置页塞一行小字，小白根本找不到。现在做成全屏向导。

**后端接口**（都在 `backend_server.py`，注意是 `/api/env/*` 真路由，不走通用分发）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/env/check` | 缺什么（`items[]`）、能不能一键装（`can_auto_install`）、手动安装提示（`manual_hint`）、`runtime/napcat` 默认目录、`python_for_pip` |
| POST | `/api/env/install_napcat` | `{dir}` → `{job}`；后台下载→解压→自动写 `onebot11` 配置→取 WebUI 地址 |
| POST | `/api/env/install_tts` | `{}` → `{job}`；后台 `pip install edge-tts` |
| POST | `/api/env/set_napcat_dir` | `{dir}`；校验目录里有没有 `napcat.bat`/`NapCatWinBootMain.exe` 后写进配置 |
| GET | `/api/env/job/{id}` | 轮询：`pct` / `msg` / `lines[]` / `status`(pending\|running\|done\|failed) / `result` |
| GET | `/api/env/paths` | `runtime/`、`napcat/`、`cache/`、`models/`、数据目录、日志路径 |

**前端结构**：

```
SetupWizard.open()/auto()
  └── mount()   注入 <style> + #swMask 遮罩（z-index 99999，不碰 index.html 原有 DOM）
        ├── napcatCard()  状态徽章 + 一键安装 / 浏览… / 启动 NapCat / 打开 WebUI
        ├── ttsCard()     一键安装（pip）/ 跳过
        └── progressHtml()  进度条 + 最近 8 行日志（失败行标红、自动滚到底）
```

**关键实现点**：

1. **不阻塞**：检查、安装、轮询全是 `fetch`（异步），安装期间界面照常刷新日志，不会出现「未响应」。
2. **进度是真的**：`napcat_mgr.download_and_install` 改成按 256KB 分块读，每 2% 回调一次
   「正在下载 xxx：12.3 / 40.0 MB（31%）」；`backend_server._pct_from_msg()` 把文案里的百分比
   映射到整体 5~92% 区间（下载占大头，解压/写配置占剩下），所以进度条是走的，不是转圈。
3. **两种安装入口**：`一键自动安装`（后端下载）与`浏览…`（Tauri 原生目录对话框
   `invoke('plugin:dialog|open', {options:{directory:true}})`，拿不到再退回后端 `pick_dir` 的 tkinter 对话框）。
4. **什么时候弹**：启动后 `pywebviewready` + 600ms（等桥接层拿到端口和令牌）调 `check`；
   `needs_setup` 为真才弹。点过「稍后再说」的 **24 小时内不再自动弹**（`localStorage.setupWizardSkipped`），
   但 `window.SetupWizard.open()` 随时能叫出来。
5. **不放行半成品**：底部「进入控制台」在所有必装项就绪前是 disabled；右上角 ✕ 会列出还差什么再问一次。
   进入控制台时顺手 `start_agent()`，等于老 `--autostart`。
6. **装完的下一步**：NapCat 装完直接弹「启动 NapCat → 打开 WebUI → 手机 QQ 扫码 → 回来重新检测」，
   带 WebUI 地址和 token，避免用户装完不知道该干嘛。

7. **打包版的 TTS 不能「在线装」**（实测踩到的坑）：`backend_server.exe` 跑在 PyInstaller 自带解释器里，
   `pip install` 装到系统 Python 是另一个解释器 —— 表现就是「pip 说装完了，但向导仍然显示未安装」。
   所以现在分两种情形说清楚：`/api/env/check` 里 `tts.can_auto_install` 只在**源码运行时**为真；
   打包版直接显示「需重新打包内置」，`/api/env/install_tts` 也会明确拒绝并说明原因。
   真正解法在构建侧：`build_backend.py` 会先 `pip install edge-tts` 再 `--hidden-import edge_tts`，
   把语音引擎打进 exe（装不上只警告，不影响其余功能，文字聊天照旧）。
   `_tts_status()` 还会用子进程问一次「那个 pip 解释器到底能不能 import」，
   于是能给出「本机 Python 里有，但打包版用自带解释器，需要重新打包」这种不再误导的提示。

#### 10. 无边框窗口在 Tauri 里怎么重做

原来 pywebview 的无边框靠 `apply_window_mode()` 改 Win32 样式位 + `enforce_window_mode()` 反复兜；
Tauri 版本直接让窗口**原生无边框**，标题栏由前端画：

| 层 | 做法 |
| --- | --- |
| 配置 | `tauri.conf.json` → `app.windows[0].decorations: false` |
| 拖拽 | `#tbDrag` 与 `#titlebar` 加 `data-tauri-drag-region`（bridge.js 在启动时兜底补上，含标题文字/图标子节点），双击标题栏原生最大化 |
| 三个按钮 | `win_min` / `win_toggle_max` / `win_close` 在 `bridge.js` 里被**拦下**，直接调 `getCurrentWindow().minimize() / maximize() / close()`，不再打到 Python 那三个空壳方法 |
| 八向缩放 | `.fb-grip` 热区的 `pointerdown` 在捕获阶段被拦下，改调原生 `startResizeDragging('NorthWest'…)`；比原来「算鼠标位移 + 70ms 节流 win_resize」平滑，且拖动西/北边时窗口位置也正确 |
| 最大化图标 | `win.onResized` → `isMaximized()` 同步 `#tbMax` 的 □ / ❐ |
| 权限 | `capabilities/default.json` 加 `core:window:allow-minimize/maximize/unmaximize/is-maximized/close/set-size/start-dragging/start-resize-dragging` 与 `dialog:default` |

`win_state` 在 Tauri 下固定返回 `frameless: true`，所以设置页那个「无边框」开关显示为开（该开关只对老的 pywebview 版生效）。

#### 11. 国内网络下的两条救命路：镜像下载与本地导入（V1.2.1）

NapCat 的安装包只发布在 GitHub Releases，国内大概率卡在 0%。V1.2.1 把它做成**三级降级**，任何一级成功都能装完：

| 级别 | 行为 | 代码位置 |
| --- | --- | --- |
| ① 官方 | 先打 `api.github.com/repos/NapNeko/NapCatQQ/releases/latest` 取资源地址，再直连 `github.com/...` 下载 | `napcat_mgr.find_release_asset()` / `download_file()` |
| ② 镜像 | 官方失败就按 `NAPCAT_MIRRORS` 顺序重试（`ghfast.top` / `gh-proxy.com` / `ghproxy.net` / `github.moeyy.xyz`），连 release JSON 取不到时也走镜像的 API 代理 | `napcat_mgr.mirror_candidates()` / `_fetch_release_json()` |
| ③ 本地导入 | 用户在浏览器里自己下好 `NapCat.Shell.Windows.Node.zip`，在向导里点「手动导入安装包」选文件，后端只负责校验+解压+写配置 | `napcat_mgr.install_from_zip()` / `POST /api/env/import_napcat` |

几个关键细节：

- **快速失败**比「无限等」重要：`_CONNECT_TIMEOUT = 20`、`_READ_TIMEOUT = 180`；`Content-Type: text/html`、首块以 `<` 开头（拿到的是代理的错误页）、正文 < 1024 字节，三种情况都立刻判这一源失败并换下一个，不让用户盯着 0% 发呆。
- **每个源失败都清掉半截文件**，避免下一个源往坏文件里续写。
- 镜像模板里的 `{url}` 是完整官方 URL，`_mirror_url()` 原样替换，所以加新镜像只需往 `NAPCAT_MIRRORS` 里加一行；用户也可以在配置 `napcat_mirror` 里指定优先用哪个源（向导会把选择记进配置）。
- **本地导入不解压整包再看**：`zip_has_launcher()` 直接用 `zipfile` 读文件名列表，看有没有 `napcat.bat` / `NapCatWinBootMain.exe` 这类启动器 —— 下错包（比如下成 QQ 本体）当场报「这个压缩包里没看到 NapCat 的启动器」，不会污染安装目录。
- **解压后仍走同一套收尾**：`_finish_install()` 负责「多一层目录自动下钻 → `set_checked("napcat_dir")` → `write_onebot_config()` → 拼引导文案」，所以下载安装和本地导入的行为完全一致。
- 导入是长任务，走和下载一样的 job 机制：`POST /api/env/import_napcat` → `{ok, job, dir}` → 轮询 `GET /api/env/job/{id}` 拿 `pct/msg/lines`。

**语音引擎为什么必须内置**：打包版跑在 PyInstaller 自带解释器里，向导的「pip install edge-tts」装进的是系统 Python，frozen 版 `import edge_tts` 永远失败 —— 用户会看到「pip 说装完了但还是未安装」。所以 `build_backend.py` 在构建机上先 `ensure_edge_tts()` 再 `--hidden-import edge_tts`，后端检测到 `sys.frozen` 且有模块就报「已安装 · 内置版」，并把 `can_auto_install` 置 false；frozen 下点在线安装会被明确拒绝并提示重新打包。

**模型/缓存目录可改**：配置项 `tts_model_dir`（空=跟随数据目录，打包版落在 `<安装目录>\runtime\models\tts`，不写 C 盘）。设置里改完由 `app_ui.save_settings()` 建目录，`agent.tts_say()` 用 `_live_cfg("tts_model_dir")` 决定合成文件放哪儿。


### V1.1.2 补丁：_wrap 恢复 / 配置缓存 / 情感深判 / 一键启动去重

**1. `_wrap` 恢复**：`app_ui.Api` 里所有方法都经 `_wrap(fn)` 包一层异常兜底。它被一次编辑弄丢后，`get_feature_models` / `get_plugins` 等直接抛 `NameError: name '_wrap' is not defined`。恢复后自检加两条断言，防止再丢。

**2. `config.load()` 时间戳缓存**（`config.py`）：

```python
_CACHE = {"stamp": None, "data": None}
def _stamp():  # (st_mtime_ns, st_size)
    ...
def load():
    stamp = _stamp()
    if stamp is not None and stamp == _CACHE["stamp"]:
        return dict(_CACHE["data"])   # 返回副本，调用方随便改
    ...
```

`save()` 里把 `_CACHE` 置空，保证写完立刻能读到新值。实际收益：`get_settings()` 首次约 75ms、后续约 5ms；`agent._live_cfg` 那一层 TTL 缓存还在，两层互不干扰。

**3. 情感深判**（`app_ui.predict_emotion(text, deep=False)`）：本地 `emotion.analyze()` 先给基线；`deep=True` 时用 `ask_model_raw()` 让模型只输出一行 JSON `{mood,intensity,hidden,advice}`，解析失败就保留本地结果并附 `msg`。不失败、不空白是这条的硬约束。

**4. 一键启动防重复**（`napcat_mgr.launch(..., allow_second_qq=False)`）：`qq_running()` 用 `tasklist /FI "IMAGENAME eq QQ.exe"` 判断；QQ 在跑而 3000 端口不通时直接返回原因，不再拉第二个登录窗。启动器统一 `CREATE_NO_WINDOW`（原来的 `CREATE_NEW_CONSOLE` 会闪黑框）。`app_ui.start_everything()` 的 `_step()` 同时支持 `(ok, msg)` 元组与 `dict` 两种返回——`napcat_mgr.launch()` 返回的是元组，之前被 `bool(tuple)` 当成成功，属于静默误报。

**5. 前端手感**：`body.no-select`（默认开）+ `body.card-resize`，两个开关进 `get_settings()["ui"]`；`call()` 加 45 秒 `Promise.race` 护栏。


### V1.1.2 补丁 2：urgency 真正生效 / easy_drag 关闭

#### 1. urgency 传递链（原实现是死代码）

| 位置 | 改前 | 改后 |
|---|---|---|
| `agent.human_delay_seconds(reply_len=0)` | `isinstance(reply_len, str)` 才算 urgency；调用方全传 int → 永不命中 | `human_delay_seconds(reply_len=0, urgency=None)` |
| `agent.human_delay(reply_len=0)` | 同上，只 sleep | `human_delay(reply_len=0, urgency=None)` 透传 |
| `process_message` | 无 | 开头 `_urg = float(_emo.urgency(raw_text or ""))`，异常兜底 0.0 |
| 调用点（4 处） | `human_delay(reply)` / `human_delay(_fast)` | `human_delay(reply, _urg)` / `human_delay(_fast, _urg)` |

不变量：`urgency >= 0.8` → 返回 `random.uniform(0.15, 0.5)`，**不再叠加**字数时间与封顶；
`urgency is None` 时保持原行为（0.3~1.0 随机 + `min(len/cps, 20)`，总时长 `min(..., humanize_max_delay)`）。

#### 2. `easy_drag=False`

`app_ui.main()` 的 `create_window` 由 `easy_drag=True` 改为 `False`：

- 原因：easy_drag 让整窗任意位置可拖，与页面内拖动控件（`#memSplit` 分割条、公告拖动、
  人设卡排序、`.fb-grip` 缩放手柄）抢手势；
- 代价：无边框窗口必须自己提供拖动区（已有 `#tbDrag.pywebview-drag-region`）；
- 有边框窗口：拖动仍走系统标题栏，无影响。

#### 3. 自检

`_selftest_v112.py` §17 新增 9 条：函数签名含 `urgency`、普通 10 字 > 0.5s、急句 ≤ 0.5s、
`emotion.urgency` 正负样本、`process_message` 里算了 `_urg`、主回复与快路由都传 `_urg`。
总数 238 → **247**。

#### 4. 排错记录

`agent.py` 用 `Get-Content | Select-Object -Skip` 打印出现 `褰撳墠鏄惁` 这类乱码，
误判为文件损坏；实际文件编码正常，是 pwsh 管道输出的编码问题。
源码是否真乱码，用 Python 读一遍再判断。

### V1.1.4 补丁 3：阶段一验收 / 守护态 / 学习底线

**验收脚本 `_selftest_v114_stage1.py`**（28 条，直接调函数）
- 静默门槛：`_silence_sec()` 在 0 / 0.5 / 60 / 老配置（只有 `checkin_silence_hours`）四种输入下的返回值；断言 0 不再被回退、0.5 不被取整、老配置换算成 `hours*3600`。
- 关怀线程：断言 `care_loop()` 体内出现 `_live_cfg("care_enabled")` 与 `_live_cfg("checkin_poll_sec")`（静态检查，防止有人再写回「开头 return」）。
- 守护态：`guard_win_open/left/close` 三函数 + `guard_window_enabled` 关掉返回 0 + `guard_window_sec` 夹到 3~120。
- 群上下文：`GroupContext.add()` 造 3 条前文 + 1 条 @ + 2 条后文，断言 `pending_before_at` 只拿 3 条（**不含 @ 之后的**）、`recent_after` 拿 2 条。
- 情感中枢：三档预设各喂一句，断言 `{emotion, weight, suggestion}` 齐全且 0 ≤ weight ≤ 100；`log_decision` 使 `stats()["total"]` +1。
- 重启失败路径：把 `app_ui._relaunch_cmd` 换成抛 `RuntimeError` 的函数，断言 `restart_app()` 返回 `{"ok": False, "msg": "重启失败: RuntimeError: ..."}` 而非抛出。
- 学习底线：默认 True → 红线内容被拒 → `sign_agreement()` 后 `bottom_line_on()` False 且能写入 → 删协议文件后自动回到 True 并写回配置。

**守护态（阶段三 3.1）**
- `config.py`：`guard_window_enabled`(True)、`guard_window_sec`(10，校验 `(int,3,120)`)，加进 `_BOOL_KEYS`。
- `agent.py`：`_GUARD_WIN`（{group_id: 到期时间戳}）+ `_GUARD_WIN_LOCK`；`guard_win_sec()`、`guard_win_open()`（开关关着返回 0，秒数夹 3~120）、`guard_win_left()`、`guard_win_close()`。
- 群聊分发处：`called` 判定之后算 `_win_left = guard_win_left(group_id)`，`should_reply = (at_me or called or _win_left > 0) and group_allowed(group_id)`；被叫到时 `guard_win_open(group_id)` 并打 `[守护] 被叫到了…进入 N 秒守护态`，窗口内接话打 `[守护] 守护态内接话（还剩 X s）`。

**学习安全底线（阶段三 3.4）**
- `config.py`：`learn_bottom_line`(True) + 加进 `_BOOL_KEYS`。
- `learn.py`：`AGREEMENT_FILE = "learn_bottom_line_agreement.json"`、`agreement_path()`、`agreement_info()`、`bottom_line_on()`（配置关 **且** 协议文件在 → 才允许关；文件不在就 `set_checked(True)` 并打日志）、`sign_agreement(signature)`（写 `{version, signed_at, ts, signature}` 再置 False）、`revoke_agreement()`（删文件 + 置 True）。
- `learn.add()`：红线判定改为受 `bottom_line_on()` 控制；关掉时只打一条「已签协议，放行」日志。
- `app_ui.py`：`learn_bottom_status()` / `learn_bottom_sign(signature)` / `learn_bottom_revoke()` + 三个 Api 方法。
- `index.html`：学习页新增「学习安全底线」卡（`swLearnBottom` / `learnBottomBadge` / `learnBottomHint` / `btnLearnBottomRevoke`）+ 协议弹层（`learnProtoMask` / `learnProtoBody` / `ckProtoAgree` / `protoSign` / `btnProtoSign`，10 秒倒计时 + 勾选双闸）；关开关时不直接改状态，先弹协议，签完才落库。

**不变量**
1. 静默门槛的 0 与 0.5 都必须原样生效，不许被「回退到 hours」或「取整」吞掉。
2. 关怀线程永不在开关关闭时退出——关只是睡，开立刻跑。
3. `pending_before_at()` 返回的永远严格早于 @ 那条消息。
4. 学习底线只认「配置关 + 协议文件在」这一种关闭状态；文件一没，立刻恢复开启。

### V1.1.4 补丁 3：单实例 / 重启 / 积雪 / 人设图

| 项 | 改前 | 改后 |
|---|---|---|
| 多开 | 无锁，两个窗口 + 两个托盘图标 | `_activation_ping()` 先探活；在跑就只发 `show` 并退出 |
| 重启 | `taskkill /PID self /T /F` 顺手杀掉新实例 | `_spawn_detached()` 用 `cmd /c start` 脱离进程树，只退自己 |
| 积雪 | 一条渐变贴边 | `::before` 四段 radial 波浪雪堆 + `::after` 霜光 + `data-melt` 融雪过渡 |
| 免责按钮 | `disabled`（点了没反应） | `aria-disabled` + toast + `.gate-shake` |
| 人设图 | 无 | `persona_faces/<key>/` + 存储/删除/识别/注入/prompt 参考 |

不变量：
1. 单实例监听失败（端口被占）只记日志，绝不阻断启动。
2. 重启必须「新进程活、旧进程死」；任何 `taskkill /T` 都视为 bug。
3. 人设图的描述一旦重传/删除就清空（`set_face_desc(key, "")`），避免拿旧脸画新图。
4. 没上传形象图时，被要「你的照片」宁可跳过，也不许凭空编一个人。

## V1.1.6 补丁技术要点（2026-10-24）

### 提示词可配置
- `config.py`：新增 `learn_prompt`(str, ≤4000)、`emotion_preset_prompts`(dict，键必须是
  `emotion_engine.PRESETS` 里的预设，单条 ≤2000，空串=删除)；`set_checked` 各有一个清洗分支。
- `learn.py::prompt_text(name, chat)`：有自定义就 `.format(name=, chat=)`；格式化失败或
  没写 `{chat}` 自动把「聊天记录：…」接在末尾；否则回内置 `LEARN_PROMPT`。
- `emotion_engine.py::preset_prompt(preset)`：返回 `(文本, 是否自定义)`，`analyze()` 用它组装 prompt。
- `app_ui.py`：`get_learn_prompt()/save_learn_prompt(text)` 与
  `emotion_center()["preset_prompts"]`、`save_emotion_center(..., prompt_text=)`。
- 前端：`#emoPromptBox/#emoPromptSrc/#btnEmoPromptSave/#btnEmoPromptReset`（情感中枢）、
  `#learnPromptBox/#learnPromptSrc/#btnLearnPromptSave/#btnLearnPromptReset`（学习页）。

### 功能性证据
`_t_evid.py` 覆盖 ①③⑥⑩⑪，共 26 项，全部为"真调一遍"级别，不做源码字符串匹配。

### 前端陷阱（已写进开发者指南）
`edit` 的 `old_string` 若只写 `function foo() {`，遇到 `async function foo() {` 会从中间截断匹配，
把 `async` 挪到新函数上 → 整脚本 `await is only valid in async functions`。
定位手法：复制 index.html，在 `<head>` 后注入 `window.onerror` 记录 `{msg, line, col}`，
用真 WebView2 打开读取 `window.__perrs`。

## V1.1.6 技术要点

### 1. 语义记忆（semantic.py）

| 层 | 实现 | 失败时 |
| --- | --- | --- |
| 向量化 | 先 `/v1/models` 挑名字带 `embed` 的模型（缓存 10 分钟），再 `POST {lm_base}/embeddings`（OpenAI 兼容）或 `POST {ollama}/api/embeddings` | 返回 `None`，走关键词 |
| 存储 | `vec_memory(id, session_key, user_id, content, vec BLOB, dim, created_at)`，`vec` 是 float32 二进制 | 表在 `memory._migrate()` 里 `CREATE TABLE IF NOT EXISTS` |
| 检索 | 全表取 vec → 解包 `struct` → 余弦相似度排序取前 k | 关键词 Dice（中文 bigram） |
| 去重 | `upsert_session()` 按 (session_key, content) 覆盖 | — |

关键取舍：不引入 sqlite-vec 与 onnxruntime。前者是 C 扩展，PyInstaller 打包要额外收 dll；后者要下 100MB+ 的模型，且用户本机已有 embedding 服务。代价是 O(n) 全表扫描 —— 万条以内毫秒级，超过再谈优化（`semantic.py` 注释里写了阈值与升级路径）。

### 2. 自省数据

- `thoughts` 表原本就有（`subconscious.py` 写入），这次只补读接口 `memory.list_thoughts()`。
- 衰减：`subconscious.decay_memories(mem, days)` 只 `UPDATE` 权重与 `last_hit`，不删行；`run_decay_now()` 返回受影响行数。

### 3. 性能指标

- 事件循环延迟：`loop_lag_ms` 由 agent 侧心跳记录（无则显示 n/a）。
- RSS / CPU / 线程数：`psutil`（可选依赖，缺失时用 `resource` 兜底，Windows 下退化为 RSS 只读）。
- 队列：`agent.pq_state()` → `{fg, bg_wait, bg_run}`。

### 4. 渲染层逐字

`typewrite(el, text, cps)`：`requestAnimationFrame` 每帧写 `per = max(2, cps)` 个字，避免 `setInterval` 在长文本下堆积回调。

### 5. 本次修的两个签名级 bug

- `config.set_checked` 枚举校验加短单位别名：`{"h":"hour","m":"minute","s":"second","d":"day"}`。
- `app_ui._append_log(line, level="INFO")`，写盘带 `[LEVEL]` 前缀；原有 11 处调用不再抛 `TypeError`。


## V1.1.6 补丁 2 技术要点

### 1. 看图：`ask_vision` + 「看图识图」分工

- `providers.FEATURES` 从 11 项变 12 项，新增 `("vision", "看图识图", ...)`；`FEATURE_KEYS` 自动跟着变。
- `agent._ask_text(messages, budget, ..., targets=None)`：`targets` 非空时替代 `_chain_targets()`，不改原有顺延逻辑。
- `agent.ask_vision(prompt, url, max_tokens=400, tag="/看图") -> (text, reason)`：组装 text+image_url 内容；`providers.feature_target("vision")` 命中就把 `targets` 钉成它，否则走主链；失败返回 `("", reason)`，**不抛异常**。
- `app_ui._face_image_url(path)`：先 `_img_data_url`，失败用 Pillow 缩到 1024px 转 JPEG 再 base64。
- `app_ui.describe_persona_face()`：成功 `desc_src="vision"`；失败用卡片 gender/age/identity/style/personality 拼 `desc_src="fallback"`，`msg` 写原因与修复路径。

### 2. 向量：`semantic.accel()` 与 `_local_bge()`

- `accel()` 首次尝试 `import sqlite_vec` + `conn.enable_load_extension(True)` + `sqlite_vec.load(conn)`，用 `SELECT vec_distance_cosine(vec_f32('[1,0]'), vec_f32('[1,0]'))` 实测，成功缓存 `_ACCEL["ok"]=True`。
- `search()`：accel 为真且有查询向量时 `SELECT ... vec_distance_cosine(vec, vec_f32(?)) AS d ... ORDER BY d LIMIT k*6`，`score = 1 - d`；异常回落 Python 余弦（结果一致）。
- `_local_bge()`：`models/bge-small-zh/{model.onnx,tokenizer.json}` + `onnxruntime` 可导入时返回 `run(text)->list[float]`（CLS+字级 ids+SEP，池化后 L2 归一化）。
- `embed()` 顺序：本地 BGE → LM Studio/OpenAI 兼容 `/embeddings` → Ollama `/api/embeddings` → `None`。
- `stats()` 新增 `accel`、`accel_msg`、`local_bge`；前端「语义记忆」卡状态行显示「sqlite-vec 加速 / Python 计算」。

### 3. 主动回复：全部走实时配置

| 函数 | 读取的键 | 作用 |
|---|---|---|
| `_care_hours()` | `care_plan_hours` | 时段（默认 8~23） |
| `_care_gap_sec()` | `care_plan_min_gap_min` | 两条主动消息最小间隔 |
| `_care_day_allow()` / `_care_day_bump()` | `care_plan_max_per_day` | 每日上限（`state["care_day"]={"d","n"}`） |
| `care_loop()` 开头 | `care_plan_enabled` | 关掉整轮跳过 |

### 4. 上线补回：三个新键

`group_catchup_enabled`(True)、`group_catchup_max_msgs`(3, 1~20)、`private_catchup_enabled`(True)；`catch_up_unread()` / `catch_up_private()` 都用 `_live_cfg` 读开关与条数。

### 5. 图片 URI 策略

`qzone.to_image_uri(path, prefer_file=True)`：默认 `file:///` + 绝对路径；`prefer_file=False` 且 ≤1MB 才 `data:` base64；>20MB 返回 `""`。理由：同机不需要大 payload，也能绕开部分 NapCat 版本的 base64 解析 bug。
## V1.1.5 补丁 6 技术要点

- **窗口句柄**：`pywebview 6.x` 的 `window.native` 才是 Form；`window.gui` 是模块。`_win_native` 是唯一入口，`_win_hwnd` / `apply_dark_titlebar` / `apply_window_mode` / `enforce_window_mode` 都经它取句柄。
- **样式契约**：无边框 = `WS_CAPTION` 清掉 + `WS_THICKFRAME` 保留；`_style_ok(hwnd, want)` 是唯一判定。`FormBorderStyle` 必须在 UI 线程改（`form.Invoke`）。
- **情感落库**：`emotion_log` 表 + `memory.log_emotion/list_emotion_log/emotion_stats/clear_emotion_log`；`emotion_engine.log_rows/stats` 先读库再退内存。
- **优先级队列**：`_PQ{fg,bg_wait,bg_run,last_fg}` + 三档常量；`ask_model(prio=PQ_PRIO_USER)` / `ask_model_raw(prio=PQ_PRIO_NORMAL)`；后台 6 处调用显式 `prio=PQ_PRIO_BG`，前台忙时最多等 25s。
- **历史开关**：`history_enabled`（默认 True）在 `_prepare_messages` 入口裁剪 `history`，对所有回复路径一次生效。
- **单位别名**：`config.set_checked` 的枚举分支接受 `h/m/s/d/hr/min/sec` 并归一化，避免前端短单位导致整包保存失败。
- **日志**：`_append_log(line, level="INFO")`，写盘带 `[LEVEL]` 前缀。

## V1.1.5 技术变更

### 1. 改动清单

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `index.html` | `<style>` 末尾 | 新增 `html[data-theme-edge="1"] .card::before/::after` 覆盖、`@keyframes snowIn`、`html[data-snow-card="0"]` / `html[data-snow-flakes="0"]` 规则 |
| `index.html` | `applySnowDepth()` | 厚度映射 `6 + d*0.20`（6~26px）、冰棱 `3 + d*0.09`、`--snow-off` 固定 2px |
| `index.html` | `applyThemeFx()` | 读写 `documentElement.dataset.snowCard/snowFlakes`；飘雪关闭时 `cnt=0`；厚度以用户设置为准 |
| `index.html` | 外观卡 / `initLooks()` | 新增 `#swCardSnow`、`#swFlakes` 两个开关；保存时一并写 `card_snow_enabled`、`snow_flakes_enabled` |
| `app_ui.py` | `get_settings()` → `ui.effects` | 增加 `card_snow`、`flakes` |
| `app_ui.py` | `apply_window_mode()` | Win32 去/加 `WS_CAPTION|WS_THICKFRAME` + `SetWindowPos(SWP_FRAMECHANGED)` + WinForms `FormBorderStyle` 兜底 |
| `app_ui.py` | `save_imagegen()` | 改 `imagegen_enabled` 时把 `imagegen.available()` 的结论拼进返回消息 |
| `imagegen.py` | `list_generated()` | 只返回路径；新增 `list_generated_info(limit)` 返回 `[{name,path,size,mtime}]` |
| `imagegen.py` | `available()` | 按来源判定：显式端点 / `local`（文本模型不能画图）/ 云端缺 Key |
| `config.py` | DEFAULTS | `card_snow_enabled`、`snow_flakes_enabled`（均默认 True，进 `_BOOL_KEYS`） |

### 2. 新增不变量

1. **雪层不得影响布局与可读性**：`::before/::after` 只做装饰，`pointer-events:none`，
   且整体位于卡片上沿之外；任何把雪带画进卡片内部的做法都算回归。
2. **用户设置优先于主题预设**：主题自带的 `effects.depth` 只能作为默认值，
   一旦用户在设置里调过 `snow_depth`，就必须以用户值为准（否则「显示 8、实际默认」）。
3. **可用性判定必须可执行**：`imagegen.available()` 不得对纯文本模型返回 `True`。
   判据是 provider（`local` 视为文本模型），不是「链上有模型」。
4. **`list_generated()` 只返回路径**：需要元信息的调用方一律用 `list_generated_info()`，
   避免再次出现「把 dict 当路径」。

### 3. 自检

`_selftest_v115.py`（新增 40 项）：雪层 CSS 关键片段、厚度映射区间、两个开关的落盘键、
`apply_window_mode` 存在且带 `FormBorderStyle` 兜底、`list_generated` 返回 `str` 列表、
`available()` 对 local 返回 False 且消息含「不能画图」、版本号 `1.1.5`。

### 4. 排错记录

- *写完 CSS 后页面布局没变*：因为 `.card::before` 原规则在 `@media (prefers-reduced-motion)`
  之前定义、特异性相同，后写的那条才生效；确认新规则确实排在文件末尾即可。
- *无边框依旧有标题栏*：`create_window(frameless=True)` 在 edgechromium 后端会被宿主
  re-apply 样式，必须再加 WinForms 层；两层都失败时看 `[UI] 应用窗口模式失败` 日志。
- *`TypeError: expected str, bytes or os.PathLike object, not dict`*：来自
  `os.path.basename(dict)`，根因是 `list_generated()` 契约被改坏，改回只返回路径。

## V1.1.4 技术变更

### 1. prompt 分层（`agent.build_messages`）
| 位置 | 内容 | 变化频率 |
|---|---|---|
| system `# 身份设定` | 人设 + 学习经验 + 管理员口吻 + 潜意识 | 低 |
| system `# 对话历史（更早的对话已压缩）` | 摘要串 | 低 |
| system `# 记忆与关系` | 永久记忆 + 好感度 + 伴侣 | 低 |
| system `# 安全约束` | GUARD_SYS_RULE | 低 |
| user `# 当前状态` | 时间 / 情绪建议 / 群上下文 | 高 |
| user `# 用户发言` | wrap_user_text 包裹的原文 | 高 |

不变量：高变化段绝不进 system，否则云端前缀缓存每轮失效（成本 ×N）。

### 2. thinking 协议
`THINK_RULE` 要求 `<thinking>…</thinking>` + 正文；`split_thinking()` 用 `re.S|re.I` 取第一段；正文经 `clean_reply`；思考写 `log(DEBUG)` 与控制台；可选 `think_log`。

### 3. 上下文压缩
`compress_old_context(scope)`：同一会话 `context_compress_hours`（默认 6h）内不重复；保留最近 `_hist_turns()` 条；旧消息拼 `我/对方：` 每行 200 字、总 1800 字，交 `ask_model_raw` 压成 ≤60 字，与 `mem.get_latest_summary` 合并 ≤800 字存 `save_summary`。

### 4. 数据库迁移
- 3 张新表：`emotion_log`（情绪决策日志）、`romance_state`（伴侣关系）、`think_log`（思考存档）。
- 5 个新列：`user_events.unit`、`care_plans.unit`、`usage_log.feature`、`affinity.romance`、`think_log.msgs_at`，全部带默认值。
- 配置侧：`checkin_silence_sec`(7200) + `checkin_silence_unit`("hour") + 一次性 `_units_migrated`。
- 自检脚本 `_t_migrate.py` 造旧库验证「旧数据不变、新列存在、换算正确」。

### 5. 伴侣与守护
`mem.get_romance/set_romance/list_romance`；`agent.build_romance_block` 按显式 level 注入语气（分数只在 level=friend 时生效）；`guard_wake()` 用 `_WAKE_LAST` 全局冷却。

### 6. 自检
`_selftest_v114.py`（新增）、`_selftest_v112` 278 项、`_ui_smoke` 35 项、`_t_migrate.py`。

## V1.1.3 技术变更（追加）

### 1. 开关状态统一收口
| 问题 | 改法 |
|---|---|
| `div.sw` 认 `.on`、`label.switch>input` 认 `:has(input:checked)`，JS 混用两套 | 新增 `swGet(id)` / `swSet(id, on)`；`:has()` 兜底 `.on`；全部 `classList.toggle('on')` 替换为 `swSet` |

不变量：任何开关的「视觉状态」只有一个来源——`input.checked`（或 `.sw` 的 `.on` 类），二者由 `swSet` 同时写。

### 2. 重启引导链路
`askRestart(name)` → 弹框（确认按钮 10 秒倒计时？否：本站是直接确认）→ `call('restart_app')` → `app_ui.restart_app()`：`_child.kill()` → `napcat_mgr.stop_agent()` → `_spawn_detached(DATA_DIR, args, self_check=True)` → `sys.exit(0)`。

不变量：`restart_app` 必须先杀 Agent 子进程，否则新实例会因端口占用起不来。

### 3. 启动判定与进程树
- `boot_check_order` 列表 + `save_boot_order(keys)`，顺序影响 `run_startup_checks()` 的执行次序。
- `start_everything()` 每步前 `_step_should_skip()`：`qq_running()` / 本地端口 `socket.connect_ex` / `local_enabled` 与云端 key。
- `subprocess.Popen(..., creationflags=GET_NO_WINDOW())`；`_endpoint_alive(host, port, timeout)` 统一探活。

### 4. 情感中枢
- `emotion_engine.analyze()`：本地词表先返回，线程内等模型；`threading.Thread(...).join(timeout)`，超时用本地结果。
- 权重语义：0~100，映射到 `_urg = max(_urg, w/100)`；`_urg >= 0.8` 时 `human_delay` 走 0.15~0.5 秒。
- `log_decision()` 只写内存（`deque(maxlen=200)`），不落库，避免每次聊天写盘。

### 5. 语义缓存三段
`_prepare_messages` 组装顺序：`system_prompt`（人设+CHAT_RULES+插件提示）→ `memory_block`（inner_thoughts+永久记忆+好感度+伴侣）→ `volatile`（time_context+mood_line+情绪建议+临时上下文+当前消息）。`build_messages(..., volatile=volatile)` 保证 volatile 拼在最后一个 user 内容里。

不变量：任何「每条消息都变」的内容（时间、情绪、当前消息）不得出现在 `system_prompt` 里，否则前缀缓存永远 miss。

### 6. 群聊环形缓冲
`GroupContext`（agent 内）：`push(key, item)` 维护 `deque(maxlen=context_buffer_size)`；`gather(key, msg_id)` 取目标前后窗口；`_gap_break()`（>5 分钟）与 `_too_many()`（连续 10 条同人灌水）截断。

### 7. 伴侣模块
`build_romance_block(user_id, name, is_group)`：`is_group=True` 时只返回警告串；私聊 `score >= romance_min_score` 或 `romance_prompt` 非空才返回伴侣设定。注入在 `memory_block`，与好感度块相邻。

### 8. 学习类型与红线
`learn.TYPES` 9 类；`ltype` 列用 `ALTER TABLE ... ADD COLUMN`（异常吞掉）；`_BANNED_RE` 正则在 `add()` 早期拦截；`active_prompt()` 对严格类型追加红线说明。

### 9. 主题特效与 TTS
- `theme._norm_effects` → `{snow, leaves, edge}`；`applyThemeFx(eff)` 按 `leaves` 切换字符集与数量。
- TTS：`edge_tts.Communicate(text, voice).save(path)`（`asyncio.run`），`send_voice()` 发 `{"type":"record","data":{"file":path}}`。

### 10. 排错记录
| 现象 | 根因 | 处置 |
|---|---|---|
| 设置页整块脚本不执行 | 管理员页少分号 → `Unexpected token 'return'` | 补分号 + `_jsprobe.py` 固化检查 |
| 开关开了刷新又变回关 | 两套开关写法 + 状态回填互相覆盖 | `swGet/swSet` 统一 |
| `learn.add` 报 no column ltype | 老库没有新列 | `ALTER TABLE` 兜底 |

## V1.1.2 技术变更（追加）

### 按功能分工的模型

- `providers.FEATURES`（11 项）+ `FEATURE_KEYS` 是唯一清单；界面、自检、`_feature_of` 都从它来。
- `providers.feature_target(feature)`：读 `feature_models[feature]`，base 空则用该提供方默认地址；Key 顺序 = 该项自带 → `provider_keys[provider]` → `cloud_api_key`；返回结构与 `chain()` 的条目同形（label/needs_key/missing_key）。
- `providers.set_feature_model(...)`：模型名必填、地址必须 http(s)、需要 Key 的提供方在没有任何 Key 时拒绝。
- `agent._post_chat(payload, ..., feature="")`：`t` 为空时查分工表；命中后覆盖 `payload["model"]`，并把 `t` 一路传给 `_headers()` 与 `_record_usage()`，所以**用量里记的是真实模型**。
- `agent._feature_of(detail, tag)`：按关键词从记账用途反推功能（写日记→diary、记忆提取→memory、私聊→chat…），避免去改几十个调用点。
- 配置 `feature_models` 在 `config.set_checked` 里有专门分支：模型名空、非 dict 的条目直接丢掉。

### 插件宿主

- `plugins.py`：`BUILTIN`（10 个内置能力，`core` 标记核心不可关）+ 用户插件目录 `DATA_DIR/plugins/*.py`。
- 载入：`importlib.util.spec_from_file_location` + `exec_module`，异常全部收进 `_LOADED[file]["error"]`，主程序不受影响。
- `PLUGIN` 字典（name/version/desc/author）、可选 `setup(api)` / `run(api)`；`PluginApi` 只暴露 log / cfg_get / cfg_set / data_dir / notify / ask_model。
- 开关存 `config.plugin_enabled`（dict，名字→bool）；`plugins.reload_plugins()` 清缓存重扫，`setup_all()` 在 agent 启动时跑一次。
- 依赖倒置：插件不许碰 agent 内部会话/连接，只能通过 `api.ask_model` 走全局（或功能分工）模型。

## V1.1.2 技术变更

### 模块增量

| 文件 | 变化 |
|---|---|
| `config.py` | 新增 `reply_timeout`(60)、`reply_retry_on_timeout`(1)、`humanize_enabled/delay_min/delay_max/typing_cps/max_delay`、`event_source`("model")、`event_prompt`("")、`event_extract_enabled`(True)；校验表与 `_BOOL_KEYS` 同步 |
| `agent.py` | `REPLY_TIMEOUT`/`REPLY_RETRY_ON_TIMEOUT`；`human_delay()` 重写（封顶 + 实时配置）；`_live_all()` 指纹缓存；`ask_model` 总预算 + 超时只走"短上下文"一次；去重拆 `_dedup_hit`/`note_replied_text`；群聊补回整批 + 历史；后台闸门 `bg_slot_ok`/`bg_slot_free`；`extract_events` 读 `event_source`/`event_prompt` |
| `memory.py` | `list_sessions(limit=40)`（按 session_key 聚合） |
| `app_ui.py` | `get_sessions(limit)` + `Api.get_sessions` |
| `index.html` | 顶栏定宽、`.switch` 胶囊样式、关于卡位置、画图说明、`#msgSessionSel`、`#evSource/#evPrompt`、`#bootMask` 过场 |
| `notice.py` | 单条 V1.1.2 合并公告（`2026-10-23-v112`） |

### 关键不变量

1. `reply_timeout` 只管聊天；后台任务仍用 `request_timeout`（长任务可以慢）。
2. 去重只在**发送成功后**登记；`_dedup_hit` 永不登记、且问句（`_ASK_RE`）直接放行。
3. 后台提取必须 `bg_slot_ok(...)` → `finally: bg_slot_free()`，且要求"用户最近 8 秒没说话"。
4. 配置读取统一走 `_live_all()`（3 秒 TTL，按 `(mtime,size)` 失效），不再每处 `cfg_mod.load()`。
5. 群聊补回必须带 `mem.get_recent_messages` 的历史，禁止 `ask_model([], prompt)` 裸调用。

### 已知缺陷与修复

| 症状 | 根因 | 修复 |
|---|---|---|
| 回复要等 5 分钟 | `request_timeout=300` 与重试链叠加 | 独立 `reply_timeout` + 超时跳过"加大 token" |
| 问了不回 | 去重把"准备回"登记成"已回"，失败也占位 | 只认发送成功，问句不拦 |
| 群里只会回"在的" | 补回逐条 `ask_model([], prompt)` | 整批 prompt + 历史 |
| 回复期间下一条被堵 | 3 个后台提取线程同时调模型 | 全局单槽 + 安静 8 秒 |
| 右上角按钮左右跳 | 宽度随模型名/状态变化 | 全部定宽 |

## V1.1.1 技术变更

### 1. 模块增量

| 文件 | 变化 |
|---|---|
| `personas_store.py` | 新增 `CARD_FIELDS`（10 字段）、`card_brief()`（角色设定块 + 说话示例）、`build_system_prompt()` 改为 brief + 正文 + extra 三段拼；`_norm_card` 支持列表字段与示例的两种写法 |
| `agent.py` | `time_context()` 重写（时段/作息/饭点/「时间对不上要点出来」）；`quote_target()` + `note_recent_msg()` + `_RECENT_IDS`（引用策略）；`_live_cfg()` 让设置改了不用重启；`CHAT_RULES` 注入；`already_replied_same()` 90 秒去重；`called_by_name()` 免 @；`maybe_draw()` 自主配图；`memory_export_loop` 同时跑记忆与日记备份和目录自动导入；`_admin_mode` 让暗号命中后真的进模型并可跳过防护；`_record_check_usage`/启动检测记账 |
| `learn.py`（新） | `learn.db`：learn_items 表；`add/update/delete/set_enabled/list_items/stats/active_prompt/touch`；`learn_from_dialog()` 从最近对话提炼；条件存 config `learn_condition` |
| `emotion.py`（新） | 词表 + 强度：`analyze()/mood_line()/affinity_delta()/summary()`；明显情绪才拼提示 |
| `admin.py`（新） | `verify()/state()/save()/admin_prompt_block()/bypass_guard()/diagnostics()/export_diagnostics()` |
| `secure_core.py`（新） | `secret_salt()/manifest_key()/codeword_digest()/version_tag()`；`security.py` 改为从这里取并保留旧盐兼容 |
| `mem_transfer.py` | 新增 `IMPORT_DIR/import_dir()/list_import_dir()/scan_import_dir()`（丢文件夹自动导入）与日记备份 `DIARY_DIR/diary_dir()/export_diary()/diary_export_tick()/import_diary_from_dir()/diary_backup_status()` |
| `memory.py` | 新增 `update_event()/delete_event()`（待办可改可删） |
| `app_ui.py` | 新增 learn/todo/admin/draw/startup-check 全套 API；`run_startup_checks()` 六步 + `cleanup_check_traces()`；`get_draw_status()/draw_now()/delete_draw()`；`win_min/win_toggle_max/win_close`；`_tray_image()/tray_start()`（pystray）；DWM 深色标题栏；`start_everything()` 一键启动 |
| `index.html` | 新增「学习」「管理员」「画图」三个 Tab；模型页「启动前检查」卡；设置「聊天与回复规则」卡；人设字段化表单；记忆页左栏重排 + 日记备份卡；深色标题栏/自绘无边框两套样式 |
| `config.py` | 新增 `quote_enabled/quote_gap/group_name_reply/bot_names/online_greet_*/learn_*/draw_*/admin_prompt/admin_bypass_guard/emotion_enabled/tray_*/window_frameless/diary_export_*` |
| `build.py` | `OBFUSCATE_MODULES` + `_obfuscate_source_package()`：源码包只发 `secure_core.pyc` 并附说明；`DOC_FILES` 加 `V1.1.1-更新说明.md` |

### 2. 关键不变量（改代码别破坏）

1. 引用规则只看「这条消息之后又来了几条」，`quote_gap=0` 等于每条都引用，`quote_enabled=False` 等于永不引用；
2. 设置页改的聊天规则必须**下一轮就生效**（用 `_live_cfg` 现读，不要退回模块级常量）；
3. 系统提示拼装顺序：人设 brief → 正文 → extra → 时间 → 记忆规则 → 聊天规则 → 情感提示 → 学习经验 → 最高权限块；
4. 暗号命中后：没开最高权限仍然只回固定话术、不调模型；开了才置 `_admin_mode` 跳过 `apply_guards`；
5. 学习条目只通过 `active_prompt()` 进提示词，长度上限 1800 字，超了截断加省略号；
6. 画图尺寸必须过 `_draw_size()`（夹到 `draw_max_side`、对齐 8 的倍数）；
7. 启动检测的每一步都返回统一结构（step/ok/name/detail/next），失败即停；
8. `secure_core` 只能被 `security.py` 引用，且必须保留兼容旧盐/旧签名密钥的分支。

### 3. 已知缺陷与修复

| 现象 | 原因 | 修法 |
|---|---|---|
| 人设第二张卡空白 | 点击只传列表摘要，摘要无 `persona` 字段 | 改成 `pcLoadCard(key)` 回后端取完整卡片 + 序号防抖 |
| 每句话都引用、像客服 | 群聊/@ 一律引用 | `quote_target()` 按「隔几条」判断 |
| 下午说「早上好」它顺着装傻 | 时间只报了钟点，没给用法 | `time_context()` 加时段语义 + 「对不上要点出来」 |
| 群聊必须 @ | 只认 at 段 | `called_by_name()` + `bot_names` |
| 管理员暗号开了没界面 | 只有后端 | 新增管理员 Tab + 模糊遮罩 + 10 秒风险确认 |
| 测试留下的垃圾 | 无清理入口 | `cleanup_check_traces()` 删用量/学习/测试图 |

## V1.1.0 技术变更

### 1. 模块增量

| 文件 | 变化 |
|---|---|
| `config.py` | 新增 `lm_engine`（`lmstudio`/`ollama`）、`memory_export_enabled`、`update_mirrors`、`api_key_hidden`、注入防护四开关、`effects_*` 六项、`disclaimer_*`；新增 `export_interval_seconds()`、`human_duration()`、`EXPORT_UNITS`/`EXPORT_UNIT_SECONDS`/`EXPORT_MAX_DAYS`；`APP_VERSION="1.1.0"`、`APP_VERSION_TAG="正式版"`、`APP_AUTHOR="Ruao"`、`APP_SUBTITLE` |
| `providers.py` | `PROVIDERS` 每项加 `key_url`；`local_engine()` / `local_default_base()` / `set_local_engine()`；`add_to_chain(..., front=False)`（置顶插入 + 同步 `lm_*`/`cloud_*`）；新增 `update_in_chain(index, base, model, api_key)`；`chain()` 是实际生效顺序（`resolve()` 只是单模型解析）；`MAX_CHAIN=10` |
| `app_ui.py` | 新增模块级 + `Api` 门面：`shell_open_url`/`open_url`、`set_local_engine`、`start_local_server`（含 `_find_ollama`/`start_ollama_server`）、`update_model_in_chain`、`get_persona_cards` 系列、记忆导出/导入/定时/目录扫描、`export_usage_report`/`import_usage_report`、`get_about`/`get_integrity`/`get_disclaimer`/`accept_disclaimer`、暗号三件套、`qzone_*`；`set_model(model, restart, kind)` 与 `set_model_base(base, model, kind)` 增加 `kind` 路由；`get_model_settings()` 增加 `local.engine/engine_cn/default_base/engines`、顶层 `current_model`；`get_status()` 增加 `version/version_tag/subtitle/author` |
| `index.html` | 动效引擎（`FX_DEF`/`applyEffects`/`fxPageIn`/`initRipple`/`riskConfirm`）；模型页本地引擎下拉 + 双卡置灰 + 优先级列表编辑/拖动/首选标记；`_fillSelectGrouped`/`_cloudGroups`/`PROV_OWNER` 云端模型分组；`toast()` 修 `z-[90]`；记忆页自动导入按钮；关于卡程序目录；免责协议遮罩 |
| `mem_transfer.py` | `EXPORT_TABLES` 集中管理导出表与业务主键；`export_to_file`/`export_to_text`/`import_from_file`/`preview_bundle`/`list_exports`/`backup_db`/`restore_backup`；`auto_export_due`/`auto_export_tick`/`save_schedule`；**新增** `import_dir()`/`list_import_dir()`/`scan_import_dir()`（目录自动导入，成功挪 `done/`、失败挪 `failed/` 并留错误信息） |
| `usage.py` | `record(..., detail=...)` 落库「这一笔做了什么」；`recent_details()` 输出 `detail` + `detail_text` 兜底（老数据不空白）；`export_report`/`import_report`（CSV 去重合并） |
| `personas_store.py` | 目录即库（`personas/<key>.json`），`_norm_card` 统一字段，`migrate_from_legacy()` 迁老 `personas.json`，`export_card`/`import_card` |
| `security.py` | `scan()`（sql/prompt/command 三类规则）、`wrap_user_input()` 隔离标签、DPAPI/XOR 加密、`write_manifest`/`verify_manifest`/`sign_manifest`/`verify_manifest_sig`（HMAC-SHA256） |
| `mem_rebuild.py` | `analyze`/`rebuild(dry_run)`/`verify`/`backup_db`/`restore_backup`：外部记忆转本端，日记不并入永久库 |
| `updater.py` | `_mirror_candidates()` 镜像轮询；下载后校验清单与签名；`rollback()`；默认 `update_enabled=False`、`update_require_sig=True` |
| `notice.py` | `NOTICES` 收敛为一条 `2026-10-23-v110`（v4~v8 合并） |
| `agent.py` | 新增 `memory_export_loop` 后台线程（60s tick：目录自动导入 + 定时导出）；`_usage_detail(tag, target)` 与 `_USAGE_TAG_CN` 生成可读细节；`_post_chat(..., detail=)` 贯通 5 个记录点；`ask_model(..., detail=)` 支持重试追加 |
| `build.py` | `DOC_FILES` 不含 `开发者指南.md`（只进源码包）；源码包排除开发期临时脚本与 `_v110t`；使用说明补 V1.1.0 段落 |

### 2. 关键不变量（改代码前先记）

1. **模型生效顺序 = `providers.chain()`**，不是 `resolve()`。链为空才回退 `resolve()`；`model_failover=false` 只取第 1 条。
2. **保存模型 = 写配置 + 置顶入链**（`add_to_chain(..., front=True)`），三个动作缺一不可。
3. **`set_model`/`set_model_base` 必须带 `kind`**，否则本地/云端字段会串。
4. **弹层层级固定**：`notice < about(60) < disc(70) < cwAlert(85) < toast(90)`，`toast()` 永远带 `z-[90]`。
5. **数据库只加不改**：`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN`（try/except 包裹），新表同步登记到 `mem_transfer.EXPORT_TABLES`。
6. **配置写入只走 `config.set_checked()`**（类型/范围/枚举校验）。
7. **定时导出默认关闭**，开了才跑；单位换算一律换算成秒并卡 365 天上限。
8. **不把日记当记忆**：`diary` 表独立，`mem_rebuild` 明确排除。

### 3. 已知缺陷与修复记录

| 问题 | 根因 | 修复 |
|---|---|---|
| 「公告与通知」绿色提示被卡片遮住 | `toast()` 重写 `className` 丢掉 `z-[80]` | 固定 `z-[90]` |
| 失败顺延开关永远显示关闭 / 本地地址框空 | 前端读 `model_failover`、后端返回 `failover`；`local.base` vs `current_base` | 双字段兼容读取 |
| API 开启时保存本地模型，云端卡出现本地模型名 | `set_model` 不区分 `kind` | 加 `kind` 强制路由 |
| 「这一笔做了什么」一直空白 | `_record_usage()` 从未传 `detail` | 全链路补 `detail` + `detail_text` 兜底 |
| 定时导出不触发 | `auto_export_tick` 无调用方 | `agent.memory_export_loop` 每 60s 调 |
| 丢进文件夹不自动导入 | 配置项存在但无实现 | `mem_transfer.scan_import_dir()` + 界面/后台调用 |
| 重复 DOM id `swGuard` | 用量卡与安全卡同名 | 用量侧改 `swUsageGuard` |

---

##### 7. NapCat 的启动与登录状态机（V1.3.1）

两个坑决定了这套逻辑必须这么写：

1. **不能重复启动**：`launch()` 以前无条件 `Popen`，而它有三个调用方
   （面板按钮 / 启动时自动拉起 / 检测并连接），每调一次多一对 node 进程，
   几十个实例抢 3000/3001，后来者把先来者顶掉 —— 用户扫的码属于已被顶死的实例。
2. **进程在 ≠ 端口通**：NapCat 是**扫码成功后才监听** 3000/3001，
   用端口判「有没有跑」必然把「等扫码」误判成「没在跑」，进而诱导重复启动。

所以状态机以**进程**为准、端口只判登录：

| 判据 | state | 前端表现 |
|---|---|---|
| `login_info()` 拿到 `user_id` | `logged_in` | 绿色徽章「已登录（QQ xxx）」，隐藏扫码按钮 |
| 进程在（`running_pids(dir)` 非空）或端口通 | `no_login` | 黄色「未登录」+「扫码登录」按钮 + 二维码路径 |
| 进程数 > 2 | `no_login` | 额外提示「检测到 N 个进程（重复开了），会抢端口导致扫码也登不上」 |
| 都没跑、`launch_if_down=True` 拉起成功 | `no_login` | 「已经拉起来了，去扫码」 |
| 都没跑、拉起失败 | `down` | 徽章「没在跑」+ **真实失败原因**（不再假装成功） |
| 都没有 | `down` | 「点『检测并连接』会自动拉起」 |

配套函数（`napcat_mgr.py`）：`napcat_procs()`（psutil 优先，退回 CIM）、
`running_pids(dir)`、`stop_all(dir)`、`launch(..., restart=False)`。
`launch()` 的第一件事就是「已经在跑吗」，是就直接返回，绝不重复 `Popen`。

#### 8. 本地向量模型与记忆路由（V1.0.0-Beta）

| 环节 | 实现 | 失败时 |
|---|---|---|
| 找模型目录 | `semantic._bge_candidates()`：配置 `embed_model_dir` → `<exe>/resources/models` → `<exe>/models` → `sys._MEIPASS/models` → 仓库 `models` | 依次尝试，都没有就判定「没有本地模型」 |
| 加载 | `semantic._local_bge()`：`onnxruntime` + `tokenizers`，读 `onnx/model_quantized.onnx` | `_warn_once()` 提示一次，返回 None |
| 降级 | `context_block()` 继续走关键词 + 重要度 + 时间衰减；如果用户配了服务端 embeddings 就用它 | 控制台不报错、功能不中断 |
| 打包 | `build_backend.py` 加 `--add-data models;models` 与 `--hidden-import onnxruntime/tokenizers/numpy` | 模型没打进去时 `local_bge=False` |
| 验收 | `POST /api/semantic_probe` → `mode` 必须是 `local` | 若是 `keyword` 说明走了降级链 |

**记忆路由**（`agent.semantic_block(scope, user_text)`）：

1. `semantic_enabled` 关 → 空串（不打日志）；
2. `_is_trivial_text()` 命中 → 短句/语气词，跳过向量检索（DEBUG 记一行）；
3. `memory_cache_enabled` 且 `_hot_mem_key()` 命中 → 直接用缓存（LRU，最多 3 条）；
4. 否则 `semantic.context_block()` 真查一次，写回缓存。

#### 9. 音视频微服务的自动拉起与进程回收（V1.0.0-Beta）

| 环节 | 做法 | 失败时 |
|---|---|---|
| 找模块 | `av_bridge.module_dir()`：`av_module_dir` → 数据目录/安装目录下的 `音视频接入模块` → 同级目录 | 返回空串，记一行 INFO 跳过 |
| 拉起 | `start_if_enabled()` → `start()`：`python -X utf8 av_server.py --port <port>`，stdout/stderr 写 `runtime/av_module.log` | 记 WARN + `hint`（缺依赖 / 端口占用 / 没 python） |
| 时机 | `backend_server._auto_start_av()` 起一个 daemon 线程，**不阻塞**首启 | — |
| 锁 | `start()` 只在「决策 + spawn」时持锁，25 秒探活放到锁外 | 否则退出时 `shutdown()` 被堵，强杀后留孤儿 |
| 回收 | Windows 下用 Job Object（`SetInformationJobObject(JobObjectExtendedLimitInformation, KILL_ON_JOB_CLOSE)` + `AssignProcessToJobObject`） | 非 Windows / 调用失败就退回普通 terminate |

**为什么非要 Job Object**：Windows 上 `TerminateProcess`（= `taskkill /F`、任务管理器）
不会跑 Python 的 `finally`，`_av_shutdown()` 根本没机会执行 —— 实测后台被强杀后
`av_server.py` 还活着并占着 5800。放进 kill-on-close 的 Job 后，父进程一消失句柄就关，
内核替我们把成员进程收掉（实测强杀后 5800 立刻释放）。

## 一、整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                     Windows 桌面（本机）                       │
│                                                              │
│  ┌────────────────────────┐        ┌───────────────────────┐ │
│  │  小鲸鱼.exe / app_ui.py │        │  LM Studio (1234)     │ │
│  │  ┌──────────────────┐  │        │  OpenAI 兼容 API      │ │
│  │  │ PyWebView 窗口    │  │        │  /v1/models           │ │
│  │  │ index.html       │  │        │  /v1/chat/completions │ │
│  │  │ (Tailwind)       │  │        └───────────▲───────────┘ │
│  │  └────────┬─────────┘  │                    │ HTTP         │
│  │           │ js_api      │                    │              │
│  │  ┌────────▼─────────┐  │                    │              │
│  │  │  app_ui.py        │  │                    │              │
│  │  │  Api 门面          │  │                    │              │
│  │  └────────┬─────────┘  │                    │              │
│  └───────────┼─────────────┘                    │              │
│              │ subprocess.Popen                  │              │
│              │ (python agent.py / exe --agent)   │              │
│  ┌───────────▼──────────────────────────────────┴──────────┐ │
│  │                    agent.py（后台守护进程）                │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │ │
│  │  │ WS 收消息线程  │  │ 关怀线程      │  │ 事件提取线程   │  │ │
│  │  │ NapCat 3001  │  │ care_loop    │  │ extract_*     │  │ │
│  │  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │ │
│  │         │                 │                  │          │ │
│  │  ┌──────▼───────┐  ┌──────▼───────┐  ┌──────▼───────┐   │ │
│  │  │ 私聊补回线程   │  │ 说说线程      │  │ 滚动摘要线程   │   │ │
│  │  │ private_     │  │ moment_loop  │  │ summary_     │   │ │
│  │  │ catchup_loop │  │              │  │ worker       │   │ │
│  │  └──────┬───────┘  └──────┬───────┘  └──────────────┘   │ │
│  │         └─────────────────┼──────────────────┘          │ │
│  │                  ┌────────▼────────┐                     │ │
│  │                  │   ask_model()   │  模型调用层          │ │
│  │                  │  normalize →    │  payload 组装        │ │
│  │                  │  post → 降级     │  异常捕获            │ │
│  │                  └────────┬────────┘                     │ │
│  │                           │                              │ │
│  │  ┌────────────────────────▼──────────────────────────┐   │ │
│  │  │ memory.py  →  memory.db (SQLite)                  │   │ │
│  │  │ messages / chat_log / memories / summaries /       │   │ │
│  │  │ user_events / perm_memory / diary                  │   │ │
│  │  └───────────────────────────────────────────────────┘   │ │
│  │  stickers.py → stickers/  （表情包目录 + emoji 池）        │ │
│  │  diary.py    → 日记 / 永久记忆 / 账号绑定 / 群绑定          │ │
│  │  theme.py    → themes/     notice.py → 公告栏 + 通知中心   │ │
│  │  updater.py  → GitHub 在线更新  autostart_mgr.py → 注册表  │ │
│  │  providers.py → 模型提供方（本地 / 云端 API 预设）          │ │
│  │  usage.py    → usage_log（token 用量 + 日限额拦截）        │ │
│  │  napcat_mgr.py → NapCat 检测 / 启动 / 下载安装             │ │
│  │  mem_rebuild.py → 旧记忆重编译（补 norm_key / 去重 / 迁移） │ │
│  │  config.py → config.json     personas.py → personas.json  │ │
│  └───────────────────────────────┬──────────────────────────┘ │
│                                  │ HTTP 3000                  │
│                       ┌──────────▼──────────┐                 │
│                       │  NapCat (QQ 客户端)  │                 │
│                       │  发消息 / 发说说      │                 │
│                       └─────────────────────┘                 │
└──────────────────────────────────────────────────────────────┘
```

### 进程模型

| 进程 | 入口 | 职责 |
|---|---|---|
| GUI 主进程 | `app_ui.py` / `小鲸鱼.exe` | 渲染界面、响应按钮、读写 config/personas、拉起/杀掉 agent 子进程 |
| Agent 子进程 | `agent.py` / `小鲸鱼.exe --agent` | 连 NapCat、收发消息、调模型、跑关怀线程、写库写日志 |

> **打包后的自举**：exe 只有一个入口。带 `--agent` 参数启动时走 `_run_agent_mode()` → `agent.main()`，不带参数时开窗口。
> 这样打包版不需要用户机器上装 Python，也能拉起"后台 agent"。
>
> **`--autostart`**：开机自启动拉起时带这个参数，`app_ui.main()` 检测到后会在窗口加载完成后自动调 `start_agent()`，
> 不需要用户手点「启动 Agent」。

### 线程表（agent 子进程内）

| 线程 | 启动点 | 周期 | 干什么 |
|---|---|---|---|
| 主线程 | `main()` | — | 维持 WS 连接、分发消息 |
| `ws_listener` | 守护线程 | 阻塞 | 收 NapCat WebSocket 事件 |
| `care_loop` | 守护线程 | 60s | 中午/晚上问候 + 到期事件查岗 + 跨天刷新说说配额 |
| `proactive_loop` | 守护线程 | 90min | 群里长时间没人说话时主动开话题 |
| `private_catchup_loop` | 守护线程 | 30min | 兜底扫描私聊，补回漏掉的未回消息（防 WS 掉线） |
| `moment_loop` | 守护线程 | 60s 检查 | 按配额随机发 QQ 空间说说 |
| `diary_loop` | 守护线程 | 20min 检查 | 到点给绑定账号写日记（v4 新增） |
| `extract_worker` | 守护线程 | 事件触发 | 聊天后异步提取待办事件（不阻塞回复） |
| `perm_worker` | 守护线程 | 事件触发 | 绑定账号的私聊后提炼永久记忆（v4 新增） |
| `summary_worker` | 守护线程 | 消息数阈值 | 滚动摘要压缩历史 |

---

## 二、模块与核心逻辑

### 2.1 `config.py` —— 配置中心（新增）

配置优先级：**环境变量 > `config.json` > 内置 `DEFAULTS`**。

```python
DEFAULTS = {
    "lm_base": "http://127.0.0.1:1234/v1",
    "lm_model": "",               # 空 = 自动探测第一个可用模型
    "lm_api_key": "",
    "max_tokens": 1200,           # 普通回复预算
    "reasoning_max_tokens": 4000, # 推理模型的思考预算
    "temperature": 0.75,
    "top_p": 0.9,
    "reasoning_effort": "auto",   # auto 会实际发 none，见 4.1.2
    "request_timeout": 300,
    "max_context_tokens": 6000,
    "enable_vision": True,

    # —— 活人感（v3 新增）——
    "private_catchup_enabled": True,   # 启动后补回离线私聊
    "private_catchup_max_msgs": 6,     # 每个好友最多补回几条
    "private_catchup_first_hours": 24, # 首次（没有游标时）往回看多久
    "private_quote_reply": True,       # 私聊用引用回复
    "moment_enabled": True,            # 自动发说说
    "moment_min_per_day": 1,           # 每天最少几条
    "moment_max_per_day": 10,          # 每天最多几条（实际每天随机取一个配额）
    "moment_hours": [8, 23],           # 只在此时段发
    "moment_check_interval_min": 17,   # 两次检查的最小间隔
    "sticker_enabled": True,           # 发表情包 + 回复带 emoji
    "sticker_dir": "stickers",         # 表情包目录（相对数据目录）
    "sticker_chance": 0.45,            # 情绪匹配时发表情包的概率

    # —— 界面 / 系统（v4 新增）——
    "theme": "dark",                   # 主题 key（内置 5 套 或 themes/ 里的文件名）
    "theme_dir": "themes",             # 自定义主题目录
    "icon_dir": "icons",               # 自定义图标目录
    "icon_file": "",                   # 选中的图标文件名，空 = 默认「鲸」字
    "autostart": False,                # 开机自启动（写 HKCU Run）

    # —— 日记与永久记忆（v4 新增）——
    "diary_enabled": False,            # 是否开启日记
    "diary_bound_accounts": [],        # 绑定的 QQ 号，最多 20 个
    "diary_auto_write": True,          # 到点自动写
    "diary_hour": 22,                  # 每天几点写
    "diary_min_msgs": 6,               # 当天至少聊了几句才写

    # —— 群聊策略（v4 新增）——
    "save_group_memory": False,        # 关=只有 @ 时才记；开=所有群消息都进上下文
    "group_bind_mode": "all",          # all / whitelist / blacklist
    "group_whitelist": [],             # 最多 20 个群号
    "group_blacklist": [],

    # —— 公告栏（v4 新增，开发者功能）——
    "notice_enabled": True,

    # —— 在线更新（v4 新增）——
    "update_enabled": False,
    "update_repo": "",                 # 形如 "yourname/qq-agent"
    "update_branch": "main",
    "update_api": "https://api.github.com",
    "update_timeout": 20,
}
```

对外 API：`load()` / `save(cfg)` / `get(key, default)` / `set_value(key, value)` / `ensure_file()` / `update(**kw)` / **`set_checked(key, value) -> (ok, msg, coerced)`**。

`set_checked()` 是 v4 新增的**唯一推荐写入口**，做四件事：

| 检查 | 行为 |
|---|---|
| 未知键 | 直接拒绝，返回 `未知配置项：xxx` |
| 数值范围 | 按 `_NUM_RULES` 夹紧（如 `max_tokens` 64–32000、`temperature` 0–2、`sticker_chance` 0–1） |
| 布尔 | `"true"/"1"/"on"` → `True`，`"false"/"0"/"off"` → `False` |
| 枚举 | `reasoning_effort` 只允许 `auto/omit/off/none/minimal/low/medium/high/xhigh` |

`CONFIG_PATH` 落在数据目录（打包版是 exe 同级）。

**为什么单独抽出来**：不同电脑装的模型不一样。以前模型名硬编码在 `agent.py` 里，换台机器就得改源码重打包。现在改成界面点两下。

### 2.2 `personas.py` + `personas.json` —— JSON 人设（重构）

**旧方案**：人设是 Python 源码里的字典，改人设 = 改代码。用户在界面上编辑 `.py` 文件，保存时还得 `compile()` + `exec()` 验证语法 —— 少一个逗号整个程序起不来。

**新方案**：`personas.py` 退化成**纯加载器**，人设数据全在 `personas.json`：

```json
{
  "active": "whale",
  "personas": {
    "whale": {
      "name": "小鲸鱼",
      "persona": "你是一个叫小鲸鱼的群友……",
      "rules": "说话短句，不用敬语……"
    },
    "cold": { "name": "冷面", "persona": "……", "rules": "……" }
  }
}
```

加一个人设 = 在 `personas` 里加一个 key。不需要懂 Python。

`personas.py` 对外 API：

| 函数 | 说明 |
|---|---|
| `load()` | 读 JSON，失败回退 `_FALLBACK` 内置人设 |
| `save(data)` | 校验 → 备份 `.bak-时间戳` → `os.replace` 原子替换 |
| `validate(data)` | 返回 `(ok, msg)`，检查 7 类结构错误 |
| `build_system_prompt(active=None)` | 拼出完整系统提示词 |
| `active_name()` | 当前人设显示名 |
| `list_personas()` | `[{key, name, persona_len, rules_len, active}]` |
| `set_active(key)` | 切换当前人设 |

**校验规则**（`validate()` 拒绝的情况）：
1. 不是字典 / 空字典
2. 缺 `personas`
3. `personas` 不是字典
4. `personas` 为空
5. `active` 不在 `personas` 里
6. 某个人设缺 `persona` 字段
7. `persona` 是空字符串/纯空白

写盘用 `os.replace()`（同分区原子替换），中途断电不会留下半个文件。

### 2.3 `agent.py` —— 模型调用层（本次重点重写）

#### 调用链

```
ask_model(history, user_text, images, scope)
  │
  ├─ refresh_system_prompt()        # 懒加载人设（曾经 NameError 的源头）
  ├─ build_messages()               # 系统提示 + 记忆块 + 历史 + 当前消息
  ├─ normalize_messages()           # ★ 合法性清洗 + token 预算截断
  │
  ├─ _build_payload()               # ★ _resolve_effort() 把 auto 翻译成 none
  │
  ├─ _post_chat(payload)  ──→ (text, reason, meta)
  │      reason ∈ ok / http_400 / timeout / connection / exception
  │                / bad_json / bad_shape / reasoning_only / empty
  │
  └─ 四级补救
       ① reasoning_only → max_tokens × 3 重试，并记住该模型需要大预算
       ② http_400 且 body 提到 reasoning_effort → 去掉该参数重试，并加入 _unsupported
       ③ 失败且带图 → 去掉图片重试（纯文本也能答）
       ④ 仍失败 → 砍半历史重试
       ⑤ 全挂 → fallback_reply() 兜底话术（永不返回 None）
```

#### `_resolve_effort(effort)` —— `auto` 的翻译层

LM Studio 只认 `none/minimal/low/medium/high/xhigh`，把配置里的 `"auto"` 原样发过去会 400。
所以 `_build_payload()` 在写入 payload 前一定先过这一层：

| 输入 | 输出 | 含义 |
|---|---|---|
| `"omit"` / `"off"` / `"none-send"` / `"false"` | `None` | 完全不发这个字段 |
| `""` / `"auto"` / `"default"` / `"true"` | `"none"` | 关掉思考（0.7s 出话） |
| `none/minimal/low/medium/high/xhigh` | 原样 | 用户明确指定的合法值 |
| 其他乱写的值 | `"none"` | 记一条 WARN 后退回安全值 |

再加一道保险：若 `"reasoning_effort" in _unsupported`（之前探测到服务端不认），一律不发。

#### `normalize_messages(messages, max_tokens)` → `(list, used_tokens)`

这是修 400 的核心，做五件事：

1. **合并连续相同 role**
   - 两条 `str` → 换行拼接
   - 任一为 `list`（多模态）→ 拼接 part 列表，`image_url` 原样保留
2. **丢弃开头的 assistant**（无对应 user 的孤儿消息，某些后端直接报错）
3. **合并多个 system**（LM Studio 部分版本只接受一个）
4. **按 token 预算从最新往前累加截断**，保留最近 `MIN_KEEP_MESSAGES` 条
5. **截断后再合并一次**（截断可能又制造出连续相同 role）

返回 `used` 让调用方打日志。

#### `_post_chat()` 异常捕获

非 200 时**一定打印 `response.text`**（截断 4000 字）：

```
[ERROR] [模型] HTTP 400 —— messages=12 payload=model=qwen3.5-35b-a3b max_tokens=1200 effort=none
[ERROR] [模型] response.text = {"error":{"message":"Invalid 'reasoning_effort' value: 'auto'. Supported values: none, minimal, low, medium, high, xhigh."}}
```

同时打印 `_payload_digest()`：只给每条消息的 role / content 类型 / 字数，**不打印 base64**，避免日志爆炸。

`_build_payload()` 构造请求体，`effort="omit"/"off"` 时不带 `reasoning_effort` 字段（不是传 `null`，是整个字段不发）。

#### `probe_model()` —— 诊断单个模型

返回 `{ok, is_reasoning, effort_supported, available, detail}`。
判据：发一句极短的 `"hi"`，看 `finish_reason` 和 `reasoning_content` 长度。
如果 `content` 为空但 `reasoning_content` 有内容 → `is_reasoning=True`。

界面「测试模型能否出话」按钮就是调它。

### 2.4 `memory.py` —— 存储层

七张表 + 自动迁移。`Memory.__init__()` 里跑 `_migrate()`，老库自动补列（`chat_log.role`、`user_events.session_key`、`user_events.fired_at`），旧的 `desc` 列自动 `RENAME` 成 `event_desc`。

> **数据是持久的**：`memory.db` 是普通 SQLite 文件，落在数据目录（打包版是 exe 同级），关程序、重启电脑都不丢。
> 想让机器人"失忆"就删掉这个文件，下次启动自动重建空表。想备份就整个复制走。
>
> **v4 分层**：`messages` / `chat_log` / `summaries` 是**临时上下文**（谁都能读，滚动截断）；
> `perm_memory` / `diary` 是**永久记忆**（只有绑定过的 QQ 号才写，不截断，靠用户手动删）。

### 2.4.1 `diary.py` —— 日记与永久记忆（v4 新增）

**要解决的问题**：以前所有记忆都是"临时"的 —— `get_recent_messages(turns=8)` 只看最近 8 轮，
滚动摘要又会压缩，聊得久了早期信息就没了。v4 把记忆分成两层。

| 层 | 存哪 | 谁能用 | 生命周期 |
|---|---|---|---|
| 临时上下文 | `messages` / `chat_log` / `summaries` | 所有会话 | 滚动截断 |
| 永久记忆 | `perm_memory` | **只有绑定过的 QQ 号** | 永久，手动删 |

**绑定机制**：

```python
MAX_BOUND_ACCOUNTS = 20      # 最多绑 20 个号
MAX_BOUND_GROUPS = 20        # 群绑定也最多 20 个

bind(user_id, note="")       # 校验 \d{5,12}，写 config.diary_bound_accounts
is_bound(user_id)            # 注入永久记忆 / 触发提炼的前置判断
unbind(user_id, keep_data=True)   # 解绑默认保留数据
```

`diary_bound_accounts` 存在 `config.json`，上限 20。超出直接拒绝，不会静默截断。

**永久记忆提炼**（`extract_perm_from_dialog`）：

1. 触发点：绑定的账号发来私聊 → `process_message` 起 `perm_worker` 线程
2. 取最近 12 轮对话拼成 dialog
3. 调模型，`PERM_PROMPT` 要求输出 JSON 数组：
   ```json
   [{"content": "养了一只叫豆豆的橘猫", "category": "relation", "importance": 4}]
   ```
   - `category` 限 7 类：`identity`（身份）/`preference`（喜好）/`relation`（关系）/`plan`（计划）/`health`（身体）/`work`（工作）/`other`
   - `importance` 1–5
   - 第三人称，不要"我""你"
4. `add_perm()` 写入 `perm_memory`，**同用户同内容去重**
5. 模型不可用时退回 `_rule_extract()` —— 9 条正则兜底（叫/我是/岁/住在/养了/喜欢/讨厌/在工作/打算）

**注入回复**（`agent.build_perm_block`）：

```python
# process_message 里
perm_uid = str(user_id) if target[0] == "private" else ""
reply = ask_model(hist, prompt, images=images, scope=key,
                  perm_user=perm_uid, perm_name=sender_name)
```

`ask_model` 里拼进系统提示词：

```
【长期记忆】你确定记得关于阿哲的这些事（这些是长期记住的，可以自然引用，但别一次全说出来）：
- (身份) 名字叫阿哲
- (关系) 养了一只叫豆豆的橘猫
- (喜好) 喜欢打游戏
```

> **群聊不注入永久记忆**：`perm_uid` 只在私聊时非空。群里人多，把某个人的私人记忆抖出来是事故。

**日记**（`write_diary_for`）：

- 到 `diary_hour`（默认 22 点）由 `diary_loop` 检查，当天聊够 `diary_min_msgs`（默认 6）句才写
- `DIARY_PROMPT` 要求第一人称、120–260 字、末尾带 `[[mood:开心]]`
- `_parse_diary_output()` 拆成 `(title, content, mood)`
- `add_diary()` 同账号同天覆盖（改一天不会堆出两篇）

### 2.4.2 `theme.py` —— 主题（v4 新增）

**设计目标**：换主题不能只换"深色/浅色"，得让用户能自己写一套。

内置 5 套：

| key | 名字 | 说明 |
|---|---|---|
| `dark` | 深海蓝（默认） | 原来的深色 |
| `light` | 日光白 | 纯白底 |
| `ocean` | 浅海青 | 青绿调 |
| `sakura` | 樱粉 | 粉调 |
| `forest` | 苔绿 | 绿调 |

一套主题就是 12 个 CSS 变量：

```json
{
  "name": "我的主题",
  "vars": {
    "bg": "#101418", "panel": "#171d23", "panel2": "#1c232b",
    "line": "#30363d", "text": "#e6edf3", "muted": "#8b949e",
    "accent": "#58a6ff", "accent2": "#7ee787",
    "ok": "#3fb950", "warn": "#d29922", "err": "#f85149",
    "radius": "12px"
  }
}
```

**安全校验**（`validate()`）：

- 变量名必须在 `_ALLOWED_VARS` 里（防止注入任意 CSS）
- 颜色只接受 `#3-8位hex` / `rgb()` / `rgba()` / `hsl()` / `hsla()` / `transparent` / `currentColor`
- `radius` 只接受数字 + `px`/`rem`/`em`/`%`
- 与内置 key 重名的自定义文件被拒绝（不允许覆盖内置）

**怎么生效**：前端 `index.html` 的 `<style>` 全部用 `var(--bg)` 这类变量，
`applyThemeCss(css)` 注入 `<style id="themeVars">:root{...}</style>`，改一个标签整站变色。

### 2.4.3 `notice.py` —— 公告栏（v4 新增，开发者功能）

**定位**：给开发者留的通知渠道，**界面上没有按钮**，用户看不到入口，只能看到横幅。

改内容只需要编辑 `notice.py` 顶部的 `NOTICES` 列表：

```python
NOTICES = [
    {
        "id": "2026-10-22-v4",          # 唯一 ID，用户点了「知道了」就记这个
        "date": "2026-10-22",
        "level": "info",                 # info / warn / important
        "title": "v4 更新：设置中心上线",
        "body": "新增：主题切换、开机自启动、日记与永久记忆……",
        "link": "",                      # 可选，只允许 http(s)
        "dismissible": True,             # 能不能点「知道了」
        "expire": "2026-11-30",          # 可选，过了这天自动不显示
    },
]
```

行为：
- `active_notices()` 过滤掉已 dismiss 的、已过期的
- 已读记录写在 `agent_state.json` 的 `notice_dismissed` 键里（只改这一个键，不动别的）
- 想全部重新显示：`restore_all()`
- `config.notice_enabled = False` 可整体关掉

**为什么不做成界面功能**：公告是"开发者对用户说一句话"，不该让用户能编辑、能关掉入口。
放在 `notice.py` 里，改完重新打包就推送出去了。

### 2.4.4 `updater.py` —— GitHub 在线更新（v4 新增）

**流程**：

```
检查：GET {api}/repos/{repo}/commits/{branch}  → 比对本地 _backup/version.json 的 sha
下载：GET https://codeload.github.com/{repo}/zip/refs/heads/{branch}（200MB 上限）
应用：解压 → 剥顶层目录 → 按白名单覆盖 → 备份到 _backup/<时间戳>/ → 写 version.json
回滚：把 _backup/<时间戳>/ 里的文件复制回去
```

**安全约束**（三层）：

| 约束 | 实现 |
|---|---|
| 只连 GitHub | `_check_host()` 强制 `https://` + 域名在 `GITHUB_HOSTS` 里 |
| 只覆盖代码 | `_should_update()` 白名单 `.py/.html/.md/.bat/.txt`，且排除 `_` 开头的自检脚本 |
| 绝不覆盖用户数据 | `NEVER_OVERWRITE`：`config.json`/`personas.json`/`memory.db*`/`agent.log`/`agent_state.json`/`使用说明.txt`；`NEVER_OVERWRITE_DIRS`：`stickers`/`themes`/`icons`/`_backup`/`__pycache__`/`dist_*`/`build`/`spec`/`.git`/`.venv` |
| 防 zip 逃逸 | 解压前检查目标路径是否仍在数据目录下（`../` 攻击） |

**备份策略**：每次更新前把要覆盖的文件先复制到 `_backup/<时间戳>/`，只保留最近 5 次。
`rollback(stamp)` 可回退到任意一次。

**Token**：私有仓库需要 `GITHUB_TOKEN` 或 `QQ_AGENT_GITHUB_TOKEN` 环境变量。公开仓库不用。

### 2.4.5 `autostart_mgr.py` —— 开机自启动（v4 新增）

**为什么用注册表不用计划任务**：旧版用的是「计划任务 + `autostart.bat`」，
那玩意儿必须管理员权限才能装，删的时候还报"拒绝访问"（实测 `Unregister-ScheduledTask` 和 `schtasks /Delete` 都被拒）。
HKCU 下的 Run 键属于当前用户，读写删都不需要提权。

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
  └─ 小鲸鱼Agent = "<exe路径>" --autostart
```

| 函数 | 说明 |
|---|---|
| `current_command()` | 打包态 `"<exe>" --autostart`；源码态 `"<pythonw>" "<app_ui.py>" --autostart`（用 pythonw 避免弹黑窗） |
| `enable()` / `disable()` | 写/删注册表值，**写后回读校验** |
| `apply(enabled)` | 幂等；路径漂移（换了安装位置）时顺手修 |
| `status()` | 返回 `{enabled, config, command, expect, drift, key, msg}` |
| `repair()` | 把路径修正为当前程序位置 |

`--autostart` 参数由 `app_ui.main()` 处理：窗口加载完成后自动 `start_agent()`。

### 2.4.6 `providers.py` —— 模型提供方（v5 新增）

**解决的问题**：v2 只能连本地 LM Studio。换台电脑、或者本地模型跑不动时没法用。v5 把「本地 LM Studio」和「云端 OpenAI 兼容 API」抽象成同一个东西。

```python
PROVIDERS = {
  "local":       {name:"本地 LM Studio", base:"http://127.0.0.1:1234/v1", models:[], needs_key:False},
  "deepseek":    {name:"DeepSeek",      base:"https://api.deepseek.com/v1",
                  models:["deepseek-chat","deepseek-reasoner"], needs_key:True},
  "openai":      {name:"OpenAI",        base:"https://api.openai.com/v1", ...},
  "moonshot":    {name:"Kimi（月之暗面）", base:"https://api.moonshot.cn/v1", ...},
  "zhipu":       {name:"智谱 GLM",       base:"https://open.bigmodel.cn/api/paas/v4", ...},
  "qwen":        {name:"通义千问",        base:"https://dashscope.aliyuncs.com/compatible-mode/v1", ...},
  "openrouter":  {name:"OpenRouter",    base:"https://openrouter.ai/api/v1", ...},
  "siliconflow": {name:"硅基流动",        base:"https://api.siliconflow.cn/v1", ...},
  "custom":      {name:"自定义",          base:"", ...},
}
```

| 函数 | 说明 |
|---|---|
| `list_providers()` | 给界面用的列表，**不含任何 key**；每项带 `is_cloud` / `preset_base` / `preset_model` |
| `get_provider(key)` | 取一个预设，找不到退回 local（**永不返回 None**，避免上层 None 崩） |
| `mask_key(s)` | `sk-abcd****wxyz`，界面只显示掩码 |
| `resolve()` | 把 config.json 的 `lm_provider` / `lm_base` / `cloud_*` 解析成 `{provider, base, model, api_key, is_cloud, ok, msg}` |
| `set_provider(pkey, base, model, api_key)` | 写回 config.json，切换后可用 |
| `current_summary()` | 一行文字，给日志用 |

**配置映射**（`resolve()` 的核心，别混）：

| provider | base 取自 | model 取自 | key 取自 |
|---|---|---|---|
| `local` | `config.lm_base` | `config.lm_model` | `config.lm_api_key` |
| 其它（云端） | `config.cloud_base`，空则用预设 `base` | `config.cloud_model`，空则用预设第一个模型 | `config.cloud_api_key` |

**热切换**：`agent.py` 里 `apply_provider()` 会重读 `providers.resolve()` 刷新 `LM_BASE/LM_MODEL/LM_API_KEY/LM_IS_CLOUD`，并清空 `_reasoning_model` / `_unsupported`（换模型后这些推断全部作废）。
界面切完会 `POST http://127.0.0.1:5700/reload_model` 让后台 Agent 立刻生效，**不用重启进程**。

### 2.4.7 `usage.py` —— token 用量看板与超限拦截（v5 新增）

表 `usage_log` 建在 `memory.db` 里（见 3.8）。

| 函数 | 说明 |
|---|---|
| `record(mem, provider, model, usage, kind, is_cloud, ok)` | 每次模型调用后记一行；`usage` 直接取 API 返回的 `usage` 字段 |
| `today_usage(mem, is_cloud=None)` | 今日合计 `{calls, prompt, completion, total}` |
| `recent_days(mem, days=7)` | 最近 N 天，按天分本地/云端 |
| `by_kind(mem, day=None)` | 今日按用途（聊天/记忆/说说/日记/补回）拆分 |
| `precheck(mem, is_cloud)` | **调用前**判断是否超限 → `(allowed, msg)`；超限直接不让发请求 |
| `warn_if_needed()` | 到 `usage_warn_percent`（默认 80%）打一次 WARN |
| `dashboard(mem)` | 界面用的完整结构（见下方字段表） |
| `clear_today(mem)` | 清空今日记录（界面「清空今日」按钮） |

`dashboard()` 返回字段（**前端必须按这个取，别自己猜层级**）：

```
{
  day, limit, percent, guard, warn_percent,
  cloud: {calls, total}, local: {calls, total},
  days:  [{day, cloud, local}],
  kinds: [{kind, calls, tokens}],
}
```

**三层保护**：

1. `precheck()` —— 调用前，今日云端 token 超 `usage_daily_limit` 就 `reason="quota"` 直接拒发，一个 token 都不花。
2. `warn_if_needed()` —— 到 80% 打 WARN + 界面百分比变黄。
3. 界面进度条 —— 到 100% 变红，「保护状态」从「正常」变「已拦截」。

`usage_guard_enabled=false` 或 `usage_daily_limit=0` 都表示「不限制」。

### 2.4.8 `napcat_mgr.py` —— NapCat 检测与安装（v5 新增）

**为什么要它**：以前要先手动装 NapCat、改配置、点登录，用户卡在这一步就放弃了。

| 函数 | 说明 |
|---|---|
| `port_open(port)` / `http_alive()` / `ws_port_open()` | 三层探活：TCP → HTTP `/get_login_info` → WS 3001 |
| `find_install_dir()` | 配置的 `napcat_dir` > 9 个常见路径 > 盘符根浅扫 > `Desktop`/`Downloads` |
| `find_launcher()` | 找 `napiLoader.bat` / `NapCatWinBootMain.exe` / `launcher.bat` |
| `probe(deep=True)` | 汇总 `{running, http_ok, ws_ok, dir, launcher, msg}`，界面那张卡就显示这个 |
| `should_check_on_start()` | 三档策略：`install`（只第一次）/ `always`（每次）/ `never`（不检测） |
| `launch(dir, wait)` | `subprocess.Popen` 起启动器；`wait>0` 时轮询等端口起来 |
| `find_release_asset()` | 查 GitHub `NapNeko/NapCatQQ` 最新 release 找 zip |
| `download_and_install(target)` | 下载 + 解压到 `DATA_DIR/napcat`，**带 zip 逃逸防护**（拒绝 `..` 和绝对路径） |

`probe()` 的 `running` 只要求「HTTP 或 WS 有一个通」，因为 NapCat 起来到登录完成之间有一段只有一个端口在。

**三档自检策略**存在 `config.napcat_check_mode`，`napcat_checked_once` 记录是否已检查过（install 模式用）。

### 2.4.9 `mem_rebuild.py` —— 记忆重编译（v5 新增）

**解决的问题**：v4 以前写进库的 `perm_memory` 没有 `norm_key`（冲突检测的归一化骨架），也没有 `status`。v5 加了「新事实顶掉旧事实」，这些旧行全是 `NULL`，冲突检测会失效。

| 函数 | 说明 |
|---|---|
| `analyze(mem)` | 只读预览：`perm_total / perm_missing_norm / perm_dup_groups / perm_dup_extra / legacy_facts / legacy_bound_facts` |
| `rebuild(mem, dry_run, fill_norm, dedupe, migrate_legacy)` | 真正执行；`dry_run=True` 只算不写 |
| `backup_db(mem)` | 用 SQLite backup API 拷一份到 `memory_backup/memory_before_rebuild_<时间戳>.db` |
| `list_db_backups()` | 列出已有备份 |

**必须先备份再执行** —— 界面点「重编译」时先弹 `confirmDialog` 说明会改什么，用户确认后才跑。

两个坑（都已修）：

1. **预览说「去重 0 组」但实际有重复**：旧代码 `SELECT norm_key ... GROUP BY norm_key`，旧行 `norm_key` 是 `NULL`，SQL 把它们当成同一组之外的东西全忽略了。改成在 Python 里对每条 `key = str(nk).strip() or mem.norm_fact(content)` 现场算，预览和执行口径一致。
2. **`rebuild` 里重复再算一次备份**：`dry_run=True` 时绝不能碰磁盘，`backup_db` 只在 `dry_run=False` 分支里调。

### 2.4.10 `qzone.py` —— QQ 空间说说与评论（v6 新增）

`agent.py` 里原来直接 `napcat("send_qzone_msg", ...)`，v6 抽成模块，因为**不同 NapCat 版本支持的 action 差别很大**，必须先探测再用。

**能力探测（关键设计）**

探测手法：给 action 发一个 `{}`，看返回的 `message`：

| 返回 | 含义 |
|---|---|
| `不支持的Api <name>` | 这个版本没有该接口 |
| `Schema compilation error` / `Expected required property` | 接口存在，只是缺必填参数 |

`caps(force=False)` 会跑一遍探测并缓存 600 秒，返回：

```python
{"post": True, "delete": True, "read": False, "reply": False,
 "reply_action": "", "checked_at": 1761...}
```

实测这台机器（NapCat 9.x）：

- `send_qzone_msg` ✅ 可用，必填参数是 **`content`**（字符串），可选 `images`
- `delete_qzone_msg` ✅ 可用
- `get_qzone_msg_list` / `get_qzone_msg` / `send_qzone_comment` / `reply_qzone_comment` / `like_qzone` 等 10 个 action ❌ 全是 `不支持的Api`

**`images` 三种写法都通**（实测）：`file:///D:/...`、裸路径 `D:\...`、`base64://...`。模块统一走 `to_image_uri(path)`：≤4MB 用 `base64://`，更大用 `file:///`。

| 函数 | 说明 |
|---|---|
| `call(action, params)` | 薄封装 NapCat HTTP |
| `caps(force=False)` | 能力表（带缓存） |
| `post(content, images=None)` | 发说说 → `{ok, tid, msg}` |
| `delete(tid)` | 删说说 |
| `read_comments(tid="")` | 读评论：有接口走接口，没有就 `drain_inbox()` |
| `reply_comment(tid, comment_id, text)` | 回评论（版本不支持返回 `(False, msg)`） |
| `inbox_dir()` / `ensure_inbox()` / `drain_inbox(move=True)` / `inbox_count()` | 评论收件箱（文件投递兜底） |
| `to_image_uri(path)` / `list_images(dir)` / `image_dir(kind)` | 配图工具 |
| `status()` | 给界面看的汇总 |

**为什么要有「收件箱」**：读不到评论的 NapCat 版本，评论回复功能不等于完全没用 —— 用户可以把评论内容手打进 `qzone_inbox/` 目录（或从手机复制过来），`drain_inbox()` 会当成一条待回评论处理，回复走私聊/评论区。

自检：`python qzone.py` → 打印 `caps`。

### 2.4.11 `imagegen.py` —— 生图（v6 新增，可选）

接 OpenAI 兼容的 `POST {base}/images/generations`，**不是所有模型都支持**，所以默认关闭。

| 函数 | 说明 |
|---|---|
| `available()` | `(ok, msg)`：没开 / 没配地址 / 没配 Key 分别给不同 msg |
| `generate(prompt, size=None, model=None, tag="", record=True)` | → `{ok, path, url, msg, meta}`，存 `generated/gen_YYYYmmdd_HHMMSS_NN.png` |
| `out_dir()` | 输出目录（**注意：没有 `OUT_DIR` 全局变量，只有这个函数**） |
| `sticker_prompt(emotions, persona_name="", extra="")` | 把情绪词拼成生图提示词 |
| `moment_image_prompt(text, persona_name="")` | 说说配图的提示词 |
| `list_generated(limit)` / `cleanup(keep_days=14)` / `status()` / `last_error()` | 管理 |

两个实现细节：

1. 请求带 `response_format: "b64_json"`；有的服务不认这个参数会回 400/422，**body 里提到该参数名就直接去掉重试一次**（和 `reasoning_effort` 的降级策略同一套思路）。
2. 生图很慢（十几秒到一分钟），**绝不能在回复线程里同步调** —— `agent.gen_sticker_async()` 起 daemon 线程，回复先发出去，图生成完再补一条。

### 2.4.12 `formats.py` —— 记忆导入导出的多格式适配（v6 新增）

用户要求：「有可能用了其他类似软件，然后转到本软件，通过记忆重编译载入本软件识别的格式；导出格式可以选择导出其他格式」。

支持 6 种：

| 格式 key | 说明 | 读 | 写 |
|---|---|---|---|
| `qq-agent` | 本软件原生 JSON（含 format/version 校验） | ✅ | ✅ |
| `generic-json` | 通用 JSON：数组 / `{"items":[...]}` / 单个对象 | ✅ | ✅ |
| `jsonl` | 一行一个 JSON 对象 | ✅ | ✅ |
| `csv` | 表头认 `user_id/账号/qq`、`content/内容/记忆/事实`、`category/分类`、`importance/重要度` | ✅ | ✅ |
| `markdown` | `## 账号` 分段 + `- 记忆内容` 列表 | ✅ | ✅ |
| `txt` | 一行一条，`账号|内容` 或纯内容 | ✅ | ✅ |

| 函数 | 说明 |
|---|---|
| `detect(text)` | 猜格式，猜不出返回 `None` |
| `parse(text, fmt=None, user_id="")` | → `(ok, data, fmt_used)`，`data` 是中间结构 `{perm, diary, events, messages}` |
| `export(mem, fmt, user_id=None, include_diary=True)` | → `(ok, text, msg)` |
| `apply(mem, parsed, user_id="", replace_conflict=True, import_messages=True)` | 真正写库 |
| `guess_filename(fmt, uid)` / `list_formats()` | 工具 |

**`detect()` 的三个坑（都已修）**：

1. 单个 JSON 对象（不是数组）以前判不出来 → 新增 `_RECORD_KEYS`（`content/内容/fact/事实/memory/记忆/text/text_raw`）和 `_looks_like_record(d)`，命中就走 `generic-json`。
2. 单行 JSONL 以前识别不出来（旧代码要求 `len(lines) > 1` 且至少 3 行）→ 现在只要每行都能 `json.loads` 就判 `jsonl`。
3. `{"items":[...]}` 包装和裸数组要都能吃 → `_parse_generic_json` 对单对象也构造 `items=[data]`。

### 2.4.13 `i18n.py` + `langs/` —— 多语言（v6 新增，可外部扩展）

用户要求：「增加多语言，语言可以通过外部拓展新的语言」。

设计要点：**内置 key 表在代码里（`_BASE`，91 个 key），翻译在 `langs/<code>.json`**。加载时用「内置 + 文件覆盖」合并，缺的 key 自动退回中文，永远不会因为漏翻而报错。

| 函数 | 说明 |
|---|---|
| `ensure_dir()` | 写 `langs/zh-CN.json`、`langs/en-US.json`、`langs/怎么加语言.txt` |
| `available()` | 列出可用语言 `[{code, name, builtin, count}]` |
| `t(key, default=None, **kw)` | 取翻译（带 `{name}` 占位替换） |
| `set_lang(code)` | 落盘 + 校验语言码 |
| `export(code)` | 导出成可编辑字典 |
| `missing_keys(code)` | 缺哪些 key |

语言码正则：`^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8}){0,2}$`（挡掉路径穿越）。

**加一门语言的完整流程**（写进「使用说明.txt」）：

1. 打开 `langs/`，复制 `zh-CN.json` → 改名 `ja-JP.json`；
2. 只翻右边引号里的值，**左边的 key 一个字都不能动**；
3. 回界面「设置 → 背景与语言」点「刷新」，下拉里就多一项。

### 2.4.14 `providers.py` 的优先级链（v7 新增）

v7 把「一个模型」改成了「一条链」。配置里存的是 `model_chain`：

```json
"model_chain": [
  {"kind": "local", "provider": "local",    "base": "http://127.0.0.1:1234/v1", "model": "qwen3.5-35b-a3b", "api_key": ""},
  {"kind": "cloud", "provider": "deepseek", "base": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro", "api_key": "sk-..."}
]
```

两个开关决定这条链实际怎么走：

| 开关 | 默认 | 关掉时 |
|---|---|---|
| `local_enabled` | `true` | 链里所有 `kind=local` 的项被跳过 |
| `api_enabled` | `false` | 链里所有 `kind=cloud` 的项被跳过 |
| `model_failover` | `true` | 关掉就只试链里第一个，失败直接兜底 |

两个开关**可以同时开**，顺序完全由 `model_chain` 的数组顺序决定（界面上拖动就是改这个数组）。

| 函数 | 说明 |
|---|---|
| `chain()` | 真正要用的列表：已按开关过滤、已解析、只保留 `ok=True`、截断到 `MAX_CHAIN=10`。**`model_chain` 为空时自动回退到老的单模型设置**（`lm_base`/`lm_model`），保证升级不炸 |
| `raw_chain()` | 给界面看的原始链。**`api_key` 一律置空**，只留 `masked_key` 和 `has_key` —— 曾经漏了这一层，界面上能直接抓到完整 key |
| `_entry_target(entry)` | 把一条链项解析成 base/model/key/is_cloud/needs_key/missing_key/ok/label |
| `add_to_chain(...)` | 查重 + 上限 10 + 顺手打开对应开关 |
| `remove_from_chain(index)` / `move_in_chain(frm, to)` / `clear_chain()` | 删 / 拖动排序 / 清空 |
| `chain_summary()` | 一行摘要，如 `本地 LM Studio / qwen3.5-35b-a3b → DeepSeek（官方） / deepseek-v4-pro` |

`needs_key` 与 `missing_key` 要分开：前者是「这个提供方本来要不要 Key」（本地永远不要），后者是「这一条现在缺不缺 Key」。界面只对 `is_cloud && !has_key` 显示「缺 Key」——早期把两者混成一个，结果本地那一行也顶着个刺眼的「缺 Key」。

`agent.py` 侧对应的是 `_chain_targets()` + `ask_model()` 的 for 循环：

```python
for i, tg in enumerate(_chain_targets(), 1):
    text, reason, meta = _ask_once(msgs, tg, ...)
    if text:
        if i > 1:
            log(f"[模型] 顺延到第 {i} 个端点 {tg['label']} 成功")
        return text
    log(f"[模型] {tg['label']} 失败（{reason}），顺延下一个（{i}/{n}）", "WARN")
```

配套改造：`_unsupported` 从 `set()` 变成 `{端点key: set()}`、`_reasoning_model` 从单值变成 `_reasoning_cache` 字典 —— 不然 A 端点不认 `reasoning_effort`，会把 B 端点也一起禁掉。

### 2.5 `stickers.py` —— 表情包与 emoji（v3 新增）

**设计目标**：不依赖生图模型，用「本地图片 + 文件名标签」实现发表情包；同时让回复自然带 emoji。

目录结构：

```
数据目录/
  stickers/                  ← 用户可随意往里丢图片
    开心_好耶.png            ← 文件名里的情绪词就是标签
    生气_炸毛.png
    无语_汗.png
    _meta.json              ← 可选，手动指定标签：{"开心_好耶.png": ["开心","庆祝"]}
    _disabled.json          ← 可选，禁用的文件名列表
    怎么用.txt              ← 首次自动生成的使用说明
```

支持的情绪（12 类）：`开心 生气 委屈 无语 害羞 调皮 惊讶 困 爱心 点赞 吃饭 打招呼`

核心函数：

| 函数 | 作用 |
|---|---|
| `ensure_dir(path)` | 建目录 + 生成「怎么用.txt」 |
| `list_stickers(cfg_dir)` | 扫描图片，`_tags_from_filename()` 从文件名提取标签 |
| `detect_emotions(text)` | 关键词匹配文本情绪，返回情绪列表 |
| `pick_sticker(text, emotions, cfg_dir, rng)` | 优先选标签命中的；同分随机；全无标签时 50% 概率随机一张 |
| `sticker_to_segment(path)` | 转成 NapCat 消息段：>4MB 用 `file:///`，否则 `base64://` |
| `pick_emoji(emotions)` | 从 `EMOJI_SETS` 里挑一个 emoji |
| `decorate_emoji(text, chance)` | 给回复补 emoji（已有 emoji 或以问号结尾时不加） |

**为什么用文件名做标签**：用户不会去编辑 JSON。把图存成 `开心_好耶.png` 就完事，零学习成本。

**发送逻辑**（`agent.py`）：

```python
# process_message() 末尾
quote_id = event.get("message_id")            # 引用回复
send_private_human(uid, reply, reply_to=quote_id)
maybe_send_sticker(key, reply, reply_to=quote_id)   # 情绪匹配 + 概率通过才发
```

`maybe_send_sticker()` 会先 `detect_emotions(reply)`，命中且 `random() < STICKER_CHANCE` 才发一张，避免每条都甩图。

**emoji 是模型自己写的**：`EMOJI_PROMPT_RULE` 拼进系统提示词，要求"三四句带一个 emoji、只用常见 emoji、对方难过或说正事时不加、一条不超三个"。`decorate_emoji()` 只做兜底补强。

### 2.6 `app_ui.py` —— 桌面客户端后端

`Api` 类通过 `pywebview` 的 `js_api` 暴露给前端：

| 方法 | 作用 |
|---|---|
| `start_agent()` / `stop_agent()` | 拉起/杀掉 agent 子进程 |
| `get_status()` | 运行状态、PID、模型、配置文件路径 |
| `get_logs(offset)` | **增量**读日志，返回 `{text, offset, size, reset}` |
| `clear_logs()` | 清空日志 |
| `load_personas()` / `save_personas(content, restart)` | JSON 人设读写 |
| `set_active_persona(key)` | 切换人设 |
| `list_models()` | 实时 GET `/v1/models`，过滤 embedding 模型；连不上时返回中文指引 + `offline=True` |
| `start_lm_server(port)` | 自动找 `lms.exe` 并 `server start --port 1234`，再用真实 `/v1/models` 验证 |
| `set_model(model, restart)` | 换模型（写 config.json） |
| `set_model_base(base, model)` | 换服务地址 |
| `test_model()` | 调 `probe_model()` 诊断，返回 `effort_supported`/`effort_sent` |
| `get_config()` / `save_config(cfg)` | 参数读写（白名单 + 范围夹紧） |
| `get_memory_data(msg_limit, event_limit)` | 记忆表格数据 |
| `delete_event(event_id)` | 删事件 |
| `get_stickers()` | 表情包列表（含 base64 缩略图、标签统计） |
| `open_sticker_dir()` | 打开表情包目录 |
| `test_sticker(text)` | 测试文本会匹配到哪张图（不发送） |
| `get_moment_state()` | 说说今日配额、已发数、最近记录 |
| `post_moment_now(text)` | 手动发一条说说（text 空则让模型生成） |
| `open_data_dir()` | 资源管理器打开数据目录 |
| **`get_settings()`** | v4：一次返回主题/自启/日记/群聊/更新/公告/图标的全部状态 |
| **`save_settings(patch)`** | v4：批量写配置，走 `set_checked` 校验；`autostart` 额外落注册表 |
| **`list_themes()`** / **`apply_theme(key)`** | v4：列出（内置 + themes/ 自定义）/ 应用主题，返回 CSS 变量串 |
| **`open_theme_dir()`** | v4：打开 themes/ 目录 |
| **`get_autostart()`** / **`set_autostart(enabled)`** | v4：读/写 HKCU Run |
| **`get_diary()`** / **`bind_account(uid, note)`** / **`unbind_account(uid, keep)`** | v4：日记状态、绑定/解绑账号（最多 20） |
| **`write_diary_now(uid)`** | v4：立即给某账号写今天的日记 |
| **`get_groups()`** / **`set_group_mode(mode)`** / **`add_group(gid, to_blacklist)`** / **`remove_group(gid)`** | v4：群聊绑定（白/黑名单，各最多 20） |
| **`get_memory_overview()`** | v4：临时/永久/日记三段数据一次返回 |
| **`delete_perm(id)`** / **`clear_perm(uid)`** | v4：删一条/清空某账号的永久记忆 |
| **`open_db_dir()`** | v4：打开 memory.db 所在目录 |
| **`get_notices()`** / **`dismiss_notice(id)`** | v4：公告栏（开发者功能，界面无入口） |
| **`get_update_status()`** / **`check_update()`** / **`apply_update(restart)`** / **`rollback_update(stamp)`** | v4：GitHub 在线更新 |
| **`get_icons()`** / **`set_icon(name)`** / **`open_icon_dir()`** | v4：自定义图标（base64 内联，≤400KB） |

**日志增量读取**：前端每 1.5s 带上次 offset 来要数据，后端 `seek(offset)` 只读新增字节。日志涨到几十 MB 也不会卡。

**缩略图为什么用 base64**：WebView2 默认禁止 `file://` 页面加载其它 `file://` 资源（跨源）。27 张 8KB 的图内联成 data URL 总共约 200KB，一次性传完，比开本地 HTTP 服务简单。

**`save_config()` 白名单**（防前端乱传）：

| 键 | 范围 |
|---|---|
| `max_tokens` | 64 – 32000 |
| `reasoning_max_tokens` | 512 – 32000 |
| `temperature` | 0 – 2 |
| `top_p` | 0 – 1 |
| `max_context_tokens` | 1000 – 200000 |
| `request_timeout` | 10 – 3600 |
| `reasoning_effort` | auto / omit / off / none / minimal / low / medium / high / xhigh |
| `lm_base` / `lm_model` | 字符串 |

其它键直接丢弃；数字越界自动夹到边界。

### 2.7 `index.html` —— 桌面 UI

- Tailwind CSS（CDN），**无框架**，纯原生 JS
- **CSS 变量驱动配色**：`:root` 定义 12 个变量，Tailwind 工具类用 `!important` 映射到变量，
  所以 `theme.py` 注入 `<style id="themeVars">:root{...}</style>` 就能整站换色
- **6 个 Tab**：控制台 / 模型设置 / 人设编辑 / 活人感 / 记忆查看 / **设置**
- 顶部常驻 `modelChip` 显示当前模型名；左上角 `#appIcon` 可被自定义图片替换
- 「模型设置」Tab 右上角两个按钮：**启动 LM Studio 服务**（自动拉起 1234）、**刷新列表**
- 日志框：级别着色（ERROR 红 / WARN 黄 / DEBUG 灰 / 关怀 青），DOM 上限 2000 行自动裁头
- 公告横幅 `#noticeBar` 在 `<nav>` 下方，有公告时才显示

**「活人感」Tab（v3 新增）** 分左右两块：

| 区域 | 内容 |
|---|---|
| 左：表情包管理 | 27 张缩略图网格（base64 内联）、按情绪统计的标签条、`打开目录` / `重新扫描` 按钮、底部「测试匹配」输入框（输入一句话看会匹配到哪张图 + 带 emoji 的效果） |
| 右：自动说说 | 今日配额进度条（`已发/配额`）、上次发布时间、每天随机区间与时段、手动输入框 + `立即发一条`（留空则由模型生成）、最近发过的说说列表（时间 + 正文） |
| 底部 | 活人感行为一览（引用回复 / 私聊补回 / 说说 / emoji / 问候 / 查岗） |

**「记忆查看」Tab（v4 改造成三子 Tab）**：

| 子 Tab | 内容 |
|---|---|
| 临时上下文 | 最近聊天记录（`messages` 表）+ 待办事件（`user_events` 表） |
| 永久记忆 | `perm_memory` 表，列：账号 / 分类 / 内容 / 重要度 / 记录时间 / 操作（删）。未绑定账号时顶部提示「还没绑定账号 → 永久记忆不会被写入」 |
| 日记 | 左侧日记列表（日期 + 心情 + 标题），右侧全文；点一条看详情 |

右上角固定两个按钮：**打开文件目录**（`open_db_dir`）、**刷新数据**。

**「设置」Tab（v4 新增）** 6 张卡片：

| 卡片 | 内容 |
|---|---|
| 外观 | 主题下拉（5 内置 + `themes/` 自定义）+ `应用` / `打开主题目录` / `重新扫描`；自定义图标下拉 + `应用` / `打开目录` / `刷新` |
| 启动与系统 | 开机自启动开关（带 `自启动注册表状态` 只读行）、**保存群聊记忆**开关 |
| 日记与永久记忆 | 开启日记开关、绑定 QQ 号输入框 + `绑定`、已绑定列表（可解绑）、`立即写今天的日记` |
| 群聊绑定 | 模式下拉（全部回复 / 只回白名单 / 屏蔽黑名单）+ 群号输入 + `加白名单` / `加黑名单` + 两个列表（可删） |
| 在线更新 | 启用开关、仓库名输入（`owner/repo`）、分支、`保存` / `检查更新` / `立即更新` / `回滚` |
| 配置文件 | 只读显示 `config.json` 绝对路径 |

前端调用的方法总数：**46 个**（`_check_ui.py` 会自动校验前后端契约）。

---

## 三、数据库表结构

数据库：`memory.db`（SQLite，落在数据目录）

### 3.1 `messages` —— 会话消息（主表）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `session_key` | TEXT | `private:<QQ号>` 或 `group:<群号>` |
| `role` | TEXT | `user` / `assistant` / `system` |
| `content` | TEXT | 消息正文 |
| `created_at` | REAL | unix 时间戳 |

索引：`idx_messages_session (session_key, id)`

### 3.2 `user_events` —— 待办事件（本次新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `user_id` | TEXT | QQ 号 / 群号 |
| `event_desc` | TEXT | 事件描述，如"明天9点考试" |
| `target_time` | REAL | 事件发生的 unix 时间戳 |
| `status` | TEXT | `pending` / `done` / `cancelled` |
| `session_key` | TEXT | 归属会话，查岗时发到这里 |
| `created_at` | REAL | 创建时间 |
| `fired_at` | REAL | 实际触发时间 |

索引：`idx_events_status (status, target_time)`

**状态机**：`pending` →（时间到 + 用户静默 > 2h）→ 发问候 → `done`。
去重：同 `user_id` + 同 `event_desc` + `target_time` 相差 < 1 小时 且 `status='pending'` → 不重复插入，返回 `0`。

### 3.3 `memories` —— 长期事实

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `scope` | TEXT | 会话 key |
| `person` | TEXT | 谁的事 |
| `fact` | TEXT | 事实内容 |
| `created_at` | REAL | |

### 3.4 `chat_log` —— 全量聊天流水（含 role）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `scope` | TEXT | |
| `person` | TEXT | |
| `role` | TEXT | 迁移新增 |
| `content` | TEXT | |
| `created_at` | REAL | |

### 3.5 `summaries` —— 滚动摘要

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `scope` | TEXT | |
| `content` | TEXT | 摘要正文 |
| `created_at` | REAL | |

### 3.6 `perm_memory` —— 永久记忆（v4 新增，v5 加 4 列）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | TEXT NOT NULL | 归属 QQ 号（必须是绑定过的） |
| `category` | TEXT NOT NULL DEFAULT 'other' | `identity`/`preference`/`relation`/`plan`/`health`/`work`/`other` |
| `content` | TEXT NOT NULL | 事实内容，第三人称 |
| `importance` | INTEGER DEFAULT 3 | 1–5，越大越靠前 |
| `source` | TEXT | 来源，如 `chat` / `rule` / `manual` |
| `created_at` | REAL | |
| `last_used` | REAL | 上次注入回复的时间 |
| `use_count` | INTEGER DEFAULT 0 | 注入次数 |
| `status` | TEXT NOT NULL DEFAULT 'active' | **v5**：`active` / `superseded`（被新事实顶掉了） |
| `supersedes` | INTEGER DEFAULT 0 | **v5**：这条顶掉了哪条旧记忆的 id（0 = 没顶） |
| `norm_key` | TEXT | **v5**：归一化骨架，冲突检测用（见 `norm_fact()`） |
| `updated_at` | REAL | **v5**：最后一次状态变化时间 |

索引：`idx_perm_user (user_id, id DESC)`

**去重规则**：同 `user_id` + 同 `content` → 不重复插入，`add_perm()` 返回 `(existing_id, False, [])`。
**排序**：`ORDER BY importance DESC, id DESC` —— 重要的先说，同等重要按新旧。
**默认只读 active**：`get_perm()` / `count_perm()` / `get_perm_list()` 默认 `status='active'`，要看被顶掉的传 `include_superseded=True`。
**不物理删除**：被顶掉的旧行留在库里（`status='superseded'`），界面上能看到"已被替换"，用户想追溯还有。

`norm_fact(text)` 归一化步骤（冲突检测的地基）：

```
① 全角转半角
② 日期归一：2024年11月27日 / 2024-11-27 / 2024/11/27 / 2024.11.27 → 20241127
③ 去年 / 去月 / 去日 / 去号
④ 只留中文 + 字母 + 数字
⑤ 去掉停用字：的了是在有和与及也都就还又很非常我你他她它这那么吗呢吧啊呀哦哪其
⑥ 转小写
⑦ 截前 120 字符
```

例：`norm_fact("纪念日是2024年11月27日")` 与 `norm_fact("纪念日 2024-11-27")` 都归一成 `纪念20241127`。

### 3.7 `diary` —— 日记（v4 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | TEXT NOT NULL | 归属 QQ 号 |
| `day` | TEXT NOT NULL | `YYYY-MM-DD` |
| `title` | TEXT | 标题 |
| `content` | TEXT NOT NULL | 正文，第一人称，120–260 字 |
| `mood` | TEXT | 心情词，从 `[[mood:xxx]]` 解析出来 |
| `msg_count` | INTEGER DEFAULT 0 | 当天聊了几句 |
| `created_at` | REAL | |

索引：`idx_diary_user (user_id, day DESC)`
**覆盖规则**：同 `user_id` + 同 `day` → 覆盖，不会堆出两篇。

### 3.8 `usage_log` —— 模型用量（v5 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `day` | TEXT | `YYYY-MM-DD`，按天聚合用 |
| `ts` | REAL | 精确时间戳 |
| `provider` | TEXT | `local` / `deepseek` / … |
| `model` | TEXT | 模型名 |
| `kind` | TEXT | `chat` / `memory` / `moment` / `diary` / `catchup` / `summary` / `image` / `plan` / `comment` / `extract` / `greeting` / `probe` / `other` |
| `prompt_tokens` | INTEGER | 输入 token |
| `completion_tokens` | INTEGER | 输出 token |
| `total_tokens` | INTEGER | 合计 |
| `is_cloud` | INTEGER | 1 = 云端（要计费），0 = 本地 |
| `ok` | INTEGER | 1 = 成功，0 = 失败（失败也记，方便看异常用量） |
| `detail` | TEXT | v6 新增：这一笔消耗**用在哪了**（「写日记 2026-10-22」「发说说：…」「私聊补回 安瑆.雨」等，界面上「用量明细」直接显示） |

索引：`idx_usage_day (day)`、`idx_usage_kind (kind, day)`

**为什么要记失败的**：模型调用失败也会消耗少量 token（尤其超时前已经生成了一部分），而且失败率突增本身就是异常信号。

**kind 怎么来的**：`agent.py` 的 `_usage_kind(tag)` 按调用点的 tag 归类 —— `/记忆提取` → `memory`、`/说说` → `moment`、`/日记` → `diary`、`/补回` → `catchup`、`/摘要` → `summary`、`/生图` → `image`、`/关心` → `plan`、`/评论` → `comment`、`/事件` → `extract`、`/问候` → `greeting`、`/探测` → `probe`，其余一律 `chat`。

**`detail` 怎么来的（v6）**：`usage.record(..., detail=...)` 由调用方传；没有明细时界面显示「聊天回复」这类通用文案。`usage.set_detail(mem, kind, detail)` 可以在事后补写（例如说说发成功拿到 `tid` 之后再补上 tid）。

### 3.9 `care_plans` —— 主动关心计划（v6 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `user_id` | TEXT | QQ 号 |
| `session_key` | TEXT | `private:<QQ号>` / `group:<群号>`，到点发到这里 |
| `plan_at` | REAL | 计划的 unix 时间戳（**未来**时间） |
| `reason` | TEXT | 为什么要关心（如"下课"），写进 prompt 当上下文 |
| `topic` | TEXT | 模型建议的话题（如"今天上了什么课"） |
| `source` | TEXT | `model`（事件提取自动排）/ `manual`（界面上加） |
| `status` | TEXT | `pending` / `done` / `cancelled` |
| `created_at` | REAL | 创建时间 |
| `fired_at` | REAL | 实际发出时间 |
| `extra` | TEXT | 预留 JSON（v6 目前写 `{"event_id": N}`，方便追溯是哪条事件排的） |

索引：`idx_plan_due (status, plan_at)`、`idx_plan_session (session_key, status)`

**和 `user_events` 的区别**：`user_events.target_time` 是「事情发生的时间」，查岗是**事后**问（过了 target_time 才发）；`care_plans.plan_at` 是「我打算关心你的时间」，是**事前/到点**发。两者互不替代，都保留。

**去重**：`add_plan(..., dedup_min=10)` 会在同会话 ±10 分钟内找 `pending` 的计划，找到就不再加（模型每次对话都提取一次事件，不去重会排出一堆同一件事的关心）。

### 3.10 `moment_comments` —— 说说评论（v6 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `moment_tid` | TEXT | 说说的 tid（NapCat 返回的） |
| `moment_text` | TEXT | 那条说说正文（生成回复时当上下文） |
| `commenter_id` | TEXT | 评论人 QQ（可能为空，收件箱手打的没有） |
| `commenter_name` | TEXT | 评论人昵称 |
| `content` | TEXT | 评论内容 |
| `reply` | TEXT | 生成的回复 |
| `status` | TEXT | `pending` / `replied` |
| `source` | TEXT | `napcat`（接口读到的）/ `inbox`（收件箱手打的） |
| `created_at` | REAL | |
| `replied_at` | REAL | 回复时间 |

索引：`idx_cmt_status (status, id)`

**去重**：`add_comment()` 按 `(moment_tid, commenter_id, content)` 判重，同一条评论读两遍不会重复入库。这一步很关键 —— 因为大多数 NapCat 版本读不到评论，靠收件箱反复扫，没去重会把自己手打的评论回几十遍。

**v7 加的四个字段（支持"追评到没有回复为止"）**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `parent_id` | INTEGER | 被回复的那条评论 id（0 = 顶层） |
| `root_id` | INTEGER | 整条线索的根 id，同一串回复共享 |
| `depth` | INTEGER | 层级，0 = 一级评论，1 = 我们回的，2 = 对方再回… |
| `direction` | TEXT | `in` = 别人说的 / `out` = 我们说的 |

索引：`idx_cmt_thread (root_id, id)` —— 注意这个索引**必须在补列之后建**。写在 `_init_tables()` 里新库会直接报 `no such column: root_id`（CREATE INDEX 跑在 ALTER TABLE 前面），所以挪到了 `_migrate()` 末尾。

配套方法：`add_our_comment()`（写 `direction='out'`）、`comment_thread(root_id)`（按 id 正序返回整串）、`threads_awaiting_reply()`（找"最后一条是 `in` 且 `pending`"的线索）。

### 3.11 `feed_seen` —— 浏览过的动态（v7 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `post_key` | TEXT | 动态唯一键（优先用 tid，没有就用 `作者+内容hash`） |
| `source` | TEXT | `qzone` / `inbox` |
| `author_id` | TEXT | 发动态的人 QQ |
| `author_name` | TEXT | 昵称 |
| `content` | TEXT | 正文 |
| `images` | TEXT | 图片（逗号分隔） |
| `action` | TEXT | `seen` / `liked` / `replied` / `skipped` |
| `reply` | TEXT | 我们回的内容 |
| `status` | TEXT | 同 `action`，便于查询 |
| `created_at` | REAL | 第一次看到的时间 |
| `updated_at` | REAL | 最后动过的时间 |

索引：`idx_feed_key (post_key)`

**为什么要有这张表**：同一条动态每轮都会被扫到，不记就每轮重复点赞/重复评论。`feed_mark()` 对已存在的 `post_key` 只更新 `action/reply/status`，不新增行。

---

### 3.12 `affinity` —— 好感度（v8 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `user_id` | TEXT PK | 对方 QQ 号（**主键，一个人只能有一行**） |
| `name` | TEXT | 昵称（每次加分时顺手更新） |
| `score` | INTEGER | 分数，默认 0，受 `affinity_max` 上限约束（默认 1000） |
| `level` | TEXT | `stranger` / `acquaintance` / `familiar` / `friend` / `close_friend` / `confidant` / `special` |
| `msgs` | INTEGER | 累计互动条数（只增不减，「清零」也不清它） |
| `positive` | INTEGER | 说好听话的次数 |
| `negative` | INTEGER | 说难听话的次数 |
| `created_at` | REAL | 第一次交互时间 |
| `updated_at` | REAL | 最后更新时间 |
| `last_decay_day` | TEXT | 上次衰减到哪一天（`YYYY-MM-DD`），防同一天重复扣 |

无额外索引（`user_id` 就是主键）。

**为什么不直接删行**：`reset_affinity()` 是 `UPDATE ... SET score=0, level='stranger'`，保留 `msgs` 便于「清零后还知道聊过多少句」。

**踩坑（真实炸过）**：`bump_affinity()` 原来写的是裸 `INSERT INTO affinity(...)`，同一个人第二次加分就抛
`sqlite3.IntegrityError: UNIQUE constraint failed: affinity.user_id`。现在改成 `INSERT OR IGNORE INTO affinity(...)` 再 `UPDATE`。

**阈值表 `AFFINITY_LEVELS`**（`memory.py` 模块级）：

| 分数 | level | 中文 |
|---|---|---|
| 0 | `stranger` | 陌生人 |
| 10 | `acquaintance` | 认识的人 |
| 30 | `familiar` | 熟人 |
| 60 | `friend` | 朋友 |
| 120 | `close_friend` | 好朋友 |
| 200 | `confidant` | 知己 |
| 350 | `special` | 特别的人 |

`score` 会按上表向下取档（`affinity_level()` 从高往低比），到 350 就封顶在 `special`，再高也还是 `special`。

---

###3.13 `ui_sign_audit` ——签署审计（V1.7.0 新增；V1.8.1 加 `algo` 列与封印表）

库文件：`<数据目录>/audit/ui_audit.db`（**不是** `memory.db`，独立连接、开 WAL）。

|字段 |类型 |说明 |
|---|---|---|
| `id` | INTEGER PK |自增 |
| `ts` | TEXT |本地落库时间 `%Y-%m-%d %H:%M:%S` |
| `agreement` | TEXT |协议标识：`disclaimer` / `learn_bottom_line` /业务自定义 |
| `version` | TEXT |条款版本号（如 `1.7.0`） |
| `title` | TEXT |弹窗标题（留痕便于人工核对） |
| `user_id` | TEXT |用户 ID（config的 `user_id`，没有则 `local-<machine_id>`） |
| `machine_id` | TEXT | `sha256(uuid.getnode()\|host\|machine\|system)[:16]` |
| `mac` | TEXT |首选网卡 MAC；取不到真实 MAC时该值为随机数，`device_info().mac_ok=false` |
| `accepted_at` | TEXT |前端点「同意」时的 ISO时间（`new Date().toISOString()`） |
| `signature` | TEXT |手写签名（`requireSign`场景，≥2字） |
| `client` | TEXT | JSON：`{ua, lang, tz, screen}` |
| `prev_hash` | TEXT |上一行的 `hash`（第一行为空） |
| `hash` | TEXT | `sha256(prev_hash \x1f ts \x1f agreement \x1f version \x1f user_id \x1f machine_id \x1f accepted_at)[:32]` |
| `algo` | TEXT | 本行用的哈希算法号：`v181` = 11 字段（V1.8.1 起）；`NULL`/其他 = 老的 7 字段（V1.7.0~V1.8.0）。`verify()` **逐行按它决定用哪套算法复算** |

索引：`idx_ui_sign_agreement (agreement, id)`、`idx_ui_sign_ts (ts)`

哈希链的意义：任何一行被改，`verify()` 逐行重算时立刻发现 `broken`。

**V1.8.1 之前只算 7 个字段**（`prev_hash/ts/agreement/version/user_id/machine_id/accepted_at`），
标题、MAC、签名、客户端指纹改了都查不出来 —— 而问「到底谁同意了哪一份条款」时，恰恰是这四项在说话。
更麻烦的是：**只把末尾几行删掉，剩下的每一行 `prev_hash` 依然自洽**，逐行校验完全查不出来。

V1.8.1 两件事一起补：

- `_digest()` 扩到 **11 个字段**（加 `title` / `mac` / `signature` / `client`），算法号写进 `algo` 列。
  老记录继续用 `_digest_v1()` 校（函数保留在 `audit.py`），升级不会让老库集体变「被篡改」。
- 新增封印表 `ui_sign_audit_seal`（单行），存「应有条数 + 尾哈希」，每次写入后刷一次。

```
CREATE TABLE IF NOT EXISTS ui_sign_audit_seal (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    n    INTEGER NOT NULL,      -- 应该有这么多条
    tail TEXT,                  -- 最后一条的 hash
    ts   TEXT
);
```

`verify()` 现在是两段式：

| 段 | 查什么 | 抓到什么 |
| --- | --- | --- |
| 逐行复算 | 每行 `hash` 重算 + `prev_hash` 衔接 | 篡改（`intact=false`，`broken` 列出 id） |
| 尾部锚点 | 实际条数 vs `seal.n`；实际尾哈希 vs `seal.tail` | 截断（`tail_ok=false`，`tail_msg` 说人话） |

两个检查是**分工不是冗余**：删掉末尾一行时 `intact` 仍是 `true`（链本身没错），只有 `tail_ok` 会变 `false`
并报 `记录条数对不上：应有 3 条，实际 2 条`。反过来改中间一行只影响 `intact`。

返回：`{ok, total, broken, intact, tail_ok, tail_msg, algo}`。

审计只增不改不删（代码里没有 UPDATE / DELETE，`_seal_update()` 是唯一一条 `ON CONFLICT DO UPDATE`，
且只动 `ui_sign_audit_seal` 那一行）。


对应 `Api`方法（V1.7.0，`app_ui.py`的 `for _av_name, _av_fn in (...)`里注册）：

|方法 |说明 |
|---|---|
| `audit_sign_accept(payload)` |写一条，返回 `{ok, id, at, hash, agreement, version, user_id, machine_id, mac}` |
| `audit_sign_state(agreement)` |查最近一次同协议记录 |
| `audit_sign_list(limit)` |最近 N条 |
| `audit_sign_verify()` |校验整条哈希链 |
| `audit_device_info()` |本机 `machine_id` / `mac` / `host` / `platform` |

**这些方法是 POST-only**：`backend_server._READ_ONLY_PREFIXES`里只有
`get_/list_/has_/is_/count_`，`audit_`不在其中，所以不会被当成只读放行。
`_DANGER_METHODS`也不含它们，因此不需要一次性票据。

`accept_disclaimer()`（首次免责协议的后端方法）在本版内部**额外**调一次
`audit.accept({"agreement": "disclaimer", "version": APP_VERSION, "title": "免责与使用声明"})`，
失败只记日志不影响同意流程——前端签署弹窗自己也会调 `accept_disclaimer`，两处都写会各有
一条记录（一条带前端 client信息、一条是后端兜底），属预期。


###3.14 `kg_entities` / `kg_relations` ——知识图谱（V1.8.0新增，同一个 `memory.db`）

**不另开库**：两张表跟 `perm_memory` 一起放在 `memory.db`，这样「永久记忆」和「关系网」永远同步，
备份/胶囊/清空都不用协调两个文件。

| 表 | 字段 |
|---|---|
| `kg_entities` | `id` PK、`user_id`、`name`（原样显示名）、`norm`（归一化名）、`etype`（默认 `thing`）、`mentions`、`created_at`、`updated_at` |
| `kg_relations` | `id` PK、`user_id`、`subject`、`predicate`、`object`、`s_norm`、`p_norm`、`o_norm`、`confidence`（0.05~1.0）、`source`（`dialog`/`perm_backfill`/`import`/`perm_skip`）、`perm_id`、`status`、`created_at`、`updated_at` |

索引：`idx_kge_user (user_id)`、`idx_kgr_user (user_id)`、`idx_kgr_s (user_id, s_norm)`、`idx_kgr_o (user_id, o_norm)`

**迁移闸门**：`kg.ensure_schema(conn)` 读 `PRAGMA user_version`，`KG_SCHEMA_VERSION = 1`；
版本够新也补一遍 `CREATE TABLE IF NOT EXISTS`，**只加表、不 ALTER、不 DROP**，异常一律 `return False`。
`Memory.__init__` 里用 `try/except: pass` 包住调用——图谱出问题绝不影响主库启动。

**归一化规则**（决定同一个实体能不能对上）：
- `norm_entity(name)`：去首尾标点 → 只留中文/字母/数字 → 去掉 `的了是在有和与及也都就还又很非常这那么吗呢吧啊呀哦` → `lower()[:40]`
- `norm_predicate(p)`：去空白 → `[:24]`（**谓词不做同义词合并**，这是刻意的：合并错了比不合并更糟）

**多跳检索链路**：

```
recall(user_id, query, hops=2, limit=14)
  ├─ find_seeds()          实体名被问句包含 1.0+len/20；实体名包含问句 0.8；2-gram Jaccard>=0.34 → 0.5+jac*0.3
  ├─ seed_by_predicate()   谓词按 [「…」「。」「的」] 切词块，块含于问句 → 两端都当种子
  ├─ seeds = 两者【合并去重】（不是「找不到才兜底」）
  ├─ walk()                BFS，最多 3 跳 / 4000 条边，边信息 {subject, predicate, object, depth}
  └─ context_block()       "
【关系网】顺着这句话，这些人和事是连在一起的（可自然引用，别硬塞）：
- A —(p)→ B
"
```

`networkx` 可用时用 `nx.MultiDiGraph`（`add_edge(s, o, key=predicate)`，同一对实体可以有多个谓词）；
不可用时 `_load_simple()` 返回邻接表，`walk()` 有独立分支。图对象缓存 30 秒，写操作通过 `_STAMP` 立刻失效。

**三个写入入口**：
1. `diary.extract_perm_from_dialog()`：`PERM_PROMPT` 新增 `triples` 字段，**只有 `is_new` 的条目**才 `ingest_triples()`；
2. `agent.kg_build_loop()`：每 `kg_build_interval_min`（默认 1440）跑一批 `kg_build_batch`（20~50）条老 `perm_memory`，
   拆不出三元组的插一条 `status='skipped', source='perm_skip'` 防重试；
3. `kg.import_rows()`：胶囊 / 记忆备份导入时整批写入。

**导出/导入**：`mem_transfer.EXPORT_TABLES` 登记两表（自然键 `(user_id, norm)`、`(user_id, s_norm, p_norm, o_norm)`）；
`spark.FORMAT_VERSION = 4`，manifest `counts` 多 `kg_entities`/`kg_relations`，
导入用 `data.get("kg_entities", [])` 兜底 → **v1~v3 的老胶囊照样能装**，`stats["kg"]` 会写明「胶囊里没有关系网数据（老版本胶囊）」。

**配置项**（`config.py` `DEFAULTS`）：`kg_enabled=True`、`kg_hops=2`(1~3)、`kg_build_enabled=True`、
`kg_build_interval_min=1440`(30~10080)、`kg_build_batch=30`(20~50)。

###3.15 关系网接口与界面（V1.8.1，表结构没变）

V1.8.0 的图谱只在聊天时被悄悄用，用户在界面上看不见。V1.8.1 补两个只读/触发接口和一个子页。

**`Api` 方法数 342 → 344**（`app_ui.py` 的 `for _av_name, _av_fn in (...)` 里，
排在 `("av_tts_custom_test", av_tts_custom_test),` 之后）：

| 方法 | 签名 | 返回 |
| --- | --- | --- |
| `kg_overview` | `(user_id="", limit=120)` | `{ok, user_id, entities, relations, networkx, edges[{subject,predicate,object,confidence}], hint}` |
| `kg_build_now` | `(user_id="", limit=40)` | `{ok, scanned, added, skipped, msg, stats:{entities,relations}, user_id}` |

实现细节：

- `_kg_uid()` 决定给谁织：`diary.bound_accounts()[0]` → `config.get("last_uid")` → `""`。
  三个都没有时 `kg_build_now` 直接回「还没绑定账号，不知道给谁织网。」，不瞎猜。
- `kg_overview` 把 `limit` 夹在 **1~500**；边来自 `kg._edges(mem, user_id)`（`ORDER BY confidence DESC, id DESC`，
  内部还有 `MAX_EDGES=4000` 的硬上限），`confidence` 统一 `round(float(x or 0), 2)`。
- `kg_build_now` 把 `limit` 夹在 **1~50**。为什么比后台线程（20~50）还宽？因为这是**用户手动点的**，
  给 1 条是「只想试一下」的合理需求；上界仍限 50，是「点下去别卡半分钟」。
- **同步执行**，不排后台队列。后台 `kg_build_loop()` 一天才跑一次（默认 1440 分钟），
  点按钮要立刻看到结果，否则用户以为没反应。
- 三种前置失败都说人话，不抛异常：
  `networkx` 没装 →「没装 networkx，建不了图（pip install networkx）」；
  模型没配 →「还没配好模型，现在拆不出关系。先去『设置 → 模型』配一个。」；
  没账号 → 见上。

**前端**（`desktop/src/index.html`）：

| 位置 | 内容 |
| --- | --- |
| `:1450` | `<button data-sub="graph" class="sub-tab w-full !justify-start text-left">关系网</button>` |
| `:1591-1618` | `#sub-graph`（`flex-1 hidden flex-col gap-3 min-h-0`），内含 `#graphStats` / `#btnKgBuild` / `#btnKgRefresh` / `#graphCount` / `#graphUser` / `#graphBody` |
| `:8163`、`:8282` | 子页数组 `['temp','perm','diary','graph']`，各加 `if (memSub === 'graph') renderGraph();` |
| `:8293` 起 | `kgEdgeRow` / `kgRender` / `loadGraphData` / `renderGraph` / `kgBuildNow` / `kgWire` |

- **缓存策略**：`KG_DATA` 存最近一次返回，切走再切回不重复请求（几千条关系时每次切页都拉一遍很浪费）；
  点「刷新」先 `KG_DATA = null` 再拉。
- **可信度配色**：`>=0.8` 用 `.chip.ok`、`>=0.5` 用 `.chip.warn`、其余 `.chip.err`。
- **`kgWire()` 用 `__kgBound` 防重绑**：`switchTab` 可能多次进 `av`/`memory` 面板，不加标记会叠加监听。
- **空态不写「暂无数据」**：用后端给的 `hint`（「还没有关系。聊几轮让它抽，或者点『立即补建』…」/
  「这个账号还没有永久记忆，图谱暂时没东西可织。」），让用户知道下一步干什么。


## 四、400 Bad Request 排查指南（重点）

### 4.1 本次的真实根因（三个，叠加）

**根因 A —— 推理模型吃光 token**

`qwen3.5-35b-a3b` 是**推理模型**：它先输出 `reasoning_content`（思考过程），再输出 `content`（正文）。
旧配置 `max_tokens=700`，实测思考用了 464 token，`finish_reason=length` 直接把预算耗尽 —— **正文一个字都没生成**。

```
finish=length  content_len=0  reasoning_len=464  elapsed=12.98s
```

表现：模型调用"成功"（HTTP 200），但 `content` 是空串 → 回复为空 → 聊天中断。

**根因 B —— `reasoning_effort` 参数不被支持**

分两种情况，都表现为 400：

- **B1：旧版 LM Studio 完全不认这个参数**
  ```json
  {"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}}
  ```
- **B2：新版认这个参数，但不接受 `auto` 这个值**（更常见，也更容易踩）
  ```json
  {"error": {"message": "Invalid 'reasoning_effort' value: 'auto'. Supported values: none, minimal, low, medium, high, xhigh.", "param": "reasoning_effort", "code": "invalid_value"}}
  ```
  我们的配置默认值恰好是 `"auto"`（想表达"自动决定"），但这个字符串被原样发给了服务端 —— 服务端只认 `none/minimal/low/medium/high/xhigh`，于是 400。

**根因 C（更隐蔽）—— `SYSTEM_PROMPT` 从未定义**

`ask_model()` 第一行 `sys_prompt = SYSTEM_PROMPT`，但整个文件里没有 `SYSTEM_PROMPT = ...`。
→ `NameError`，异常被上层 `try` 吞掉 → **收到消息但永远不回复**。这个才是"明明加载了但没回复"的直接原因。

### 4.1.1 顺带发现：LM Studio 的服务器默认不启动

界面上显示「连不上 LM Studio … WinError 10061 由于目标计算机积极拒绝」，但任务管理器里 LM Studio 明明开着。

原因是 **LM Studio 的 GUI 进程和本地 HTTP 服务器是两个东西**：GUI 开着不代表 1234 端口在监听。

```powershell
# 验证：有监听才说明服务器起来了
Get-NetTCPConnection -LocalPort 1234 -State Listen
```

命令行拉起（`lms.exe` 通常在 `C:\Users\<你>\.lmstudio\bin\lms.exe`）：

```powershell
lms.exe server start --port 1234
# Success! Server is now running on port 1234
```

界面上已经做了对应按钮：「模型设置」Tab 右上角 **「启动 LM Studio 服务」** —— 点击后自动按几个常见路径找 `lms.exe`、拉起服务、再用真实 `GET /v1/models` 验证，而不是只信命令行输出。

### 4.1.2 关闭思考能快 50 倍（重要经验）

实测同一个模型、同一句话，只改 `reasoning_effort`：

| 配置 | 耗时 | finish | content 字数 | reasoning 字数 |
|---|---|---|---|---|
| 不带 reasoning_effort | 34.2s | stop | 21 | 2078 |
| `reasoning_effort=none` | **0.7s** | stop | 14 | **0** |
| `reasoning_effort=low` | 27.3s | stop | 21 | 1992 |
| 不带 effort + max_tokens=4000 | 16.3s | stop | 19 | 1169 |

聊天场景不需要模型"深思熟虑"，关掉思考后 0.7 秒就能出话，且回复质量对闲聊完全够用。
所以配置默认 `reasoning_effort="auto"` 会被 `_resolve_effort()` **翻译成 `none` 再发出去**。

### 4.2 修复对照表

| 症状 | 根因 | 修复 |
|---|---|---|
| 200 但回复空 | 推理模型 token 不足 | `max_tokens` 提到 1200，识别到 `reasoning_only` 后自动 ×3 重试（默认提到 3000+） |
| 400 Bad Request | `reasoning_effort` 不支持 | 400 时若 body 提及该参数，加入 `_unsupported`，本次运行不再发送 |
| 400 Bad Request | `reasoning_effort=auto` 非法值 | `_resolve_effort()` 把 `auto`/空串翻译成 `none`；非法值也退回 `none` |
| 400 Bad Request | 连续相同 role | `normalize_messages()` 强制合并 |
| 400 Bad Request | 多个 system 消息 | 合并成一个 |
| 400 Bad Request | 首条是 assistant | 直接丢弃 |
| 400 Bad Request | 上下文超限 | 按 token 预算从新到旧截断 |
| 400 Bad Request | 模型名写错 | 界面「模型设置」→「刷新列表」重选 |
| 400 Bad Request | 多模态格式错 | `_merge_contents()` 保留 `image_url` 结构；失败时降级去图 |
| 连不上 1234 | LM Studio 服务器没启动 | 界面「启动 LM Studio 服务」按钮，或 `lms.exe server start --port 1234` |
| 回复很慢（30s+） | 模型在思考 | `reasoning_effort` 设 `auto`（→ none）或 `none` |
| 收到消息不回复 | `SYSTEM_PROMPT` NameError | 新增全局 + `refresh_system_prompt()` + 导入时初始化 |
| 收到消息不回复 | `log_message()` 参数不匹配 TypeError | 统一 4 参签名 `(scope, person, role, content)` |
| 收到消息不回复 | `mem.get_facts` AttributeError | 补齐 `memory.py` 缺失的 6 个方法 |

### 4.3 出问题时怎么查

**第一步：看 `agent.log`**

```powershell
Get-Content agent.log -Tail 60 -Encoding UTF8
```

搜这几类关键字：

| 关键字 | 含义 |
|---|---|
| `[模型] HTTP 400` | 后面紧跟 `response.text` 就是 LM Studio 的原话 |
| `[模型] 正文为空但思考有` | 推理模型 token 不够 |
| `[模型] 连不上` | LM Studio 没开 / 地址写错 |
| `[模型] 请求超时` | 模型太大跑不动 / 显存不够 |
| `[人设] 加载失败` | personas.json 坏了 |
| `[模型] 全部重试失败` | 四级补救全挂，已发兜底话术 |

**第二步：手工验模型**

```powershell
curl http://127.0.0.1:1234/v1/models
```

确认 `lm_model` 在返回的 id 列表里。

**第三步：手工发一条**

```powershell
curl http://127.0.0.1:1234/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"你的模型名\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":100}'
```

看返回的 `choices[0].message.content` 是不是空。如果空但 `reasoning_content` 有内容 → 确认是推理模型，去界面把 `reasoning_max_tokens` 调大。

**第四步：用界面自带的诊断**

「模型设置」→「测试模型能否出话」。它会告诉你：
- 能不能连上（连不上时会把 `WinError 10061` 翻译成人话，并提示点「启动 LM Studio 服务」）
- 是不是推理模型
- 支不支持 `reasoning_effort`，以及实际发出去的 `effort_sent` 是什么
- 实际出话内容

**第五步：确认 1234 端口在监听**

```powershell
Get-NetTCPConnection -LocalPort 1234 -State Listen
```

没有输出 → 服务器没起。别被"LM Studio 进程在跑"骗了，GUI 开着 ≠ 服务器在跑。

### 4.4 不用真模型也能复现（离线自检）

LM Studio 关掉、或者手头没模型时，可以用两个自带脚本验证模型层逻辑：

```powershell
# 1. 起一个假 LM Studio（模拟"认 effort 报 400"+"推理模型吃 token"）
python _mock_lm.py 1234

# 2. 另开一个窗口，跑 89 项单元自检
python _selftest_v3.py

# 3. 跑 67 项活人感专项自检（表情包/说说/补回/emoji）
python _selftest_v3_alive.py

# 4. 或者直接打真实调用链
python -c "import agent; print(agent.ask_model([], '在吗'))"
```

`_mock_lm.py` 的行为：

| 条件 | 返回 |
|---|---|
| payload 带 `reasoning_effort` | HTTP 400 + `Unrecognized request argument supplied` |
| `max_tokens < 2500` | `content=""`、`reasoning_content` 有值、`finish_reason=length` |
| 其他 | 正常 `content` |

跑通时日志应该是这个顺序（就是真实故障的复现与修复过程）：

```
[模型/探测] HTTP 400 —— response.text = {"error": {"message": "Unrecognized request argument supplied: reasoning_effort"...
[模型] LM Studio 不认 reasoning_effort，本次运行不再发送该参数
[模型/探测-无effort] 正文为空但思考有 300 字，finish=length（推理模型吃光了 max_tokens）
→ 最终回复 '在的在的，刚在摸鱼，咋啦？'
```

`_mock_lm.py` 带 `/v1/stats` 端点，可以看 `{"reasoning_only": 1, "ok": 1, "400": 1}`
确认每条补救路径都被走过。两个脚本都不参与打包，只在源码目录里。

---

## 五、主动关心与场景感知

### 5.1 事件提取

聊天结束后（异步，不阻塞回复），`extract_events()`：

1. **正则预筛**：文本里没有时间线索词（明天/下周/点/号/周X/月X…）→ 直接跳过，省一次模型调用
2. **调模型**：`EVENT_EXTRACT_PROMPT` 要求输出纯 JSON 数组
   ```json
   [{"desc": "考试", "time": "2026-10-23 09:00"}]
   ```
3. **解析**：`_extract_json_array()` 容忍代码块包裹和前后废话；`_parse_event_time()` 支持多种格式，纯日期自动补 `09:00`
4. **入库**：`mem.add_event()`，带 1 小时去重

### 5.2 定时问候

`care_loop` 线程每 60s 醒一次：

| 常量 | 默认值 | 含义 |
|---|---|---|
| `CARE_ENABLED` | `True` | 总开关 |
| `CARE_HOURS` | `(8, 23)` | 只在这个时间段发 |
| `GREETING_SLOTS` | `[(12,0), (18,30)]` | 中午 / 傍晚 |
| `GREETING_WINDOW_MIN` | `15` | 时间窗口，错过就算了 |
| `GREETING_IDLE_MIN` | `30` | 用户静默超过 30 分钟才发 |
| `EVENT_SILENCE_HOURS` | `2.0` | 查岗的用户静默门槛 |
| `CARE_MAX_TARGETS` | `6` | 每轮最多问候几个人 |

### 5.3 智能查岗

扫 `user_events` 里 `status='pending'` 且 `target_time <= now` 的：

- 用户最后一条消息距今 **> 2 小时** → 主动发问（"考得怎么样？"）
- 发完立刻 `mark_event_done(id)`，不会重复骚扰

`is_silent(session_key, hours)` **只看 `role='user'`** 的最后时间 —— 机器人自己说的话不算"用户活跃"。

---

## 五之二、活人感（v3 新增）

目标：让机器人不像"客服机器人"，而像一个真的会想事情、会主动说话的朋友。

### 5.4 私聊引用回复

每条私聊回复都带 `reply` 消息段，QQ 客户端里显示成"引用你的消息再回复"：

```python
# agent.py process_message()
quote_id = event.get("message_id")
send_private_human(uid, reply, reply_to=quote_id)
```

底层构造的 NapCat 消息段：

```json
[
  {"type": "reply", "data": {"id": "732405459"}},
  {"type": "text",  "data": {"text": "在的在的"}}
]
```

> **注意**：`reply` 段必须是数组第一项，否则 NapCat 不认。
> 群聊也支持，但只有被 @ 时才带引用（避免刷屏）。

开关：`config.json` → `private_quote_reply`（默认 `true`）。

### 5.5 私聊补回（不限时间）

**问题**：WS 掉线、程序没开、电脑休眠期间收到的私聊，重启后就永远错过了。

**做法**：`private_catchup_loop` 线程 + 启动时跑一次 `catch_up_private()`：

```
1. _get_recent_private_peers()   → get_recent_contact 拿到最近有往来的会话
2. get_friend_msg_history(uid)   → 拉每个会话的完整往返历史
3. _find_pending_private()       → 从后往前扫，找出"对方发的、我还没回的"
4. 逐条生成回复 + 引用发送 + 写入 messages
5. 游标推进到"该会话最后一条消息的时间"
```

**关键设计**：

| 点 | 说明 |
|---|---|
| 不设时间上限 | 昨天、上周的私聊都会补回（`private_catchup_first_hours` 只在**首次**没有游标时生效，默认回看 24 小时） |
| 游标推进到消息时间 | 不能用 `time.time()`，否则会吃掉补回期间刚进来的新消息 |
| 遇自己发的就停 | `_find_pending_private()` 从后往前扫，遇到 `self_id` 发的消息立即停 —— 保证只补"连续未回"的那一段 |
| 已回过的跳过 | 查 `mark_replied` 的 state 集合 |
| 每会话上限 | `private_catchup_max_msgs=6`，防止对方发了 50 条把你淹没 |
| 30 分钟兜底 | 即使 WS 正常也定期扫一次，防漏事件 |

开关：`config.json` → `private_catchup_enabled`（默认 `true`）。

### 5.6 自动发说说

**频率控制**：不是固定次数，而是**每天 0 点后随机抽一个配额**，再随机分配发布时间。

```python
def _moment_quota_today():
    # 跨天则重新抽配额
    if state["moment"]["date"] != today:
        quota = random.randint(MOMENT_MIN_PER_DAY, MOMENT_MAX_PER_DAY)  # 1~10
        state["moment"] = {"date": today, "count": 0, "quota": quota, "log": []}
```

`moment_loop` 每 60s 检查一次，四个条件全满足才尝试：

| 条件 | 说明 |
|---|---|
| `MOMENT_ENABLED` | 总开关 |
| 当前时间在 `MOMENT_HOURS`（8~23 点）内 | 半夜不发 |
| `count < quota` | 今日配额没用完 |
| 距上次 ≥ `MOMENT_CHECK_INTERVAL_MIN`（17 分钟） | 避免连发 |

**内容由模型自己决定**，不是模板填空：

```python
MOMENT_PROMPT = """你是{name}，一个普通的 QQ 用户，正在考虑要不要发一条 QQ 空间说说。
现在是 {now}。
今天聊天的零碎内容：
{context}

规则：
1. 如果觉得现在没什么值得发的，就只输出 SKIP，什么都别多说
2. 想发就写 1~3 句，像真人发空间那样，别写成心灵鸡汤
3. 可以吐槽、可以分享小确幸、可以自嘲，语气要符合你的人设
4. 不要提"聊天记录""用户"这类词
"""
```

`_moment_context()` 从 `mem.recent_sessions(20)` 捞 20 小时内的聊天素材喂进去。模型返回 `SKIP` 就不发 —— 所以实际条数可能少于配额，更自然。

**发布**：`POST /send_qzone_msg`，参数 `{"content": "..."}`，返回 `data.tid`。

> **NapCat 版本差异**：`send_qzone_msg` 在较新版本才有；老版本会返回 `不支持的Api`。
> 该接口**只支持发，不支持读**（`get_qzone_msg_list` 在当前版本不存在）。
> 网络故障时返回 `getaddrinfo ENOTFOUND user.qzone.qq.com` —— 这是 NapCat 进程自身的 DNS 问题，不是代码问题。

### 5.7 表情包与 emoji

见 [2.5 `stickers.py`](#25-stickerspy--表情包与-emoji v3-新增)。

发送时机：

```python
# process_message() 末尾
maybe_send_sticker(key, reply, reply_to=quote_id)
```

内部逻辑：

```python
emo = detect_emotions(reply_text)          # 关键词匹配
if not emo: return False                   # 没情绪不发
if random.random() > STICKER_CHANCE: return False   # 45% 概率
pick = pick_sticker(emotions=emo)          # 标签命中优先
send_sticker_segments(...)                 # base64 或 file://
```

**表情包和文字是两条消息**：先发文字，再发图。因为 QQ 的图片消息段和文本消息段混在一条里时，部分客户端排版会乱。

### 5.8 活人感相关常量速查

| 常量 | 默认值 | 作用 |
|---|---|---|
| `PRIVATE_CATCHUP_ENABLED` | `True` | 私聊补回开关 |
| `PRIVATE_CATCHUP_MAX_MSGS` | `6` | 每会话最多补几条 |
| `PRIVATE_CATCHUP_FIRST_HOURS` | `24` | 首次回看多久 |
| `PRIVATE_CATCHUP_SCAN_FRIENDS` | `8` | 最多扫几个最近会话 |
| `PRIVATE_QUOTE_REPLY` | `True` | 引用回复开关 |
| `PRIVATE_CATCHUP_RECHECK_MIN` | `30` | 兜底扫描间隔（分钟） |
| `MOMENT_ENABLED` | `True` | 说说开关 |
| `MOMENT_MIN_PER_DAY` / `MOMENT_MAX_PER_DAY` | `1` / `10` | 每日配额随机区间 |
| `MOMENT_HOURS` | `[8, 23]` | 可说说的时段 |
| `MOMENT_CHECK_INTERVAL_MIN` | `17` | 两次检查最小间隔 |
| `STICKER_ENABLED` | `True` | 表情包 + emoji 开关 |
| `STICKER_CHANCE` | `0.45` | 情绪匹配后发表情包的概率 |
| `EXTRACT_BUDGET` | `512` | 结构化抽取的 token 预算 |
| `SUMMARY_BUDGET` | `400` | 滚动摘要的 token 预算 |

> **`EXTRACT_BUDGET` / `SUMMARY_BUDGET` 是 v3 的关键修复**：
> 之前这两个任务用 `effort="omit"` + 小预算，推理模型把 token 全花在 `reasoning_content` 上，
> 日志里每条都是 `[模型/记忆提取] 正文为空但思考有 4xx 字，finish=length` —— **长期记忆其实一直没写进去**。
> 现在改成 `effort="none"` + 固定小预算，并在 `reasoning_only` 时用 `budget*4` + `effort="omit"` 重试。

---

## 五之三、v4 新增行为

### 5.9 群里 @ 时的上下文拼接（v4 修复的重要漏洞）

**用户报的 bug**：

```
R: 你吃饭了吗
R: @我部署的ai聊天
```

机器人只读到 `@我部署的ai聊天` 这条（正文其实是空的），完全不知道上一句是「你吃饭了吗」。

**根因（三处叠加）**：

1. `handle_event()` 里群里 `should_reply = at_me`，非 @ 的消息直接 `return` ——
   **非 @ 的群消息根本没进 `mem.add_message()`**，所以模型的历史里没有那句「你吃饭了吗」。
   写入只发生在 `process_message()` 里，而 `process_message()` 只在要回复时才被调用。
2. 即使消息落了库，@ 那条的 `raw_text` 被置成 `（只是@了你一下，没说话）`，
   模型看到的就是一个空问题。
3. **最隐蔽的一处**：`extract_text()` 会把 `at` 段拼成 `@2145216414` 当正文返回。
   于是「@我但没说话」的 `raw_text` 不是空串，而是 12 个字符的 `@2145216414` ——
   `need_prelude` 的 `not raw_text` 判定永远不成立，上面两处修复全部失效。
   真实日志里看到的是 `[group] R: @2145216414`，模型收到的 prompt 就是这一串。

**修复 1：`extract_text()` 丢掉 @ 自己的段**

```python
elif t == "at":
    qq = str(d.get("qq") or "")
    if self_id is not None and qq == str(self_id):
        continue                      # ← @ 自己不算正文
    parts.append(f"@{qq}")
```

字符串形式（`[CQ:at,qq=...]`）同样只去掉自己的那一个：

```python
if self_id is not None:
    text = text.replace(f"[CQ:at,qq={self_id}]", "")
```

> `@别人` 必须保留 —— 那是对着第三个人说话，是上下文的一部分（场景 4）。

**修复 2：`GroupContext` 内存环形缓冲**

```python
class GroupContext:
    """每个群一个 deque，最多存 GROUP_CONTEXT_LOOKBACK=12 条最近消息"""
    def add(group_id, user_id, name, text, msg_id=None, is_at=False):
        if not text and not is_at:      # ← 关键
            return
        self.data[group_id].append({...})

    def pending_before_at(group_id, at_msg_id, limit=None):
        """从 @ 那条往前扫，遇上一个 @ 就停，最多取 GROUP_MERGE_PREV=3 条，返回时间正序"""
```

> **`if not text and not is_at: return` 为什么重要**：
> 「只有 @ 段、正文为空」的消息也必须记进去，否则 `pending_before_at` 会**穿越**这个 @
> 把更早的老消息当成"刚才的对话"，拼出一段完全不相关的上下文。

**拼接时机**（`process_message()`）：

```python
need_prelude = (not raw_text) or len(raw_text) <= 6 or raw_text.startswith("（只是@")
if prev and need_prelude:
    group_prelude = "【这个群刚才的对话】\n" + "\n".join(f"{m['name']}：{m['text']}" for m in prev) \
                    + "\n【现在】对方@了你"
elif prev and len(raw_text) <= 20:
    group_prelude = "【刚才群里在说】\n" + ... + "\n"     # 短问题也带两句前文
```

拼进 prompt 最前面，日志会打 `[群上下文] 拼接了 @ 之前的 N 条消息`。

> **v4 补丁**：`elif` 分支原来还要求 `len(prev) >= 2`，导致「@别人 你说 @我」这种
> 只有一条前文的场景被跳过（自检场景 4 抓到的）。既然这条消息 @ 了我，`prev` 非空
> 就该带上 —— 只看 `@999999 你说` 这一句，模型根本不知道在说什么。

**五种真实场景的最终行为**（`_selftest_v4.py` 第 12 组回归）：

| 场景 | 前文 | @ 的那条 | 模型实际收到 |
|---|---|---|---|
| 1 | `你吃饭了吗` | `@我`（空正文） | `【这个群刚才的对话】R：你吃饭了吗【现在】对方@了你` + `R：（只是@了你一下，没说话）` |
| 2 | `你吃饭了吗` / `我今天加班` / `累死了` | `@我`（空正文） | 三条按时间正序全带上 |
| 3 | `你吃饭了吗` | `@我 你觉得呢` | 前情一句 + `R：你觉得呢` |
| 4 | `你吃饭了吗` | `@999999 你说 @我` | 前情一句 + `R：@999999 你说`（保留 @别人） |
| 5 | 无 | `@我` | 不编造上下文，只有 `R：（只是@了你一下，没说话）` |
| 6 | `很早以前的话` / `@我` / `刚才这句` | `@我` | 只带 `刚才这句`，**不穿越**上一个 @ |

**为什么用内存缓冲而不是数据库**：群里消息量大，全写库会撑爆 `messages` 表；
上下文只需要"最近十几条"，进程内 `deque(maxlen=12)` 最省事，重启后重新积累也无所谓。

### 5.10 「保存群聊记忆」开关

`config.save_group_memory`（默认 `False`）：

| 值 | 行为 |
|---|---|
| `False`（默认） | 群里只有 @ 我的消息才写 `messages` 表；其余只在 `GroupContext` 内存里，用于拼接上下文 |
| `True` | 群里所有消息都写 `messages`（格式 `发送者：内容`），历史更完整，但库会长得快 |

代码位置（`handle_event()` 群聊分支）：

```python
if raw_text or at_me:
    group_ctx.add(...)                                  # 内存缓冲，永远都记
    if SAVE_GROUP_MEMORY and MEMORY_ENABLED and not at_me:
        mem.add_message(key, "user", f"{sender_name}：{raw_text}")   # 只有开了才落库
should_reply = at_me and group_allowed(group_id)
```

### 5.11 群聊绑定（白名单 / 黑名单）

`config.group_bind_mode` + `group_whitelist` / `group_blacklist`，各最多 20 个群。

| 模式 | 判定逻辑 |
|---|---|
| `all`（默认） | 所有群 @ 都回 |
| `whitelist` | 只有 `group_whitelist` 里的群才回 |
| `blacklist` | 除了 `group_blacklist` 里的群都回 |

判定入口是 `diary.group_allowed(group_id)`，在 `handle_event()` 里：

```python
should_reply = at_me and group_allowed(group_id)
```

> **被屏蔽的群不回复，但仍然进 `GroupContext`** —— 万一以后解除屏蔽，上下文是连续的。

### 5.12 永久记忆与临时记忆的分层

| | 临时上下文 | 永久记忆 |
|---|---|---|
| 表 | `messages` / `chat_log` / `summaries` | `perm_memory` |
| 谁都有 | 是 | **只有绑定过的 QQ 号** |
| 会不会丢 | 滚动截断（`MAX_HISTORY_TURNS` + 摘要压缩） | 永久 |
| 怎么注入 | `get_recent_messages(turns=8)` + `get_latest_summary()` | `build_perm_block()` |
| 怎么产生 | 每条消息自动写 | 绑定后异步提炼（模型 or 规则兜底） |
| 界面在哪看 | 记忆查看 → 临时上下文 | 记忆查看 → 永久记忆 |

**为什么必须"绑定"才写永久记忆**：永久记忆不截断、不清空，如果对所有人都写，
库会无限膨胀且全是无关内容。绑定 = 用户明确说"这个人的事我要你一直记着"。

### 5.13 日记

`diary_loop` 每 20 分钟检查一次，满足全部条件才写：

| 条件 | 说明 |
|---|---|
| `config.diary_enabled` | 设置里开启日记 |
| 当前小时 >= `diary_hour`（默认 22） | 到点才写 |
| 当天还没写过（`state["diary_wrote"]["day"] != today`） | 一天一篇 |
| 当天消息数 >= `diary_min_msgs`（默认 6） | 聊得太少不硬编 |

写入流程：

1. 取该账号当天的 `messages`
2. 调模型，`DIARY_PROMPT` 要求第一人称、120–260 字、末尾 `[[mood:开心]]`
3. `_parse_diary_output()` 拆出 `(title, content, mood)`
4. `add_diary()` 写库（同账号同天覆盖）
5. 更新 `state["diary_wrote"]`

界面上「记忆查看 → 日记」左列点一条，右列看全文。

---

## 五之四、v5 新增行为

### 5.14 永久记忆「新事实顶掉旧事实」（本次最核心的修复）

**用户报的问题**：

```
不对，我和她在一起的日子是 24 年 11 月 27 日
```

机器人回"记住了"，但「永久记忆」里旧那条（2024-11-27）还在，新的没替换 → 两条互相矛盾。

**为什么 v4 不行**：`add_perm()` 只按 `user_id` + `content` **完全相同**去重。内容只要差一个字就是两条新记忆，永远不会互相否定。

**v5 的两条路径**（优先级从高到低）：

1. **模型显式指认（首选）** —— `diary.py` 的 `PERM_PROMPT` 会把该用户已有的记忆带上编号一起给模型：

   ```
   已有的长期记忆（如果你要更新其中一条，把它的编号填进 replaces）：
   #1 (relation) 与名为梅梅的人是恋爱关系，纪念日是2024年11月27日
   #2 (preference) 喜欢打游戏
   ```

   要求模型输出：

   ```json
   [{"category":"relation",
     "content":"和梅梅在一起的纪念日是2023年11月27日",
     "importance":5,
     "replaces":1}]
   ```

   `replaces` 直接变成 `add_perm(supersede_ids=[1])` —— **精确、不误伤**。

2. **启发式兜底（模型没给 replaces 时）** —— `_perm_conflict_ids()` 四条判据，命中任一即视为"说同一件事"：

   | # | 判据 | 说明 |
   |---|---|---|
   | ① | `norm_key` 完全相同 | 归一化后一字不差 |
   | ② | 一方是另一方子串，且长度 >= 较短方一半 | "养了橘猫" vs "养了一只叫豆豆的橘猫" |
   | ③ | 2-gram Jaccard >= 0.50 | 词序不同但用词高度重合 |
   | ④ | **双方都有 4 位以上数字** 且 2-gram Jaccard >= 0.22 | 日期/纪念日类 ← 关键的一条 |

   第④条是专门为"纪念日改了"设计的：`"纪念日是2024年11月27日"` 和 `"纪念日是2023年11月27日"` 的 2-gram Jaccard 只有 0.24 —— 按③的 0.50 根本命中不了。但两条**都含 4 位数字**，加上 0.22 阈值就命中了。

**写入方向别搞反**：

```
旧条目 #1:  status='superseded', supersedes=0      ← 它没有顶掉谁
新条目 #2:  status='active',     supersedes=1      ← 它顶掉了 #1
```

**返回签名变了**：`add_perm()` 从 `(id, is_new)` 变成 `(id, is_new, superseded_ids)` —— 所有调用方都要三个变量接（否则 `ValueError: too many values to unpack`）。

**注入时只看 active**：`build_perm_block()` 走 `mem.get_perm()`，默认 `status='active'`，所以矛盾的那条不会进 prompt。

### 5.15 记忆三档策略

`config.memory_mode`：

| 值 | 行为 | 什么时候选 |
|---|---|---|
| `auto` | 每轮聊天后让模型判断"这轮有没有值得长期记住的新信息"，有才写 | **默认，推荐**。省 token，也不会把废话记进去 |
| `always` | 每条消息都尝试提炼；模型说没有就退回正则规则抽（`_rule_extract`） | 想尽量多记，不怕噪音 |
| `temp` | **完全不写永久记忆**，只保留 `messages` 里的临时上下文 | 只想当聊天机器人用，不要长期记忆 |

代码位置：`agent.py` 的 `_perm_extract_job()` 开头就判 `MEMORY_MODE == "temp"` 直接 return。

**`fallback_rule` 的含义**：只有 `always` 模式才 `fallback_rule=True`。`auto` 模式下模型说"没有可记的"就真的不记 —— 不再偷偷退回正则硬抽（v4 的 bug：模型说不记，代码还是用正则塞了一条进去）。

### 5.16 云端 API 接入与用量保护

**切换流程**（界面「模型设置 → 模型来源」）：

```
选提供方 → 自动回填 base + 推荐模型 → 填 API Key → 保存并切换
   ↓
providers.set_provider() 写 config.json
   ↓
POST http://127.0.0.1:5700/reload_model
   ↓
agent.apply_provider() 重读配置、清空 _reasoning_model/_unsupported
   ↓
下一次 ask_model() 就走新端点（进程不用重启）
```

**用量保护三层**：

| 层 | 位置 | 行为 |
|---|---|---|
| 调用前 | `_post_chat()` 里 `usage_mod.precheck()` | 今日云端超 `usage_daily_limit` → `reason="quota"`，**请求根本不发** |
| 调用中 | `_record_usage()` → `warn_if_needed()` | 到 `usage_warn_percent`（默认 80%）打一次 WARN |
| 界面 | 进度条 + 「保护状态」 | >= 60% 黄，>= 100% 红并显示「超限即拦截」 |

**已知边界**：

- 只统计**云端**是否超限；本地不计费，永远放行。
- token 数取自 API 返回的 `usage` 字段。**有些端点（尤其本地 llama.cpp）不返回 usage**，这时 `_record_usage` 按 `len(prompt)//4` + `len(completion)//4` 估算，只能当参考值。
- `usage_daily_limit=0` 或 `usage_guard_enabled=false` → 不限制。

### 5.17 NapCat 集成（自动检测 / 一键安装）

**解决了什么**：以前必须自己装 NapCat、找 `napiLoader.bat`、点登录、确认 3000/3001 端口，用户第一步就卡住。

**启动时的三档自检**（`config.napcat_check_mode`）：

| 值 | 行为 |
|---|---|
| `install` | **只在第一次启动时检测**（靠 `config.napcat_checked_once` 记），之后不再打扰 |
| `always` | 每次启动都检测 |
| `never` | 完全不检测（自己手动起） |

检测结果分三种，日志里能直接看到：

```
[NapCat] 正在运行，登录账号：大肥鱼DS (2145216414)   ← 正常
[NapCat] 已安装但没运行：D:\napcat-fw               ← 能给"启动 NapCat"按钮用
[NapCat] 没找到 NapCat                              ← 界面提示要不要装集成版
```

**界面那张卡**（设置 → QQ 接入）显示：状态点（绿/黄/红）、登录账号、安装目录、启动器路径、HTTP 3000 / WS 3001 是否在听。四个按钮：重新检测 / 启动 NapCat / 打开目录 / 检查最新版本 → 下载并安装。

**下载安装的安全边界**：`download_and_install()` 解压前会校验每个 zip 成员的路径，拒绝 `..`、绝对路径、盘符 —— 防止 zip 逃逸写到程序目录外面。装到 `DATA_DIR/napcat`，不动系统任何地方。

### 5.18 公告弹窗 + 通知中心

**v4 的公告只是一条横幅**，用户不看就过去了。v5 改成：

| 行为 | 实现 | 状态键（都在 `agent_state.json`） |
|---|---|---|
| 新公告**启动弹一次窗** | `popup_pending()` → 前端 `#popupMask` | `notice_popup_shown`（弹过就不再弹，`notice_popup=false` 可关） |
| 右上角**铃铛 + 未读角标** | `refreshBell()` / `center()` | `notice_read`（读过的 id 列表） |
| 「不再提示」 | `dismiss(notice_id)` | `notice_dismissed` |
| 一键清空所有状态（调试用） | `restore_all()` | 三份**全部**清掉 |

**坑**：`restore_all()` 一开始只清了 `notice_dismissed`，`notice_read` 没清 → 未读数还是 0，用户以为按钮没生效。现在三份全清。

**公告还是写在 `notice.py` 顶部的 `NOTICES` 列表里**，不进数据库、不进界面 —— 开发者功能，改完重新打包即生效。界面**没有**任何"发布公告"的入口（这是用户明确要求的）。

### 5.19 日记自由编辑

| 操作 | API | 说明 |
|---|---|---|
| 改现有那篇 | `save_diary(user_id, day, content, title, mood)` | 同账号同天覆盖 |
| 重写（让模型重生成） | `regenerate_diary(user_id, day)` | 覆盖当天那篇 |
| 删掉 | `delete_diary(diary_id)` | |
| 立即写一篇（不等 22 点） | `write_diary_now(user_id)` | 调 `diary.write_diary_for(force=True)` |

界面上「记忆查看 → 日记」有「编辑 / 重写 / 保存 / 取消」四个按钮，编辑时正文变 `textarea`。

### 5.20 记忆导出 / 导入 / 重编译

**导出的 JSON 结构**（`memory.export_memory()`）：

```json
{
  "format": "qq-agent-memory",
  "version": 2,
  "exported_at": "2026-10-22 13:40:00",
  "perm":   [{"user_id","category","content","importance","source"}],
  "diary":  [{"user_id","day","title","content","mood"}],
  "events": [{"user_id","event_desc","target_time","status"}]
}
```

**临时上下文（`messages`）故意不导出** —— 那是随时可重建的，导出会让备份文件大十倍。

**导入两种模式**：

| mode | 行为 |
|---|---|
| `merge`（默认） | 追加；冲突的旧条目标 `superseded`，新的生效 |
| `replace` | 先 `clear_perm(user_id)` 再整批写入 —— 就是"直接载入成永久记忆" |

导入前校验 `data["format"] == "qq-agent-memory"`，不认识的格式直接拒绝。

**重编译**（`mem_rebuild.py`）解决三种历史问题：

| 问题 | `rebuild` 的哪一步 |
|---|---|
| 旧行 `norm_key` 是 NULL → 冲突检测失效 | `fill_norm=True` 补算 |
| 同一件事被记了多遍（内容略有差异） | `dedupe=True` 只留最旧那条 active，其余标 superseded |
| v4 之前的 `memories` 表数据没进 `perm_memory` | `migrate_legacy=True` 搬过去（只搬 `scope=private_<QQ号>` 的） |

**必须先备份**：`backup_db()` 用 SQLite backup API 拷一份 `memory_backup/memory_before_rebuild_<时间戳>.db`，点按钮时先弹确认框说明会改什么。

### 5.21 卸载时保留数据

`config.keep_data_on_uninstall`（默认 `true`）：

- **保留**（true）：卸载只删程序文件（exe / `_internal/` / `.py` / `.html`），**保留** `memory.db` / `config.json` / `personas.json` / `stickers/` / `themes/` / `icons/` / `memory_backup/`。
- **不保留**（false）：连数据一起清。

界面「设置 → 卸载与数据保留」有开关，下面那行文字会实时说明"保留会留下哪些文件"。这个开关**不会**真的删任何东西 —— 它只决定卸载脚本（`build.py` 生成的说明）里那一句提示怎么写。真正的删除仍然由用户自己做（`build.bat` 不会删数据目录）。

---

## 五之五、v6 新增行为

### 5.22 主动关心计划（场景感知，v6 最核心的新功能）

**用户的原话**：「我一会儿要下课了，模型判断你需要关心，会在决策后，在后台列表设定一个主动回复你的时间，到时间内就会主动关心你，这个列表可选择模型主动决策，或手动编辑，让用户选择」。

**和 v3「智能查岗」的区别**：

| | v3 智能查岗（`user_events`） | v6 主动关心计划（`care_plans`） |
|---|---|---|
| 触发时机 | 事件 `target_time` **过去之后** | 用户说的**未来某个时点**（下课/考完/下班） |
| 语义 | 「考得怎么样？」（事后追问） | 「快下课了吧？」（事前/到点关心） |
| 谁创建 | 事件提取顺带 | 模型判断 + 用户手动 |
| 静默要求 | 超过 2 小时没说话 | 默认只要 8 分钟内没说话就发 |

**流水线**

```
用户说「我一会儿要下课了，好累」
        ↓ process_message → extract_events()（模型已有一份完整对话）
模型返回 JSON 数组，每项多了三个字段：
        { "desc":"下课", "time":"2026-10-22 15:40",
          "care": true, "care_min": 15, "care_topic": "今天上了什么课" }
        ↓
mem.add_plan(user_id, session_key,
             plan_at = now + care_min*60,
             reason  = desc,
             topic   = care_topic,
             source  = "model")
        ↓ care_plan_loop() 每 60 秒扫一次
到点了 → 生成一句话 → send_to_session() → mark_plan_done()
```

**`care_plan_loop()` 的五个闸门**（任一不过就跳过，不报错）：

| 闸门 | 配置 | 说明 |
|---|---|---|
| 时段 | `care_plan_hours=[8,23]` | 半夜不打扰 |
| 每日上限 | `care_plan_max_per_day=6` | `mem.count_plans_today()` |
| 最小间隔 | `care_plan_min_gap_min=25` | `mem.last_plan_ts()` 之后不足 25 分钟就等 |
| 对方刚说过话 | `care_plan_respect_silence=True` | 8 分钟内对方发过消息 → 跳过（人家正在聊，不用"关心"） |
| 模型静默 | `allow_fallback=False` | 模型不可用时**不发**，也不标 done（下次再试） |

**谁来定（`care_plan_source`）**：

- `model`（默认）：只有模型在事件提取时给了 `care:true` 才排计划；
- `manual`：模型不排，只能界面「活人感 → 主动关心计划 → 加一条」手动加；
- `both`：模型排 + 用户也能加。

**界面**：`活人感` Tab 左下有「主动关心计划（N 条待发 / 共 M）」，每条卡片显示 `#id`、时间、`reason`、`来源`，按钮「删除」「取消」。加一条支持三种时间写法（`_parse_plan_time`）：`+30m`（30 分钟后）、`2026-10-22 15:40`、`15:40`（已过则自动滚到明天）。

**数据库**：`care_plans` 表（见 3.9）。

### 5.23 说说：让模型自己决定发不发 + 配图

**用户的原话**：「动态可以让模型支持主动决策是否发还是不发，原来的保留在选项中」「让她可以发布图片（可以让用户预制放入图片，或者发表情包，如果模型支持生图可以发自己生图的照片）」。

**发不发（`moment_decide`）**

| 值 | 行为 |
|---|---|
| `model`（默认，推荐） | 到点问模型「现在要不要发一条？」，模型回 `SKIP` 就不发 —— 所以**一天可能一条都没有** |
| `timer` | 到点就发（v3 的老行为），prompt 里追加「这次不用判断要不要发，直接写一条」 |

`SKIP` 判定只在 `model` 模式下生效，实现上是在 `moment_loop()` 里：

```python
if MOMENT_DECIDE == "model" and re.search(r"\bSKIP\b", text, re.I):
    log("[说说] 模型判断现在不适合发，跳过")
    return
```

**配图（`moment_image_enabled` + `moment_image_source`）**

| 来源 | 说明 | 目录 |
|---|---|---|
| `preset` | 用户自己放进去的图 | `moment_images/` |
| `sticker` | 从表情包库里随机抽 | `stickers/` |
| `generated` | 调生图模型现画一张（需要 `imagegen_enabled`） | `generated/` |
| `mix`（默认） | 上面三种混着挑，哪个有就用哪个 | — |

`_moment_images(content)` 返回 `image_uri` 列表（`qzone_mod.to_image_uri()` 转成 `base64://` 或 `file:///`），数量上限 `moment_image_max`（默认 1，可 0~9）。

发完在 `agent_state.json` 的 `moment` 里记 `tid` / `last_text` / 图片张数，界面「最近发过的」就是读这里。

### 5.24 说说评论回复

**用户的原话**：「我在动态评论：你在干啥啊，模型看到会主动回复相应话题…可以选择开启或关闭」。

**关键限制（必须先说清）**：实测这台机器的 NapCat **读不到说说评论**（`get_qzone_msg_list` / `get_qzone_msg` 都是 `不支持的Api`）。所以 v6 的评论回复有两条路：

```
moment_comment_loop()  每 MOMENT_COMMENT_POLL_MIN(默认5) 分钟
        ↓
qzone_mod.read_comments()
        ├─ 有接口 → 直接拿评论列表
        └─ 没接口 → drain_inbox()  读 qzone_inbox/ 目录里用户手打的评论
        ↓
mem.add_comment(...)  去重入库（同内容不重复）
        ↓
mem.get_pending_comments(5)
        ↓
_comment_reply_text(moment_text, commenter_name, content)  调模型生成回复
        ↓
_deliver_comment_reply(c, reply)
        ├─ moment_comment_deliver="comment" → 先试评论区（多数版本会失败）
        ├─ 失败 / 配置为 "private"    → 私聊发给评论人
        ↓
mem.mark_comment(id, COMMENT_REPLIED, reply)
```

**`qzone_inbox/` 是什么**：一个目录，用户把评论内容写进 `.txt`（一行一条，或 `昵称: 内容`），程序扫一遍就当评论处理。它让「读不到评论」的 NapCat 版本也能用这个功能 —— 从手机看一眼评论，复制粘贴进来即可。

界面「活人感 → 说说评论」有「收件箱」「收取」两个按钮，每条待回评论有「立即回复」「删除」。

### 5.25 生图表情包

**用户的原话**：「聊天表情包，让支持生图的模型可以发自己生成的图片发送，让用户选择是否可开启」。

两个开关叠加才生效：`imagegen_enabled`（能生图）+ `sticker_gen_enabled`（聊天里允许发）。

`maybe_send_sticker()` 的逻辑变成：

```python
if STICKER_GEN_ENABLED and IMAGEGEN_ENABLED and random() < STICKER_GEN_CHANCE:
    gen_sticker_async(session_key, emotions, reply_text)   # 起线程，不阻塞
    return True
# 否则走原来的预置表情包库 + STICKER_CHANCE
```

**为什么必须开线程**：生图一次 10~60 秒，同步调会把回复卡在那儿。`gen_sticker_async()` 是 daemon 线程：回复先发出去，图生成完再补一条 `send_image_file()`。生成失败只记日志，不影响聊天。

### 5.26 背景自定义

`backgrounds/` 目录放图 → 界面「设置 → 背景与语言」下拉里就出现 → 应用。

| 配置 | 说明 |
|---|---|
| `bg_file` | 选中的文件名（空 = 不用背景） |
| `bg_dim` | 压暗程度 0~90（默认 35），保证文字还能看清 |
| `bg_blur` | 模糊 0~40（默认 0） |
| `bg_fit` | `cover`（铺满裁切）/ `contain`（完整显示）/ `repeat`（平铺） |

**实现方式**：`index.html` 里给 `<body>` 加 `has-bg` 类 + 一个固定定位的 `#bgLayer` 元素，图以内联 data URL 注入（原因同表情包缩略图：WebView2 默认禁止 `file://` 页面加载其它 `file://` 资源）。`--bg` 变量仍然驱动所有面板配色，所以背景和主题可以叠加。

**代价提示**：大图内联会让 `index.html` 的 DOM 变重，建议用 ≤1MB 的图。

### 5.27 多语言

见 2.4.13。这里只补一个**行为边界**：v6 的多语言只覆盖界面上的**固定文案**（按钮、标签、提示）。**模型说的话不跟着语言变** —— 那由人设 `personas.json` 决定。想让它说英文，改人设，不是改 `langs/en-US.json`。

### 5.28 记忆多格式导入导出

见 2.4.12。界面上是两个按钮：

- 「导出记忆」（`#btnExportMem`）：选格式 → 弹保存对话框（默认 `memory_backup/`）→ 写文件；
- 「载入记忆」（`#btnImportMem`）：选文件 → 自动识别格式（识别不出再让用户手选）→ 合并进库（`mode="merge"`，`replace_conflict=True`）。

**临时对话（`messages` 表）默认不导出** —— 它是聊天上下文，不是"记忆"。导出的只有 `perm`（永久记忆）+ `diary`（日记）+ `events`（待办）。

### 5.29 修复：所有「打开目录」点了没反应

**用户的原话**：「所有的打开目录都打不开，几乎有这个功能的都打不开，马上修」。

**排查过程**（值得记住，因为结论反直觉）：

1. 探针直接调 12 个 `open_*` API → 全部返回 `{"ok": true, "msg": "已打开…"}`，**Python 侧没问题**；
2. 探针加载真实 `index.html`，`evaluate_js` 点按钮读 `#toast` → 六个按钮 toast 全是「已打开…」，**桥接也没问题**；
3. `os.startfile(r'D:\qq-agent')` 单独跑 → `startfile OK`，explorer 进程 4 个，**系统也没问题**。

**真根因（两个叠加）**：

1. pywebview 窗口占着前台焦点，`os.startfile` 拉起的资源管理器窗口**落在本程序后面**；
2. Windows 11 默认「在同一窗口打开文件夹」，新目录被塞进一个本来就在背后（甚至最小化）的资源管理器窗口 → 用户看到的完全是"点了没反应"。

**修法**：`app_ui.py` 新增三个函数，所有 12 个 `open_*` 统一走 `shell_open(path)`：

| 函数 | 作用 |
|---|---|
| `_explorer_windows()` | `EnumWindows` 枚举可见窗口，返回 `{hwnd: 标题}` |
| `_focus_window(hwnd, tries=3)` | `ShowWindow(SW_RESTORE)` + `BringWindowToTop` + 模拟 ALT 键绕过前台锁 + 循环 `SetForegroundWindow` |
| `_focus_explorer_for(path, before, wait_sec=3.0)` | 轮询最多 3 秒：先找「`before` 之后新出现 + 标题含目录名」的窗口，找不到再退回找「任何标题含目录名」的（Win11 复用老窗口的情况） |

`shell_open()` 的降级链：`subprocess.Popen(["explorer.exe", path])` → 失败退 `os.startfile` → 再失败退 `cmd /c start ""`。返回 `{ok, msg, path, focused, title}`，msg 里带中文标签（「已打开表情包目录：D:\...」）。

**实测**：12 个 `open_*` 全调 → 12/12 成功且 12/12 抢到前台，每个 0.68~1.10 秒。

### 5.30 修复：云端 API 填了 Key 却提示「LM Studio 未启用」

**用户的原话**：「云端API接入不可以用，我把自己的API填进入去显示的是LM studio未启用，马上修复」。

**三个根因**：

1. `list_models()` 里写死 `cfg["lm_base"]` —— 选了云端照样去敲 `http://127.0.0.1:1234/v1/models`，报「连不上 LM Studio 服务」。**这就是用户看到那句话的直接来源。**
2. `set_model()` 写死写 `lm_model`、`set_model_base()` 写死写 `lm_base` —— 云端下保存模型/地址会**污染本地配置**。
3. `test_model()` 直接调 `ag.probe_model()`，而 `agent` 的 `LM_BASE`/`LM_MODEL` 是**模块导入时**算出来的 —— GUI 进程里切过提供方后仍是旧值。

**修法**：

- 新增 `_provider_ctx()`：`providers.resolve()` 的薄包装，返回 `{res, provider, name, base, model, api_key, is_cloud, key_base, key_model}`。`key_base`/`key_model` 会自动选 `cloud_base`/`lm_base`。
- `list_models()` / `set_model()` / `set_model_base()` / `test_model()` / `get_status()` 全部改用它。
- `list_models(base="", api_key="", provider="")` 三个可选参数：界面**刚填完还没保存**时也能直接拿输入框里的值试连。
- 云端返回 401 单独给中文指引（「API Key 不对或没填」+ `auth_failed=True`）；没配 Key 给 `needs_setup=True`。
- `test_model()` 调用前先 `ag.apply_provider(verbose=False)` 刷新模块级变量。
- 界面：云端时卡片标题改「当前模型（{提供方}）」、说明文案换掉、「启动 LM Studio 服务」按钮隐藏。

**验证**：`_mock_cloud.py`（假云端，401/正常两态 + `/v1/stats` 计数）→ 38 项 API 契约 + 10 项 agent 侧全通过；截图确认 chip 显示 `deepseek-chat`、拉取结果「当前地址可用，发现 2 个模型」。

---

## 五之六、v7 新增行为

### 5.31 模型来源拆成两块 + 优先级链（v7 最核心）

用户原话：「直接分成两个模块，一个本地部署模块，旁边一个开关，然后下面一个模块是API模块，旁边有一个开关……可以同时开启，增加一个优先级列表，可以拖动，以最前面的那个优先，第一个用不了就顺延到第二个，以此类推，最多10个」。

界面结构（`index.html` 的 `#tab-model`）：

| 卡片 | 内容 |
|---|---|
| 本地部署模型 | 右上开关 `#swLocal`；服务地址 `#lmBaseInput`；「启动 LM Studio 服务」；「拉取」`#btnFetchLocal`；模型下拉 `#localModelSel`；「保存本地模型」`#btnSaveLocal`；「加入优先级列表」`#btnLocalToChain` |
| API 模型（云端） | 右上开关 `#swApi`；提供方 `#provSelect`；地址 `#provBase`；Key `#provKey`；「拉取」`#btnFetchCloud`；模型下拉 `#cloudModelSel`；「保存并切换」`#btnSaveCloud`；「清除 Key」；「加入优先级列表」`#btnCloudToChain` |
| 模型优先级列表 | 「清空」`#btnClearChain`；「测试第一个能不能出话」`#btnTestModel`；开关 `#swFailover`；列表 `#chainList`（可拖动）；「当前实际顺序」`#chainHint` |

拖动实现：每行 `<div draggable="true" data-idx="i">`，`drop` 时算 `from/to` 调 `move_model_in_chain(from, to)`；另外每行还给一组 `↑`/`↓` 按钮（拖动在某些触控板/远程桌面下不灵，得有兜底）。

**拉取模型列表为什么改成真 `<select>`**：v6 用的是 `<input list="provModelList"> + <datalist>`，在 WebView2 里点那个小箭头**什么都不弹**（Chromium 的 datalist 渲染在 Edge WebView2 上不可靠），用户看到的就是「点了箭头没有任何显示，然后一直卡在这里用不了」。换成原生 `<select>` 后一切正常。

**顺延日志**（出问题就看这两行）：

```
[WARN] [模型] 本地 LM Studio / dead-model 失败（connection），顺延下一个（2/2）
[INFO] [模型] 顺延到第 2 个端点 自定义（OpenAI 兼容） / deepseek-chat 成功
```

### 5.32 浏览动态（v7 新增）

用户原话：「增加一个模型浏览动态的功能模块，至于点赞和回复可以让模型自主决策，也可以选择所有的都回复和点赞，或者不浏览动态……如果模型在别人发的动态下评论了，别人回复了模型的评论，那么模型也要回复哦，直到没有任何回复」。

配置项：

| 键 | 默认 | 说明 |
|---|---|---|
| `feed_enabled` | `false` | 总开关 |
| `feed_decide` | `model` | `model` 自己决定 / `always` 全部都点赞+评论 / `readonly` 只看不互动 |
| `feed_poll_min` | `30` | 多少分钟看一轮（1–1440） |
| `feed_max_per_round` | `3` | 每轮最多互动几条（1–20） |
| `feed_dir` | `feed_inbox` | 收件箱目录（NapCat 读不到时手动喂） |
| `feed_comment_reply` | `true` | 别人回了我们的评论要不要接着回 |

**现实限制（必须知道）**：本机 NapCat（v4.x 常见版本）**没有**读说说列表的接口 —— `get_qzone_msg_list` / `get_qzone_msg` / `like_qzone` / `send_qzone_comment` 全部返回 `不支持的Api`。所以「浏览动态」真正能做的是：

1. 能读到就自动扫（新版 NapCat 支持时直接生效）；
2. 读不到就看 `feed_inbox/` 收件箱：你把看到的动态贴成 `.txt`（一行一条，可带 `作者:` 前缀），点「现在看一轮」就处理；
3. 点赞/评论走 `qzone.call("like_qzone"/"send_qzone_comment")`，接口不存在就只落库不实际发，并在结果里说明。

**追评到没有回复为止**（`moment_comment_loop` + `threads_awaiting_reply`）：

```
对方评论 → add_comment(direction='in',  status='pending')
我们回复 → add_our_comment(direction='out', status='replied')
对方再回 → add_comment(direction='in', parent_id=上一条, root_id=线索根)
循环：threads_awaiting_reply() 只要还有「最后一条是 in 且 pending」的线索，就继续回
```

`threads_awaiting_reply()` 的 SQL 思路：先按「线索根」分组取每组最后一条 id，再判断那条是不是 `in` + `pending`。这样一串十层楼的对话只会回最新那一层，不会从头再回一遍。

### 5.33 主动关心计划 / 主动回复：可视化管理（v7）

「活人感」页新增两块，都是**看得到、改得动、删得掉**：

| 模块 | 数据源 | 能做什么 |
|---|---|---|
| 主动关心计划 | `care_plans` | 列表（待发/已发/已取消筛选）、就地改时间与问法、取消、删除、手动加（`+30m` / `21:30` / `2026-10-23 09:00`） |
| 主动回复 | `user_events` | 同上（`desc` 改、时间改、取消、删除、手动加 `QQ号 + 干什么 + 什么时候`） |

两者区别（容易混）：

- `user_events`：**明确时间点的待办**（「明天9点考试」→ 考完问一句），v2 就有了，语义是「到点查岗」。
- `care_plans`：**模型自己决定的关心**（「我一会儿要下课了」→ 半小时后问「下课了没」），v6 才有，语义是「场景感知」。

时间解析 `_parse_plan_time()` 支持 `+30` / `+30m` / `+2h` / `+1d` / `09:00`（已过则明天）/ `2026-10-23 09:00` / `10-23 09:00`。返回 **float 时间戳**（失败返回 0），不是元组 —— 曾按元组解包，`TypeError: cannot unpack non-iterable float object`。

### 5.34 临时记忆 ⇄ 永久记忆（v7 新增）

用户原话：「可选临时记忆加入永久记忆，永久记忆也可以加入临时记忆，加一个按钮，通过选择一条或多条/全部转成永久记忆或临时记忆」。

| 方向 | 入口 | 背后 |
|---|---|---|
| 临时 → 永久 | 记忆 → 临时记忆 → 勾选若干条 → 「选中转永久」 | `promote_to_perm(user_id, texts=[...])` → `mem.add_perm()`（带冲突替换） |
| 永久 → 临时 | 记忆 → 永久记忆 → 勾选 → 「选中转临时」 | `demote_to_temp(user_id, perm_ids)` → 条目 `status='archived'` + 往会话里写一条 `system` 消息「（以前记住的，现在只当临时上下文）…」 |

细节：

- 临时列表只允许勾 `role="user"` 的行（勾机器人自己说的话没意义），其余 `disabled`；
- 「转给哪个QQ」输入框留空时，从会话名 `private_1234567` 里正则推账号；
- 降级**不删数据**，只把 `status` 改成 `archived`（`archive_perm(id, archived=False)` 能恢复），所以在永久记忆列表里仍可追溯；
- 提升走的是 `add_perm(replace_conflict=True)`，会和旧记忆做冲突判据（同 v5 那四条），不会留两条互相矛盾的。

### 5.35 记忆目录 `memory/`（v7 修复）

用户原话：「记忆模块的文件夹在哪，怎么没有，看一下，修复」。

v7 之前 `memory.db` 直接甩在数据目录根部，用户找不到、导出/备份也没固定地方。现在：

```
数据目录/
  memory/
    memory.db          ← 主库（11 张表）
    exports/           ← 导出的 json/csv/md
    backups/           ← 手动/自动备份
  config.json
  personas.json
  memory.db            ← 老库如果还在，启动时会被复制进 memory/（老文件保留）
```

`memory.memory_dir(root=None)` 负责建目录 + 迁移老库。`app_ui.MEMORY_DIR` 指向同一处，「记忆 → 打开记忆目录」直接跳过去。**打包版同样在 exe 同级的 `memory/`**，不在 `_internal`。

### 5.36 背景换图即时生效（v7）

排查结论：背景**本来就是生效的**（`#bgLayer` + `body.has-bg` 都正常），用户看到"没替换"是因为——选完下拉只改了下拉的值，还得再点「应用」才真的写配置，很多人不会点。

修法：`#bgSel` 加 `change` 事件，选完立刻 `applyBg(value)`；同时 `boot()` 里三处 `catch (e) { }` 改成 `catch (e) { console.error(...) }` —— 静默吞异常是上一个版本排查困难的直接原因。

顺带修的两件事：

- 公告栏只显示最新 2 条 + 「还有 N 条，点右上角 🔔 查看全部」，否则攒到 4 条就把整页顶下去；
- 链里本地那条不再显示「缺 Key」（`needs_key` 与 `missing_key` 分开，见 2.4.14）。

### 5.37 v7 相关常量/配置速查

| 键 | 默认 | 范围/取值 |
|---|---|---|
| `local_enabled` | `true` | 本地模型开关 |
| `api_enabled` | `false` | 云端 API 开关 |
| `model_chain` | `[]` | list of `{kind,provider,base,model,api_key}`，最多 10 |
| `model_failover` | `true` | 第一个失败是否顺延 |
| `provider_keys` | `{}` | `{provider: api_key}`，按提供方分别存 Key |
| `memory_dir` | `memory` | 记忆子目录名 |
| `feed_enabled` | `false` | 浏览动态总开关 |
| `feed_decide` | `model` | `model` / `always` / `readonly` |
| `feed_poll_min` | `30` | 1–1440 |
| `feed_max_per_round` | `3` | 1–20 |
| `feed_dir` | `feed_inbox` | 收件箱目录名 |
| `feed_comment_reply` | `true` | 追评开关 |

---

## 五之七、v8 新增行为

v8 的 15 项改动，按「机制」分节，每节都写了**为什么这么改**（很多是修 v7 的真实 bug）。

### 5.38 主动回复重做：什么时候才肯开口（修「1 分钟测试条目永远不发」）

**老 bug**：`care_loop` 拿 `user_events` 里的条目到点就发，但发之前**无条件**要求「对方静默 ≥ 2 小时」。
用户当场加一条「1 分钟后发」的测试条目 → 因为刚聊过天，静默不够 → 被跳过 → 用户看到的现象就是「设了不响」。

**现在的判定链**（`agent.py`）：

```python
def _checkin_should_send(sk, ev=None):      # -> (bool, 理由)
    hours = CHECKIN_SILENCE_HOURS
    if hours > 0:
        if not is_silent(sk, hours=hours):
            return False, "对方刚聊过（静默 %.1fh < 要求 %gh）"
    if CHECKIN_SILENCE_MODE == "model":
        return _model_wants_checkin(sk, ev)
    return True, ...
```

| 配置 | 含义 |
|---|---|
| `checkin_silence_mode` | `fixed` = 只看静默时长；`model` = 静默达标后再让模型决定「现在开口合不合适」 |
| `checkin_silence_hours` | 要求对方安静几小时。**`0` = 到点就发，完全不看静默**（这就是测试条目的解法）|
| `checkin_poll_sec` | 后台多久扫一次（默认 30 秒，最小 10） |
| `care_plan_use_silence` | 「关心计划」是否也套静默门槛（默认关：计划到点就发） |

**model 模式**用 `CHECKIN_DECIDE_PROMPT`（只回「发 / 不发」，不是 JSON，`max_tokens=40`、`effort="none"` 省时间）：
把最近 8 条对话 + 这次要关心的事一起给模型，让它判断「自然」还是「打扰」。
解析顺序是**先查「不发」再查「发」**——因为「不发」两个字里也含「发」，顺序反了会永远判成「发」。

**手动自测**：设置页把 `checkin_silence_hours` 填 `0.02`（≈1 分钟），加一条 1 分钟后的测试条目，等 2 分钟即可。

### 5.39 好感度系统（默认关闭，`affinity` 表）

开关 `affinity_enabled` 默认 `False`——不开就不加卡片、不注入提示词、不扣减，行为与 v7 完全一致。

**算分**（`bump_affinity_for(user_id, text, name)`，每收到一条私聊/群聊消息调一次）：

| 事件 | 增量 | 配置 |
|---|---|---|
| 聊一句 | `+affinity_gain_per_msg`（默认 1） | 0–10 |
| 说了让人开心的话（正则命中 `_POSITIVE`） | 再 `+affinity_gain_positive`（默认 3） | 0–20 |
| 说了难听的话（命中 `_NEGATIVE`） | `-affinity_gain_positive // 2` | — |
| 一天没来往（`_affinity_decay_once`，每轮只跑一次） | `-affinity_decay_per_day`（默认 1） | 0–20 |

最终 `score = MAX(0, MIN(affinity_max, score + 增量))`，然后按 `AFFINITY_LEVELS` 回写 `level`。

**怎么影响回复**（`build_affinity_block()`，拼进 system prompt）：
- `affinity_show_score = True` → 告诉模型「你和 TA 现在是『好朋友』（好感度 132）」，并附语气要求 `_AFFINITY_TONE[level]`。
- `False` → 只给等级，不给数字（模型不会说漏嘴报分数）。
- 提示词里明确写「**不要主动报出分数**」，否则模型会回「我们好感度 132 了呢」。

看板在「活人感」页（`cardAffinity`）：表格列 QQ/昵称、关系、分数、条数、清零按钮；顶部有手动设分（`set_affinity_score`）和全部清零（`reset_affinity('')`）。

### 5.40 界面卡片透明度

配置 `ui_panel_alpha`（20–100，默认 86）。前端 `applyPanelAlpha(pct)` 把它换算成透明度后，写进 CSS 变量
`--panel` 和 `--panel2`（`--panel` 是卡片底色，`--panel2` 比它亮 5 个百分点，用于悬浮层）。
全站卡片都用这两个变量做 `rgba()`，改一处全生效。

滑块在「设置 → 外观」；拖动即时预览，必须点「应用」才写进 `config.json`（重启不丢）。主题切换时 `applyPanelAlpha(_panelPct)` 会被重新调一次，所以换主题不会把透明度重置。

### 5.41 英文界面「完全无法显示」的两个真因（v8 修）

切到 English 后界面一个字都不变，查出来是**两个独立 bug 叠加**：

**真因 A —— `get_ui_lang()` 不接受参数。**
前端切语言后调 `get_ui_lang` 想拿新表，但后端签名是无参的，永远返回当前旧语言（还是中文）→ 前端拿到的映射里没有 key → 一个字都替换不了。
修法：`def get_ui_lang(code=""): code = (code or "").strip() or i18n.current()`，`Api.get_ui_lang(self, code="")` 同步。

**真因 B —— 只认一个语言目录，数据目录里那份是空样板。**
`i18n.LANG_DIR` 只指向数据目录。打包版/换过数据目录后，`ensure_dir()` 会在数据目录写一份**只含样板**的 `en-US.json`（`ui` 段是空的），而程序自带的 `langs/en-US.json` 里才有 433 条翻译 → 读到的永远是空表。
修法：
- 新增 `PROGRAM_LANG_DIR`（程序目录下的 `langs/`）；
- `ensure_dir()` 先 `_copy_from_program_dir()` 把程序自带的语言文件补进数据目录（**已有绝不覆盖**）；
- `_load_dir()` 依次读「程序目录」和「数据目录」，同名语言**按 code 合并**：`pui = dict(prev["ui"]); pui.update(ui)`，数据目录优先、缺的用程序自带的补。

**前端机制**（`index.html`）：`applyUiLang()` 取表 → 建 `UI_MAP` → `_uiWalk(document.body)` 做**整句精确替换**（`textContent` 全等才换，绝不做子串替换，否则「保存并重启」会被拆坏）→ `_uiObserve()` 起 MutationObserver 盯着动态渲染出来的新节点。
`zh-CN` 的表是空字典（`ui_dict("zh-CN") == {}`），所以中文界面**一个字都不会被动**，天然安全。

**怎么加翻译**：直接编辑 `langs/en-US.json` 的 `ui` 段，键是**中文原文**，值是英文。例：
`"保存": "Save"`、`"取消": "Cancel"`、`"删除": "Delete"`。

### 5.42 公告改成堆叠卡片

老版公告一条一行往下排，五条就把页面顶掉半屏。现在：

- `notice_stack = True`（默认）→ 最多同时显示 **4 张**卡片叠在一起（后面的用
  `translateY(i*11px) scale(1-i*0.03)` 错开，`z-index` 递减，只有顶层可点）。
- 顶层卡有「知道了」（标记已读，`dismissible` 才显示）和「下一条 ↓」（把当前条目挪到队尾轮换）。
- `notice_stack = False` → 退回老的一行一条列表（`_wireNoticeRows()`），展开/已读逻辑不变。
- 已读/已关闭的公告**不再显示**（`loadNotices()` 过滤 `!n.read && !n.dismissed`）。

`notice.py` 的 `all_notices()` 返回 **list**（不是 dict），按 `(date, id)` 倒序；v8 那条公告在文件顶部，一眼能看到。

### 5.43 说说每日条数自己定（最低 0，最高 50）

| 配置 | 范围 | 说明 |
|---|---|---|
| `moment_min_per_day` | 0–50 | 每天最少几条 |
| `moment_max_per_day` | 0–50 | 每天最多几条 |

- 前端预设按钮：`0 / 1 / 3 / 5 / 10 / 15 / 20`（`class="mom-pre" data-v="10"`）。
- 保存时：`min > max` 自动交换；超过 `MOMENT_MAX_HARD = 50` 直接拒绝；`> MOMENT_MAX_WARN = 30` 弹确认框
  「真的要设置这么多吗」，下面一行**红色小字**「设置太多会出问题的~」（`confirmDialog` 的第 5 个参数 `note`）。
- 每天的实际条数在 `[min, max]` 里随机取，再由模型决定「现在有没有话说」（模型说没有就跳过，不硬凑）。

### 5.44 日记升级（v8）

老版日记只喂「今天的聊天记录」。现在 `DIARY_PROMPT` 一次给四样东西：

1. `{persona}` 人设（`active_persona_text()`）
2. `{new_perm}` **今天新增**的长期记忆
3. `{old_perm}` 以往的长期记忆（更早的，让日记有连续感）
4. `{dialog}` 今天的临时对话（`mem.messages_of("private_{uid}", limit=200)`）

- `diary_hour`（0–23，默认 23）：每天几点自动写。
- 「手动生成」按钮走 `write_diary_for(..., force=True)`：**未绑定的账号也能写**，只是不进永久记忆库。
- 会话 key 必须用**下划线** `private_{uid}`（不是 `private:{uid}`），否则一条消息都读不到——这是实际踩过的坑。

### 5.45 生图直接用当前模型链路（不额外配 API）

`imagegen_use_current = True`（默认）时，生图**复用「模型」页那条优先级链**：
`targets_preview()` 返回 `[{base, model, api_key, provider, label, score}, ...]`（`chain_entries` 派生），
逐个试；当前模型不支持 `/v1/images/generations` 就自动跳到链里下一个**支持生图**的（云端 OpenAI 兼容服务）。

所以「生图开关点不动」的根因不是 CSS，是 `save_settings` 少了 `imagegen_use_current` 这个白名单键 → 前端传了后端直接丢弃 → 刷新回去又变成关。**新增开关时一定要同时改三处**：`config.py` 的 `DEFAULTS`、`_BOOL_KEYS`、`app_ui.save_settings` 的白名单。

### 5.46 关心计划识别更准 + 关闭即隐藏

`EVENT_EXTRACT_PROMPT` 新增几条硬规则（`extract_events`）：

- 带**最近 4 条对话**一起喂（`get_recent_messages(scope, turns=4)`）——只看当前一句经常漏掉「他刚说周六体检，然后回了个『对』」。
- `desc` 去掉句首人称（`我/你/他/她`）和句尾「要/会/得」，否则查岗话术会变成「对了，我明天9点考试——后来怎么样啦？」。
- 过滤空泛描述：`事情|事|那个|安排|计划|东西|啥|什么` 全命中就丢。
- `len(desc) > 40` 丢弃（太长的多半是模型复述原句）。
- 必须显式 `effort="none"`：这类结构化抽取不需要思考，用 `omit` 会让推理模型先想几百字、把 `max_tokens` 吃光 → 正文空 → 永远提取不到（v7 踩过，v8 保留修法）。
- 读不到事件类别时用**话题兜底**，不再塞「其他」两个字。

**关闭即隐藏**：`care.enabled = False` 时，「活人感」页的关心计划卡片整块 `display:none`（`loadPlans()` 里判断 `settingsCache.care.enabled`），不留空盒子。

### 5.47 一键卸载（`uninstall.py`）

「设置 → 卸载」先弹**清单**（`get_uninstall_plan`），逐项列出会删什么、留什么：

- **留下**：`memory/`、`config.json`、`personas.json`、`stickers/`、`themes/`、`langs/`、`icons/`、`backgrounds/`、`moment_images/`、`generated/`、`qzone_inbox/`
- **删除**：程序文件（`exe`、`_internal/`、`*.py`、`*.html`、`*.md` 等）、日志、缓存

勾选「保留数据」再确认后，`run_uninstall(keep_data)` 把活交给 `uninstall.py`：它先等主进程退出（轮询 PID），再删目录，最后用 `mshta` 弹一句「卸载完成」。
源码版（`is_frozen == False`）不允许卸载，只显示清单。

### 5.48 v8 常量 / 配置速查

```python
# config.py
"checkin_silence_mode": "fixed",    # fixed | model
"checkin_silence_hours": 2.0,       # 0-72，支持小数（0.02 ≈ 1 分钟，方便测试）
"checkin_poll_sec": 30,             # 10-3600
"care_plan_use_silence": False,
"affinity_enabled": False,          # 总开关，默认关
"affinity_gain_per_msg": 1,         # 0-10
"affinity_gain_positive": 3,        # 0-20
"affinity_decay_per_day": 1,        # 0-20，0 = 不掉
"affinity_max": 1000,               # 10-100000
"affinity_show_score": True,
"ui_panel_alpha": 86,               # 20-100
"notice_stack": True,
"moment_min_per_day": 1,            # 0-50
"moment_max_per_day": 10,           # 0-50
"diary_hour": 23,                   # 0-23

CHECKIN_SILENCE_MODES = ("fixed", "model")
MOMENT_MAX_HARD = 50
MOMENT_MAX_WARN = 30
```

---

## 六、日志

所有重要动作格式化写 `agent.log`（带时间戳），格式：

```
[2026-10-22 08:44:15] [INFO] [模型] 已记住该模型需要 max_tokens>=3000
[2026-10-22 08:44:15] [WARN] [消息清洗] 丢弃开头的 assistant 消息
[2026-10-22 08:44:15] [ERROR] [模型] HTTP 400 —— messages=12 ...
```

级别：`DEBUG` / `INFO` / `WARN` / `ERROR`。
模块标签：`[模型]` `[人设]` `[记忆]` `[关怀]` `[事件]` `[消息清洗]` `[WS]` `[摘要]`。

界面「控制台」实时滚动显示，级别着色。

---

## 七、打包

### 7.1 一键打包

```powershell
python build.py
```

产物：

| 路径 | 内容 |
|---|---|
| `dist_程序包/小鲸鱼/` | 完整客户端（exe + 依赖 + 可编辑配置） |
| `dist_开源包/` | 纯源码（无 venv / 无日志 / 无 db） |
| 桌面 `小鲸鱼_程序包.zip` | 上面第一个的压缩包 |
| 桌面 `小鲸鱼_开源包.zip` | 上面第二个的压缩包 |

参数：`--skip-app` 只打源码包 / `--no-zip` 不生成 zip。

### 7.2 PyInstaller 关键参数

```
--onedir --windowed --name=小鲸鱼
--add-data <绝对路径>;.
--hidden-import webview webview.platforms.edgechromium webview.platforms.winforms
               clr pythonnet sqlite3 websocket requests bottle proxy_tools
--exclude-module numpy pandas matplotlib scipy PIL tkinter PyQt5 PyQt6 PySide2 PySide6 IPython pytest
```

> ⚠️ 指定了 `--specpath` 后，`--add-data` 的**相对路径按 spec 目录解析**，所以必须传绝对路径。

### 7.3 数据目录规则（打包版必读）

打包后 `__file__` 指向 `_internal/`，如果直接用 `os.path.dirname(__file__)` 存数据库，用户根本找不到自己的聊天记录。

`_resolve_data_dir()` 的优先级：

1. 环境变量 `QQ_AGENT_DATA_DIR`（GUI 拉起子进程时显式传入）
2. `sys.executable` 所在目录（打包版 = exe 同级）
3. `__file__` 所在目录（源码运行）

`memory.py` 的 `_default_db_path()` 同逻辑。

结果：`memory.db` / `agent.log` / `config.json` / `personas.json` 全部落在 **exe 同级**，`_internal/` 保持干净。

### 7.4 ZIP 目录层级

压缩时以 `dist_程序包/小鲸鱼` 为根、`root_name="小鲸鱼"`，解压出来是：

```
小鲸鱼/
  小鲸鱼.exe
  config.json
  personas.json
  stickers/            ← 表情包目录（27 张默认图 + 怎么用.txt）
  themes/              ← 自定义主题目录（示例-午夜紫.json + 怎么自定义主题.txt）
  icons/               ← 自定义图标目录（怎么换图标.txt）
  agent.py 等源码
  TECH_DOC.md
  人设自定义指南.md
  使用说明.txt
  _internal/
```

**不是** `小鲸鱼/小鲸鱼/小鲸鱼.exe`。

### 7.5 数据目录 vs 程序目录

打包版有个容易踩的点：**代码在 `_internal/`，数据在 exe 同级**。

| 类别 | 位置 | 举例 |
|---|---|---|
| 程序文件（只读） | `_internal/` | `.py` 源码副本、DLL、`index.html` |
| 用户数据（可写） | exe 同级 | `config.json`、`personas.json`、`memory.db`、`agent.log`、`agent_state.json`、`stickers/`、`themes/`、`icons/`、`_backup/` |

`_resolve_data_dir()` 优先级：`QQ_AGENT_DATA_DIR` 环境变量 > `sys.frozen` 时的 `sys.executable` 目录 > 脚本目录。
`memory.py` 的 `_default_db_path()`、`config.py` 的 `_data_dir()` 都走同一套逻辑。

> **在线更新的影响**：`updater.py` 只覆盖代码文件，`NEVER_OVERWRITE` 里的用户数据一律不动。
> 所以更新不会把用户的配置、人设、记忆冲掉。

---

## 八、故障排除速查

| 现象 | 检查 | 处理 |
|---|---|---|
| 界面白屏 | WebView2 运行时 | 装 https://developer.microsoft.com/microsoft-edge/webview2/ |
| 点启动没反应 | `agent.log` 有没有新行 | 看是不是 Python 路径错；打包版看 exe 能否 `--agent` 起来 |
| 收到消息不回复 | 搜 `[人设]` `[模型]` | 人设加载失败 → 修 personas.json；模型报错 → 看下一行 |
| 回复为空 | 搜 `正文为空但思考有` | 推理模型，调大 `reasoning_max_tokens` |
| 400 Bad Request | 搜 `HTTP 400` 后一行 | 那是 LM Studio 原话，按 4.2 对照表处理 |
| 400 且提到 `'auto'` | 配置里 `reasoning_effort` | 设 `auto` 或 `none`，程序会自动翻译成 `none` |
| 连不上 127.0.0.1:1234 | `Get-NetTCPConnection -LocalPort 1234 -State Listen` | 服务器没起，点「启动 LM Studio 服务」或 `lms.exe server start --port 1234` |
| 回复很慢（30s+） | `reasoning_effort` | 设 `auto`/`none` 关掉思考，0.7s 出话 |
| 回复很慢 | `request_timeout` | 模型太大，换小模型或加显存 |
| 不主动问候 | `CARE_ENABLED` / 当前时间 | 只在 8:00–23:00，且用户静默 30min+ |
| 查岗不触发 | `user_events.status` | 必须是 `pending` 且 `target_time` 已过 |
| 数据库找不到 | 数据目录 | 打包版在 exe 同级，源码版在项目根 |
| 换电脑后不回复 | `config.json` 的 `lm_model` | 界面「模型设置」刷新重选 |
| 端口被占 | 残留进程 | `stop.bat` 或任务管理器结束 python |
| **私聊没补回** | 搜 `[补回]` | 看 `private_catchup_enabled` 是否为 true；对方最后一条是你发的就不会补 |
| **补回重复发** | 搜 `[补回]` 同一时间 | state 文件被删过导致游标丢失；删掉 `agent_state.json` 让它重建 |
| **不发说说** | 搜 `[说说]` | ①配额用完了（界面「活人感」看进度条）②不在 8~23 点 ③模型老返回 SKIP |
| **发说说报 ENOTFOUND** | NapCat 进程 DNS | 宿主机能解析不代表 NapCat 能；重启 NapCat 或检查系统代理 |
| **发说说报"不支持的Api"** | NapCat 版本 | `send_qzone_msg` 在较新版本才有，升级 NapCat |
| **表情包不发** | 搜 `[表情包]` | ①`stickers/` 目录空 ②回复里没识别到情绪 ③45% 概率没抽中（多试几次） |
| **表情包不显示缩略图** | 界面「活人感」 | 图 > 400KB 不内联；改用小图或直接看目录 |
| **引用回复没生效** | 搜 `send_private_segments` | NapCat 版本太老不支持 `reply` 段 |
| **记忆一直空** | 搜 `[记忆]` | 之前是 `EXTRACT_BUDGET` 太小导致推理模型吃光 token，v3 已修 |
| **@ 了但答非所问** | 搜 `[群上下文]` | 没拼上前文 → 检查 `GROUP_CONTEXT_LOOKBACK`；日志里看到 `[group] R: @2145216414` 说明 `extract_text` 又把 @自己 当正文了；拼错了 → 看 `pending_before_at` 是否穿越了上一个 @ |
| **永久记忆一直是空** | 界面「设置 → 日记与永久记忆」 | 必须先**绑定 QQ 号**，没绑定不写 |
| **日记不生成** | 搜 `[日记]` | ①`diary_enabled` 关着 ②没到 `diary_hour` ③当天消息数 < `diary_min_msgs` |
| **群聊不回复** | 界面「设置 → 群聊绑定」 | 模式是 `whitelist` 但群号不在名单里 |
| **想屏蔽某个群** | 同上 | 模式切 `blacklist`，把群号加进黑名单（最多 20 个） |
| **开机没自启** | 界面「设置 → 启动与系统」 | 看 `自启动注册表状态`；或查 `HKCU\...\Run` 有没有「小鲸鱼Agent」 |
| **自启后没自动开 Agent** | 命令行有没有 `--autostart` | 手动点「修复路径」重写注册表值 |
| **主题不生效** | 界面「设置 → 外观」 | 点「应用」后如果没变，刷新一下；自定义主题先点「重新扫描」 |

**v5 新增故障**

| 现象 | 检查 | 处理 |
|---|---|---|
| **说了"不对，是 XXX"旧记忆还在** | 界面「记忆查看 → 永久记忆」看有没有标「已被替换」 | ①确认 `memory_replace_conflict=true` ②看 `agent.log` 有没有 `（替换旧 #N）` ③模型没给 `replaces` 时靠启发式，日期类靠"双方都有 4 位数字"那条判据；把两条记忆写成明显不同的主体（"我住的城市"）就不会误判 |
| **旧数据冲突检测不生效** | 搜 `perm_missing_norm` | 老库 `norm_key` 是 NULL → 「记忆 → 重编译」跑一遍（`fill_norm=True`），跑前会自动备份 `memory_backup/memory_before_rebuild_*.db` |
| **重编译预览说"0 组重复"但明明有** | 版本 | v5 已修（旧版按 `norm_key` 列 GROUP BY，NULL 行全被忽略）；现在按现场 `norm_fact()` 算 |
| **云端模型连不上** | 「模型设置 → 模型来源」 | ①base 要不要以 `/v1` 结尾（DeepSeek 要，智谱是 `/v4`）②Key 填了吗 ③点「测试连通」看原始报错 |
| **云端 401 / 403** | Key | Key 只存在本机 `config.json` 的 `cloud_api_key`；换电脑要重填 |
| **云端突然不说话了** | 「模型设置 → 用量看板」 | 到日限额了 → 保护状态变「已拦截」；调大 `usage_daily_limit` 或点「清空今日」 |
| **用量一直是 0** | 端点是否返回 `usage` | 本地 llama.cpp 多数不返回，代码按 `字符数//4` 估算；云端一般都有 |
| **开机时弹"要不要装 NapCat"太烦** | 「设置 → QQ 接入 → 启动时自动检测」 | 改成「只在第一次启动时检测」或「完全不检测」 |
| **NapCat 显示已安装但没运行** | 那张卡黄点 | 点「启动 NapCat」，等 20~30 秒登录完成再「重新检测」 |
| **NapCat 下载安装失败** | 搜 `[NapCat]` | 内网/被墙；手动下 https://github.com/NapNeko/NapCatQQ/releases 解压，再把路径填进「安装目录」点保存 |
| **公告弹窗每次都弹** | `agent_state.json` 的 `notice_popup_shown` | 文件被删/被重置过；点「知道了」会写回去。想彻底不弹 → 设置里关「新公告弹窗提醒」 |
| **点了公告"知道了"铃铛还有未读** | `notice_read` vs `notice_popup_shown` 是两套状态 | 弹窗只记「弹过」，铃铛记「读过」；在通知中心里逐条点或点「全部已读」 |
| **铃铛不见了** | `get_settings().notice.center` | 设置里「右上角铃铛」被关了，重新打开 |
| **日记改了没保存** | 编辑态有没有点「保存」 | 「取消」会丢弃；编辑时正文变 textarea，改完点保存 |
| **导入记忆说格式不对** | JSON 顶层 | 必须有 `"format": "qq-agent-memory"`；只认本程序导出的文件 |
| **导入后旧记忆还在** | 导入模式 | `merge` 会保留旧的（冲突的标 superseded）；要全清重来用 `replace` |
| **主题文件被拒绝** | `themes/*.json` | 颜色只接受 `#hex`/`rgb()`/`hsl()`；变量名不能瞎写；不能和内置 5 套重名 |
| **图标不显示** | `icons/` 目录 | ①格式不支持 ②图 > 400KB ③没点「应用」 |
| **在线更新失败** | 界面「设置 → 在线更新」 | ①仓库名要写 `owner/repo` ②分支名要对 ③私有仓库需要 `GITHUB_TOKEN` |
| **更新后想回退** | 界面「立即回滚」 | 备份在 `_backup/<时间戳>/`，只保留最近 5 次 |
| **公告关不掉** | `config.json` 的 `notice_enabled` | 设 `false` 整体关掉；单条点「知道了」记在 `agent_state.json` |
| **模型没开却给好友发了"我脑子卡壳了"** | 搜 `本次不发送` | v4 前的 bug：主动开口也走了兜底话术。现在 5 个主动开口调用点都是 `allow_fallback=False`，模型不可用时静默跳过 |
| **离线 @ 消息再也不补回** | 搜 `不推进时间戳` | v4 前的 bug：补回中止后仍推进了 `last_catchup_ts`。现在中止就不推进，下次重试 |
| **所有「打开目录」点了没反应**（v6 修） | 搜 `[打开]` | 资源管理器窗口落到本程序后面了。v6 所有 12 处统一走 `shell_open()`，会抢前台；确实打不开就看任务栏 |
| **填了云端 Key 还提示「LM Studio 未启用」**（v6 修） | 界面「模型设置 → 模型来源」 | ①提供方是否已选成云端 ②点「保存并切换」再试 ③v6 前是 `list_models` 写死敲 1234 的 bug |
| **切云端后点「测试连通」还是打本地** | `config.json` 的 `lm_provider` | 保存后 agent 需要 `POST /reload_model` 热切换；界面「保存并切换」会自动发 |
| **云端 401** | API Key | 「模型来源」里重填一次 Key 再保存（界面会显示「Key 不对或没填」） |
| **关心计划一条都没排** | 搜 `[关心计划]` | ①`care_plan_enabled` 关着 ②`care_plan_source="manual"`（模型不排）③模型没在事件里给 `care:true` |
| **关心计划排太多了** | `care_plan_max_per_day` / `care_plan_min_gap_min` | 默认 6 条/天、间隔 25 分钟；`add_plan` 还有 ±10 分钟去重 |
| **到点了没发关心** | 搜 `[关心计划]` | ①不在 `care_plan_hours` ②当天额度用完 ③对方 8 分钟内刚说过话（`care_plan_respect_silence`） |
| **说说一条都不发** | `moment_decide` | 默认 `model`，模型判断"现在不适合"就不发；想固定节奏改成 `timer` |
| **说说没有配图** | `moment_image_enabled` + `moment_image_source` | 三个来源目录都要有图；`generated` 还要 `imagegen_enabled` |
| **说说评论不回** | `moment_comment_enabled` | ①开关关着 ②NapCat 读不到评论 → 用 `qzone_inbox/` 手打进去 |
| **同一条评论回了好几遍** | `moment_comments` 去重 | 正常情况按 `(moment_tid, commenter_id, content)` 去重；反复回说明 content 每次都不一样 |
| **生图一直失败** | 搜 `[生图]` | ①`imagegen_base` 要以 `/v1` 结尾 ②模型名不对 ③服务不认 `response_format` 时程序会自动去参重试一次 |
| **生图表情包不发** | `sticker_gen_enabled` | 要 `imagegen_enabled` + `sticker_gen_enabled` 同时开，且抽中 `sticker_gen_chance`（默认 0.12） |
| **背景图不显示** | 界面「设置 → 背景与语言」 | ①图放 `backgrounds/` 后要点「刷新」再选 ②图 > 400KB 不内联 ③选了要点「应用」 |
| **换语言没效果** | `langs/<code>.json` | ①JSON 语法错（右下角会报）②key 被改了 ③只翻了部分 → 缺的自动退回中文 |
| **导入记忆说识别不出格式** | 界面「载入记忆」 | 可以手动指定格式；支持 `qq-agent`/`generic-json`/`jsonl`/`csv`/`markdown`/`txt` |
| **拉取模型后点箭头什么都不弹**（v7 修） | — | v6 的 `<datalist>` 在 WebView2 里点不出选项，v7 全改成原生 `<select>`。如果还看不到，先点「拉取」等结果出来 |
| **链里第一个模型连不上，机器人就不说话了**（v7） | 搜 `顺延下一个` | ①`model_failover` 是不是关了 ②那一项所在类型的开关（`local_enabled`/`api_enabled`）是不是关着 ③日志里应该能看到 `失败（connection），顺延下一个（2/2）` |
| **两个开关都开，但只用了本地/只用了云端**（v7） | `model_chain` | 链里没有对应 `kind` 的项时，光开开关没用；要先在对应卡片点「加入优先级列表」 |
| **加了 10 个还想加**（v7） | — | 上限就是 10（`MAX_MODEL_CHAIN`），提示「最多 10 个，先删一个再加」 |
| **界面上的 Key 是 `sk-abc****3456`，能直接用吗** | — | 那是打码后的展示值。明文只在 `config.json` 的 `provider_keys` 里，`raw_chain()` 永远不会把明文给界面 |
| **浏览动态里一条动态都没看到**（v7） | 搜 `[动态]` | 本机 NapCat 读不到说说列表（返回 `不支持的Api`）→ 把动态贴进 `feed_inbox/*.txt`，点「现在看一轮」 |
| **同一条动态被反复点赞**（v7） | `feed_seen.post_key` | 正常按 `post_key` 去重；反复点说明 `post_key` 每次都变（`tid` 拿不到时会用 `作者+内容` 拼，内容改一个字就是新 key） |
| **换了背景没变化**（v7 修） | 界面「设置 → 背景与语言」 | 之前必须点「应用」才生效，v7 改成选完立刻换。若仍无变化：确认图在 `backgrounds/`、点过「刷新」、图不是超大（>400KB 只走路径不内联） |
| **记忆文件在哪**（v7 修） | 界面「记忆 → 打开记忆目录」 | 统一在 `<数据目录>/memory/`，里面 `memory.db` + `exports/` + `backups/`；老库会自动复制进去（老文件保留） |
| **临时/永久互转后，原记录不见了**（v7） | 界面「记忆 → 永久记忆」 | 永久→临时不是删除，是把 `status` 改成 `archived`；`archive_perm(id, archived=False)` 可恢复 |
| **生图开关点了没反应**（v8 修） | `config.json` 的 `imagegen_use_current` | 真因不是 CSS：`save_settings` 的白名单少了这个键，前端传了被后端丢掉，刷新又变回关。现在已加进 `DEFAULTS` + `_BOOL_KEYS` + 白名单 |
| **设了 1 分钟后发的测试条目，到点没动静**（v8 修） | `config.json` 的 `checkin_silence_hours` | 老逻辑无条件要求静默 2 小时。现在填 `0`（或 `0.02`）就不看静默；`checkin_poll_sec` 默认 30 秒扫一次 |
| **主动回复的提示被卡片盖住了**（v8 修） | 界面右上角 | 公告改成堆叠卡片（最多 4 张错开），不再一条一行往下顶 |
| **切 English 界面毫无变化**（v8 修） | 搜 `PROGRAM_LANG_DIR` | 两个真因叠加：`get_ui_lang()` 不收参数 + 数据目录里那份 `en-US.json` 是空样板。现在按 code 合并程序自带的 433 条翻译 |
| **`UNIQUE constraint failed: affinity.user_id`**（v8 修） | `bump_affinity` | 原来裸 `INSERT`，同一个人第二次加分就炸；现在是 `INSERT OR IGNORE` + `UPDATE` |
| **手动生成日记说「未绑定」**（v8 修） | `write_diary_for(..., force=True)` | 手动生成允许未绑定账号写（只是不进永久库）；自动生成仍然要求绑定 |
| **日记内容空/读不到聊天**（v8） | 会话 key | 必须是 `private_{uid}`（下划线），写成 `private:{uid}` 一条都查不到 |
| **好感度开关开了但没反应**（v8） | `affinity_enabled` | ①总开关没开 ②开了要重启 Agent ③看板在「活人感」页，`cardAffinity` 会隐藏当开关关闭时 |
| **好感度分数被模型说漏嘴**（v8） | `affinity_show_score` | 设 `false` 只给关系等级不给数字；提示词里也写死了「不要报分数」 |
| **说说条数设不进去**（v8） | 设置页「说说条数」 | ①上限 50（`MOMENT_MAX_HARD`）②`min > max` 会自动交换 ③超过 30 会弹确认框，点「取消」就不保存 |
| **公告一条都没了**（v8） | 筛选逻辑 | 已读/已关闭的不再显示；想重看 → 通知中心里点全部标记未读，或删 `agent_state.json` 的 `notice_read` |
| **卸载按钮点了只显示清单**（v8） | `is_frozen` | 源码版（`python app_ui.py`）不能自删，只在打包版可用；勾「保留数据」则 `memory/`、`config.json`、`stickers/` 都留下 |

---

## 九、配置速查

### 9.1 `config.json`

| 键 | 默认 | 说明 |
|---|---|---|
| `lm_base` | `http://127.0.0.1:1234/v1` | LM Studio 地址 |
| `lm_model` | （空=自动探测） | 模型 id，必须和 `/v1/models` 一致 |
| `lm_api_key` | `""` | 需要鉴权时填 |
| `max_tokens` | `1200` | 普通回复预算 |
| `reasoning_max_tokens` | `4000` | 推理模型思考预算 |
| `temperature` | `0.75` | |
| `top_p` | `0.9` | |
| `reasoning_effort` | `"auto"` | `auto`/`none`/`minimal`/`low`/`medium`/`high`/`xhigh`；`omit`=完全不发该参数。**`auto` 会被翻译成 `none` 再发出去**（LM Studio 不认 `auto`，直接发会 400） |
| `request_timeout` | `300` | 秒 |
| `max_context_tokens` | `6000` | 上下文预算，超了从旧到新砍 |
| `enable_vision` | `true` | 图片理解开关 |
| `private_catchup_enabled` | `true` | 启动后补回离线私聊 |
| `private_catchup_max_msgs` | `6` | 每好友最多补几条 |
| `private_catchup_first_hours` | `24` | 首次回看时长 |
| `private_quote_reply` | `true` | 私聊引用回复 |
| `moment_enabled` | `true` | 自动发说说 |
| `moment_min_per_day` | `1` | 每日配额下限 |
| `moment_max_per_day` | `10` | 每日配额上限 |
| `moment_hours` | `[8, 23]` | 可说说的时段 |
| `moment_check_interval_min` | `17` | 检查间隔 |
| `sticker_enabled` | `true` | 表情包 + emoji |
| `sticker_dir` | `"stickers"` | 表情包目录名 |
| `sticker_chance` | `0.45` | 发表情包概率 |
| `theme` | `"dark"` | 主题 key（内置 5 套或 `themes/` 里的文件名） |
| `theme_dir` | `"themes"` | 自定义主题目录 |
| `icon_dir` | `"icons"` | 自定义图标目录 |
| `icon_file` | `""` | 选中的图标文件名，空 = 默认「鲸」字 |
| `autostart` | `false` | 开机自启动（写 HKCU Run） |
| `diary_enabled` | `false` | 开启日记 |
| `diary_bound_accounts` | `[]` | 绑定的 QQ 号，最多 20 个 |
| `diary_auto_write` | `true` | 到点自动写日记 |
| `diary_hour` | `22` | 每天几点写 |
| `diary_min_msgs` | `6` | 当天至少几句才写 |
| `save_group_memory` | `false` | 关=只有 @ 时记；开=所有群消息都记 |
| `group_bind_mode` | `"all"` | `all` / `whitelist` / `blacklist` |
| `group_whitelist` | `[]` | 白名单群号，最多 20 |
| `group_blacklist` | `[]` | 黑名单群号，最多 20 |
| `notice_enabled` | `true` | 公告栏总开关 |
| `update_enabled` | `false` | 在线更新开关 |
| `update_repo` | `""` | `owner/repo` |
| `update_branch` | `"main"` | 分支名 |
| `update_api` | `"https://api.github.com"` | API 地址（企业版可改） |
| `update_timeout` | `20` | 秒 |

**v5 新增（19 项）**

| 键 | 默认 | 说明 |
|---|---|---|
| `memory_mode` | `"auto"` | `auto` / `always` / `temp` 三档记忆策略 |
| `memory_replace_conflict` | `true` | 新事实顶掉矛盾的旧事实 |
| `lm_provider` | `"local"` | `local` / `deepseek` / `openai` / `moonshot` / `zhipu` / `qwen` / `openrouter` / `siliconflow` / `custom` |
| `cloud_base` | `""` | 云端 base_url，空则用预设 |
| `cloud_model` | `""` | 云端模型名，空则用预设第一个 |
| `cloud_api_key` | `""` | 云端 API Key（**只写本机 config.json，不进开源包**） |
| `usage_daily_limit` | `200000` | 云端每日 token 上限，`0` = 不限 |
| `usage_guard_enabled` | `true` | 超限是否拦截 |
| `usage_warn_percent` | `80` | 到百分之多少打 WARN |
| `napcat_check_mode` | `"install"` | `install`（只第一次）/ `always`（每次都查）/ `never`（不查） |
| `napcat_dir` | `""` | 手动指定的 NapCat 安装目录 |
| `napcat_http` | `"http://127.0.0.1:3000"` | NapCat HTTP 端点 |
| `napcat_ws` | `"ws://127.0.0.1:3001"` | NapCat WS 端点 |
| `napcat_allow_install` | `true` | 允许界面下载安装 NapCat |
| `napcat_checked_once` | `false` | 内部标记：install 模式是否已检查过 |
| `notice_popup` | `true` | 新公告启动弹窗一次 |
| `notice_center` | `true` | 右上角铃铛 + 通知中心 |
| `memory_export_dir` | `"memory_backup"` | 导出/备份目录 |
| `memory_auto_export` | `false` | 每天自动导出一份记忆备份 |
| `memory_auto_export_days` | `7` | 自动备份保留几天 |
| `keep_data_on_uninstall` | `true` | 卸载时保留数据（只删程序） |

**v6 新增（33 项）**

| 键 | 默认 | 说明 |
|---|---|---|
| `bg_dir` | `"backgrounds"` | 背景图目录 |
| `bg_file` | `""` | 当前背景文件名，空 = 不用背景 |
| `bg_dim` | `35` | 压暗程度 0~90 |
| `bg_blur` | `0` | 模糊 0~40 |
| `bg_fit` | `"cover"` | `cover` / `contain` / `repeat` |
| `lang` | `"zh-CN"` | 界面语言码 |
| `lang_dir` | `"langs"` | 语言包目录 |
| `care_plan_enabled` | `true` | 启用主动关心计划 |
| `care_plan_source` | `"model"` | `model` / `manual` / `both` |
| `care_plan_hours` | `[8, 23]` | 允许关心的时段 |
| `care_plan_max_per_day` | `6` | 每天最多关心几次 |
| `care_plan_min_gap_min` | `25` | 两次关心最小间隔（分钟） |
| `care_plan_respect_silence` | `true` | 对方 8 分钟内说过话就跳过 |
| `moment_decide` | `"model"` | `model`（模型自己决定发不发）/ `timer`（到点就发） |
| `moment_image_enabled` | `false` | 说说带配图 |
| `moment_image_source` | `"mix"` | `preset` / `sticker` / `generated` / `mix` |
| `moment_image_dir` | `"moment_images"` | 预制配图目录 |
| `moment_image_max` | `1` | 每条说说最多几张图 0~9 |
| `moment_comment_enabled` | `false` | 回复说说评论 |
| `moment_comment_poll_min` | `5` | 多久扫一次评论（分钟） |
| `moment_comment_deliver` | `"comment"` | `comment`（优先评论区）/ `private`（走私聊） |
| `moment_comment_inbox` | `"qzone_inbox"` | 评论收件箱目录 |
| `imagegen_enabled` | `false` | 启用生图 |
| `imagegen_base` | `""` | 生图接口 base（OpenAI 兼容） |
| `imagegen_model` | `"gpt-image-1"` | 生图模型名 |
| `imagegen_api_key` | `""` | 生图 Key（**只写本机**） |
| `imagegen_size` | `"1024x1024"` | 出图尺寸 |
| `imagegen_style` | `""` | 风格（部分模型支持） |
| `imagegen_timeout` | `180` | 超时秒数 |
| `imagegen_dir` | `"generated"` | 生成图输出目录 |
| `sticker_gen_enabled` | `false` | 聊天里允许发生成的图当表情包 |
| `sticker_gen_chance` | `0.12` | 发生成图的概率 0~1 |
| `memory_format` | `"qq-agent"` | 默认导出格式 |

枚举常量（`config.py`）：`MEMORY_MODES=("auto","always","temp")`、`NAPCAT_CHECK_MODES=("install","always","never")`、`CARE_PLAN_SOURCES=("model","manual","both")`、`MOMENT_DECIDES=("model","timer")`、`MOMENT_IMAGE_SOURCES=("preset","sticker","generated","mix")`、`MOMENT_COMMENT_DELIVERS=("comment","private")`、`BG_FITS=("cover","contain","repeat")`。

#### v7 新增（13 项）

| 键 | 默认 | 说明 |
|---|---|---|
| `local_enabled` | `true` | 本地部署模型开关 |
| `api_enabled` | `false` | 云端 API 开关（可与本地同时开） |
| `model_chain` | `[]` | 模型优先级列表，`[{kind,provider,base,model,api_key}]`，最多 `MAX_MODEL_CHAIN=10` |
| `model_failover` | `true` | 第一个端点失败是否顺延下一个 |
| `provider_keys` | `{}` | 按提供方分别存 Key：`{"deepseek":"sk-...","moonshot":"..."}` |
| `memory_dir` | `"memory"` | 记忆子目录名（库/导出/备份都在里面） |
| `feed_enabled` | `false` | 浏览动态总开关 |
| `feed_decide` | `"model"` | `model` 自己决定 / `always` 全都赞+评 / `readonly` 只看 |
| `feed_poll_min` | `30` | 多少分钟看一轮（1–1440） |
| `feed_max_per_round` | `3` | 每轮最多互动几条（1–20） |
| `feed_dir` | `"feed_inbox"` | 收件箱目录 |
| `feed_comment_reply` | `true` | 别人回了我们的评论要不要接着回 |

枚举常量（v7 新增）：`FEED_DECIDES=("model","always","readonly")`、`MAX_MODEL_CHAIN=10`。

`set_checked()` 对这两个复合类型有专门分支：

- `model_chain`：逐项校验 `kind ∈ {local,cloud}`、`provider` 必须在 `PROVIDERS` 里、必须有 `model`，其余丢掉；超过 10 项截断；返回 `(True, "优先级列表共 N 项（上限 10）", cleaned)`。
- `provider_keys`：只收 `str→str`，返回 `"已保存 N 个提供方的 Key"`。

#### v8 新增（15 项）

| 键 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `checkin_silence_mode` | `"fixed"` | `fixed`/`model` | 主动回复判定方式：只看静默 / 再让模型决定 |
| `checkin_silence_hours` | `2.0` | 0–72（小数） | 要求对方安静几小时；**0 = 到点就发** |
| `checkin_poll_sec` | `30` | 10–3600 | 后台多久扫一次到点条目 |
| `care_plan_use_silence` | `false` | bool | 关心计划是否也套静默门槛（默认不套） |
| `affinity_enabled` | `false` | bool | 好感度总开关 |
| `affinity_gain_per_msg` | `1` | 0–10 | 每聊一句加多少 |
| `affinity_gain_positive` | `3` | 0–20 | 说了让人开心的话额外加多少（难听话按一半扣） |
| `affinity_decay_per_day` | `1` | 0–20 | 每天不来往掉多少（0 = 不掉） |
| `affinity_max` | `1000` | 10–100000 | 分数上限 |
| `affinity_show_score` | `true` | bool | 是否把分数告诉模型（关则只给等级） |
| `ui_panel_alpha` | `86` | 20–100 | 卡片透明度（%） |
| `notice_stack` | `true` | bool | 公告是否堆叠显示（关 = 老的一行一条） |
| `moment_min_per_day` | `1` | 0–50 | 每天最少几条说说 |
| `moment_max_per_day` | `10` | 0–50 | 每天最多几条说说 |
| `diary_hour` | `23` | 0–23 | 每天几点自动写日记 |

枚举/硬上限常量（v8 新增）：`CHECKIN_SILENCE_MODES=("fixed","model")`、`MOMENT_MAX_HARD=50`、`MOMENT_MAX_WARN=30`。

⚠ **加新开关时必须同时改三处**：`config.py` 的 `DEFAULTS`、`config.py` 的 `_BOOL_KEYS`（布尔键）、`app_ui.py` 的 `save_settings` 白名单。少改白名单 → 前端传了被静默丢弃 → 刷新后开关又变回去（v8 的「生图开关点不动」就是这么来的）。

### 9.2 `personas.json`

```json
{
  "active": "whale",
  "personas": {
    "whale": { "name": "小鲸鱼", "persona": "……", "rules": "……" }
  }
}
```

`persona` 必填（不能为空），`rules` 可选。

### 9.3 环境变量

| 变量 | 作用 |
|---|---|
| `QQ_AGENT_DATA_DIR` | 强制指定数据目录（数据库/日志/配置） |
| `QQ_AGENT_LM_BASE` | 覆盖 `lm_base` |
| `QQ_AGENT_LM_MODEL` | 覆盖 `lm_model` |
| `GITHUB_TOKEN` / `QQ_AGENT_GITHUB_TOKEN` | 私有仓库在线更新用 |

---

## 十、文件清单

| 文件 | 行数 | 作用 |
|---|---|---|
| `agent.py` | 4371 | 主逻辑：WS 收发、模型调用（**v7 优先级链 + 顺延**）、关怀线程、事件提取、私聊补回、说说、表情包、群上下文、云端热切换、关心计划、评论追评、生图调度、浏览动态、**v8 主动回复判定 / 好感度** |
| `memory.py` | 1831 | SQLite 存储层，**13 张表** + 自动迁移 + 永久记忆冲突/导出导入 + 关心计划 + 说说评论 + `feed_seen` + 临时⇄永久互转 + **v8 `affinity`** |
| `app_ui.py` | 4044 | PyWebView 后端 + `Api` 门面（**139 个方法**）+ `shell_open` 目录抢前台 |
| `index.html` | 4757 | 桌面 UI（Tailwind + 原生 JS，6 个 Tab + 3 个弹层，前端调用 **119 个**后端方法） |
| `config.py` | 631 | 配置中心（**122 个配置项** + `set_checked` 校验 + `_coerce_bool` + 10 组枚举） |
| `config.json` | — | 配置数据 |
| `personas.py` | 205 | JSON 人设加载器 |
| `personas.json` | — | 人设数据 |
| `stickers.py` | 351 | 表情包扫描 + 情绪匹配 + emoji 池 |
| `stickers/` | — | 27 张默认表情包 PNG（文件名带情绪标签）+ 怎么用.txt |
| `theme.py` | 489 | 主题：5 套内置 + `themes/` 自定义 + CSS 变量生成 |
| `themes/` | — | 自定义主题目录（示例-午夜紫.json + 怎么自定义主题.txt） |
| `icons/` | — | 自定义图标目录（怎么换图标.txt） |
| `backgrounds/` | — | **v6** 背景图目录 |
| `langs/` | — | **v6** 语言包目录（zh-CN.json / en-US.json / 怎么加语言.txt） |
| `moment_images/` | — | **v6** 说说预制配图目录 |
| `generated/` | — | **v6** 生图输出目录 |
| `qzone_inbox/` | — | **v6** 评论收件箱（读不到评论时手打进来） |
| `feed_inbox/` | — | **v7** 动态收件箱（读不到说说列表时手打进来） |
| `memory/` | — | **v7** 记忆目录：`memory.db` + `exports/` + `backups/` |
| `memory_backup/` | — | 数据库备份（`mem_rebuild.backup_db()` 输出） |
| `diary.py` | 587 | 日记 + 永久记忆提炼（带 `replaces`）+ 账号绑定 + 群绑定 |
| `notice.py` | 428 | 公告栏 + 弹窗 + 通知中心（开发者功能，改 `NOTICES` 列表） |
| `updater.py` | 451 | GitHub 在线更新（检查/下载/应用/回滚） |
| `autostart_mgr.py` | 217 | 开机自启动（HKCU Run，免管理员） |
| `providers.py` | 476 | 模型提供方预设 + `resolve()` + 掩码 + **v7 优先级链**（`chain`/`raw_chain`/`add`/`remove`/`move`/`clear`） |
| `usage.py` | 329 | v5 token 用量统计 + 日限额 + 三层保护 + **v6 明细** |
| `napcat_mgr.py` | 422 | v5 NapCat 三层探活 + 启动 + 下载安装 |
| `mem_rebuild.py` | 372 | v5 记忆重编译（补 norm_key / 去重 / 迁移旧表） |
| `qzone.py` | 518 | **v6** QQ 空间：能力探测 + 发/删说说 + 评论读取/回复 + 收件箱 |
| `imagegen.py` | 504 | **v6** OpenAI 兼容生图（b64_json + 降级重试）+ **v8 用当前模型链路** |
| `formats.py` | 624 | **v6** 记忆 6 种格式的读写与自动识别 |
| `i18n.py` | 569 | **v6** 多语言（内置 key + `langs/` 覆盖）+ **v8 程序目录/数据目录按 code 合并** |
| `uninstall.py` | 171 | **v8** 一键卸载（等主进程退出 → 删目录 → mshta 提示） |
| `qq_faces.py` | 83 | QQ 表情编号 → 中文 |
| `build.py` | 796 | 双版本打包 |
| `build.bat` | — | 双击打包 |
| `start.bat` / `stop.bat` | — | 无界面启停 |
| `_selftest_v3.py` | 412 | 89 项离线自检（不打包） |
| `_selftest_v3_alive.py` | 246 | 67 项活人感专项自检（不打包） |
| `_selftest_v4.py` | 559 | 134 项 v4 专项自检（不打包） |
| `_selftest_v5.py` | 587 | 145 项 v5 专项自检（不打包） |
| `_selftest_v6.py` | 369 | 138 项 v6 专项自检（不打包） |
| `_selftest_v7.py` | 445 | 156 项 v7 专项自检（不打包） |
| `_selftest_v8.py` | 395 | **137 项 v8 专项自检**（好感度/主动回复/日记/生图/i18n/公告/卸载/契约/HTML 关键字） |
| `_check_ui.py` | 79 | 前后端 API 契约校验（不打包） |
| `_check_js.py` | 56 | index.html 主脚本括号/引号平衡粗检（不打包） |
| `_make_stickers.py` | 146 | 一次性生成默认表情包（不打包，需要 PIL） |
| `_mock_lm.py` | 110 | 假 LM Studio，复现 400 与推理模型（不打包） |
| `_mock_cloud.py` | 96 | 假云端 API（401/正常两态，不打包） |
| `TECH_DOC.md` | 本文档 | 技术文档 |
| `人设自定义指南.md` | 373 | 面向用户的人设教程 |
| `README.md` | 1340 | 项目说明 |
| `更新日志.md` | 317 | **v1→V1.1.0 全量更新记录（整合版）** |
| `V1.1.0-更新说明.md` | — | **本版更新摘要（给人看的，随程序包发布）** |
| `v8-更新说明.md` | — | 上一版更新摘要（留档） |
| `v7-更新说明.md` | 254 | 上一版摘要 |
| `v6-更新说明.md` | — | 更早版本摘要 |
| `v5-更新说明.md` | 235 | 更早版本摘要 |

>上表停在 V1.1.0，之后新增的文件见下面这张补充表（只列还没进上表的新增项）。

|文件 |作用 |
| --- | --- |
| `desktop/src/ui/ui.css` | V1.7.0三组件样式（只用 CSS变量，亮色主题自动跟随） |
| `desktop/src/ui/ui-core.js` | V1.7.0底座：模态栈 /焦点锁定 / Esc / i18n / RPC |
| `desktop/src/ui/ui-toast.js` | V1.7.0 `UI.toast` |
| `desktop/src/ui/ui-confirm.js` | V1.7.0 `UI.showConfirm` + `UI.prompt` |
| `desktop/src/ui/ui-sign.js` | V1.7.0 `UI.showSignDialog`（含审计落库） |
| `desktop/src/ui/ui-compat.js` | V1.7.0兼容层（旧全局函数名转发到新组件） |
| `audit.py` | V1.7.0签署审计（SQLite `ui_sign_audit` +哈希链 + Machine ID） |
| `desktop/src/first-run.js` | V1.3.0首启顺序编排（协议 →性能模式 →向导 →公告） |
| `desktop/src/setup-wizard.js` | V1.2.0首次启动依赖安装向导 |
| `desktop/src/onboarding.js` | V1.0.0-Beta新手引导（三步） |
| `desktop/src/bridge.js` | V1.2.0 Tauri桥接：`window.pywebview.api.xxx()` → HTTP |
| `resources/av_module/av_server.exe` | V1.3.2音视频微服务（自带 Python运行时，端口5800） |
| `resources/av_module/av_core/` |音视频核心：`audio_io`/`screen`/`rpa`/`stt`/`pipeline`/`config`/`tts`/`video`/`drivers`/`fetch`/`winutil`/`events` |
| `resources/av_module/rpa/templates/` | V1.6.3内置接听/挂断模板 PNG（accept_green_48/72、hangup_red_48/72） |
| `computer_use.py` | V1.4.0 Computer Use（浏览器/键鼠/代码沙箱/动作账本/急停） |
| `spark.py` | V1.4.0时光胶囊（.spark加密导入导出） |
| `qq_call.py` | V1.4.0主动打电话护栏 + RPA拨号 |
| `route_memo.py` | V1.4.0高频话「记录模式」 |
| `打包发布.py` | V1.2.2一键出包（离线包 →后端 exe → av模块 → Tauri →收集 →绿色版/开源包） |
| `build_av.py` |音视频模块打包（V1.6.3起把 `faster_whisper/assets/*`资产打进 exe） |
| `build_backend.py` |后端 exe打包（版本信息从 `version.py`读，不手改 `build_version_info.txt`） |
| `Docs/弹窗与提示规范_V1.7.0.md` |三个 UI组件的规范与字段表 |
| `Docs/弹窗替换清单_V1.7.0.md` |全站替换点清单（由脚本扫描生成） |
| `kg.py` | V1.8.0知识图谱（`kg_entities` / `kg_relations` 两张表 + `ingest_triples` 入图 + `context_block` 多跳检索 + `build_from_perm` 历史建图 + `export_rows`/`import_rows`） |
| `resources/av_module/av_core/tts.py` | V1.8.0新增 `custom_voice` 引擎（5 种外部协议 + 静默降级 edge-tts） |
| `<数据目录>/voices/` | V1.8.0自定义音色的参考音频存放目录（上限 30MB，中文文件名保留） |
| `Docs/知识图谱与自定义音色_V1.8.0.md` | V1.8.0 两项能力的用户向说明（怎么开、怎么配、坏了怎么办）；V1.8.1 只加了界面入口，文档未改 |
| `audit.py` | V1.7.0 签署审计；V1.8.1 加 `algo` 列、封印表 `ui_sign_audit_seal`、`_digest` 扩到 11 字段、`verify()` 两段式（逐行 + 尾部锚点） |
| `desktop/src/ui/ui-compat.js` | V1.8.1 加 `stripQuotes`/`stripVague`，「确定卸载」→按钮「卸载」、取消「暂不卸载」 |
| `computer_use.py` | V1.8.1 加 `_TITLE_BLACKLIST`/`_bad_title`（新闻里的备案号等直接丢）、空查询相关度改 `0.0`、修 bing 分支 `m.group(3)` 的 `IndexError` |
| `notice.py` | V1.8.1 最新公告对齐 `version.APP_VERSION`；`_normalize()` 补回被丢掉的 `version` 字段 |


---

## 十一、二次开发指南

这一章写给"以后要加功能的人"（包括三个月后的你自己）。按这个顺序改，不会漏。

### 11.1 加一个新配置项（最常做的事）

以加一个 `my_feature_enabled` 为例，**四步**：

1. **`config.py` 的 `DEFAULTS` 里加一行**
   ```python
   "my_feature_enabled": True,
   ```
   不加这一行，`save()` 会把它当垃圾字段丢掉（`save()` 只保留 `DEFAULTS` 里的键）。

2. **如果是数字，加进 `_NUM_RULES`**
   ```python
   _NUM_RULES = {
       ...
       "my_feature_threshold": (int, 1, 100),
   }
   ```
   不在这里配，`set_checked()` 会拒绝前端传值。

3. **如果是布尔，加进 `_BOOL_KEYS`**；如果是枚举，在 `set_checked()` 里加白名单分支。

4. **`agent.py` 顶部读它**
   ```python
   MY_FEATURE_ENABLED = bool(_CFG.get("my_feature_enabled", True))
   ```
   > 注意：`agent.py` 的常量是**启动时读一次**的。改了配置要重启 agent 才生效。
   > 需要热更新的话，在用到的地方直接 `cfg_mod.get(...)`。

### 11.2 加一个新表

1. **`memory.py` 的 `_init_tables()` 里加 `CREATE TABLE IF NOT EXISTS`**
   ```python
   conn.execute("""
       CREATE TABLE IF NOT EXISTS my_table(
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           user_id TEXT NOT NULL,
           content TEXT NOT NULL,
           created_at REAL
       )
   """)
   conn.execute("CREATE INDEX IF NOT EXISTS idx_my_user ON my_table(user_id, id DESC)")
   ```

2. **老库要加列的话，写进 `_migrate()`**
   ```python
   self._add_column_if_missing(conn, "my_table", "new_col", "TEXT")
   ```
   `_migrate()` 是幂等的，每次启动都跑，加不存在的列/索引。

3. **加读写方法**（照 `add_perm` / `get_perm` 的样子写，都是 `with self._conn() as c:` 风格）

4. **`stats()` 里加计数**，界面「记忆查看」的统计条会自动显示。

### 11.3 加一个界面 Tab

1. **`index.html` 的 `<nav>` 里加按钮**
   ```html
   <button class="tab-btn" data-tab="myTab">我的功能</button>
   ```

2. **加 `<section id="tab-myTab" class="tab-pane hidden">…</section>`**（放在其它 section 旁边）

3. **JS 的 `TABS` 数组加名字**
   ```js
   const TABS = ['console','model','persona','alive','memory','settings','myTab'];
   ```

4. **`switchTab()` 里加加载逻辑**
   ```js
   if (name === 'myTab') { loadMyTab(); }
   ```

5. **写 `async function loadMyTab()`**，内部用 `await call('my_api_method')` 调后端。

6. **跑 `_check_ui.py`** —— 它会检查：
   - 每个 `call('xxx')` 在 `Api` 里都有实现
   - 每个 `data-tab` 都有对应的 `section`
   - 每个 `$('id')` 引用的元素都真实存在

### 11.4 加一个 Api 方法

1. **`app_ui.py` 里写函数**（`class Api` 之前，模块级）
   ```python
   def my_api_method(arg1="", arg2=0):
       try:
           ...
           return {"ok": True, "data": ..., "msg": ""}
       except Exception as e:
           return {"ok": False, "msg": f"出错了：{e}"}
   ```
   约定：**永远返回 dict，永远带 `ok` 和 `msg`，绝不抛异常**（抛了前端只会看到 `null`）。

2. **`class Api` 里加一行转发**
   ```python
   def my_api_method(self, arg1="", arg2=0):
       return my_api_method(arg1, arg2)
   ```
   > 容易忘的一步。忘了前端调不到，`_check_ui.py` 会报出来。

3. **注意 `**dict` 覆盖坑**：如果内部函数返回的 dict 里有 `ok` 键，展开时会把你的结果盖掉。
   正确写法：
   ```python
   st = binding_status()
   st.pop("ok", None)
   st.update({"ok": bool(ok), "msg": msg})
   return st
   ```

### 11.5 加一个后台线程

```python
def my_loop():
    while True:
        try:
            ...干活...
        except Exception as e:
            log(f"[我的功能] 出错: {e}", "ERROR")
        time.sleep(60)

# _main_inner() 里
threading.Thread(target=my_loop, daemon=True, name="my_loop").start()
```

**三条铁律**：

1. `daemon=True` —— 否则主线程退出时卡住
2. **整个循环体包在 `try/except` 里** —— 一次异常不能把线程打死（打死了就再也不工作了，而且没有任何提示）
3. **异常一定 `log()`** —— 静默失败是最难查的 bug（参考 4.1 根因 C 的 `SYSTEM_PROMPT` NameError）

### 11.6 改数据库 schema 的注意事项

| 做什么 | 怎么做 | 不要做 |
|---|---|---|
| 加表 | `CREATE TABLE IF NOT EXISTS` | 直接 `CREATE TABLE`（第二次启动就报错） |
| 加列 | `_add_column_if_missing()` | `ALTER TABLE ADD COLUMN` 不加判断 |
| 改列名 | `_migrate()` 里先检查旧列存在再 `RENAME` | 假设用户都是新库 |
| 删列 | **别删**，留着不用 | SQLite 删列要重建表，风险大 |

### 11.7 加一个表情包情绪类别

1. `stickers.py` 的 `EMOTION_KEYWORDS` 加一类 + 关键词
2. `EMOJI_SETS` 加同一类的 emoji 池
3. 表情包文件名用新情绪词（如 `得意_哼哼.png`），`_tags_from_filename()` 自动识别

### 11.8 加一个自定义主题

不用改代码。在 `themes/` 里放一个 json：

```json
{
  "name": "我的主题",
  "vars": {
    "bg": "#0d1117", "panel": "#161b22", "panel2": "#1c2128",
    "line": "#30363d", "text": "#c9d1d9", "muted": "#8b949e",
    "accent": "#58a6ff", "accent2": "#7ee787",
    "ok": "#3fb950", "warn": "#d29922", "err": "#f85149",
    "radius": "10px"
  }
}
```

界面「设置 → 外观」→「重新扫描」就能选到。

### 11.9 发布一次更新（走在线更新通道）

1. 改代码 → 本地测试（跑 `_selftest_v3.py` / `_selftest_v4.py` / `_check_ui.py`）
2. `python build.py` 确认能打包
3. 把**源码**（不含 `_` 开头的自检脚本、不含 db/log）推到 GitHub
4. 用户在「设置 → 在线更新」里填 `你的用户名/仓库名`，点「检查更新」→「立即更新」
5. `updater.py` 只会覆盖 `.py/.html/.md/.bat/.txt`，**绝不会碰**用户的 `config.json`/`personas.json`/`memory.db`/`stickers/`/`themes/`/`icons/`

> **发布前检查**：别把带密钥的 `config.json` 推上去。仓库里应该只有源码 + 默认配置模板。

### 11.10 调试技巧

| 场景 | 做法 |
|---|---|
| 不想连真模型 | 起 `_mock_lm.py`（监听 1234），能模拟 400 / 推理模型吃 token |
| 不想连真 QQ | 直接 `import agent; agent.mem = Memory(":memory:")` 然后调函数 |
| 想复现群里 @ 上下文 | `agent.group_ctx.add(gid, uid, "阿哲", "你吃饭了吗")` 再 `add(..., is_at=True)` |
| 看前端调了什么 | `webview.start(debug=True)`，右键「检查」看 Console |
| 截图验证界面 | `PIL.ImageGrab.grab()`（pywebview 6.x 的 `Window` 没有 `screenshot()` 方法） |
| 日志太吵 | `log(msg, "DEBUG")` 的不会进文件（只在界面显示），需要时改级别 |

> **别用 PowerShell 的 `-replace` + `Set-Content` 批量改 Python 文件** —— 会把 UTF-8 中文变成乱码 + BOM。
> 用编辑器或 `write` 工具整文件重写。

### 11.11 加一个模型提供方（v5）

只改 `providers.py` 的 `PROVIDERS` 一处：

```python
PROVIDERS["my_llm"] = {
    "name": "我的自建模型",
    "base": "https://my-server.example.com/v1",
    "models": ["my-model-7b"],
    "needs_key": True,
    "note": "自己搭的，按需填 key",
}
```

界面「模型设置 → 模型来源」下拉立刻多一项，**前端不用改**。

注意三点：

1. `base` 必须是 OpenAI 兼容端点（`/v1/chat/completions`、`/v1/models`）。
2. `models` 第一个会成为「推荐模型」自动回填；留空数组就是让用户手填。
3. `needs_key=False` 的项，界面上 Key 框会显示"本地不需要 Key"，且 `resolve()` 不会因为缺 key 报错。

### 11.12 加一个用量分类（v5）

`agent.py` 的 `_usage_kind(tag)` 是一张 tag 前缀 → kind 的表：

```python
def _usage_kind(tag):
    t = (tag or "").lower()
    if "记忆" in t:   return "memory"
    if "说说" in t:   return "moment"
    ...
    return "chat"
```

新调用点的 `tag` 想单独统计，就在这里加一行 + 在 `index.html` 的用量看板 `KIND_CN` 里加中文名（不加就显示英文 key）。

### 11.13 加一条公告（v5）

`notice.py` 顶部 `NOTICES` 列表加一个字典就行，**界面不放按钮**（开发者功能）：

```python
{
    "id": "2026-10-23-v6",       # 唯一，别重复（重复的会自动加 -2 后缀）
    "date": "2026-10-23",        # 日期倒序用；同一天按 id 倒序
    "level": "important",        # info / warn / important
    "title": "标题",
    "body": "第一行\n第二行",
    "link": "",                  # 只允许 http(s)
    "dismissible": True,
    "expire": "2026-12-31",      # 可选，过了这天自动不显示
}
```

三种状态键都在 `agent_state.json`：`notice_popup_shown`（弹过窗）、`notice_read`（读过）、`notice_dismissed`（不再提示）。
调试时调 `notice.restore_all()` 三份全清。

### 11.14 扩展记忆重编译（v5）

`mem_rebuild.rebuild()` 是三步流水线，加新一步就在这里插：

```python
def rebuild(mem, dry_run=True, fill_norm=True, dedupe=True, migrate_legacy=True,
            my_new_step=False):        # ← 加参数
    ...
    if not dry_run:
        backup_db(mem)                 # 只在真执行时备份
    if fill_norm:      _fill_norm_keys(mem, dry_run)
    if my_new_step:    _my_new_step(mem, dry_run)     # ← 插这
    if dedupe:         _dedupe(mem, dry_run)
    if migrate_legacy: _migrate_legacy(mem, dry_run)
```

铁律两条：

1. 每一步都要支持 `dry_run=True` 只统计不写。
2. **备份只在 `dry_run=False` 分支里做** —— 预览时碰磁盘是最容易翻车的地方。

同时把统计项加进 `analyze()`（界面预览要显示），并在 `app_ui.memory_rebuild_preview()` 的中文摘要里加一行。

### 11.15 v5 踩过的坑（写新代码时避开）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| `add_perm` 返回三元组 | `ValueError: too many values to unpack (expected 2)` | `pid, is_new, superseded = m.add_perm(...)` |
| dict 展开覆盖 `ok` | `{"ok": False, "msg": "格式不对", **status}` 被 status 里的 `ok: True` 盖掉，界面显示成功 | 先展开再 `d.pop("ok", None)`，最后写 `{"ok": ok, "msg": msg, **d}` |
| `set_checked` 的 bool | 传 `"0"` / `"false"` 被 `bool(x)` 判成 True，"关开关"变"开开关" | 用 `_coerce_bool()`，它认 `0/false/no/off/否/关` |
| 新增配置项 | 存了重启就没了 | 必须同时在 `DEFAULTS` 里加一行（`save()` 只保留 DEFAULTS 里的键） |
| 数字配置项 | 存 100 变成 1000 | 在 `_NUM_RULES` 里定上下限；想允许 0 就把下限写成 0 |
| JS 和 Python 字段名 | 后端返回 `popup`，前端读 `nc['notice_popup']` → `undefined` → 开关全是关的 | 前后端字段名对照着写；拿不准就 `console.log` 打印一次 |
| `evaluate_js` 里 await | 传 Promise 直接返回 `{}` | 只能用同步表达式，异步逻辑要写进页面函数里再调用 |
| 界面截图看不到控件 | 以为是渲染 bug | 先用 `evaluate_js` + `getComputedStyle` 查 `display/visibility/rect`，多数是滚出视野 |
| 滚动截图 | `document.querySelector('main').scrollTop = N` 无效 | 真正的滚动容器是 `<section id="tab-xxx" class="overflow-auto">`，滚它 |

### 11.16 v6 踩过的坑（写新代码时避开）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| `Memory` 的删除方法返回 bool | `TypeError: cannot unpack non-iterable bool object` | `cancel_plan`/`delete_plan`/`delete_comment` 返回 `bool`，写成 `ok = m.xxx(...)`，别解包 |
| `{"ok":..., **inner()}` | 内层函数自带 `ok`/`msg` 把本次操作结果盖掉（删失败却显示成功） | 用 `app_ui._with(base, ok, msg, **extra)`，它先剔掉 base 里的 `ok`/`msg` |
| NapCat action 存在性 | 直接调 `get_qzone_msg` 报错 | 先用 `qzone.caps()` 探测：`不支持的Api` = 没接口；`Schema compilation error` = 有接口缺参数 |
| 生图阻塞 | 回复卡十几秒到一分钟 | `gen_sticker_async()` 起 daemon 线程，回复先发、图后补 |
| 云端/本地配置串门 | 云端下保存模型把 `lm_model` 改了，切回本地模型变了 | 用 `_provider_ctx()` 的 `key_base`/`key_model` 自动选 `cloud_*` 还是 `lm_*` |
| `agent` 的模块级变量是导入时算的 | GUI 进程里切了提供方，`test_model` 还在打旧地址 | 调 `ag.apply_provider(verbose=False)` 刷新 |
| 前端传短键后端认长键 | `save_imagegen({"enabled":true})` 静默全失败 | 加 `_IMAGEGEN_ALIAS` 这类别名表，或在界面上直接用长键 |
| `os.startfile` 打开目录 | 用户说"点了没反应" | 新目录被塞进背后的资源管理器窗口 → 统一走 `shell_open()`（`explorer.exe` + `EnumWindows` + `SetForegroundWindow`） |
| 格式自动识别 | 单个 JSON 对象 / 单行 JSONL 判不出来 | 见 2.4.12 的三个坑 |
| 语言包 key | 翻错 key 名导致界面显示 key 本身 | `i18n.t()` 缺 key 自动退回中文；加语言只用改右侧的值 |
| 自检断言 | 把「设计如此的拒绝」当 bug | 写断言前先 `print()` 一遍真实返回结构 |

### 11.17 v7 踩过的坑（写新代码时避开）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| `raw_chain()` 漏打码 | 界面上能抓到完整 `sk-xxx` | 给界面用的函数一律把 `api_key` 置空，只留 `masked_key`/`has_key`；明文只留在 `chain()` |
| `needs_key` 混用两种语义 | 本地那一行也顶着「缺 Key」 | `needs_key`（提供方本身要不要 Key）与 `missing_key`（这一条现在缺不缺）分开 |
| `_unsupported` 是全局 set | A 端点不认 `reasoning_effort`，把 B 端点也一起禁了 | 改成 `{端点key: set()}`，配 `_tkey(target)`/`_unsup(target)` |
| `_reasoning_model` 是单值 | 换端点后仍然按上一个端点的结论走 | 改成 `_reasoning_cache = {端点key: bool}`，`apply_provider()` 里清空 |
| `datalist` 在 WebView2 | 点箭头什么都不弹，用户以为卡死 | 用原生 `<select>`；选项要先点「拉取」才有 |
| 索引建在 ALTER 之前 | 新库 `sqlite3.OperationalError: no such column: root_id` | 依赖新列的索引放到 `_migrate()` **末尾**，用 try/except 包着 |
| `save_state()` 收参数 | `TypeError: save_state() takes 0 positional arguments` | 它用模块级 `state`，原地改就生效，调用时不要传参 |
| `_parse_plan_time()` 返回值 | `TypeError: cannot unpack non-iterable float object` | 返回单个 float 时间戳（失败 0），不是元组 |
| `{"ok":..., **get_xxx()}` | 删计划失败却显示成功 | 统一用 `_with()`（v6 就有，v7 又踩了 6 次） |
| 背景「换了没变化」 | 其实是没点「应用」 | 下拉 `change` 直接 `applyBg()`；`boot()` 里的 `catch(e){}` 一定要 `console.error` |
| 公告堆积 | 攒 4 条把整页顶下去 | 公告栏只渲染最新 2 条 + 「还有 N 条」，完整列表去铃铛 |
| 假云端探测 | `urllib` 对 401 抛异常，被 `except` 当成"服务没起来"跳过测试 | 用 `requests` 并接受 401 也算 alive |

### 11.18 v8 踩过的坑（写新代码时避开）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 同名函数写了两遍 | v8 的 `write_diary_for` 完全没生效，跑的还是旧逻辑 | 改动前先 `grep "def 函数名"`；Python 取**最后一个**定义，空壳/旧版放前面等于白改 |
| 会话 key 用冒号 | 日记读不到当天的聊天，正文空白 | 全项目统一 `private_{uid}`（**下划线**）；`agent.py`、`diary.py` 必须是同一个拼法 |
| 新增开关只加了 `DEFAULTS` | 界面点开关，刷新后又变回关闭（「生图开关点不动」） | 三处同改：`DEFAULTS` + `_BOOL_KEYS` + `app_ui.save_settings` 白名单 |
| `get_ui_lang()` 无参 | 切语言后界面一个字都不变 | 后端接收 `code` 参数；前端传目标语言码，不要靠"当前语言" |
| 语言目录只认一个 | 打包版/换数据目录后英文界面空白 | `PROGRAM_LANG_DIR` + 数据目录**按 code 合并**，程序自带的兜底 |
| 语言替换做子串替换 | 「保存并重启」被拆成「Save并重启」 | 整句 `textContent` 全等才替换；中文表返回空字典，天然不动 |
| `INSERT` 撞主键 | `UNIQUE constraint failed: affinity.user_id`，第二次加分就炸 | 有主键的单行表统一 `INSERT OR IGNORE` + `UPDATE` |
| 主动回复无条件要静默 | 用户设 1 分钟后的测试条目永远不发 | 静默门槛做成配置，`0` = 不看静默；理由用 `(bool, 说明)` 返回，日志能看到为什么没发 |
| 解析「发 / 不发」顺序错 | 永远判成「发」（「不发」里也含「发」） | 先判 `"不发" in ans` 再判 `"发"` |
| 弹框参数塞进正文 | 红色小字提醒没法高亮 | `confirmDialog(title, body, okText, danger, note)` 用第 5 个参数，走独立 `#confirmNote` |
| 关掉功能还留空盒子 | 关心计划关了，页面上一块空白 | 渲染时 `card.classList.toggle('hidden', !on)`，开关状态从 `settingsCache` 读 |
| PowerShell 改带中文的 .py | 文件被写成 GBK/带 BOM，Python 直接语法错 | 改源码用编辑器/写入工具，别用 `Set-Content`/`-replace` 整段替换 |
| 本机没有 `node` | 想 `node --check` 校验 JS 报「找不到命令」 | 用 `_check_js.py` 做括号粗检，或直接用 DSH 自带的 node 路径 |

---

### V1.1.2 二次收尾的模块增量

| 模块 | 新增/变化 | 说明 |
| --- | --- | --- |
| `web_server.py` | 新建 | `start(port)` 起 `ThreadingHTTPServer`（只绑 127.0.0.1），`/health` 返回状态，其余 POST 走 `Api` 同名方法；`stop()` 关。 |
| `imagegen.py` | `PROGRAM_PROMPT` / `draw_program()` | 让文本模型写 Pillow 代码，`exec` 前做 token 黑名单 + 内置函数白名单，6 秒超时，成功落盘并 `usage.record`。 |
| `app_ui.py` | `draw_program` / `save_canvas` / `web_state` / `start_web` / `stop_web` / `open_web` / `save_startup_order` / `get_settings` 的 `ui` 与 `notice.poll_*` | 手动画布把 data URL 解成 PNG 存盘；网页版状态机在 `_WEB`。 |
| `theme.py` | `snow` 主题 + `_norm_effects` | 主题返回 `effects`（snow/edge），前端 `applyThemeFx()` 读它生成雪花。 |
| `agent.py` | `_hist_turns()`、`emotion` 注入、`build_affinity_block()` 亲密段、`feed_comment_round()` | 历史轮数读配置；情感/亲密/回评论都在系统提示或独立循环里。 |
| `personas_store.py` | `save_order()` / `list_cards()` 排序 | 顺序存 `config.persona_order`。 |
| `providers.py` | `FEATURES`/`feature_target()`/`set_feature_model()` | 11 个功能位，未配回退优先级链。 |
| `plugins.py` | 新建 | 内置 10 项 + 用户插件目录，`setup_all()` 在 `agent.main()` 里调用。 |

关键不变量（这批新增的）：

1. 生图与程序生图是两条路：`draw_now` 必须有云端 Key，`draw_program` 只要有文本模型。
2. `draw_program` 的 `exec` 环境只有 `img/draw/W/H/Image/ImageDraw/ImageFont/random/math` 与白名单内置函数。
3. 网页版只监听 `127.0.0.1`，且必须 `web_enabled=True` 才起。
4. 情感/亲密/学习/分工四处注入都读 `_live_cfg`，改完不用重启 Agent。

补充（程序生图与排版）：

- `imagegen.PROGRAM_PROMPT` / `PROGRAM_FIX`：五步画法 + 失败重写指令；`_looks_blank(img)` 用 `ImageStat.Stat(img.convert("L")).stddev[0] < 3` 判定纯色。
- `imagegen.draw_program(..., code="")`：给的代码只跑 1 次，模型生成的跑 2 次（第二次带错误反馈）；返回 `attempts`。
- `index.html` 的 `_slugKey()` / `_cardKey()`：排版顺序按「tab-id:标题去标点」存，`applyCardOrder` 依次尝试稳定键 → 去标点标题 → 原始标题（兼容旧预设）。

### V1.1.4 补丁 5：安全核心混淆 / 并发测试 / 好感度开关实时化

| 项 | 改法 | 不变量 |
|---|---|---|
| `secure_core.py` | 两个派生密钥改为 Base64(XOR(反转(原文))) 分片，`_one`/`_parts` 还原 | `secret_salt()`/`manifest_key()` 结果逐字节不变 |
| 并发测试 | `tests/conftest.py` 隔离数据目录；`tests/test_concurrency.py` 7 例 | 测试不触碰真实库；30 秒内必须收敛（防死锁） |
| 好感度 | `AFFINITY_ENABLED`/`AFFINITY_SHOW_SCORE` 改为 `_live_cfg` 读取 | 关掉只停止注入 prompt，不删分数 |
| 等级文案 | 页面按 `AFFINITY_LEVELS` 七档重写 | 文案与代码阈值一一对应 |

自检口径：`python -m pytest tests -q` → 7 passed；`python _selftest_v114.py` → 98 项；`_ui_smoke.py` → 35 项。

## V1.1.6 补丁 5 技术要点（2026-10-24）

### 1. 语义向量后端分级（semantic.py）

```
embed(text)
  ├─ backend in (auto, onnx) 且 _model_ready()  → _embed_local()  → 512 维（BGE-small-zh）
  ├─ backend in (auto, server)                 → _embed_server() → /v1/embeddings
  └─ 都失败                                     → None（调用方退关键词 Dice）
```

- 本地推理：`onnxruntime` + `tokenizers`，输入 `input_ids/attention_mask/token_type_ids`，
  取 `last_hidden_state` 做 mean-pooling 后 L2 归一化；失败即 `_LOCAL_OK=False`，不再重试刷日志。
- 距离：有 `sqlite_vec` 用 `vec_distance_cosine`，没有就用纯 Python 点积（向量已归一化）。
- 旧库向量维度不一致时 `stats()` 会报 `dim` 与期望不符，`rebuild_vectors()` 可重建。

### 2. 窗口模式（app_ui.py）

`_win_native(w)` → `winforms` 的 Form（`window.native`，不是 `window.gui`，后者是模块）。

### 3. 证据化测试

- `_selftest_v117.py`：静态 + 配置 + 真实向量（26 项）。
- `_t_audit14c.py`：行为级（真的调 API 看返回值，23 项）。


## V1.7.0技术要点（2026-10-29）——弹窗与提示统一组件层

###1.组件分层与加载顺序

```
desktop/src/ui/
 ├─ ui.css只用 CSS变量（--bg --panel --panel2 --line --text --muted
 │ --accent --ok --warn --err --radius），亮色主题靠
 │ html[data-theme-dark="0"]覆盖，不硬编码任何颜色
 ├─ ui-core.js底座：模态栈 /焦点锁定 / Esc / i18n / RPC
 ├─ ui-toast.js UI.toast
 ├─ ui-confirm.js UI.showConfirm + UI.prompt
 ├─ ui-sign.js UI.showSignDialog（含审计落库）
 └─ ui-compat.js兼容层（同名全局函数转发）
```

`desktop/src/index.html`第17–25行引入，**六条全部 `defer`**。
理由：主脚本是内联的（`<script>`无 src），同步执行；`defer`脚本在内联脚本之后、
`DOMContentLoaded`之前依次执行，此时 `window.toast` / `confirmDialog`已被旧定义赋好，
兼容层才能安全覆盖。若改成同步 `<script src>`，会先于内联脚本执行，覆盖失效。

###2. z-index分层

|元素 | z-index |来源 |
| --- | --- | --- |
| Toast |100000 | `--xjui-z-toast` |
|强模态签署 |99999 | `--xjui-z-sign` |
|阻断警告 |99990 | `--xjui-z-confirm` |
|首启向导 `#frMask` |100 | `first-run.js` |
|免责 `#discMask` |120 |遗留（本版已不用） |
|危险动作 `#dangerMask` |80 |遗留 |
|公告 `#popupMask` /通知 `#bellMask` |40 |遗留 |

Toast最高是因为它必须永远可见；但它容器 `pointer-events:none`、只有卡片本身
`pointer-events:auto`，所以不会挡住任何点击。

###3.模态栈（ui-core.js）

```
UI.openLayer(maskEl, {onEsc, initialFocus}) → entry
UI.closeLayer(entry) / UI.layerDepth() / UI.topLayer() / UI.isBlocking()
```

-打开时：`documentElement.style.overflow='hidden'`锁滚动，`role=dialog`、
 `aria-modal=true`，30ms后聚焦（默认取**最后一颗**按钮 =安全按钮）；
-关闭时：恢复 overflow，焦点还原到打开前元素；
-键盘：`keydown` capture阶段拦 `Escape`（只关最上层）与 `Tab`（首尾循环）；
-焦点：`focusin` capture，若 `ev.target`不在最上层遮罩内就拉回；
 `focusout`时若 `relatedTarget`为空且 `activeElement`跑到 `body`，同样拉回；
-遮罩上 `mousedown` + `preventDefault()`，防止点空白把焦点甩到 `body`。

###4.强模态签署与审计

```
UI.showSignDialog(title, content, options) → Promise<{ok,id,at,agreement,version,signature,raw}|null>
```

执行序（关键：**先落库，再算同意**）：

```
用户勾选 → accept解除 aria-disabled
 ↓点「同意并继续」
busy=true，按钮文案改「正在保存签署记录…」
 ↓
UI.rpc(auditMethod, [{agreement, version, title, accepted_at, signature, client, ...extra}])
 ↓失败 ↓成功
弹窗不关，hint显示失败原因， resolve({ok:true, id, at, ...})，弹窗关闭
按钮文案改「重试保存并同意」
```

`UI.rpc`通道优先级：`window.__bridge.call(m, args)` → `window.call(m, ...args)`。
选 `__bridge`打头是因为首启动时后端可能还没把 API对象挂到 `window.call`，
而 `bridge.js`是独立的 HTTP封装，`first-run.js`一直用它读写配置，最稳。

审计后端 `audit.py`：

|函数 |说明 |
| --- | --- |
| `machine_id()` | `sha256(uuid.getnode()\|platform.node()\|platform.machine()\|platform.system())[:16]` |
| `device_info()` | `{machine_id, mac, mac_ok, host, platform}`；`uuid.getnode()`第40位为1说明是随机数，`mac_ok=false` |
| `user_id()` | config的 `user_id`，没有就 `'local-' + machine_id` |
| `accept(payload)` |写一行，`prev_hash`取上一行 `hash` |
| `state(agreement)` |最近一次同协议记录 |
| `verify()` |逐行重算哈希链，返回 `{ok, total, broken, intact}` |

表结构：

```sql
CREATE TABLE IF NOT EXISTS ui_sign_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ts TEXT NOT NULL, agreement TEXT NOT NULL, version TEXT NOT NULL,
 title TEXT, user_id TEXT, machine_id TEXT, mac TEXT,
 accepted_at TEXT, signature TEXT, client TEXT,
 prev_hash TEXT, hash TEXT
);
```

`hash = sha256(prev_hash  ts  agreement  version  user_id  machine_id  accepted_at)[:32]`
——任一字段被改，`verify()`立刻报 `broken`。

###5.阻断警告的文案兜底

`ui-confirm.js`里 `BANNED`词表 = `确定 /确认 / ok / okay / yes /是 /好的 /好 /
取消 / cancel /关闭 / close`。命中时：

```
okText → '确认' + (actionName || '执行')
cancelText → '暂不' + (actionName || '执行')
```

同时 `console.warn`提醒调用方改成明确动作。`actionName`由标题推导（去掉尾部
`？?！!。…、,，`，超14字截断）。

###6. Toast去重与防轰炸（ui-toast.js）

```
key = options.key || (kind + '' + text)
```

-同 key已存在：`count +=1`、显示 `×N`、重置倒计时、加 `xjui-pulse`抖动、挪到末尾；
- `entries.length >3`：`removeEntry(entries[0], true)`挤掉最老的；
-计时：`remaining`记录剩余毫秒，`mouseenter`暂停、`mouseleave`续上；
-可视化剩余时间：底部 `.xjui-toast-bar`用 `transform: scaleX(1) → scaleX(0)`线性过渡；
-按钮来源：`onRetry` → `toast.retry`（primary）、`onDetails` → `toast.details`、
 `undoCallback` → `toast.undo`、`actions: [{label, onClick, kind}]`。

###7.兼容层映射（ui-compat.js）

|旧调用 |新实现 |特殊处理 |
| --- | --- | --- |
| `toast(msg, type)` | `UI.toast` | `ok/err/warn/fail` → `success/error/warning` |
| `confirmDialog(t, b, ok, danger, note)` | `UI.showConfirm` | `ok`命中 `VAGUE`时用标题动作；`note` → `risk`；`ok`命中 `ACK`词表时 `single: true` |
| `riskConfirm(t, b, ok, sec, note)` | `UI.showConfirm` | `countdown: sec`，`danger: true` |
| `promptDialog(t, b, def, ok)` | `UI.prompt` | `okText`为空时用「保存」 |
| `askRestart(featureCN)` | `UI.showConfirm` |文案保持原样 |
|原生 `confirm()` |包一层 | `UI.isBlocking()`为真时直接 `false` |

###8. i18n

`UI.t(key, vars)`：先查内置 `TEXT`（中文原文），再用 `window.UI_MAP[中文原文]`翻译。
`UI_MAP`由既有的 `applyUiLang()`从后端 `get_ui_lang`拉取（`i18n.ui_dict()`生成的
「中文原文 →译文」映射）。所以：

-组件新增文案只需在 `langs/<语言>.json`的 `ui`段加一条，代码不用动；
- `ensure_dir()`只在文件缺失时写，手改 `langs/en-US.json`不会被覆盖；
- V1.7.0已给 `en-US.json`补25条组件文案（`ui`段460 →485）。

###9.自验证（CDP）

`_cdp_verify.py`（开发期临时脚本，不随包分发）：Playwright `launch(args=["--remote-debugging-port=9333"])`
再 `connect_over_cdp()`，走真 CDP。前置：

```powershell
cd desktop\src
python -m http.server5999 --bind127.0.0.1
```

页面里用 `add_init_script`桩住 `window.__bridge`（否则 `checkDisclaimer()`会重试
90×800ms才放行）。断言32项，末行 `===32/32 PASS ===`。

关键断言与结果：

|断言 |结果 |
| --- | --- |
|强阻断不可穿透 |底层探针 `bgHit=0`，`elementFromPoint`落在遮罩内 |
|点遮罩不关闭、焦点不丢 |遮罩仍在，`activeElement`仍在弹窗内 |
| Tab12次不逃逸 |焦点始终在弹窗内循环 |
|危险按钮颜色 | `rgb(220,38,38)` |
| Esc关闭警告框 | resolve `false`，焦点还原 |
| Toast自动消失 |2.6s后存活 `1 →0` |
| Toast防轰炸 |同内容连发3次 → `nodes=1, count='×3'` |
| Toast不阻断 |底层探针 `bgHit=1` |
|模糊词替换 | `['暂不清空动作日志', '清空动作日志']` |
|审计失败可重试 |弹窗不关，按钮变「重试保存并同意」 |

###10.未动的部分（刻意）

-静态 DOM `#discMask` / `#learnProtoMask`块已从 HTML删除，`#confirmMask` /
 `#promptMask` / `#dangerMask`仍留在 HTML里但不再被 JS引用（仅占位，不影响功能）；
- `#cwAlert`（管理员暗号提醒）、`#popupMask`（公告）、`#bellPanel`（通知中心）、
 `#aboutMask`、`#frMask`、设置向导：全部保持原状，未纳入本次重构；
- `web_server.py` / `backend_server.py`的 RPC安全约束（Origin白名单 +强制 JSON +
一次性票据）没有改动，`audit_*`走普通写方法通道（不在 `_DANGER_METHODS`里）。



## V1.8.0 技术要点（2026-10-29）——知识图谱记忆与自定义音色

### 1. 为什么是「轻量图谱」而不是向量图谱

`semantic.py` 已经有向量检索，为什么还要图谱？因为向量解决的是「语义相近」，解决不了「关系路径」。
「老板那只猫叫什么」这句话跟「张三养了橘子」在语义上几乎不相似，但**沿着关系走两跳**就能答。

所以这一版**不引入图数据库、不引入新的 embedding**，只在现有 SQLite 里加两张表 + 内存 `networkx` 图：
- 存储成本 ≈ 0（三元组平均 60 字节）；
- 检索成本 ≈ 0（默认 4000 条边以内，BFS 是微秒级）；
- **失败成本 = 0**（任何一步异常都退化成「没有关系网」，聊天照常）。

### 2. 三处「不能只按实体名找种子」的坑

「老板那只猫」这个 case 值得写进技术文档，因为它是**设计缺陷的典型形态**：

1. 只按实体名找种子 → 「老板」不是实体名，它是谓词「是…的老板」的一半 → 只命中「猫」；
2. 加了谓词种子但写成 `if not seeds:` 兜底 → 命中「猫」之后就不再走谓词分支，**整条链还是丢**；
3. 正确做法是**合并**：`seeds = find_seeds(...) + seed_by_predicate(...)` 去重。

教训：**「兜底」只适合「主路径完全失败」的场景；当主路径「部分成功」时，兜底必须改成「补充」**。

### 3. 冷却 / 批量 / 跳过

`kg_build_loop()` 是**慢线程**，不是「实时建图」：默认一天一批 20~50 条。
理由：三元组抽取要调模型，实时跑会跟聊天抢 token；而老记忆本来就不急。
拆不出三元组的记忆插一条 `status='skipped'` 的占位行，下次 `build_from_perm()` 用
`perm_id NOT IN (SELECT perm_id FROM kg_relations)` 过滤时自然跳过——**不重试无法拆解的条目**。

### 4. 自定义音色的四种返回体

外部 TTS 服务返回什么都有，`_custom_pick_audio()` 按顺序判断：
1. 前两字节 `PK` → zip（GPT-SoVITS 常返回 wav 但不打包，这里只是防御）
2. 前四字节 `RIFF` → wav
3. 前三字节 `ID3` → mp3
4. 否则当 JSON 解析，从 `audio` / `data` / `audio_base64` / `wav` / `result` 里 base64 解码

都不匹配才抛「自定义音色服务没返回音频」。**注意 decode 也走 `decode_to_pcm()`**，
所以 mp3/wav 都靠 miniaudio 或 ffmpeg 兜底，跟 edge-tts 那条路径完全一样。

### 5. 降级链与缓存

```
engine == "sapi"                                  → ["sapi"]
engine in ("custom_voice","auto") 且配置完整       → ["custom_voice", "edge", "sapi"]
其余                                               → ["edge", "sapi"]
```

缓存 key 里混入 `custom_fingerprint()`（地址/格式/voice_id/参考文本/参考音频路径 + **mtime**），
所以**换了参考音频，老缓存自动失效**，不需要手动清 `runtime/tts_cache`。

### 6. 参考音频的路径与文件名

存 `<数据目录>/voices/`，文件名 `re.sub(r"[^\w.\-]+", "_", basename, flags=re.UNICODE)[:60]`。
`flags=re.UNICODE` 是必须的——否则中文文件名会被整串替换成下划线，用户看到 `____.wav` 会以为坏了。
上限 30MB（前端 20MB 先拦一次），提示语是「截一段 5~15 秒的就够了」。

### 7. 与 V1.7.0 组件层的关系

本版没有新增弹窗，但复用了上一版的兼容层：
- 试听失败用 `toast(..., 'warn')`，走 `UI.toast`；
- 参考音频上传（>20MB）也只 toast，不弹模态；
- **不需要**签署弹窗（音色不涉及协议）。

### 8. 刻意未做的部分

- **没有做「图谱可视化」界面**：`kg.stats()` 已能返回实体/关系数，但前端暂时只有 `memStats` 那一行统计，
  画图（力导向布局等）留到下一版，避免这一版改动面过大；
- **没有做实体消歧**（「张三」和「小张」仍是两个点）：需要模型判断，成本高、误合并风险大；
- **没有做谓词同义词归一**（「养了」/「养着」是两个谓词）：同上；
- **没有把 kg 接进 `get_perm` 的返回**：关系网只走 system prompt 注入，不污染记忆列表接口。

## V1.8.1 技术要点（2026-10-29）——关系网界面与四项 QA 收尾

### 一、搜索结果黑名单（`computer_use.py`）

```
_TITLE_BLACKLIST = re.compile(r"备案|ICP|许可证|公网安备|增值电信|Copyright|©", re.I)

def _bad_title(title) -> bool:
    t = str(title or "").strip()
    if not t:
        return True
    return bool(_TITLE_BLACKLIST.search(t))
```

两条解析路径都过它：`_http_parse()`（通用 HTTP 兜底，bing/cnbing 与 ddg 两个分支）和
`_http_parse_news()`（新闻专用）。空标题也丢。

**为什么这件小事值得单列**：上一版的相关度过滤只回答「像不像这个问题」，
回答不了「像新闻但根本不是新闻」。`粤ICP备12345678号` 分词后跟「新闻」类查询能撞上一大片二元组，
分数不低，于是被当成新闻喂给模型。

**顺带修掉的老 bug**：`_http_parse()` 的 bing 分支读 `m.group(3)`，可那条正则
`r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.{4,120}?)</a>'` **只有两个分组** →
一进 bing 分支就 `IndexError: no such group`，整个通用搜索兜底被上层当成「抓取失败」，
**从 V1.6.3 起一直静默失效**。现在摘要位留空，不再崩。

### 二、空查询的相关度（`computer_use.py`）

`_relevance_score(q)` 原来 `if not q: return 1.0` —— 空查询算满分。
问题是「查询词被停用词过滤光了」也会走到这里，于是什么垃圾都保留。改 `return 0.0`，
让 `_filter_relevant()` 全部丢掉。

> **但 `_news_search()` 里那条 `keep, drop = list(items), 0`（没抽出 topic 时）是故意留的**。
> 「今天有什么新闻」本来就该原样给，跟「查询词没了」不是一回事，别一起改掉。

### 三、公告版本对齐（`notice.py`）

新公告放 `NOTICES` 列表首位（首位即最新），`version` 跟 `version.APP_VERSION` 一致。

**顺带修的缺陷**：`_normalize()` 原来只挑固定几个键输出，**公告里的 `version` 被整个丢掉**，
接口层根本看不出公告是哪个版本的。现在带上了 —— 不然「公告与版本脱节」这类问题下次还是查不出来。

### 四、取消按钮文案（`desktop/src/ui/ui-compat.js`）

`okText` 传 `确定卸载` 时，兼容层原来直接拿它当按钮文案，取消就成了 `暂不` + `确定卸载`
=「暂不确定卸载」，像病句。修法是**先剥前缀再拼**：

```
var VAGUE_HEAD = /^(?:确定|确认|是否要|是否|要不要|需要|要|请)(?=\S)/;

function stripQuotes(s) {      // 「小鲸鱼」这类引号内容对按钮没意义，连壳丢掉
    return String(s || '').replace(/[「『【\[（(][^」』】\]）)]*[」』】\]）)]/g, '');
}

function stripVague(s) {
    var t = stripQuotes(s).replace(/\s+/g, '');
    var prev = null;
    while (t && t !== prev) {   // 可能连着好几层前缀，剥到不动为止
        prev = t;
        t = t.replace(VAGUE_HEAD, '');
    }
    return t;
}
```

`actionFromTitle()` 与 `confirmDialog()` 两处都过一遍，于是得到确认「卸载」/ 取消「暂不卸载」。
全站 52 个 `confirmDialog` 调用点共享这条规则；自测里有一条断言：
**任何取消文案都不许以「暂不确定」或「暂不确认」开头**。

### 五、审计盲区（`audit.py`）

见 `###3.13`。要点：`_digest()` 从 7 字段扩到 **11 字段**（加 `title`/`mac`/`signature`/`client`），
算法号落 `algo` 列、`verify()` 逐行按它选算法；再加单行封印表 `ui_sign_audit_seal`
（应有条数 + 尾哈希）抓「删末尾几行」。

**为什么非要两套检查**：哈希链只能证明「中间没被改」。删掉最后 N 行后，剩下每一行的
`prev_hash` 依然自洽，`intact` 仍是 `true`。而「把最后一条不想要的签署记录删掉」
恰恰是最省事的造假方式 —— 所以必须有尾部锚点。

### 六、刻意没做的

- **关系网没做可视化连线图**：`A →(谓语)→ B` 的表格 + 可信度色块已经够用，
  引一个图形库（d3 / vis）为了几十条边不值得，还得处理缩放与暗色主题。
- **没做实体合并/去重**：`张三` 与 `小张` 在图上仍是两个人。做消歧要模型参与，代价大于收益，先放着。
- **`kg_overview` 没做分页**：一次最多 500 条，够了。真到几千条，用户该点「刷新」看最新的，而不是翻页。
- **`ui_sign_audit` 没做修复工具**：`verify()` 只报问题，不自动修。审计库出了 `broken`，
  正确做法是查日志、别让程序自己「修好」。


## V1.8.2技术要点（2026-10-30）——无边框状态机、标题栏与本地提供商

###一、无边框为什么「配置是对的，运行时有边框」

`desktop/src-tauri/tauri.conf.json`的 `app.windows[0].decorations`一直是 `false`。
装饰是被**运行时**装回去的，链路：

```
config.json window_frameless = false ←历史版本写坏，一直没被纠正
 → app_ui.get_status / win_state → {"frameless": false}
 → bridge.js startup() → applyFrameless(false)
 → T.core.invoke('set_decorations', {flag: !UI.frameless}) → flag = true
 → lib.rs set_decorations(true) → window.set_decorations(true)
 →系统标题栏装回来；同时 body.frameless被摘掉，自绘条消失
```

结论：**「配置项写了 decorations:false」≠「运行时窗口无边框」**。要修的是「谁把它改回去了」。

###二、改动清单（一条链，五处）

|文件 |位置 |改动 |作用 |
| --- | --- | --- | --- |
| `config.py` | `load()`里 v1.3.1数据自愈之前 |老配置 `window_frameless`不是 `true`就改 `true`，并写 `_frameless_migrated = "1.8.2"` |修复已被写坏的存量配置，且**只修一次**（用户自己关掉不会被反复覆盖） |

> **易漏点**：`config.save()` 的实现是 `clean = {k: cfg[k] for k in DEFAULTS if k in cfg}` ——
>**只有登记在 `DEFAULTS` 里的键才会被写盘**。所以 `_frameless_migrated` 必须也在 `DEFAULTS` 里占一行，否则标记等于没写，用户自己关掉无边框后每次启动又会被强行改回 `True`。
| `app_ui.py` | `--frameless/--no-frameless`参数解析、`get_status`、`win_state`（共4处） | `bool(cfg.get("window_frameless", False))` →默认 `True` |与 `config.py`出厂默认对齐 |
| `bridge.js` | `var UI = { frameless: false, ... }` |初值改 `true` |读不到状态时不该退回有边框 |
| `bridge.js` | `startup()` |不再 `applyFrameless(st0.frameless)`；先 `T0.core.invoke("get_decorations")`拿 `realDeco`，再三分支：①`wantFl && realDeco===true` → `applyFrameless(true)`；②`wantFl && realDeco!==true` →只 `UI.frameless=true` + `body.classList.add("frameless")`，**不再重复 invoke**；③`!wantFl && realDeco!==true` → `applyFrameless(false)` |区分「配置意图」与「窗口真实状态」 |
| `index.html` | `renderStatus()` | `if (st && typeof st.frameless === 'boolean')`才动 DOM |后端未就绪时 `st.frameless`是 `undefined`，原来会走 `else`把自绘条摘掉 |

**副作用**：`applyFrameless()`本身保留（设置页开关 `set_frameless`仍走它），只是启动路径不再无条件调用。

###三、自绘标题栏

DOM（`desktop/src/index.html`）：`#titlebar` > `#tbDrag[data-tauri-drag-region]`（`img#tbLogo` + `span#tbText` + `span.tb-dot`）+ `.tb-actions` > `#tbMin` / `#tbMax` / `#tbClose`。
三个按钮内是12×12内联 SVG（`fill:none; stroke:currentColor; stroke-width:1.4`）。

**最大化/还原双态**：`#tbMax`里同时放 `.tb-ico-max`与 `.tb-ico-restore`，用 `data-state`属性 + CSS二选一：

```css
.tb-btn[data-state="max"] .tb-ico-restore { display: none; }
.tb-btn[data-state="restore"] .tb-ico-max { display: none; }
```

> **必须两处一起改**：`index.html`的 `wireTitlebar()`与 `bridge.js`的 `syncMaxBtn()`。
>这两个函数原来都在写 `textContent`，只改一处的话另一处会把 SVG冲掉。

**动效**：hover `background + color + translateY(-.5px)`（`.14s ease`）、`:active` `scale(.94)`、`:focus-visible`蓝描边；标题右侧5px圆点 `animation: tbBreath3.8s ease-in-out infinite`（`opacity .22 ↔ .70`）；标题栏底线用 `#titlebar::after`画1px居中对称品牌渐变。

###四、Ollama：为什么加个条目不够

`config.py`早就支持 `LOCAL_ENGINES = ("lmstudio", "ollama")`与 `LOCAL_ENGINE_DEFAULT_BASE`（`:1234` / `:11434`），
但 `PROVIDERS`（`providers.py`）里只有 `"local"`（名字「本地 LM Studio」），所以「按功能分工」的提供商下拉看不到 Ollama。

只加一个 `"ollama"`条目会踩8处 `== "local"`判据 —— Ollama会被判成**云端**，于是强制要 API Key、还会被 `api_enabled`过滤。
所以：

```python
LOCAL_KEYS = ("local", "ollama") # providers.py，判断本地/云端一律用这一对
```

改到的位置：`list_providers`（`is_cloud`）、`get_provider`、`resolve`（两处）、`set_provider`、`set_feature_model`的分支提示语、`_feature_target`、`set_feature_model`的 key校验。

**另外**：`set_provider("ollama", …)`时 `lm_engine`与 `lm_base`要一起切（`ollama` ↔ `:11434`），切回 `local`时同理切回 `lmstudio` + `:1234`；
`resolve()`与 `_entry_target()`也要按 `preset["key"]`选默认地址，否则优先级链里的 Ollama项会被 `lm_base`（LM Studio的 `:1234`）带跑。

> **`kind == "local"`的那8处不要动**（`_norm_entry` / `chain` / `add_to_chain`）：那是「优先级链项类型」，与提供商 key无关。

###五、消耗预估（`token_est.py`）

`rows()`里新增「记忆」组（跟着 `kg_enabled`走）：

|id |name |per |times |
| --- | --- | --- | --- |
| `kg_extract` |图谱记忆提取（抽关系） | `_tok(600) +150` | `est_kg_extract_per_day`（默认8，夹0~500） |
| `kg_inject` |图谱注入（关系网） | `100` | `est_msgs_per_day` |

`tts`那行 note补明「自定义音色 /声音克隆走独立进程，也一样不吃大模型 token」，`per`仍为0。

###六、刻意没做的四件事

1. **不做「记住我上次的边框选择」的二次确认** ——迁移带 `_frameless_migrated`标记，用户想关就关，程序不反复劝。
2. **不给标题栏加更多按钮**（如设置/最小化到托盘）——用户要求「不要乱到认不出来」，保持三颗。
3. **不把 `set_decorations`从 Rust侧删掉** ——设置页开关还需要它，只是启动路径不再无条件调用。
4. **不自动探测 Ollama是否在跑** ——探测会加启动延迟；`resolve()`连不上时已有的错误提示够用。



## V1.8.3技术要点（2026-10-30）——标题栏收进头部、QQ 接管的授权边界

###1. 为什么「美化标题栏」不算改对

v1.8.2 把标题栏做成了独立 `#titlebar`（`position:fixed; top:0; height:28px; z-index:100`），并且用 `body.frameless main{height:calc(100vh - 133px)!important}` 给主区域扣掉高度。用户看到的是「顶上多一条深色条，标题还写了两遍」。v1.8.3 把这条**删掉**，只留 `<header>` 一处标题，三颗按钮挂进 `<header>` 的 `.tb-actions`。

| 项 | v1.8.2 | v1.8.3 |
|---|---|---|
| `#titlebar` | 存在（fixed 独立层） | 删除 |
| `#tbDrag` / `#tbText` / `#tbLogo` / `.tb-dot` | 存在 | 删除 |
| `main` 高度 | `calc(100vh - 133px)` | 不再覆盖 |
| `header` | `margin-top:28px` 让位 | 不再偏移，自身带 `data-tauri-drag-region` |
| 按钮位置 | 独立条内 | `<header>` 右侧 `#tbActions`（id 仍是 `tbMin/tbMax/tbClose`） |

###2. 拖动区的实现约束

Tauri 只认 `data-tauri-drag-region` **所在的那个 exact target**：属性挂在一个容器上，容器里的子按钮仍然可点，不会被拖动吞掉。所以把属性同时挂在 `<header>`、左侧标题容器、`#appIcon`、`#appTitle` 四处，随便按哪都能拖。

###3. `data-state` 双图标（不能改回 textContent）

`#tbMax` 里放两个 SVG：`.tb-ico-max` 与 `.tb-ico-restore`，靠父元素属性二选一：

```css
[data-state="max"] .tb-ico-restore { display:none; }
[data-state="restore"] .tb-ico-max { display:none; }
```

`bridge.js` 的 `syncMaxBtn()` 必须跟着改成 `btn.setAttribute('data-state', mx ? 'restore' : 'max')`。**如果照旧写 `btn.textContent = ...`，SVG 会被清掉，只剩一个字符**。

###4. QQ 接管的授权边界（靠代码结构，不靠注释）

`napcat_mgr.launch(install_dir="", wait=0, allow_second_qq=False, restart=False, close_qq=False, takeover=False)`

| 逻辑 | 代码 |
|---|---|
| 是否动 QQ | `_take = bool(close_qq or takeover)` |
| 动法 | `if _take: quit_qq_client()`（`taskkill /PID x /T /F` 只打 QQ 进程） |
| 动完复查 | `if takeover and qq_running(): return False, '还有 QQ 进程没关掉…'` |
| 老守卫 | `if not allow_second_qq and not shell and not _take and not port_open(...) and qq_running():` |

| 调用方 | `takeover` |
|---|---|
| `app_ui.start_napcat()` | `True` |
| `app_ui.napcat_quit_qq_and_connect()`（`qq_client_busy` 分支 / 通用分支） | `True` |
| `app_ui.napcat_login_state()` 自动拉起 | **不传** |
| Agent 子进程 `launch(wait=12)` | **不传** |

###5. 刻意没做的

1. **不做「标题栏隐藏 / 显示」开关** —— 用户要的是「没有」，不是「可切换」。
2. **不把三颗按钮塞进下拉菜单** —— 那样窗口按钮不够直观。
3. **不给接管加二次确认弹窗** —— 用户已经点了「启动 NapCat」，再问一次等于回到 v1.6.2 的挡人。
4. **不在 `quit_qq_client()` 里扩大击杀面** —— 只打 `qq_client_procs()` 列出的 QQ 进程，不碰别的。


## V1.8.4 技术要点（2026-10-30）—— 指针、保存守卫、账号隔离

### 一、CSS 指针（`desktop/src/ui/ui-cursor.css`）

```
body.cursor-custom { cursor: url("data:image/png;base64,…") 3 2, auto; }
body.cursor-custom a, button, select, [data-sub], .btn, .chip, .acc-pill, … {
  cursor: url("data:image/png;base64,…") 16 16, pointer;
}
body.cursor-custom input[type=text], textarea, [contenteditable=true] {
  cursor: url("data:image/png;base64,…") 16 16, text;
}
body.cursor-custom [disabled], .is-busy, [aria-busy=true] { cursor: progress; }
```

- **必须 PNG**：Chrome/Windows 把 SVG cursor 当无效值丢弃，静默退回 `auto`。
- **必须带兜底**：每张图后面跟 `, auto` / `, pointer` / `, text`。
- **禁止 JS 跟随**：只改 `cursor` 属性。`mousemove` + 自绘粒子每帧跑布局，掉帧明显。
- 开关：`custom_cursor_enabled`（`DEFAULTS` + `_BOOL_KEYS`）；`applyUiLook` 里 `body.classList.toggle('cursor-custom', u.custom_cursor !== false)`。

### 二、设置项保存守卫

| 概念 | 说明 |
|---|---|
| `SG_TEXT_SEL` | `input[type=text/password/number/search/url/email]` + `input:not([type])` + `textarea` |
| `SG_CTRL_SEL` | `input[type=checkbox]` / `input[type=range]` / `select` |
| `SG_SKIP_TEXT` | `['layoutPresetName']` —— 当次用的预设名不算配置 |
| `SG_AUTO` | 34 条显式映射：控件 id → 那张卡的保存按钮 id |
| `SG_DIRTY` | `Set`，存脏的文本框元素 |
| `SG_BYPASS` | 一次性放行标志（弹窗点了「保存并离开」之后重入 `switchTab`） |

**核心：自动保存 = 替用户点那颗真按钮**

```
控件 change
   └─► sgSaverFor(el, card)
          ├─ SG_AUTO 命中 → 返回按钮
          └─ 否则：卡里没有文本框 → sgSaverOf(card)（卡内唯一一颗 Save 按钮）
                   卡里有文本框 → 不给（避免把用户没写完的文本一起提交）
   └─► sgClickSave(btn)：劫持 window.toast → btn.click() → 还原 toast
```

**为什么不自拼 `save_settings`**：一张卡里常有多颗保存键、参数各不相同（有的带 `restart`，有的先校验）。猜错 key 就是**静默失效**——界面显示存了、重启又回去。

「放弃修改」= 文本框恢复 `focus` 时记的 `el.__sgBase` + `SG_DIRTY.delete`，不写盘。

接入点：`switchTab()` 开头、标题栏 ✕（`call('win_close')` 之前）、`loadSettings()` 末尾（幂等 `sgBuild()`）。

### 三、记忆多账号隔离

| 接口 | 变化 |
|---|---|
| `memory_accounts()` **新增** | 从 `perm_memory` / `diary` / `sessions(private_*)` / `affinity` / `kg_entities` 五处汇聚；`diary.bound_accounts()` 里没数据的标 `empty:True`；按 `last` 倒序 |
| `get_memory_data(msg_limit, event_limit, session_key, user_id)` | `user_id` 非空时只查 `private_<uid>`；events 与 stats 同步过滤 |
| `get_memory_overview(user_id)` | `sessions` 只留该账号；`perm.items` / `diary.items` 过滤；`perm_total` / `perm_active` 重算 |
| `kg_overview(user_id)` / `kg_build_now(user_id)` | 本来就有参数，前端开始真的传 |

前端 `#accBar` 胶囊：`localStorage['xj_mem_account']` 记忆；`pickAccount()` **先清 `lastOverview` / `KG_DATA` / `lastMsgs` 再 `loadMemory()`**（否则切过去一瞬间会闪上一个账号的数据）；选中账号消失 → 回落「全部账号」。

**注意**：`kg_overview` 只在「关系网」子页激活时拉（`renderGraph()`），不在首屏。

### 四、`napcat_proc.py`（进程清道夫）

| 函数 | 作用 |
|---|---|
| `scan_napcat_nodes()` | 返回 `{ours, kept, denied, total, psutil}` |
| `kill_all_napcat_nodes(wait)` | 返回 `{ok, killed[], failed[], kept[], denied[], before, after, left[], need_admin, msg, elapsed}` |
| `job()` / `attach_job()` / `in_job()` | Windows Job Object（`_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000`） |
| `elevate_cleanup()` | `ShellExecuteW(None, "runas", exe, params, None, 1)`，返回码 ≤32 判失败（5 = 用户点了「否」） |
| `main(argv)` | `--clean-napcat-nodes` 入口（提权后由 `backend_server.py` 转进来，**不启服务、不抢实例锁**） |

**判据（比字面要求严）**：命令行含 `napcat` / exe 在 `runtime/napcat` 下 / 命令行走的是记录的 NapCat 目录。**光有 `index.js` 不算** —— 本机 `billion-context\dist\index.js` 会被字面判据误杀。

**防孤儿**：`napcat_mgr._spawn()` 每起一个 node 就 `attach_job`；主程序被强杀，内核回收。

**退出顺序**（`lib.rs` `stop_backend`）：先 `ask_backend_kill_nodes(&base)` → 停 av_server → 停 backend → 关壳。三条退出路径都汇到这里。

### 五、验证

界面交互自测 41/41、多账号自测 29/29、全量回归 37/37、内联块 + 外部 JS 语法检查 8 块 0 错、`cargo check` 通过、`Api` 方法数 **349**。

**测试桩**：纯浏览器测前端必须假 `window.__TAURI__`（`core.invoke` + `window.getCurrentWindow`），因为 `bridge.js` 的 `windowMethod()` 最上面就 `if (!tauriWin()) return {msg:'当前不是 Tauri 窗口'}`，只桩 `fetch` 的话所有 `call()` 都在这一层被短路。

### 六、出包

| | 值 |
|---|---|
| `小鲸鱼_x64-setup.exe` | 443,095,188 B（422.57 MB），`FileVersion`/`ProductVersion` = **1.8.4**（版本资源是 **UTF-16LE**，校验脚本要 `.encode('utf-16-le')`） |
| `小鲸鱼_V1.8.4_绿色版.zip` | 441,876,426 B，47 个条目（含 `resources/napcat_bundle.zip` 118.34 MB） |
| `小鲸鱼_V1.8.4_开源包.zip` | 7,192,930 B，207 个条目；**新增进包**：`napcat_proc.py`、`desktop/src/ui/ui-cursor.css`、`desktop/src-tauri/src/lib.rs` |
| 全程耗时 | `--skip-napcat` 8.4 分钟（tauri `release` profile 2m 50s） |

**依赖顺序的坑**：`tauri.conf.json` 的 `resources` 里写着 `resources/napcat_bundle.zip`，Tauri 的构建脚本会**校验资源存在性**。`--skip-napcat` 跳过 `build_napcat.py`，所以这个 zip 必须事先在那儿；把整个 `desktop/src-tauri/resources` 删掉会直接让 `npm run tauri build` 报 `resource path ... doesn't exist` 并退出 1。

**打包版冒烟 27/27**：`version=1.8.4` / `methods=349` / `frozen=true`；`memory_accounts` 空库返回 `{ok:true, accounts:[], total:{...}}`；`napcat_node_status` 在打包环境里也能建起 Job Object（`job.handle=1764, kill_on_close=8192`）；`get_settings.ui.custom_cursor=true`；`memory.db` 19 表、`user_version=1`。


## V1.8.5 技术要点（2026-10-30）—— 互斥、闸门、探针与增量包

### 一、语音 / 文字互斥

```python
_voice_ok = False
try:
    _vp = tts_prepare(user_id=user_id, text=text)
    if _vp:
        _voice_ok = bool(tts_send(user_id=user_id, path=_vp))
except Exception:
    _voice_ok = False
if _voice_ok:
    return            # 语音真的发出去了，不再补文字
for p in split_reply(text):     # 只有失败才走这里
    send_private(user_id, p)
```

`tts_say` 里 `edge_tts.Communicate(t, v, rate=_rate, pitch=_pitch)`，`rate` / `pitch` 来自 `tts_rate` / `tts_pitch`（默认 `+5%` / `-2Hz`），`except TypeError` 退回两参数（老版本 edge-tts 不认）。

### 二、快路由的判定顺序（顺序本身是设计）

| 顺序 | 判断 | 结果 |
|---|---|---|
| 1 | `fast_route_enabled`（默认 **False**） | 关 → `None`，一整条不参与 |
| 2 | `_fast_in_conversation(key)`：`fast_route_gap_min`（默认 10）内聊过 | 真 → `None` + DEBUG |
| 3 | 图片 | `_FAST_IMG` 随机 |
| 4 | `_fast_user_rules()`：`触发词 => 回复` | 命中 → 该回复 |
| 5 | `len<=24` 且命中 `_FAST_TIME_PAT` / `_FAST_HW_PAT` | 本地答案 |
| 6 | `route_memo` 重复模式 | 历史回复 |
| 7 | `fast_route_filler`（默认 **False**）+ `_FAST_FILLER` | 语气词预设 |

命中后调用点还会 `_fast_review(cand, key, user_text)`：拼最近 4 条上下文 + 候选，`ask_model_raw(max_tokens=60, temperature=0.7, effort="none", tag="/快路由复核")`，`fast_route_review` 关或模型异常 → 原样返回候选。

### 三、关怀循环：强制触发 + 探针

```python
sec = _silence_sec()
if sec <= 60:                      # 用户设 0 / 1 分钟 → 无条件发
    return True, "到点了（门槛 %s，直接发）" % _fmt_quiet(sec)
```

| 循环 | 探针数 | key 前缀 |
|---|---|---|
| `care_loop` | 7 | `cp_off` / `care_off` / `care_hours` / `greet_done` / `greet_busy` / `gap_greet` / `gap_ev` |
| `proactive_loop` | 5 | `pa_off` / `pa_hours` / `pa_gap` / `pa_idle` / `pa_chance` |
| `care_plan_loop` | 4 | `cp_hours` / `cp_none` / `cp_nosk` / `cp_gen` |

`_loop_log(tag, msg, key="", gap=300)`：同 key 5 分钟只打一条；表超 400 条清 6 小时前的。
`_care_plan_text` 用 `care_plan_prompt`（`_SafeDict` + `format_map`，缺占位符原样保留），留空回落 `CARE_PLAN_PROMPT`。

### 四、评论投递三档

| `moment_comment_deliver` | 评论区 | 私聊兜底 | WARN |
|---|---|---|---|
| `auto`（默认） | 先试 | 失败/异常/缺 id 时发 | 有 |
| `comment` | 只走这条 | **不发** | 有（只报告失败） |
| `private` | 不试 | 直接发 | 无 |

### 五、增量更新包

| 环节 | 实现 |
|---|---|
| 出包 | `打包发布.py` → `step_delta_zip(version)`：`_zip_index()` 比 sha256，只打变化条目 + `delta_manifest.json` + `apply_delta.ps1` + `应用增量包.bat` |
| 手动应用 | 解压到程序目录 → 双击 bat → 查进程（开着就退出 3）→ 校验 sha256（不对退出 4）→ 备份 `_backup\pkg-<ts>\` → 覆盖 → 按 `remove` 删除 |
| 程序内应用 | `updater.apply_package(path)`：只覆盖 `UPDATE_EXTS`；失败整批回滚；返回 `updated/skipped/unchanged/failed/rolled_back` |
| 在线更新 | `apply_update` 也跳过内容相同的文件（`_sha_bytes` vs `_sha_file`） |

`Api` 方法数 **350**（新增 `apply_update_package`）。
## V1.8.6 技术要点（2026-10-30）—— 卡片排序只认 DOM

### 一、为什么不写 style.order

`#tab-settings` 及各栏容器都是 `flex-1 min-h-0 flex flex-col gap-*`。CSS Flexbox 规范里，**`order` 决定视觉顺序，DOM 顺序只在 order 相同时生效**。历史上 `applyCardOrder()` 给每张卡写 `style.order = 1000 + i`，后果是：

- 拖拽（`dragover` → `insertBefore`）搬了 DOM，视觉 0 变化；
- `collectCardOrder()` 按 `style.order` 排序，读到的是旧顺序；
- 应用预设只改 `style.order`，触发浏览器的滚动锚定，把视口拽到被移动的卡片处。

### 二、现在的实现

```js
function _orderedCards(sec, list) {   // 预设 key 列表 -> 卡片数组（未列出的按现有相对顺序接在后面）
  const pool = _layoutCards(sec).slice(); const out = [];
  (list || []).forEach((k) => { /* _cardKey / _slugKey / 标题 三种匹配 */ });
  return out.concat(pool);
}
function applyCardOrder(order) {
  _layoutSections().forEach((sec) => {
    const keep = sec.scrollTop;                 // 保住视口
    const anchor = sec.style.overflowAnchor;
    sec.style.overflowAnchor = 'none';          // 关掉滚动锚定
    const want = _orderedCards(sec, o[sec.id] || []);
    _layoutCards(sec).forEach((c) => { c.style.order = ''; });
    if (!sameOrder(now, want)) want.forEach((c) => sec.insertBefore(c, after));
    if (keep) sec.scrollTop = keep;             // 写回
    requestAnimationFrame(() => { sec.style.overflowAnchor = anchor; });
  });
}
```

- `collectCardOrder()` = `_layoutCards(sec).map(c => _cardKey(c, sec))`，不再排序。
- `LAYOUT.def` 在第一次 `__applyLayoutFromSettings(ui)` 时抓取出厂 DOM 顺序，「恢复默认排版」= `applyCardOrder(LAYOUT.def)`。
- `_cardKey()` 格式保持不变（`sec.id + ':' + slug(标题)`，有 `data-cd` 时用 `data-cd`），老预设仍可匹配。
- 顺序相同的分支直接跳过搬运，避免每次进设置页白搬 25 张卡。

### 三、滚动锚定（scroll anchoring）

Chrome 的滚动锚定会在 DOM/布局变化后调整 `scrollTop`，让「锚点元素」在视口里的位置不变。卡片整栏重排时，这个机制会把视口带到被移动卡片的新位置，观感就是「整页自己滚到底」。

判据：**劫持 `scrollTop` 的 setter 记录写入栈，如果一条 JS 写入都没有而 `scrollTop` 变了，就是锚定干的。**

处置：重排前 `overflow-anchor: none`，重排后写回 `scrollTop`，`requestAnimationFrame` 里恢复原值。

### 四、验证

排版预设 **17/17 PASS**（含 `scrollTop` 差 ≤ 2px、老预设兼容、跨栏生效、切 Tab 顺序不丢）、样式回归 **14/14 PASS**（背景特效直切、切主题保字体）、全量回归 **50/50 PASS**。



### 六、增量包的结构与退出码（QA P0）

绿色版增量包 = 4 个根文件 + 一个 `payload/`：

```
小鲸鱼_V1.8.6_增量包.zip
  delta_manifest.json      # kind=release-delta, from/to, files[{path,sha256,base_sha256,size,action}], remove[]
  apply_delta.ps1          # 校验 → 备份 → 替换 → 复核（UTF-8 BOM，中文不乱码）
  应用增量包.bat            # chcp 65001 + powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_delta.ps1" %*
  README.md                # 用法 + 退出码表
  payload/                 # 真正要写入程序目录的文件（只有新增和改动过的）
```

| 退出码 | 含义 | 目标目录状态 |
|---|---|---|
| 0 | 成功（含「包被解压进程序目录」的黄字提醒路径） | 已更新，备份在 `_backup\pkg-<ts>\` |
| 2 | 缺 `delta_manifest.json` / 读不出 / 缺 `payload/` | **零改动** |
| 3 | 小鲸鱼 / backend_server / av_server 还在跑（`-IgnoreRunning` 可跳过） | **零改动** |
| 4 | `payload/` 内文件哈希对不上清单 | **零改动** |
| 5 | `-Target` 指向 `payload/` 或其内部（源=目标） | **零改动** |
| 6 | `-Target` 里没有 `小鲸鱼.exe`（认错目录，`-Force` 可跳过） | **零改动** |
| 7 | 复制后复核哈希不符 | 已改动，提示跑 `restore.ps1` |

`base_sha256` 的用途是**软提示**：脚本拿目标和它比，对不上就黄字提醒「你的程序和这个包不是同一个起点」
（比如用户跳了两个版本），但照装不误 —— 硬拒绝只在 payload 自身校验失败时发生。

### 七、后端启动的「假成功」怎么识别

`spawn_backend_once()` 原来只等 `backend_server.exe` 打印 `http://127.0.0.1:PORT` 就当成功。
但坏掉的 exe 往往能起来、也能打印端口，一接受请求就崩 —— 壳子判定成功、随后悄悄退出，用户只看到窗口一闪。

现在 `health_ok(base, ms)` 做一次真实探活：解析端口 → `TcpStream::connect_timeout` → 发
`GET /api/health HTTP/1.1`（`Connection: close`）→ 读 8KB → 判 `200` 与 `"ok"` 同时出现。
`spawn_backend_once` 在拿到地址后轮询 **20 次 × 900ms**，都不通就 `kill_tree` + 报错。
同一函数还用在单实例锁分支：拿不到锁时先探活，死了才弹「小鲸鱼好像还开着，但它的后端已经没响应了」。

### 八、去重不再静默（P2-2）

`already_replied_same()` 命中后原来直接 `return`。现在 `dup_reaction(key)` 决定要不要给一个短反应：
同一会话 90 秒内最多一次（`_DUP_REPLY_GAP = 90.0`），候选 `["？", "…", "（听着呢）", "👂", "嗯？"]`；
超过 500 条记录时清掉 1 小时前的。发送失败只打 DEBUG，不影响正常流。

## V1.8.7 技术要点（2026-10-31）—— 启动探活与「别再瞎猜」

### 一、心跳 vs 体检（这条最值钱）

| 维度 | 心跳（`/api/ping`） | 体检（`/api/health`） |
|---|---|---|
| 职责 | 只回一句「我在」 | 汇报 NapCat / edge-tts / Node 装没装 |
| 允许做的事 | 读内存常量 | 探端口、跑 `node -v`、`import edge_tts` |
| 耗时上限 | 10ms | 没上限（干净机器 1580~2000ms） |
| 谁在用 | Rust 壳的启动判活 | 设置 → 环境检查、前端状态卡 |

把体检当心跳，就会出现「后端明明活着却被判定没起来」——V1.8.6 在干净机器上 100% 复现（界面永远白屏）。

```rust
const SPAWN_PING_MS: u64 = 5000;
const SPAWN_PING_TRIES: usize = 10;

fn health_ok(base: &str, ms: u64) -> bool {
    // 解析端口 → TcpStream::connect_timeout → 发 GET /api/ping HTTP/1.1
    // → s.take(8192).read_to_end → txt.contains("200") && txt.contains("\"ok\"")
}
```

要点：`health_ok` 用的是**裸 TCP**，不依赖任何 HTTP 客户端库；判定要同时看状态码和 `"ok"` 字面量，
因为端口被别的服务占着时也可能回 200。

### 二、`/api/health` 的三层提速

| 层 | 代码 | 说明 |
|---|---|---|
| 缓存 | `_HEALTH_CACHE` / `_HEALTH_TS` / `_HEALTH_TTL = 60.0` | 60 秒内直接返回快照 |
| 预热 | `_health_warmer()`（`start_health_warmer()` 在 `main()` 里起 daemon） | 每 45 秒 `_refresh_plugins()` 一次 |
| 冷启动 | `_health_plugins()` 缓存空 → `return {}` + 起 `_cold()` 线程 | **不能当场探**，否则 1580ms 又回来了 |

`health_payload()` 拿到空 plugins 时填三个占位：

```python
{"key": "napcat", "name": "NapCat", "installed": False, "msg": "检测中…"}
```

前端几秒后重新拉一次就能看到真值（后台线程已经写进缓存）。

**不要顺手「优化」的地方**：`/api/env/check` 里那一份 plugins 必须现查 —— 用户点「查缺什么」时给缓存等于骗人。
补丁脚本定位时务必按行号/上下文确认改的是 `health_payload()` 那一处（全文件该字面量出现 2 次）。

### 三、`last_boot_error()`

```rust
fn last_boot_error() -> String {
    // 读 %LOCALAPPDATA%\xiaojingyu-console\boot.err.log
    // f.take(65536).read_to_end(&mut buf); txt.lines().rev() 取第一条非空
}
```

注意 `let f`（不加 `mut`），只读 `File` 加 `mut` 会报 `unused_mut`。
错误框拼装时 `msg` 是 `String`，`message_box(&msg)` 才不报类型错。

### 四、`_fnum_live(key, default)`

老 `_fnum` 的实现是 `_CFG.get(key, dflt)` —— 它的第一个参数是**值**不是 key。
所以 `_fnum(_live_cfg("care_plan_min_gap_min", 25), 25)` 等于永远读模块加载时的快照，
用户把「最小间隔」改成 0 或 25 都不生效。

```python
def _fnum_live(key, default):
    v = _live_cfg(key, default)
    if v is None or v == "":
        return float(default)
    return float(v)          # 0 是合法值
```

五处调用点：`moment_check_interval_min` / `checkin_poll_sec` / `checkin_silence_hours` /
`care_plan_max_per_day` / `care_plan_min_gap_min`；`_care_gap_sec()` 与模块级
`CARE_PLAN_MIN_GAP_MIN` 都改成 `is None` 判空。

### 五、设置项保存闸门

```js
let isSettingsLoaded = true;   // 初值 true = 没在加载；给 false 会永久拦住自动保存

async function loadSettings() {
  isSettingsLoaded = false;
  try { /* ... */ } finally { isSettingsLoaded = true; }
}

async function call(method, ...args) {
  if (method === 'save_settings' && !isSettingsLoaded) {
    toast('设置还在加载，稍等一下再改', 'warn');
    return { ok: false, msg: 'settings loading' };
  }
  /* ... */
}
```

闸门放在 `call()` 而不是每个保存按钮上，是因为设置页有 63 处 `call('save_settings')`。
`loadSettings()` 里 `if (!r.ok)` 的早退分支也必须置回 true，否则一次加载失败会把保存永久锁死。


## V1.8.8 技术要点（2026-10-31）—— 假值陷阱与外观状态机

### 一、`_ndef()`：把「0」和「没填」分开

```python
def _ndef(v, d):
    """v1.8.8：0 / 0.0 / False 都是合法配置值，只有 None 和空串才回落默认。"""
    if v is None:
        return d
    if isinstance(v, str) and not v.strip():
        return d
    return v
```

替换模式（只在 `LEGIT_ZERO` 白名单内）：

```
int(cfg.get("care_plan_min_gap_min") or 25)
→ int(_ndef(cfg.get("care_plan_min_gap_min"), 25))
```

| 项 | 值 |
|---|---|
| 改动处数 | 72（`app_ui` 32 / `agent` 17 / `token_est` 12 / `diary` 3 / `mem_transfer` 3 / `subconscious` 3 / `usage` 2） |
| `LEGIT_ZERO` 白名单 | 44 个键（从 `config.DEFAULTS` 的 109 个数值键里筛「设 0 有意义」的） |
| 故意不动 | 34 处（`max_tokens`/`top_p`/`request_timeout`/`affinity_max`/`web_port`/`draw_max_side` …） |
| 助手副本 | 7 个文件各一份（这些模块互不 import，宁可重复） |

### 二、外观状态机

#### 1) 唯一真源

```js
function _store() {
  if (!settingsCache || typeof settingsCache !== 'object') settingsCache = {};
  const s = settingsCache;
  if (!s.ui || typeof s.ui !== 'object') s.ui = {};
  if (!s.effects || typeof s.effects !== 'object') s.effects = {};
  if (!s.theme || typeof s.theme !== 'object') s.theme = {};
  if (!s.theme.effects || typeof s.theme.effects !== 'object') s.theme.effects = {};
  return s;
}
```

**永不为 null**：这就是「切主题后字体消失」的根治点 —— 老代码 `applyUiLook((settingsCache || {}).ui || {})`
在缓存没填时传了 `{}`，`font_family` 为空就 `removeProperty('--ui-font')`。

#### 2) 优先级只在 `fxEffective()` 里算一次

| 字段 | 来源顺序 | 说明 |
|---|---|---|
| `snow` / `leaves` / `edge` | 主题 → 出厂 | 用户没有对应开关 |
| `depth` | `STORE.effects.snow_depth` → 主题 `depth` → `null` | 厚度滑块 |
| `card_snow` / `flakes` | `STORE.effects` → 主题 → `true` | 用户 `false` 必须赢 |
| `card_effect` / `bg_effect` | 只认 `STORE.effects` | `bg_effect === ''` = **跟随主题**，不是「没设置」 |

判定只有一条：`v === undefined || v === null` 才算「没选过」。**`false` 和 `''` 都是显式选择。**

#### 3) 三条渲染管线（顺序固定）

```js
function renderAll() { renderTheme(); renderFx(); renderLook(); }
```

主题先铺 CSS 变量 → 特效叠用户选择 → 外观最后压字号/字体/强调色。

#### 4) 写入通道 + 序号防竞态

```js
async function commitFx(patch, sp) {
  const s = _store();
  const my = ++_seqFx;
  const prev = {};
  Object.keys(sp || patch).forEach(function (k) { prev[k] = s.effects[k]; s.effects[k] = (sp || patch)[k]; });
  renderFx();
  let r = null;
  try { r = await call('save_settings', patch); } catch (e) { r = { ok: false, msg: String(e) }; }
  if (my !== _seqFx) return r;            // 后面又点了一次 → 这次作废
  if (!r || r.ok === false) { Object.keys(prev).forEach(function (k) { s.effects[k] = prev[k]; }); renderFx(); toast(...); }
  return r;
}
```

`commitLook` 同构（`_seqLook`）。`commitTheme` 先 `apply_theme`，成功后整体替换 `STORE.theme`
再 `renderAll()`；`my !== _seqTheme` 时直接丢。

**两个参数的语义**：`sp` 是「要改的 STORE 键」，`patch` 是「要落盘的 config 键」。两套名字
历史上就不一样（`STORE.effects.flakes` ↔ `snow_flakes_enabled`），映射表：

```js
const FX_CFG_KEY = { snow_depth: 'snow_depth', card_snow: 'card_snow_enabled',
  flakes: 'snow_flakes_enabled', card_effect: 'card_effect', bg_effect: 'bg_effect' };
const LOOK_CFG_KEY = { font_scale: 'ui_font_scale', font_family: 'ui_font_family',
  accent: 'ui_accent', text_select: 'ui_text_select', card_resize: 'ui_card_resize' };
```

#### 5) `resetLook()`：四样一起清

| 清哪 | 内容 |
|---|---|
| 落盘值 | `LOOK_FACTORY` / `FX_FACTORY` 全字段 → `save_settings` |
| CSS 变量 | `--ui-font` / `--accent` / `--snow-h` / `--snow-drip` / `--snow-off`、`body.style.zoom` |
| `dataset` | `themeFx` / `cardFx` / `snowCard` / `snowFlakes` / `themeEdge` / `melt` |
| 节点 | 移除 `#snowLayer` |
| 控件 | `uiFontScale` / `uiFontFamily` / `uiAccent` / `snowDepth` / `cardFx` / `bgFx` 回填 |

### 三、改造的 7 个调用点

| 位置 | 改成 |
|---|---|
| 主题「应用」按钮 | `await commitTheme($('themeSel').value)` |
| `cardFx` / `bgFx` 下拉 | 构造 `store`/`patch` → `await commitFx(patch, store)` |
| `swCardSnow` / `swFlakes` | `commitFx`，失败 `swSet` 回滚 |
| 「保存外观」 | 先 `commitFx` 再 `commitLook`，任一失败即停 |
| 厚度滑块 `input` | 只改 `STORE.effects.snow_depth` + `applySnowDepth` + `renderFx()`，**不落盘** |
| `loadSettings()` 末尾 | `renderAll()`（原来单独调 `applyUiLook(u)` / `applyThemeFx(...)`） |
| `boot()` 首屏 | 同上，合成一次 `renderAll()` |

### 四、验证

| 项 | 结果 |
|---|---|
| 假值陷阱（全 0 配置） | **37/37 PASS** |
| 状态机 Playwright（字号→切主题→切特效→关雪效→连点→恢复默认→reload） | **34/34 PASS** |
| 增量包端到端 | **16/16 PASS** |
| `index.html` 内联块 | 8 块 / 0 错 / 15581 行 |


## V1.8.9 技术要点（2026-10-31）—— 卡片身份、物理重排与滚动锚定

### 一、卡片身份：`data-card-id`

43 张卡片全部注入 `data-card-id`：有 `id` 的按 camelCase→kebab（`cardLook` → `card-look`、`cardTokenEstSwitch` → `card-token-est-switch`），没 `id` 的按 `card-<tab>-<nn>`（`card-settings-01` … `card-settings-18`、`card-model-01` … `06`、`card-av-01` … `07`、`card-emotion-01` … `03`）。脚本断言过全局唯一。

```js
function _cardKey(c, sec) {
  return (c && c.dataset && c.dataset.cardId) || (sec.id + ':' + _slugKey(_cardTitle(c)));
}
```

### 二、物理重排（`applyCardOrder`）

```js
const keep = sec.scrollTop;
const anchorStyle = sec.style.overflowAnchor;
sec.style.overflowAnchor = 'none';
try { const ae = document.activeElement;
      if (ae && ae !== document.body && sec.contains(ae) && ae.blur) ae.blur(); } catch (e) { }
const tail = cards[cards.length - 1].nextSibling;
cards.forEach((c) => { if (c.parentNode === sec) sec.removeChild(c); });
want.forEach((c) => { sec.insertBefore(c, tail); });
sec.scrollTop = keep;
requestAnimationFrame(() => { sec.scrollTop = keep; sec.style.overflowAnchor = anchorStyle; });
```

三件事缺一不可：**关锚定**（否则 Chrome 把视口拽到被搬走的卡片）、**blur**（否则为保焦点同样拽）、**无条件写回 `scrollTop`**（`if (keep)` 会漏掉「本来就在顶部」的情况）。

### 三、缺 id 的兼容

`_orderedCards(sec, list)`：先按 `data-card-id` 取，取不到再用 `_slugKey(标题)` / 原串兜底（老预设），最后 `out.concat(pool)` 把剩下没被点名的卡片追加到末尾 —— 新版本加卡片不会崩、也不会乱序。

### 四、锁定布局

| 层 | 实现 |
|---|---|
| 配置 | `config.py` `DEFAULTS["lock_layout"] = False` + `_BOOL_KEYS` |
| 接口 | `get_settings()["ui"]["lock_layout"]` |
| 前端状态 | `LAYOUT.lock`，`__applyLayoutFromSettings()` 里 `LAYOUT.lock = !!u.lock_layout` 并回填开关 |
| 生效 | `setCardDrag(on)` 里 `can = on && !LAYOUT.lock`；`body.layout-locked`；逐卡 `setAttribute('draggable', can)` |
| 落盘 | `#swLockLayout` 的 `change` → `call('save_settings', {lock_layout})`，失败回滚 `checked` |

### 五、验证

CDP 全链路 **18/18 PASS**（含「应用预设后视口原地不动」）；全量回归 **37/37 PASS**（68 个 `.py` ast、9 个 JS、8 个内联块、16 条前端探针、`Api` 方法数 **350**）。

## V1.8.10 技术要点（2026-10-31）—— 保存闸门、追问状态机、base 自愈

### 一、`isSettingsLoaded` 闸门（前端保存的总开关）

```js
async function loadSettings() {
  isSettingsLoaded = false;
  try { return await _loadSettingsBody(); }
  finally { isSettingsLoaded = true; }   // 成功失败都开闸
}

// call() 里：
if (method === 'save_settings' && !isSettingsLoaded) {
  const _t0 = Date.now();
  while (!isSettingsLoaded && Date.now() - _t0 < 4000)
    await new Promise((r) => setTimeout(r, 120));
  if (!isSettingsLoaded) console.warn('[call] 设置还没加载完，先放行', method);
}
```

病根：`_loadSettingsBody()` 里 `loadUninstall(); await loadNapcat();` 不在 try 内，探活超时就抛，
末尾开闸被跳过 → 之后所有保存静默失败。**凡是关闸/开闸必须 `try/finally`，闸门本身要有超时。**

### 二、预设：服务端为准 + 本地镜像 + 可证伪提示

| 环节 | 实现 |
|---|---|
| 存 | `LAYOUT.presets[nm] = collectCardOrder()`（DOM 顺序 → `data-card-id` 数组）→ `save_settings({ui_layout_presets})` → 同时 `localStorage.setItem('xj_layout_presets', ...)` |
| 应用 | 内存没有就回落镜像；`_layoutSig()` 取前后快照；`applyCardOrder()` 物理重排；`_layoutDiffCount(a,b)` 逐位比对 |
| 提示 | 相同 → 「已应用：X（和当前顺序一样，没有卡片需要挪）」；不同 → 「已应用：X（N 张卡片换了位置）」 |
| 开机 | `__applyLayoutFromSettings(u)` 用镜像补齐服务端缺的预设 |

`_layoutSig()` 形如 `tab-settings=card-look,card-effects|tab-model=card-model-01,...`，栏间 `|`、卡间 `,`。

### 三、追问状态机（`agent.py`）

```
send_to_session() ──发出任何消息──> followup_mark(sk, text)   # 记「我问过、还没回」
process_message() ──收到对方消息──> followup_clear(sk)        # 清掉
followup_loop()   ──每 40 秒───> 到点？次数没满？窗口内？时段内？──> _fu_ask() ──> send_to_session()
```

| 常量/键 | 默认 | 说明 |
|---|---|---|
| `followup_enabled` | `True` | 总开关 |
| `followup_gap_min` | `8` | 隔几分钟追一次（1~240） |
| `followup_max` | `3` | 最多追几次（1~6） |
| `followup_window_min` | `180` | 超过多久算话题结束（10~1440） |
| `followup_private_only` | `True` | 只私聊追问 |
| `_FOLLOWUP_INTERNAL` | — | 追问自己发出的消息不打标记，防自激 |
| `followup.json` | — | 落盘文件（`.tmp` + `os.replace` 原子写） |

`_FU_PROMPT` 约束：一句 20 字内、不重复原话、不问「在吗」、不说「你怎么不回我」、话题说完只输出 `END`。

### 四、`bridge.js` base 自愈

| 函数 | 作用 |
|---|---|
| `pingBase(b, ms)` | `GET /api/ping`，默认 800ms 超时，异常算 false |
| `invalidateBase()` | `base=''; basePromise=null;` |
| `resolveBase()` | `__API_BASE` → `backend_info` → 扫 `CANDIDATE_PORTS`，**每一步都先探活**；全失败返回 5701 但不缓存；`finally { basePromise = null; }` |
| `rpcOnce()` | 原 `rpc` 逻辑；网络失败打 `__net: 'timeout' / 'down'` |
| `rpc()` | 见 `__net` 就 `invalidateBase()`；`down` 重试 3 次（退避 `300 + attempt*700` ms） |
| 心跳 | 20 秒一次 `pingBase`，连续 2 次失败 → `invalidateBase()` + `resolveBase()` |

### 五、验证

| 项目 | 结果 |
|---|---|
| 全量回归 | 12/12 PASS（35 `.py` / 5 JS / 8 内联块 / 35 探针 / `Api` 350） |
| 追问模块 | 27/27 PASS |
| 界面（预设 / 生图 / 追问 / 闸门） | 26/26 PASS |
| 掉后端自动重连 | 8/8 PASS |

## V1.8.11 技术要点（2026-10-31）—— 接线、自愈与一键安装

### 一、`isSettingsLoaded` 闸门与 try/finally

```js
async function loadSettings() {
  isSettingsLoaded = false;
  try { return await _loadSettingsBody(); }
  finally { isSettingsLoaded = true; }      // 抛异常也必须放开，否则保存全被吞
}
```

`call()` 侧再加超时兜底（4 秒），并且**只在设置真的没加载完时**才拦，等不到就放行 + `console.warn`。

### 二、追问登记点

`send_private_human()` / `send_group_human()` 是唯一出口，登记放在两处：

- 语音发送成功后的早退分支
- 文字片段循环结束后

```python
def followup_note(session_key, text="", uid=""):
    try:
        followup_mark(session_key, text, uid=uid)
    except Exception as e:
        log(f"[追问] 登记待回话失败（忽略）: {e}", "DEBUG")
```

`_FOLLOWUP_INTERNAL` 在 `followup_loop` 发消息时置位，避免自己发的追问被登记成新的待回话。

### 三、静默开口（`_care_idle_try`）

| 步骤 | 说明 |
|---|---|
| 门槛 | `care_idle_enabled`、`care_plan_source != "manual"` |
| 静默判定 | `mem.last_message_ts(sk, role="user")` 距今 ≥ `care_idle_silence_hours`（0.2~72） |
| 防撞车 | `_care_lock` + `_last_care_at` + `_care_gap_sec()`（与定时问候、查岗共用） |
| 每日上限 | `_care_day_allow()` / `_care_day_bump()` |
| 生成 | `_care_idle_text()`：取最近 3 轮对话 + `_ask_text(..., 200, temperature=0.9, tag="/主动开口")`，首词 `END` 视为不开口 |

挂载点：`care_plan_loop()` 里 `due = mem.get_due_plans()` 为空的 `if not due:` 分支。

### 四、`bridge.js` base 自愈

| 函数 | 职责 |
|---|---|
| `pingBase(base, ms)` | `GET <base>/api/ping` 探活（默认 800ms） |
| `invalidateBase()` | 清 `base` 与 `basePromise` |
| `resolveBase()` | `__API_BASE` → `backend_info` → 扫 `CANDIDATE_PORTS`，**每个候选都先探活**；兜底地址不缓存 |
| `rpcOnce()` | 原 `rpc`，catch 里打 `{ __net: 'timeout' \| 'down' }` 标记 |
| `rpc()` | 外层：`down` 最多重试 3 次（`300 + attempt*700` ms），返回前删标记 |
| 心跳 | 20 秒一次；连续 2 次不通 → `invalidateBase()` + `resolveBase()` |

### 五、`pq_stats.json`（跨进程队列状态）

`agent.pq_state()` 在 `_PQ_LOCK` 里组 `{fg, bg_wait, bg_run, enabled}`，
1 秒节流写 `<数据目录>/pq_stats.json`（`at` 是 epoch 秒，先写 `.tmp` 再 `os.replace`）。
`app_ui.get_perf()` 读该文件，`age > 120` 标记 `stale`。
前端 `loadPerf()` 用 `Array.isArray(ag.children)` 兜住类型，避免 `.reduce` 崩掉整块面板。

### 六、`qzone_interact._to_ts()`

容错：`int/float`、纯数字串、毫秒串、`%Y-%m-%d %H:%M:%S`、`%Y-%m-%d %H:%M`、
`%Y/%m/%d …`、`%Y-%m-%dT%H:%M:%S`、`%Y-%m-%d`、「x 秒/分钟/小时/天/周前」、「M月D日 HH:MM」。
认不出返回默认值，**绝不抛**。

### 七、增量包一键安装（`DELTA_PS1`）

`Test-AppDir($p)` 判 `$p\小鲸鱼.exe` 是否存在；查找顺序：
包目录往上 5 层 → 桌面/公共桌面/开始菜单 `*.lnk`（`WScript.Shell.CreateShortcut().TargetPath`）→
常见安装位置 → `Read-Host` 交互。退出码不变（2/3/4/5/6/7）。

## V1.8.12 技术要点（2026-10-31）—— 三档引擎、情绪量化与画像沉淀

### 12.1 `sing.py`：一首歌怎么被唱出来

```
点歌意图 is_sing_request() -> (True, "稻香")
   ↓ lyrics_from_search(name)   tool_web_search -> parse_lyrics()
   ↓ polyphone_fix(line)        50 条规则，homophone / pinyin / off
   ↓ order = {"cloud": ["cloud","svc","read"],
              "svc":   ["svc","read"],
              "read":  ["read"]}[sing_engine]
   ↓ 每档试，文件 > 1024 字节才算成功；成功的档位写进 note
   ↓ send_voice(path=mp3)
```

| 函数 | 职责 |
|---|---|
| `polyphone_fix(text, mode=None)` | 返回 `(修正后, 命中列表)`；`mode=None` 读 `sing_polyphone` |
| `parse_lyrics(raw, max_lines=14)` | 去时间轴 → **整行丢署名** → 去括号/编号 → 2~40 字 → 去重 |
| `lyrics_from_search(name)` | 三种签名依次 try `tool_web_search`，把 title+snippet 拼成 blob |
| `synth_read(lines, cfg)` | 逐行 `edge_tts.Communicate(..., rate, pitch).save()`，最后字节拼接 |
| `_post_audio(url, payload, key)` | 魔数认 `PK/RIFF/ID3`，否则 JSON 里取 base64 或 url |
| `sing(...)` | 组合上面全部，返回 `{ok, path, engine, note, lines, fix}` |
| `sing_status()` | 给前端的状态快照（三档、就绪、规则条数） |

### 12.2 主动回复：按秒存，按单位显示

| 配置键 | 默认 | 校验 | 说明 |
|---|---|---|---|
| `care_idle_silence_sec` | 1800 | `(int, 60, 604800)` | **唯一真源** |
| `care_idle_silence_unit` | `minute` | 字符串白名单 | 只是显示单位 |
| `care_trigger_by_emotion` | `True` | `_BOOL_KEYS` | 情绪不对时门槛降到 35%（下限 120 秒） |

### 12.3 情感画像表

```sql
CREATE TABLE IF NOT EXISTS user_emotion_profile(
  user_id TEXT PRIMARY KEY, traits TEXT, landmines TEXT, expressions TEXT,
  notes TEXT, mood_bias TEXT, samples INTEGER, updated_at REAL);
```

`prompt_block()` 的输出（没画像返回空串，不占 token）：

```
【你摸透的这个人】（别直接复述这些词，用它决定怎么说）
性格：…
最近基调：…
他/她的情绪表达：…
别踩的雷：…
最近观察：
- 2026-10-31 提到加班语气变冷
```

### 12.4 情绪量化的三段处理

1. 词库命中 → 基础权重；
2. 回看前 5 个字，否定词命中则跳过（全是否定 → 「否认情绪」34 分；「别/甭/勿」→「安慰」48 分）；
3. 程度副词加减：`_DEG_HIGH` +14 / `_DEG_MID` +5 / `_DEG_LOW` −12。

`emotion_mode` 三档：`local` 直接返回本地；`model`/`both` 再让模型复核并覆盖（超时退回本地），返回体里带 `mode`。

### 12.5 队列快照

`pq_state()` 现在由 `_pq_enter()` / `_pq_exit()` 调用，1 秒节流写 `<数据目录>/pq_stats.json`：

```json
{"fg": 3, "bg_wait": 1, "bg_run": 2, "enabled": true, "threads": 26, "at": 1730000000}
```

`app_ui.get_perf()` 读它并给出 `age`/`stale`（>120 秒算过期）；数据库那格改成 sqlite 只读直查行数。

## V1.8.13 技术要点（2026-10-31）—— 签名、关键字传参与三路歌词提取

### 13.1 `save_emotion_center` 的签名契约

```python
def save_emotion_center(preset=None, enabled=None, timeout=None, prompt_text=None,
                        mode=None, profile_enabled=None):
```

`Api` 包装：

```python
    def save_emotion_center(self, preset=None, enabled=None, timeout=None, prompt_text=None,
                            mode=None, profile_enabled=None):
        # v1.8.13：一律关键字传参。以前是位置传参，签名一改（少了 preset）
        # 就会整体错位 —— 用户看到的是「整页保存全废」的 NameError。
        return save_emotion_center(preset=preset, enabled=enabled, timeout=timeout,
                                   prompt_text=prompt_text, mode=mode,
                                   profile_enabled=profile_enabled)
```

| 入参 | 取值范围 | 说明 |
|---|---|---|
| `preset` | `normal` / `rational` / `gentle` / 自定义键 | 判断预设，非法值返回 `ok:false` |
| `enabled` | bool | 情感中枢总开关 |
| `timeout` | float 秒 | 大模型判定超时 |
| `prompt_text` | str | 判断口径覆盖 |
| `mode` | `local` / `model` / `both` | 非法值返回 `ok:false`，不回退 |
| `profile_enabled` | bool | 情感画像总开关 |

### 13.2 歌词提取三路并进（`sing.py`）

```
搜索（歌名 + "歌词 完整版" / "歌词 全文" / "歌词"）
  └─ parse_lyrics(标题+摘要)          ── 够 5 行就收
      └─ 抓 url（最多 4 个）
          ├─ _lyric_blocks_from_html()  HTMLParser 按标签深度收 lyric/gciba/songtext/content/verse
          └─ 退回整页：去 script/style → <br> 换行 → 去标签
              └─ parse_lyrics(页面文本)
                  └─ 仍 < 5 行 → _heuristic_split() 按标点切句补足
```

| 函数 | 关键点 |
|---|---|
| `_lyric_blocks_from_html(html)` | `HTMLParser` + 标签栈；`class`/`id` 命中关键词即开始收；`<br>` 与块级标签转 `
`；`convert_charrefs=True` 自动解实体 |
| `_fetch_lyric_page(url, timeout=8)` | 桌面 UA + `Accept-Language: zh-CN`；读前 600KB；`utf-8` → `gbk` → `gb18030` 依次试解码 |
| `_heuristic_split(blob, want=8)` | 按 `，。！？；、/|·…` 切；保留 2~30 字且含中文；上限 `want*2` |

`note` 文案：`< 3` 行 → 「只搜到 N 行（网页歌词不全，先这么唱）」；否则「从网上搜到 N 行」。

### 13.3 预设命中率（`desktop\src\index.html`）

```js
function _presetMatchCount(order) {   // 返回 { total, hit }
  const pool = new Set();
  _layoutSections().forEach((sec) => _layoutCards(sec).forEach((c) => pool.add(_cardKey(c, sec))));
  let total = 0, hit = 0;
  Object.keys(order || {}).forEach((sid) => (order[sid] || []).forEach((k) => {
    if (!k) return;
    total += 1;
    if (pool.has(k)) hit += 1;
  }));
  return { total: total, hit: hit };
}
```

`applyPreset` 的判定顺序（**不可调换**）：

1. `total > 0 && hit === 0` → 黄字「⚠️ 预设中的卡片均不存在…」（6 秒）
2. `_sigAfter !== _sigWant` → 红字「页面顺序没能完全对上」
3. `_sigBefore === _sigAfter` → 「和当前顺序一样」
4. 否则 → 「N 张卡片换了位置」

### 13.4 刻意没做的

- 没给歌词提取加缓存：一次点歌最多抓 4 个页面，加缓存反而要处理失效。
- 没做「歌词对齐时间轴」：朗读模式是逐行合成，不需要时间轴；云端引擎由对方决定。
- 没改预设的数据结构：仍然是 `{栏 id: [卡片 id, …]}`，老预设仍能读，只是命中 0 时会明确报错。

## V1.8.14 技术要点（2026-11-01）—— 开关时序、落库顺序与「不兜底」

### 14.1 三个开关的时序真相

`click` 事件在 checkbox 的 `checked` **已经翻转之后**才派发。所以：

| 写法 | 结果 |
|---|---|
| `const on = !swGet(id)` | 永远等于旧值 → 保存旧状态 → 界面重载后弹回 |
| `const on = el.checked` | 新值 ✅ |
| `const on = !el.classList.contains('on')`（div 开关） | 新值 ✅（div 没有原生状态） |

统一入口 `desktop\src\index.html` 的 `swNext(id)`，四处调用点：
`swEmoProfile`、`swEmoEngine`、`swPerfAuto`、`swCardSnow`/`swFlakes`。

### 14.2 预设选择：先落库，再刷新，先给反馈

```
sel.change → call('save_emotion_center', key, null, null)
            ├─ 失败 → toast + loadEmotionCenter()（回滚显示）
            └─ 成功 → 改徽标文案（立刻可见）→ toast → loadEmotionCenter()（换判断口径）
```

关键点：`save_emotion_center` 的**位置参数顺序是 `(preset, enabled, timeout, prompt_text, mode, profile_enabled)`**，
所以「只存预设」必须写成 `call('save_emotion_center', key, null, null)`，
少写两个 `null` 会把 `key` 之后的 `undefined` 当成 `enabled` 传下去。

### 14.3 歌词：黑名单 + 去掉兜底

`sing.py` 三道闸：

1. `_NAV_LINE`（27 个导航/版权/协议词）—— `parse_lyrics` 里整行命中就丢；
2. `_fetch_lyric_page` **不再整页去标签**，抠不到 `.lyric-content` 这类区块就返回空；
3. `_heuristic_split` 切出的短句也过黑名单，且只吃 `snippet/desc/content`（`snip_blob`），
   不吃 `title`（标题形如「稻香 歌词」，切出来必是噪声）。

失败路径的语义：`lyrics_from_search` 返回 `([], "搜到了但没抽出歌词")`，
上层 `sing.sing()` 得到 `ok=False`，`_sing_perform` 会老实说「想给你唱来着，但没成：…」。

### 14.4 重复 DOM id

`swEmoForecast` 在工具栏和设置页各有一份，`getElementById` 只返回第一个。
设置页那个改名 `swEmoForecast2`，`_syncEmoForecast(on)` 双向同步，保存失败时两边一起回滚。

### 14.5 自测怎么才能测出「弹回」

桩后端必须**有状态**：`page.route` 拦下 `save_settings` / `save_emotion_center` 时更新 `ST` 字典，
`emotion_center` / `get_settings` 从 `ST` 读。否则界面重载后回到初始值，
「弹回」和「没弹回」在测试里看起来一模一样。

## V1.8.15 技术要点（2026-11-01）—— 歌词四道闸

### 15.1 数据来源只认正文

```
搜索结果 items[]
  ├─ title    ← 彻底丢弃（形如「稻香 - 完整版歌词 - 百度文库」）
  └─ snippet/desc/content → blob → parse_lyrics()
        └─ 不足 5 行 → 抓 items[].url 页面 → _lyric_blocks_from_html()
              └─ 仍不足 5 行 → _heuristic_split(blob) 补
                    └─ 仍不足 5 行 → 返回 []（放弃）
```

### 15.2 `_TITLE_LINE`（整行丢弃）

```
完整版|歌词|LRC|百度文库|百度知道|百度百科|文库|知道|
在线试听|免费下载|伴奏|铃声|简谱|吉他谱|钢琴谱|MP3|
无损|高清|专辑|作词|作曲|原唱|翻唱|试听|下载|版权所有|
百度|QQ音乐|网易云|酷狗|酷我|虾米|咪咕|搜狐|新浪|知乎|豆瓣|哔哩|
\d+\s*万?\s*次播放|收藏|分享|举报
```

放在 `parse_lyrics` 的 `_NAV_LINE` 之后、去括号之前。

### 15.3 搜索失败的判定

```python
r = call()
if isinstance(r, dict):
    _items0 = r.get("items") or r.get("results") or r.get("list") or []
    if r.get("ok") is False or not _items0:
        r = None
        continue        # 换下一个 query / 换下一种调用签名
```

并且**删掉了** `blob = json.dumps(r, ensure_ascii=False)` 这条兜底 ——
它会把整个返回体（含 `"ok": false, "msg": "搜索超时"`）当歌词送进 TTS。

### 15.4 最少行数

```python
if len(lines) < 5:
    return [], "只搜到 %d 行，不够唱" % len(lines)
```

`sing()` 拿到空歌词返回
`{"ok": False, "msg": "没搜到这首歌的完整歌词，换一首试试吧"}`，
`_sing_perform` 会把这句原话发给用户。

### 15.5 `perf_auto_refresh`

| 层 | 改动 |
|---|---|
| `config.py` | `DEFAULTS["perf_auto_refresh"] = True`，登记进 `_BOOL_KEYS` |
| `app_ui.get_settings()` | `ui` 段输出 `"perf_auto_refresh": bool(cfg.get("perf_auto_refresh", True))` |
| `index.html` | 点击 → `save_settings({perf_auto_refresh})` + 失败回滚；`loadSettings` 按 `st.perf_auto_refresh` 回填并 `restartPerfTimer()` |

## V1.8.16 技术要点（2026-11-01）—— 预设三段应用与词表进包

### 16.1 预设 v2 的数据结构

```js
function collectPreset() {
  const st = _store();
  const fx = fxEffective(st.theme.effects || {});
  return {
    v: 2,
    theme: { key: st.theme.key || '', name: st.theme.name || '' },
    fx: { snow_depth: (fx.depth === null ? 55 : fx.depth),
          card_snow: fx.card_snow, flakes: fx.flakes,
          card_effect: fx.card_effect, bg_effect: fx.bg_effect },
    layout: collectCardOrder(),
    at: Date.now()
  };
}
```

兼容：`_isPresetV2(v)` 看有没有 `theme`/`fx`/`layout`；老预设（纯 `{tabId:[id]}`）由
`_presetLayout(v)` 原样返回，所以历史预设不会失效。

### 16.2 应用顺序（applyPresetFull）

1. **主题**：`v.theme.key !== 当前 key` 才 `await commitTheme(key)`；随后**重新取**
   `document.getElementById('themeSel')` 再赋 `value`（卡片重排会搬走 DOM，闭包里的引用可能失效）。
2. **特效**：按 `Object.keys(FX_CFG_KEY)` 组两个对象 —— `sp`（STORE 键）与 `patch`（config 键），
   调 `await commitFx(patch, sp)`，再同步 `#bgFx` / `#snowDepth`+`#snowDepthVal` / `#cardFx`。
3. **布局**：`_layoutSig()` 前后比对 + `_layoutSigFrom(layout)` 校验目标签名，不一致就重排一次，
   最后 `await saveCardOrder(layout)`。

返回 `{theme, fx, cards, notes}`，toast 用「主题「X」· N 项特效 · N 张卡片换了位置」如实汇报。

### 16.3 特效取值的合法集合

`window.FX_KINDS` 只有四个键：`snow` / `leaf` / `sakura` / `rain`。
`applyThemeFx()` 里：

```js
const _forcedBgRaw = String(F.bg_effect || '');
const _forcedBg = (_forcedBgRaw && window.FX_KINDS[_forcedBgRaw]) ? _forcedBgRaw : '';
const kind = _forcedBg || (leaves ? 'leaf' : 'snow');
document.documentElement.dataset.themeFx = kind;   // 永远是 FX_KINDS 的键
```

所以：**`bg_effect` 传 `leaves` 会被静默丢弃并回落成 `snow`**；`dataset.themeFx` 也永远不会是
空串 —— 「跟随主题」只体现在 `#bgFx` 的 `value === ''` 上。

### 16.4 词表进包与内置兜底

| 位置 | 作用 |
|---|---|
| `build_backend.py` | `--add-data <HERE>/langs<sep>langs` → 运行时 `_MEIPASS/langs` |
| `i18n_builtin_en.py` | `UI`（1335 条）+ `DICT`（89 条），编进程序，由脚本从 `langs/en-US.json` 生成 |
| `i18n.ensure_dir()` | 文件缺失时用内置表写完整结构（`dict` + `ui` 两段都在） |
| `i18n._load_dir()` | 磁盘条目少时用内置表补齐（`merged.update(cur)`，用户改过的优先） |
| `app_ui.get_ui_lang` | 非默认语言但 `ui` 为空 → `ok:False` + 「没有 xx 的界面词表…」 |

### 16.5 翻译查找：空白归一化

```js
let UI_NORM = {};
function _uiBuildNorm() {
  UI_NORM = {};
  Object.keys(UI_MAP || {}).forEach(function (k) {
    const nk = String(k).replace(/\s+/g, ' ').trim();
    if (nk && !UI_NORM[nk]) UI_NORM[nk] = UI_MAP[k];
  });
}
function _uiT(s) {
  if (!s) return '';
  return UI_MAP[s] || UI_NORM[String(s).replace(/\s+/g, ' ').trim()] || '';
}
```

`_uiWalk()` 的文本节点与 `[placeholder],[title],[alt]` 两处查找都改走 `_uiT()`；
`applyUiLang()` 里 `UI_MAP = r.ui` 之后立刻 `_uiBuildNorm()`。

### 16.6 验证

| 套件 | 结果 |
|---|---|
| 核心自测 | 18/18（i18n 九项 + 打包接线两项 + 预设 v2 七项） |
| 真链路界面 | 24/24（预设三段真链路 18 项 + 英文覆盖率 6 项） |

英文覆盖实测：整体中文占比 <10%，console/model/memory 三个子页签各 <15%。

---

## V1.9.0 技术要点（2026-11-01）—— 加密、签名、门禁与编译

### 19.1 时光胶囊 v3：口令加密

```
布局：MAGIC_V3(12) | flags(1) | log2n(1) | salt(16) | nonce(12) | ct+tag
MAGIC_V3 = b"XJY_SPARK_V3"
flags bit0 = 1  →  该文件用 PBKDF2 派生（scrypt 不可用时的兜底）
AAD = MAGIC_V3 + flags + log2n + salt
```

| 参数 | 值 |
|---|---|
| `_SALT_LEN` | 16 |
| `_V3_LOG2N` | 15（默认）；`_V3_LOG2N_MIN` 14 / `MAX` 20 |
| `_V3_PBKDF2_ITER` | 600000 |
| `PWD_MIN_LEN` | 6 |
| `maxmem` | 512 MB |

关键函数：`needs_password(blob)`、`check_password(password)`、
`_derive_pwd(password, salt, log2n, use_pbkdf2=False)`、`encrypt_with_password(data, password)`、
`decrypt(blob, want_info=False, password=None)`、`peek_version()` → 3/2/1。

`spark.py` 侧：`needs_password(path)`、`_open_capsule(path, info=None, password=None)`、
`export_spark(..., password="")`、`read_manifest/preview_spark/import_spark(..., password=None)`。

> 为什么不是「文件头存口令哈希」：那样等于把校验钥匙和锁放在一起。这里只存盐，
> 口令正确与否由 AES-GCM 的 tag 判定 —— 错口令解不出合法 tag，天然就是「口令不对或
> 文件被改」这一个结论。

### 19.2 Ed25519 清单签名

| 常量/函数 | 说明 |
|---|---|
| `MANIFEST = "manifest.sha256"` | 每行 `<sha256>  <相对路径>` |
| `SIG = "manifest.sig"` | 对清单字节的 Ed25519 签名（base64） |
| `PUBLIC_KEY_B64` | 内置公钥（打包时不需要私钥） |
| `build_manifest(root, patterns=None)` | 按路径排序；跳过 `__pycache__/node_modules/target/dist/build/.git` 与 `.pyc/.log/.tmp` |
| `sign_manifest(root, private_key_b64, patterns=None)` | 生成清单 + 签名 |
| `verify(root, public_key_b64=None)` | → `{ok, signed, tampered, missing, changed, msg}` |
| `verify_program_dir()` / `status()` | 给 `get_status()` 用 |

判定语义（**设计要点**）：

| 状态 | `signed` | `tampered` | 前端行为 |
|---|---|---|---|
| 清单+签名都对 | True | False | 正常 |
| 哈希对不上 | True | **True** | 全屏拦截（无关闭按钮） |
| 签名验不过 | True | **True** | 全屏拦截 |
| 没有清单（自己编译） | False | False | 只 `console.warn` |

### 19.3 强制 API Token

```
壳：make_api_token() → 32 字节 → hex  →  cmd.env("QQ_AGENT_API_TOKEN", &token)
后端：_load_or_make_token()  环境变量 → 文件 → 生成
      文件路径 <QQ_AGENT_DATA_DIR>/runtime/api_token.txt（chmod 0o600）
前端：backend_info 带 token → setToken() → tokenHeader() 给每个 POST 加 X-API-Token
```

`_TOKEN_FREE = ("/api/ping", "/api/health", "/api/get_disclaimer",
"/api/accept_disclaimer", "/api/confirm/ticket")`；只读 `GET /api/*` 也放行。

> 只对 POST 强制的原因：所有写操作都只能走 POST（后端有 `json_only` 判据），
> 而 GET 全是只读查询 —— 给诊断工具和网页版留条路，同时把「改东西」这条路锁死。

### 19.4 CSP 与 `safeUrl()`

`tauri.conf.json` → `app.security.csp`（437 字符）。要点：`connect-src` 只列
`127.0.0.1` / `localhost` 与 `ipc:`；`object-src 'none'`；`frame-ancestors 'none'`；
`base-uri 'self'`。

`safeUrl(u)` 白名单：

```js
/^(https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/|data:image\/|blob:|\.{0,2}\/)/i
```

放行后还要把 `" ' < > \` 转成 `%XX` —— 光靠白名单挡不住「合法域名 + 引号跳出属性」。

### 19.5 Cython 编译四模块

| 步骤 | 说明 |
|---|---|
| `TARGETS` | `secure_core` / `spark_crypto` / `security` / `audit` |
| 编译 | `cythonize(..., language_level=3, compiler_directives={"binding": True})` + `build_ext --inplace` |
| 产物 | 复制成 `_cy_build/<模块名>.pyd`（**去掉 ABI 标签**） |
| 清理 | 项目根 `*.c` / `*.pyd` / `build` 全部删掉 |
| 自检 | `--check`：插 `_cy_build` 到 `sys.path` 最前，逐个 import 并抽查符号 |
| 失败策略 | `build_backend.py` 里 `step_cython()` 失败只打印，退回用 `.py` |

打包接线：`--add-binary _cy_build/audit.pyd;.` + `--exclude-module audit`。
`--exclude-module` 不能省 —— 否则 `.py` 仍会被塞进 PYZ，编译等于没做。

### 19.6 前端翻译三级查找

```
_uiT(s) =  UI_MAP[s]                      // 精确
        || UI_NORM[空白折叠后的 s]         // 跨行带缩进的长段
        || _uiFrag(s.replace(UI_PAT[i]))   // 带数字的动态串 + 残留中文参数
```

`UI_NORM` 由 `_uiBuildNorm()` 在 `applyUiLang()` 里建；`UI_PAT` 约 33 条正则（「N 个方法
可用」「Agent 线程 N · 前台 / 后台」等）；`UI_FRAG` 处理模式替换后捕获组里的中文
（`朗读模式` / `是` / `（未知）` / `正式版` 等）。

## V1.9.1 技术要点（2026-11-01）—— 让安全真的生效

### 19.7 验签的真实路径（复测报告 N-01）

```
安装目录/
├── 小鲸鱼.exe                      ← 外壳，Rust 侧验签
├── resources/
│   ├── manifest.sha256            ← 清单（随 bundle.resources 装进来）
│   ├── manifest.sig               ← Ed25519 签名（64 字节）
│   ├── backend_server.exe         ← 清单覆盖
│   └── av_module/av_server.exe    ← 清单覆盖
└── ...
```

`verify_release()`（`lib.rs`）从 `current_exe()` 目录向上最多 4 层找清单；
若清单父目录名为 `resources`，基准上移一层（清单里的路径相对包根）。

清单只覆盖**能被执行的东西**：`backend_server.exe`、`av_module/av_server.exe`、
`napcat_bundle.zip`。`小鲸鱼.exe` 无法把自己算进自己的清单，靠代码签名，不假装覆盖。

### 19.8 `signing.verify()` 的四态

| 情形 | ok | signed | tampered | 是否拦启动 |
|---|---|---|---|---|
| 清单+签名都对，文件全对 | True | True | False | 否 |
| 清单+签名都对，**某个文件被改** | False | True | **True** | 是 |
| 签名验不过（伪造/换私钥） | False | False | True | 是 |
| **发行版但没有清单** | False | False | **True** | 是 |
| 源码运行且没有清单 | True | False | False | 否 |

`tampered` 的语义是「有人在改东西」，所以**文件与清单不符也算** ——
只置 `ok=False` 而留 `tampered=False`，前端（只看 tampered）就会放行。

### 19.9 增量包验签

```python
# 签名（打包时）
txt = json.dumps(man, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
man["sig"] = b64(priv.sign(txt))          # 注意：对「不含 sig」的规范化 JSON 签

# 校验（backend_server.exe --verify-delta <目录>）
man.pop("sig"); txt = json.dumps(man, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
pub.verify(b64decode(sig), txt.encode())  # 再逐个核对 payload 的 sha256
```

`apply_delta.ps1` 在动目标目录之前调它，退出码非 0 → `Fail 4`、零改动。
PowerShell 没有原生 Ed25519，借目标目录里那个**已被外壳验过**的 `backend_server.exe`。

### 19.10 BUILD_SECRET 滚动与多盐迁移

```
secret_salt()  = HMAC-SHA256(_base_salt(), BUILD_SECRET)
_base_salt()   = 历史固定盐（V1.8.16 及更早用的那个）
```

`_XOR_SALTS = [新盐] + 历史盐 + [_LEGACY_SALT]`，解密时逐个尝试；
`decrypt_secret_meta()` 返回 `(明文, 需否用新盐重写)`，命中旧盐就重写一次。
**BUILD_SECRET 的价值是「能滚动」，不是「能藏」** —— 所以必须留迁移路径，
否则老用户升级后 API Key 全废。

### 19.11 明确不做的两件事

| 做法 | 为什么不做 |
|---|---|
| UPX 压缩 | 杀软误报率高，且对逆向只是多花几分钟 |
| 反调试 / 蜜罐 | 判断失误会让正常用户的胶囊加解密**静默失效**，绝不能碰加解密路径 |

## V1.9.2 补记（打包当天）—— 增量包验签到底由谁做

```
payload（不可信）  ──✗ 不能给自己作证
    ↑
目标目录里已安装的 resources\backend_server.exe   ← 首选锚点（控制台程序，退出码可靠）
    ↑ 没有它才退
目标目录里已安装的 小鲸鱼.exe --verify-delta --result-file <tmp>  ← GUI 程序，靠结果文件
    ↓
清单里的 Ed25519 签名（signing.PUBLIC_KEY_B64，长期固定，不随 BUILD_SECRET 轮换）
```

| 问题 | 现象 | 解法 |
|---|---|---|
| 老版本没有 `--verify-delta` 入口 | 1.9.1 目标全员 rc=4 | `Test-VerifyDeltaSupport` 扫二进制里的命令串做能力探测；主锚点改为已装后端 |
| GUI 程序的退出码拿不到 | `$LASTEXITCODE` 为 `$null`，好包被判失败 | `--result-file` 写结果文件，ps1 轮询 60 秒读第一行 |
| 验签污染目标目录 | 多出 `runtime\api_token.txt`，零改动断言 37 → 38 | 验签期间 `QQ_AGENT_DATA_DIR` 指向 `%TEMP%` 一次性目录 |

## V1.9.2 技术要点（2026-11-01）—— 主题挂载点、截断放开与验签路径闭环

### 19.12 主题变量样式必须挂在 `document.body`

```js
function applyThemeCss(css) {
  let el = document.getElementById('themeVars');
  if (!el) { el = document.createElement('style'); el.id = 'themeVars'; }
  el.textContent = ':root{' + css + '}';
  // 内置主题的静态 <style> 就写在 <body> 里 —— 同优先级下后出现的赢，
  // 挂在 <head> 里会被整段盖掉（表现就是「换主题后画面刺眼」）。
  if (el.parentNode !== document.body || el !== document.body.lastElementChild) {
    document.body.appendChild(el);
  }
}
```

### 19.13 `_relaxClipped()`：放大后放开被截断的盒子

| 项 | 值 / 行为 |
|---|---|
| 触发阈值 | `font_scale >= 130`（`html[data-ui-big="1"]`） |
| 判定 | `el.scrollHeight >= el.clientHeight + 1 && el.clientHeight > 0` |
| 跳过 | `img/svg/canvas/input/select/textarea`；`overflow:hidden` 且 `border-radius != 0px` 的图片容器 |
| 处理 | `el.style.height = 'auto'` + `data-xjy-relaxed="1"` |
| 收回 | `font_scale < 130` 时清内联 `height/minHeight` 并删标记 |
| 调用点 | `applyUiLook()` 里设完 `zoom` 立刻调一次 + `requestAnimationFrame` 再补一次 |

### 19.14 验签路径闭环（N-01）

```rust
fn verify_release() -> Result<std::path::PathBuf, String>  // 返回清单里 backend 的 canonicalize 结果
static VERIFIED_BACKEND: std::sync::OnceLock<PathBuf>;

// spawn_backend_once() 里
if let Some(expect) = verified_backend() {
    let got = std::fs::canonicalize(cmd.get_program()).unwrap_or_else(|_| PathBuf::from(cmd.get_program()));
    if &got != expect { /* 写 boot 错误 + 拒绝启动 */ }
}
```

### 19.15 增量包验签（N-02）

`DELTA_PS1` 调 `$Target\小鲸鱼.exe --verify-delta $here`；`lib.rs` 在 `run()` 顶部处理该参数，
`run_verify_delta()` 先自己验签、再用 `resources/backend_server.exe` 校验包内容，返回其退出码。

### 19.16 构建期 SECRET：编进 `.pyd`，不落盘

| 环节 | 做法 |
|---|---|
| 生成 | `build_backend.step_build_secret()`：`secrets.token_bytes(32)` → `os.environ["XJY_BUILD_SECRET"]`，**不写文件** |
| 注入 | `build_cython._patch_secure_core()` 把 base64 字面量替换进 `_BUILD_SECRET_B64 = "..."` |
| 编译 | 在 `_cy_src/` 临时目录编译，收完 `.pyd` 整个删除（`finally` 里无条件删） |
| 消费 | `secure_core._build_secret()` → `base64.b64decode(_BUILD_SECRET_B64)`；`secret_salt()` / `manifest_key()` 都走 HMAC 派生 |
| 兼容 | `secret_salt_legacy()` / `manifest_key_legacy()`；`security._SIGN_KEYS` 逐个试 |

## V1.9.3 技术要点（2026-11-01）—— 防提取、诱饵与上锁的更新脚本

### 19.17 密钥链路：从「整串常量」到「构建期派生 + 分片」

```
构建机                                编译输入（临时目录）              程序运行时
XJY_BUILD_SECRET ──► salt = hmac(_base_salt(), secret, sha256)
                      mkey = hmac(_base_manifest_key(), secret, sha256)
                      8 片 × 4 字节，每片 ^= (seed*31 + i*67 + 11) & 0xFF
                                        │
                                        ▼
                          _SHARDS_SALT = ((0x.., b"\x..\x.."), …)    ──►  _join() 逐片取反拼接
                          _SHARDS_MKEY = ((0x.., b"\x..\x.."), …)    ──►  _legit() 通过才返回
```

- 源码树的占位：`_SHARDS_SALT = ()`、`_SHARDS_MKEY = ()`；构建期 `build_cython.py` 的 `_patch_secure_core()` 负责填。
- 种子读完立刻 `os.environ.pop("XJY_BUILD_SECRET")`，避免被 PyInstaller 子进程继承。
- `secure_core.py` 关键函数：`_join(shards)`、`_spin(shards)`、`_seal_live()`、`_legit()`、`_decoy_derive(base, idx)`、
  `secret_salt()`、`manifest_key()`、`secret_salt_legacy()`、`manifest_key_legacy()`、`shard_state()`。
- `shard_state()` 只报分片数量/sealed/legit/tag，**不含任何字节**，可以放心暴露在前端自检里。

### 19.18 诱饵与陷阱

| 名称 | 内容 | 用途 |
|---|---|---|
| `_DECOY_A` | 上一版真 SECRET 的 base64 | 最像真的，逆向的人第一时间会试 |
| `_DECOY_B` | Ed25519 发布公钥 | 长得也像 43 字符 base64 |
| `_DECOY_C` / `_DECOY_D` | 上一版盐 / 清单密钥的 base64 | 凑够 4 条，增加误判概率 |
| `_TRAP_ENVS` | `XJY_BUILD_SECRET`/`XJY_SECRET`/`XJY_INJECT_SECRET`/`XJY_FORCE_SALT` | 出现任何一个 → `_legit()` 为假 → 只给假盐 |

- 编译要求：`# cython: binding=False`，诱饵写成 `def _decoy_a(): return b"…"`。
- 假盐用 `_decoy_derive(基准, 索引)`，是**稳定**的 —— 这很重要：不稳定会让「看起来像是坏了」，稳定才像真的。

### 19.19 `_self_in_package()`：自检必须能认出「自己」

```python
def _self_in_package(root):
    rel = os.path.relpath(os.path.abspath(sys.executable), root)
    return not rel.startswith("..") and rel.replace("\\", "/") in _manifest_rels(root)
```

`verify_program_dir()` 在 `verify(base)` 之前调用它；release 构建下不通过 → `tampered=True`。

### 19.20 增量包脚本上锁

| 位置 | 做什么 |
|---|---|
| `打包发布.py::step_delta_zip()` | `man["scripts"] = {ps1: sha256, bat: sha256}`，**在签名之前**算 |
| `DELTA_PS1` 开头 | 读 `scripts` → `Get-FileHash` 比对 → 不等 `Fail 8`；缺字段也 `Fail 8` |
| `backend_server.py::_verify_delta_dir()` | 独立再验一遍两个脚本哈希，防「删掉 ps1 的检查段」 |

### 19.21 `guard_sig`：开关的签名

- `_GUARD_KEYS = ("guard_enabled","guard_sql","guard_prompt","guard_command","guard_block")`
- `_guard_sig(cfg)`：`hmac.new(secure_core.secret_salt(), "guard|1.9.3|guard_enabled=1,…", sha256).hexdigest()[:32]`
- `save()`：`clean["guard_sig"] = _guard_sig(clean)`；`load()`：`_guard_check(cfg)` 不通过 → 五个键全 True + `save(cfg)`
- 注意：`guard_sig` 必须登记进 `DEFAULTS`（`save()` 只保留已知键）。

### 19.22 本轮刻意没做的

- 没上 UPX、没上反调试蜜罐、没做「检测到调试器就自杀」—— 逆向报告明确不建议，代价高且容易误伤。
- 没把 `manifest_key` 也做成「构建期直接派生常量」以外的形态：它已经和盐一样不进种子链路了。

## V1.9.4 技术要点（2026-11-01）—— 原子升级、fail-closed 与防护章

### 19.17 增量包的四个阶段（`打包发布.py` 的 `DELTA_PS1`）

```
0) 验签      Start-Process -PassThru -Wait → WaitForExit(60000) → Get-Process backend_server | Wait-Process -Timeout 30
             数据目录 = %TEMP%\xjy-verify-<8位>（必须先 New-Item -ItemType Directory -Force）
1) 校验      manifest / payload / scripts 哈希
2) 备份      目标文件 → _backup\pkg-<ts>\ + restore.ps1 + added.txt（包在 try/catch 里）
3) 备齐      payload → <Target>\_update_tmp，逐个复核 sha256（不符 throw）
4) 换上去    Move-Item -LiteralPath $stage -Destination $dst -Force -ErrorAction Stop
             再复核；$postbad 非空 → throw
             catch { Invoke-Rollback "..." } → Popup(…, 25, …) → exit 9
```

`Invoke-Rollback` 的判定**以哈希为准**：遍历 `$backup` 里每个文件与目标的 sha256，
读不动的跳过不算差异，目标不存在记 `$diff++`；没有 `restore.ps1` 时退化成「删掉这次新写进去的文件」
（由 `$done -gt 0` 守卫，避免还没替换就去删）。

退出码：`2` 缺清单/payload、`3` 程序还开着、`4` 哈希不符（零改动）、`5` Target 是 payload 本身、
`6` 目标不像安装目录、`7` 没有可用的验签锚点、`8` 脚本自校验失败、`9` 换文件失败已回滚。

### 19.18 `signing.verify()` 的 fail-closed

```python
if release and not os.path.isfile(sp):
    return {"ok": False, "signed": False, "tampered": True, "missing": [], "changed": [],
            "msg": "签名文件不见了（%s）……" % sp}
```

位置在「清单存在检查」之后、「读清单」之前。**必须在读清单前返回**：一旦读了清单，
后面的逻辑就会走进「哈希都对得上 → ok=True」。

### 19.19 防护章（`config.py`）

| 元素 | 说明 |
|---|---|
| `guard.seal` | 首次运行生成的 32 字节密钥，`dpapi:<b64>`（DPAPI 不可用时 `raw:<b64>`） |
| `_guard_sig(cfg)` | `hmac(guard_key, "guard\|1.9.4\|" + "k=v,...")`，hex[:32] |
| `_GUARD_SEAL` | 最近一次盖章通过的取值快照（内存） |
| `save(cfg, guard_ok=False)` | 非 `guard_ok` 时把与 `_GUARD_SEAL` 不一致的五个开关**退回**，再盖章 |
| `guard_apply(values, note, actor)` | 唯一允许改的入口：先写 `guard_audit.log`，再 `save(cfg, guard_ok=True)` |
| `Api.set_guard(payload)` | 界面入口；`payload={"values": {...}, "note": "..."}`，两种调用形态都认 |
| 前端 | `btnGuardSave` 拆成 guard 五项 → `set_guard`，其余 → `save_settings`；关闭时 `UI.showConfirm` 写明后果 |

### 19.20 分片表不再导出（`secure_core.py`）

```python
def _load_shards():
    s = b"\x.."  # 构建期写入的 32 字节（打散）
    m = b"\x.."
    return bytes(b ^ _mask_of(i) for i, b in enumerate(s)), ...
```

- `_mask_of(i) = (_base_salt()[i % 32] * 7 + i * 73 + 0x5A) & 0xFF`：掩码现场算，不落盘，
  也不和分片放在同一段数据里（避免 `seed` 穷举）。
- 表只在函数内是局部变量，外部 `hasattr(sc, "_SHARDS_SALT")` 为 False。
- `_seal_live()` 在导入时把 `n`/`spin` 记在 `_LIVE`，`_legit()` 校验它们 + 四个陷阱环境变量不存在；
  不合法时 `secret_salt()` 返回 `_decoy_derive(_base_salt(), 2)` 这类**稳定的假值**。

### 19.21 沙箱受限（`sandbox.py`）

| 判据 | 说明 |
|---|---|
| `VM_OUI` | VMware `00:05:69/00:0c:29/00:1c:14/00:50:56`、VBox `08:00:27/0a:00:27`、Hyper-V `00:15:5d/00:03:ff`、Xen、Parallels、QEMU `52:54:00`、Docker |
| `_board_hint()` | `wmic computersystem` / `wmic bios` 里出现 vmware/vbox/qemu/hyper-v… |
| **`is_vm`** | `bool(hint) or (bool(vendor) and not real)` —— 还有真实网卡就不算（否则装了 VMware 的普通电脑被误伤） |
| `limited()` | `XJY_ALLOW_VM` 放行；`XJY_FORCE_VM=1` 强制命中（**先于缓存判断**） |
| 落地 | `backend_server._limited_block(name)`：前缀命中 `napcat_/rpa_/screen_/click_/computer_/browser_/…` → `{"ok": false, "code": "env_abnormal"}`；`health.backend.env` 暴露状态 |

### 19.22 旧密文与旧胶囊

- `security.migrate_xor1_config()`：`xor1:` → `decrypt_secret` → `encrypt_secret`（优先 dpapi）；`_startup_work()` 里调用。
- `spark.V2_SUNSET_VERSION = "1.10.0"`；`import_spark()` 返回 `sunset`，`warn` 里带期限。
- `secure_core.fetch_remote_key(timeout=3.0, url="")`：读 `XJY_KEY_URL`，没配就返回 None（行为不变），
  配了才去取 `{"key": "<base64 32 字节>"}`，异常一律 None —— 给以后云端下发密钥留的口子。

### 19.23 假密钥搬进 `legacy_keys.pyd`（V1.9.4 打包当天补）

打包后校验发现：`secure_core.pyd` 里 4 条诱饵**扫不到了**。
逐层排查（`_bisect.py`：同一份源码换 `binding` 编三次、再砍 docstring、再做个最小模块）：

| 结论 | 证据 |
|---|---|
| 与 `binding` 无关 | `binding True/False` 各编一次，`cands` 都是 0 |
| 与 docstring 无关 | 砍掉大段 docstring 后仍是 0 |
| 是模块级现象 | 同样的诱饵放进 30 行的最小模块，`cands=2`、整串连续 |
| 大模块里会被切碎 | 十六进制看是 `\xff` 每 8 字符一段：`\xffrwkDgJFz\xffYIEVABPB\xff8dfcIe3O\xff4=HUtmVR\xff5jyL28sB…` |

修法：把 4 条诱饵搬进独立小模块 `legacy_keys.py`，由 `build_cython.py` 编成 `legacy_keys.pyd`
（`TARGETS` 加一项，`--add-binary` 自动带上），`secure_core.py` 用
`try: import legacy_keys as _lk; _DECOY_A..D = tuple(_lk.keys())[:4]` 取，
导入失败就回落到原地内联的字面量（源码树仍可独立运行）。

验收：`legacy_keys.pyd` 里 4 条 43 字符 base64 连续可扫（`HUtm…`、`7hiO…`、`ZGVm…`、`NjBk…`），
`secure_core.pyd` 里没有整串真密钥、也没有 `_SHARDS_SALT` 名字；运行时
`shard_state() = {salt_shards: 8, mkey_shards: 8, sealed: True, legit: True}`。

> **顺带纠正一个之前的错误结论**：v1.9.3 以为「`# cython: binding=False` 能让常量连续」，
> 实测不是 —— 那一版能扫到是因为别的巧合。所以 `SETUP_PY` 里的
> `compiler_directives` 现在也写成 `"binding": False`（它本来就覆盖源码里的指令注释）。

## V1.9.5 技术要点（2026-11-01）—— 让「写在纸上的防护」真的执行

### 19.24 沙箱闸门的正确位置

```python
def call_method(name, args=None):
    ...
    if name in _DANGER_METHODS:
        ...
    _lim = _limited_block(name)          # ← 必须在 getattr 之前
    if _lim is not None:
        _log("[受限] 沙箱环境，已跳过：%s" % name, "WARN")
        return _lim
    fn = getattr(mod, name, None)
```

`_limited_block()` 内部顺序：先判 `_READ_ONLY`（放行），再 `sandbox.limited()`；
`except` 分支返回 `{"ok": False, "code": "env_abnormal", "msg": "环境检测异常", "limited": True}`
（**fail-closed**，即检测不出「干净」就当「不干净」）。

### 19.25 防护章（guard.seal）的信任模型

| 文件形态 | 处理 |
|---|---|
| `dpapi:<b64>` 且能解出 32 字节 | 采信 |
| `raw:<b64>` | 判篡改：不采信密钥，五开关强制 `True`，WARN |
| 其它前缀 / 解不出 32 字节 | 判未知来源，同上 |
| 文件不存在 | 生成新密钥，**只写 `dpapi:`**；DPAPI 不可用则只留内存 |

`_guard_sig(cfg)` 的 body 前缀随版本走（`guard|1.9.4|…`），只覆盖 `_GUARD_KEYS` 五个键。
`save(cfg, guard_ok=False)` 会用 `_GUARD_SEAL`（最近一次合法取值）做回退基线；
`_GUARD_SEAL is None`（还没 load 过）时基线取「五个都 True」，避免首次保存被绕过。

### 19.26 legacy_keys 的形态

3 条 `tuple[int, ...]`（每条 32 个 0~255 的整数），只暴露内部 `_tables()`：

```python
def _tables():
    ...
    return tuple(tuple(t) for t in tbl)   # 自检不过就返回 ()
```

`secure_core` 用它算假盐（`_decoy_derive`），索引 0/1/2 对应旧 SECRET / 旧盐文本 / 旧清单密钥文本。

### 19.27 两侧一致的「自己在不在清单里」

Rust：`exe.strip_prefix(&root)` → 相对路径（`\`→`/`、小写）→ 与清单行逐一比对 → 不在就 `Err`。
Python：`signing._self_in_package(root)`（`os.path.relpath(sys.executable, root)`，含 `..` 直接 False）。

## V1.9.6 技术要点（2026-11-01）—— 清单的「覆盖范围」与候选清单逐个尝试

### 19.28 `verify_one_manifest(mp, exe)`

原来「读清单 → 逐个比哈希 → 判 exe 是否在清单里」挤在一个 120 行函数里，且 exe 检查无条件执行。
现在拆开，并在开头先算一个布尔量：

```rust
// 清单里只要有一条不以 resources/ 开头的路径，就说明这是全量清单
let mut full_manifest = false;
for rel in &rels {
    if !rel.replace('\\', "/").starts_with("resources/") { full_manifest = true; break; }
}
...
if full_manifest && !listed_exe {
    return Err("这个程序不在安装包的清单里（…）—— 有人把改过的程序塞进了子目录，为安全起见不启动。".into());
}
```

返回值为**被验通过的后端路径**（`PathBuf`），供 `spawn_backend_once()` 做 `canonicalize` 比对（V1.9.2 的 N-01）。

### 19.29 `verify_release()`：候选清单集合

```rust
let mut cands = Vec::new();
for _ in 0..4 {
    cands.push(base.join("manifest.sha256"));
    cands.push(base.join("resources").join("manifest.sha256"));
    match base.parent() { Some(p) => base = p.to_path_buf(), None => break }
}
let mut errs = Vec::new();
for mp in cands.iter() {
    if !mp.is_file() { continue; }
    match verify_one_manifest(mp, &exe) {
        Ok(p) => return Ok(p),
        Err(e) => errs.push(e),
    }
}
Err(errs.join("\n"))
```

要点：

| 设计点 | 理由 |
|---|---|
| 先收齐再逐个试 | 用户目录里老绿色版的包根清单与新安装包的 `resources/` 清单常并存，不能取决于遍历顺序 |
| 任一通过即返回 | 两份清单覆盖范围不同，能过一份就说明这份覆盖范围内没被动过 |
| 全失败才报错，并把每条原因拼起来 | 现场排障时能看出是「缺文件」「哈希不符」还是「exe 不在清单里」 |

### 19.30 `--verify-self` 入口

```rust
if let Some(pos) = std::env::args().position(|a| a == "--verify-self") {
    let result = std::env::args().position(|a| a == "--result-file")
        .and_then(|p| std::env::args().nth(p + 1));
    std::process::exit(run_verify_self(result.as_deref()));
}
```

`run_verify_self` 与 `run_verify_delta` 同构：结论写进 `--result-file`（GUI 子系统程序在 PowerShell 里
拿不到退出码，V1.9.2 已经踩过），打包后的验收脚本靠读这个文件判断。

### 19.31 验收矩阵

| 布局 | 清单 | 期望 |
|---|---|---|
| 安装包（NSIS） | `resources/manifest.sha256`（3 行，全在 `resources/` 下） | 通过 |
| 绿色版 | 包根 `manifest.sha256`（全量，含 `小鲸鱼.exe`） | 通过 |
| 混装 | 包根旧全量清单 + `resources/` 新清单 | 通过（任一） |
| 主程序被追加字节 | 全量清单 | 拒绝 |
| 主程序拷到 `sub/` | 借父目录全量清单 | 拒绝（exe 不在清单里） |


## V1.9.7 技术要点（2026-11-02）—— 清单的「覆盖范围」不能靠运行时猜

### 19.32 verify_one_manifest：删掉 full_manifest 分支

```rust
// v1.9.6：只有全量清单才检查 exe（放宽，被 C 组攻击打穿）
// v1.9.7：不区分，没写着就不启动
if rel_norm == rel_self { listed_exe = true; }
// ...
if !listed_exe {
    return Err(format!("这个程序不在清单里（{}）—— 要么 ... 要么有人把清单换成了一份覆盖范围更小的。"));
}
```

### 19.33 verify_release：候选清单全部通过

```rust
// 向上最多 4 层，每层收 base/manifest.sha256 与 base/resources/manifest.sha256（不 break）
for mp in &cands {
    match verify_one_manifest(mp, &exe) {
        Ok(p) => ok_path = Some(p),          // 记下但继续验
        Err(e) => errs.push(format!("{}: {}", mp.display(), e)),
    }
}
if !errs.is_empty() { return Err(...); }
```

| 设计取舍 | 理由 |
|---|---|
| 不再「任一通过即可」 | 宽松的那份通常是攻击者放的；只要允许「挑一份过」，就等于允许降级 |
| 多份里有一份失败就拒绝启动 | 失败信息里点名是哪一份，并给出「删掉或用恢复工具重装」的具体动作 |
| 安装包 resources 清单与包根清单同权 | 两者都必须是官方签的；只信任包根等于让安装包必须带全量清单 |

### 19.34 打包链：签 → 构建 → 比对 → 不一致重签重打

```python
# step_tauri()
for attempt in range(3):
    sign_tauri_resources()            # 清单里含 小鲸鱼.exe
    run(["npm.cmd", "run", "tauri", "build"])
    if manifest_exe_hash(mp) == sha256_of_file(built_exe_path()):
        return
raise RuntimeError("清单里的 exe 和构建出来的 exe 对不上，拒绝出包")
```

为什么必须比对：Tauri/NSIS 阶段会重写 exe（资源段、时间戳），构建前签的哈希必然对不上。
V1.9.6 就是因为没比对，才用「放宽检查」绕过去。

### 19.35 Python 侧：路径基准

```python
# find_package_root()：命中 resources/manifest.sha256 时把 root 上提一层
if os.path.basename(base).lower() == "resources":
    return os.path.dirname(base)

# _manifest_rels()：两种布局都要找，否则装出来的机器一律判篡改
cands = [os.path.join(root, MANIFEST), os.path.join(root, "resources", MANIFEST)]
```

### 19.36 guard.seal：32 字节密钥 + 16 字节指纹

| 解出来的长度 | 判据 | 结果 |
|---|---|---|
| 48 且 `blob[32:] == HMAC(secret_salt, key|machine_id)[:16]` | 合法 | 采信 |
| 48 但指纹不对 | 换锁 | 五开关强制开 + WARN |
| 32（1.9.5/1.9.6 老格式） | 分不清升级还是伪造 | 按换锁处理 + WARN |

### 19.37 恢复工具：验签器 `Tools/verify_bundle.exe`

- 内置公钥（与 `signing.PUBLIC_KEY_B64` 同一把），支持 `<zip>` 与 `--dir <目录>` 两种入参；
- 退出码：0 通过 / 2 用法错 / 4 验不过（无签名、签名不对、文件被改、清单不含主程序）；
- 输出**一律 ASCII**（PowerShell 5.1 抓 stdout 会按控制台编码解码，中文会乱）；
- 恢复脚本的选包逻辑：对每个候选包跑一次，只有 rc=0 的进候选池，再从池里取版本号最大的；
  全不过 → `Fail 2`；自检失败 → 用 `_repair_backup` 逐文件还原（备份里没有的删掉）→ `Fail 9`。


## V1.9.9 技术要点（2026-11-02）—— 回声、直达通道、清单白名单

### 20.1 评论回声的三层拦截

```python
# memory.py  add_comment()：入口层已有 uin/昵称过滤，这里再按「内容 + direction=out」判回声
if (direction or "in") != "out":
    c.execute("""SELECT id FROM moment_comments
               WHERE COALESCE(moment_tid,'')=? AND content=? AND direction='out'
               LIMIT 1""", (moment_tid or "", content))
    if c.fetchone():
        return 0, False          # 这句话是我自己说的，别再入队
```

| 层 | 函数 | 判据 |
|---|---|---|
| 入口 | `agent._self_comment_ids()` | `commenter_id` 落在自己 uin 集合里，或 `commenter_id` 为空但昵称命中 |
| 落库 | `memory.add_comment()` | 同说说 + 同内容 + `direction='out'` 已存在 |
| 发送前 | `memory.thread_last_direction(root_id)` | 线索最后一条是 `out` → `COMMENT_SKIPPED` |

### 20.2 `ignore_silence` 直达通道

| 表 | 新列 | 默认 | 用途 |
|---|---|---|---|
| `care_plans` | `ignore_silence INTEGER` | 0 | 1 = 该计划跳过静默门槛 |
| `care_plans` | `priority INTEGER` | 0 | 1 = 用户明确要求，优先发 |
| `user_events` | `ignore_silence INTEGER` | 0 | 同上（到点提醒走事件链路） |
| `user_events` | `priority INTEGER` | 0 | 同上 |

置位规则：`agent._REMIND_RE`（`叫我|提醒我|别忘了|喊我|闹钟|到点叫|几点叫…`）命中即 1；模型输出 `remind/priority` 同样置 1。

```python
# agent._checkin_should_send()
if int((ev or {}).get("ignore_silence") or 0):
    return True, "用户明确要求这个点提醒（不看静默门槛）"

# agent.care_plan_loop()
_ign = int(p.get("ignore_silence") or 0)
if (not _ign) and bool(_live_cfg("care_plan_use_silence", False)): ...
if CARE_PLAN_RESPECT_SILENCE and not _ign: ...
```

### 20.3 清单白名单

```python
SIGN_CORE_FILES = ("小鲸鱼.exe",
                   "resources/backend_server.exe",
                   "resources/av_module/av_server.exe")

def is_signed_core(rel):
    r = rel.replace("\\", "/").lstrip("./")
    return r in SIGN_CORE_FILES or r.lower().endswith(".pyd")
```

`install_manifest_text()`（安装包清单）与 `sign_zip()`（绿色版清单）都只保留白名单内的条目；`sign_tauri_resources()` 的 `pats` 去掉 `resources/napcat_bundle.zip`。

**为什么**：清单的职责是「证明程序本体没被换」。说明书、示例图、`.py` 源码改了不影响安全，却会让用户被拦在门外 —— V1.9.8-Beta 有用户就因为 README 对不上而「被锁死」。

### 20.4 验收

| 用例 | 期望 |
|---|---|
| 自己的评论被读回来 | `add_comment()` 返回 `(0, False)`，不入队 |
| 线索最后一条是 out | 不重复回复 |
| `add_event/add_plan(ignore_silence=1)` | `get_due_*` 读回 1 |
| 老库（无新列）打开 | 自动 `ALTER` 补列，老数据读出 0 |
| 「下午四点叫我」 | `_REMIND_RE` 命中 → 到点照发 |
| 「明天考试」 | 不命中 → 仍走静默判断 |
| 清单 | 只含 exe + 两个 server + `*.pyd` |

### 20.5 增量包脚本的函数定义顺序

`打包发布.py` 里的 `DELTA_PS1` 是一个 PowerShell 脚本模板。它**不是**被解析后整体注册的，
而是自上而下执行的 —— 函数定义写在调用点之后，调用时就会 `CommandNotFoundException`。

正确的顺序（v1.9.9 起）：

```
Fail()            -> 退出码与弹窗
Invoke-Rollback() -> 回滚（必须在使用之前定义）
0) 验签 -> 1) 校验 -> 2) 备份（失败即 Invoke-Rollback）
3) 暂存区 -> 4) 原子替换 -> 5) 复核（失败即 Invoke-Rollback）
```

验收要看**失败路径**，不只是成功路径：`锁住小鲸鱼.exe` 时应当 rc=9 且目录逐字节复原；
只测「换文件途中断掉」会漏掉「备份阶段就失败」这条分支。

## V1.9.9.1 技术要点（2026-11-02）—— 「这是我自己」的三层识别

### 21.1 识别函数

```python
# qzone.py
_SELF_CACHE = {"at": 0.0, "uins": set(), "names": set()}

def self_uins(force=False):      # 60 秒缓存
    # 1) config.json: qq_uin / self_uin / bot_uin / my_uin / owner_uin / uin
    # 2) napcat_mgr.login_info(): user_id / uin / self_id / qq
    # 3) 昵称：bot_name / self_name / bot_nickname / agent_name / nickname

def is_self(uin="", name=""):    # 有号认号，没号认昵称
```

### 21.2 三层过滤

| 层 | 函数 | 过滤条件 |
|---|---|---|
| 网页协议 | `qzone_interact._is_mine(uin, sess)` | `uin == sess["uin"]` |
| NapCat 接口 | `qzone.read_feed()` / `read_comments()` | `is_self(...)` → `continue` |
| 收件箱 / RPA | `qzone._norm_comment()` | `is_self(...)` → `return None` |

`agent.feed_fetch()` 三条来源各再调一次 `_skip_self_post()`（内部就是 `qzone.is_self`），
并且日志会写「这条是自己发的，跳过点赞/评论」。

### 21.3 评论防套娃的四道闸

1. `add_comment()`：同一条说说里已有内容完全相同的 `direction='out'` 记录 → 判回声丢弃；
2. `add_comment()`：`(tid, 人, 内容)` 已存在 → 返回已有 id、`False`，不新建；
3. `get_pending_comments()`：只取 `status='pending'`，回过的不会再被取出；
4. `thread_last_direction(root_id)`：这一串最后一条是 `out` → 跳过，不接自己的话。

只有对方又说了**新的一句**（`in` + 不同内容）才会重新入队。

## V1.9.9.2 技术要点（2026-11-02）—— 多轮对话里的图片上下文

### 22.1 三个环节都要带图

```python
# 1) 入栈历史（agent.py handle_message 末尾）
if images:
    _db_user_text = ("[发送了一张图片] " + (raw_text or "")).strip()
    hist_text = f"{sender_name}：{_db_user_text}"
sessions.add(key, "user", hist_text)

# 2) 后台补注记（_after_reply_job 开头）
_u_text = _db_user_text
if images and not reuse_images and ENABLE_VISION:
    _note = vision_caption(images[0], raw_text)
    if _note:
        _u_text = ("[发送了一张图片：" + _note + "] " + (raw_text or "")).strip()
        sessions.patch_last_user(key, f"{sender_name}：{_u_text}")

# 3) 写库用同一份文本
mem.add_message(key, "user", _u_text)
```

### 22.2 `vision_caption()`
走 `ask_vision()`（即「模型设置 → 按功能分工 → 看图识图」指定的模型），
prompt 要求「不超过 30 字、只答内容本身」，返回前 `splitlines()[0]` + 去引号 + 截 40 字；
任何异常都返回空串，绝不拖垮回复。

### 22.3 为什么必须回填历史
历史是内存里的 `SessionStore`（`deque`），注记生成时那条消息早就 append 进去了。
只改数据库的话，当前会话下一轮仍然看不到图 —— 必须 `patch_last_user()` 改内存里那条。

### 22.4 提示词规则
`refresh_system_prompt()` 里固定追加【图片记忆（重要）】：接着上一轮的话题说，
不许问「买的什么/这是啥」，只有对方明说又发了新图才当新内容。

### 22.5 验收
| 项 | 判据 |
|---|---|
| 注记 | 去空白、去引号、≤40 字、异常返回 `""` |
| 回填 | `patch_last_user` 只动最后一条 user，空会话返回 False |
| 规则 | `refresh_system_prompt()` 的返回值里含【图片记忆（重要）】 |
| 历史文本 | 含图的 user 消息形如「[发送了一张图片] 原文」 |

## V1.9.9.3 技术要点（2026-11-02）—— 读评论的三条路与探测判据

### 23.1 探测判据（`qzone._probe_action`）

```python
def _probe_action(action, need_param_hint=True):
    resp = call(action, {}, timeout=8)
    m = _msg_of(resp)
    if _unsupported(resp):            # 不支持的Api / unsupported
        return False, m
    if "Schema compilation error" in m or "required property" in m:
        return True, "接口存在（缺必填参数）"
    if str(resp.get("status")) == "ok":
        return True, "接口存在"
    if not m:
        return False, "探测没有答复（当作不支持）"
    return False, m
```

判据原则：**默认不支持**。只有「明确成功」「明确缺参数」两种才算支持。

### 23.2 三条读评论的路

| 顺序 | 实现 | 适用 |
|---|---|---|
| 1 | `qzone.read_comments()` → NapCat `get_qzone_msg_list` | 少数支持该接口的 NapCat |
| 2 | `qzone.drain_inbox()` → `qzone_inbox/` | 装了空间插件的环境 |
| 3 | `agent._read_comments_web()` → `qzone_interact.comment_list()` | 通用兜底（网页协议，需要登录态 cookie） |

第 1 条报「不支持的Api」→ `clear_caps()` → 第 2 条；第 2 条空 → 第 3 条。
任一拿到内容就继续走去重与回复，日志里会写清是哪条路拿到的。

### 23.3 网页协议补读
`_read_comments_web()` 复用 `list_feeds("", num=5)`（自己的说说）+ `comment_list(tid)`，
只处理 `cmtnum > 0` 的说说；过滤自己（`uin == sess["uin"]`）与空内容；
任何异常都只打 DEBUG，返回空列表，绝不影响主循环。

### 23.4 验收
| 项 | 判据 |
|---|---|
| 探测 | 不支持/空答复 → False；schema 报错 → True |
| 兜底 | 收件箱有货 → ok=True 且带内容；收件箱空 → ok=True、空列表、文案含「没有新评论」 |
| 其它错误 | 仍返回 ok=False（不能被吞掉） |
| 补读 | 字段齐全、带说说正文、跳过 0 评论、异常不外抛 |

## V1.9.10 技术要点（2026-11-03）—— 视频解析、记忆快照与三段式 messages

### 23.1 视频解析（`videocatch.py`）

```python
detect(url)            # → (platform, vid)，platform ∈ bilibili/youtube/douyin/other
find_urls(text, limit) # 从聊天文本里挑出链接
fetch(url, timeout=10, with_subtitle=False)
    # 0) 视频平台先走 yt-dlp：title / uploader / description[:100] / cover / subtitle
    # 1) 未成功再打 B 站官方接口（仅 bilibili + BV 号）
    # 2) 仍未成功再读网页 og 标签
cover_data_url(img_url)      # 下载封面 → data:image/...;base64
describe_for_prompt(info, who="对方")   # 拼成一小段给模型看的话
hot_bilibili(limit=20)       # 热门列表（标题 + 链接）
```

要点：

- yt-dlp 在**函数内 import** → `build_backend.py` 的 `LAZY` 必须补
  `yt_dlp` / `yt_dlp.extractor` / `yt_dlp.utils`，否则打包版直接 `ModuleNotFoundError`。
- 封面直接转 data URL 交给视觉模型（不落盘、不留 URL 过期问题），上限 1.5 MB。
- `out["ok"]` 为真之后，后面的分支不许再覆盖 `via`。

### 23.2 记忆快照（原日记）

| 项 | 值 |
|---|---|
| 间隔 | `diary_interval_value`（默认 4）× `diary_interval_unit`（`hour`/`minute`，默认 `hour`），下限 60 秒 |
| 最少消息 | `diary_trigger_min_msgs`（默认 10） |
| 表结构 | `diary` 加 `slot TEXT DEFAULT ''`；`add_diary(..., slot="")` 按 `(user_id, day, slot)` 覆盖 |
| 查询 | `Memory.recent_diary(user_id="", limit=3, hours=0)` |
| 注入 | `diary.diary_memory_block(...)` → `【你最近的记忆碎片】`，拼在 system 的中等稳定区 |
| 状态 | `state["diary_last_ts"]`（`load_state`/`save_state` 白名单已包含） |

Prompt 语义变化：从「第一人称日记」变成「关键事实 + 情绪走向，100 字以内，`[[mood:xxx]]`」——
它不再是给用户看的产物，而是**给下一轮模型看的压缩上下文**。

### 23.3 三段式 messages

```python
messages = [
  {"role": "system", "content": 人设 + 规则 + 工具 + 长期记忆 + 记忆碎片},   # 字节级一致
  ... 最近 8 轮（16 条）user/assistant ...,
  {"role": "user", "content": 用户原话 + 易变段},                          # 时间/情绪/召回都在这里
]
```

- `build_messages()` 里加了 `_hist = _hist[-16:]`。
- 易变段（volatile）从 V1.1.3 起就在最后一条 user 里，这版只是把历史压到 8 轮，
  让「前半段不动」这件事真的成立。

### 23.4 傲娇换资料（secret）

- `computer_use.tool_update_profile(avatar_path, nickname, secret)` → `selfmod.update_profile(...)`。
- `secret=True` 时返回体带 `note`「…不要主动提起…」；同时 `_touch("profile", secret=True)`
  记下一条待提醒，`selfmod.pop_secret_notice()` 取出后**注入一次**易变段。
- 冷却：`selfmod_state.json` 里按 kind 记时间戳，`_COOL_SEC = 24 * 3600`。
- 工具提示词（`agent._agent_tools_prompt()`）末尾追加一段「关于 secret 参数」的行为规则，
  让模型知道「什么时候可以这么干、干完怎么演」。

### 23.5 验收

| 断言 | 做法 |
|---|---|
| system 前缀字节级一致 | 同一 `system_prompt` + 不同 volatile 各拼一次，比较 `messages[0]` |
| 只有最后一条不同 | 逐条比较前 n-1 条 |
| 记忆碎片能召回 | 新建 Memory 实例（模拟新会话）后 `diary_memory_block()` 仍有内容 |
| 视频真解析 | 真网跑 `BV1GJ411x7h7`，断言 `ok` / `title` / `via` 含 `ytdlp` |
| secret 演戏 | 无 NapCat 环境时退化为「门路存在 + 文案已写好」两断言 |

## V1.9.11 技术要点（2026-11-03）—— 缓存前缀与版本比较

### 24.1 `ask_model_raw` 的时间开关

```python
def ask_model_raw(prompt, system=None, max_tokens=None, temperature=None,
                  effort="none", tag="/裸调用", no_time=False):
    sys_p = system if system is not None else SYSTEM_PROMPT
    ...
    if ENABLE_TIME_AWARENESS and not no_time:
        sys_p = sys_p + time_context()
```

情感判定（`emotion_engine.analyze`）的调用方改成：

```python
_emo_dec = _ee.analyze(raw_text or "", _live_cfg("emotion_presets", "normal"),
                       timeout=float(_live_cfg("emotion_engine_timeout", 0.9) or 0.9),
                       ask=lambda _p, **_kw: ask_model_raw(
                           _p, system=EMO_JUDGE_SYSTEM, no_time=True, **_kw))
```

`EMO_JUDGE_SYSTEM` 是模块级常量（44 字），保证多次调用逐字节一致。

### 24.2 三段式消息的真实结构（实测）

| 调用 | system | messages |
|---|---|---|
| 情感判定 | `EMO_JUDGE_SYSTEM`（44 字，固定） | system + user(打分指令+对方原话) |
| 正式回复 | 人设/规则/工具（2312 字，**无时间戳**） | system + 最近 8 轮(user/assistant) + 当前 user |

当前 user 的形态：`<user_input># 当前状态 【当前时间】… 【对方情绪】… # 用户发言 …</user_input>`；
历史轮次里**不带**这些。

### 24.3 版本号比较（SemVer）

```python
def _ver_parts(v):        # '1.9.8-Beta' -> ((1,9,8), 0, ((1,0,'beta'),))
    ...
def _ver_key(v):          # 可直接排序
    return _ver_parts(v)
def _ver_lt(a, b):
    return _ver_key(a) < _ver_key(b)
```

`step_delta_zip()` 选基线：`older = [c for c in cands if _ver_lt(c[1], version)]`，
取 `older[-1]`；空则兜底 `cands[-1]` 并打「！目录里没有比 X 更早的绿色包…」。

### 24.4 签名私钥查找顺序

`_sign_key_b64(with_src=False)`：`XJY_SIGN_KEY` → `<上级>\_v190_work\keys\` →
`<上级>\_keys\` → `<上级>\keys\` → `<ROOT>\keys\`；必须 32 字节；找不到时清单不签名**并打警告**。

### 24.5 验收

| 套件 | 项 |
|---|---|
| `t_verkey.py` | 11（排序 15 组 + 基线沙盘 4 组） |
| `t_chat.py` | 19（首/次轮 system 字节一致、历史进入、最后一条 user 带状态） |
| `t_snap.py` | 18（1 分钟间隔真跑、mood 标签、召回块） |
| `t_video.py` | 5（真网 B 站 → 标题/UP 主进 prompt） |
| `t_secret.py` | 21（note、只提醒一次、24h 冷却落盘） |
| `t1910.py` | 54（V1.9.10 全量回归） |

## V1.9.11.1 技术要点（2026-11-03）—— 能力 vs 剧本

### 25.1 三处措辞的去人设化

| 文件 | 位置 | 现在的措辞要点 |
|---|---|---|
| `agent.py` | `_agent_tools_prompt()` 的 `【关于 update_profile：换不换由你自己决定】` 段 | 「你拥有更换自己昵称和头像的能力」＋三条路（直接换 / 直接拒绝 / `secret=true`）＋「没有哪种人设被规定必须用它」 |
| `computer_use.py` | `TOOLS` 里 `update_profile.when` | 「换不换、怎么回，按你的人设与三观自行决定」 |
| `computer_use.py` | `args["secret"]` | 「true=悄悄换（对方不会收到提示；是否这样做由你的人设决定）」 |
| `selfmod.py` | `update_profile()` 的 `note` | 「对方现在还不知道。要不要主动提、怎么回应追问，按你的人设自行决定。」 |
| `selfmod.py` | `pop_secret_notice()` | 「要不要提、怎么提、要不要继续装作没换，都按你的人设自行决定。」 |

### 25.2 为什么不能写「如果傲娇就……」

`_agent_tools_prompt()` 是**所有**人设共享的一段文本。往里写「傲娇的人可以……」等价于
给其他性格发了一条「这条不适用于你」的指令；而写「你拥有这个能力，怎么用按人设决定」
对每一种性格都成立。

判定标准：把某句话里的人设换成「软萌」「高冷」「纯助手」，句子是否仍然成立。不成立就得改。

### 25.3 验收

`t_generic.py`（26 项）分三层：

- **文本层**：`agent.py` 提示段 / `TOOLS.when` / `args.secret` / `note` / 提醒里不含
  「傲娇」「装凶」「嘴硬」；且都含「人设」「三观」「自行决定」这类通用表述。
- **运行期**：真调用 `_agent_tools_prompt()` 与读 `TOOLS`，确认拼接结果是通用口径。
- **能力仍在**：打桩 `set_nickname`/`set_avatar` 后 `update_profile(secret=True)` 成功、
  返回 `note` 只陈述事实；`secret=False` 不带 `note`。

## V1.9.11.2 技术要点（2026-11-03）—— 重启、人设、CQ 码

### 26.1 为什么「启动」不能当「重启」

`start_agent()` 是幂等的：进程活着就返回「已在运行」。这在「确保 Agent 在跑」的场景下是对的，
但在「换了配置要它重新读」的场景下就是灾难 —— 调用成功、效果为零。

```python
def restart_agent(wait=1.0):
    running = _proc is not None and _proc.poll() is None
    if running:
        st = stop_agent()
        if not st.get("ok"):
            return {"ok": False, "msg": "重启失败：旧进程没停下来（%s）" % st.get("msg"), ...}
        time.sleep(max(0.2, float(wait or 1.0)))
    r = dict(start_agent() or {})
    r["restarted"] = bool(running)
    ...
```

`stop_agent()` 走 `taskkill /PID <pid> /T /F` 整树清理再 `_proc.wait(timeout=8)`，
所以等 1 秒之后端口确实空出来了，新进程能正常绑 5709。

### 26.2 人设必须「用完就扔」

`SYSTEM_PROMPT` 是模块级变量，进程启动时生成一次；长期活着的子进程（Agent）拿到的就是快照。

```python
def _agent_run_system():
    global SYSTEM_PROMPT
    try:
        refresh_system_prompt()
    except Exception as e:
        log("[Agent] 刷新人设失败（用旧的那份）: %s" % e, "WARN")
    p = (SYSTEM_PROMPT or "").strip()
    if not p:
        return AGENT_SYSTEM_PROMPT
    return AGENT_SYSTEM_PROMPT + "\n\n【你当前的人设】\n" + p
```

顺序很关键：动手规矩在前（先立流程），人设在后（再定口吻）—— 反过来模型容易拿人设当免死金牌。

### 26.3 CQ 码只该出现在协议层

`[CQ:type,key=value,…]` 是 OneBot 的协议标记，不是给人看的。日记 / 永久记忆这类
「以后要被人读到」的文本，出口处必须过 `diary._clean_cq()`：

| 类型 | 占位 |
|---|---|
| image | `[图片]` |
| face / mface / emoji | `[表情]` |
| record / video / file | `[语音]` / `[视频]` / `[文件]` |
| at | `@昵称`（有 `name=` 时） |
| reply / 未知 | 删除 |

解析 `[CQ:at,qq=…,name=…]` 要按第一个 `name=` 取值（昵称里可能有逗号/空格），不能取 split 的末段。

### 26.4 验收

| 用例 | 断言 |
|---|---|
| 单元（`t_fix.py`） | `restart_agent` 四种输入形态；`_agent_run_system()` 含人设标记；`_clean_cq` 六种替换；两个按钮都调 `restart_agent` |
| 端到端（`t_e2e.py`） | 真起 Agent → 切「阿冷」→ 重启 → **PID 变化** + `agent.log` 出现 `已加载「阿冷」` + `_agent_run_system()` 含「阿冷」且不含旧硬编码 |

### 26.5 拆句不该切在成对符号里（V1.9.11.2 补）

```python
_PAIR_RE = re.compile(r"《[^》]*》|「[^」]*」|『[^』]*』|【[^】]*】|（[^）]*）|\([^)]*\)|“[^”]*”")
_PAIR_OPEN, _PAIR_CLOSE = "\ue000", "\ue001"     # 私有区占位符

def _protect_pairs(text):   # 成对符号 → 占位符，返回 (新文本, 原文表)
def _restore_pairs(text, saved)
```

`split_reply()` 变成「保护 → 切句 → 逐句还原 → 按长度累积」；末尾再加一条兜底：
某段首字符是 `》」』”’】）)]}` 就并回上一段。分享文案另走 `no_split=True` 整条发送。

### 26.6 昵称只能从 affinity 拿

`messages` 表（`session_key, role, content, ts`）没有昵称列，
`get_affinity(user_id)["name"]` 才是唯一来源（记忆页账号胶囊同源）。
凡是「要显示对方名字」的地方都从这里取，取不到退回 `user_id`。

### 26.7 辅助调用也要 `no_time`

`ask_model_raw(prompt, system=..., no_time=True)` 才不拼 `time_context()`。
要求 system 稳定的调用（情感判定、动手模式每一步、结构化打分）都必须传 `no_time=True`。
判据：**system 里出现精确到分钟的时间 = 前缀缓存必然失效**。

## V1.9.12 技术要点（2026-11-03）—— 防抖、引用、记得自己发过什么

### 27.1 防抖缓冲

```python
_DEBOUNCE = {}           # key -> {"items": [...], "last": ts, ...}
def _debounce_push(key, event, mtype, sender_name, raw_text, target, user_id, label=""):
    ...
    if n >= 6 or merge_delay_sec() <= 0.6:
        _debounce_flush(key)          # 攒够了立刻处理
```
- 合并格式：每行 `用户：文本`，表情/图片转成 `[用户发送了一个表情包：开心]` 挂在末尾。
- 带图的那条会替换 `st["event"]`，保证合并后仍能把图喂给视觉模型。
- `merge_enabled=False` 时 `handle_event` 不缓冲，回到一条一回。

### 27.2 引用原话

- `_cache_msg_text(msg_id, text, name)`：`handle_event` 每条消息进来就记（FIFO，上限 800，6 小时过期）。
- `extract_text` 的 list 分支新增 `reply`：`_lookup_msg_text(id)` → `【你引用了历史消息：…】`（截 120 字）；
  字符串形态用 `re.sub(r"\[CQ:reply,id=(-?\d+)\]", ...)`。

### 27.3 记得自己发过什么

- `remember_assistant(key, text, images)`：`sessions.add(key,"assistant",t)` + `mem.add_message` + `mem.log_message`。
- `send_to_session(..., remember=False)`，主动分享处传 `remember=True`。
- 人设新增【你自己说过的话】：历史里 assistant 的话都是她说的，**不要装作不知道**。

### 27.4 发图换头像的意图提示

命中 `换上|换掉|换成这个|当头像|做头像|用这个当|改头像|换个头像|改名|改个名|换个名字|换名字`
且带图 → `_prepare_messages` 在易变段追加【意图提示（重要）】；`_agent_tools_prompt()`
末尾补【优先级】，明确「先判断是不是在给你换资料」。

### 27.5 验收

| 项 | 结果 |
|---|---|
| 单元 `t_ctx.py` | 27/27 |
| 端到端 `t_e2e_ctx.py` | 10/10 |

### 27.6 聊天链路的「模型决定 + 系统执行」通道（V1.9.12 验收后补）

```python
_PROFILE_MARK_RE = re.compile(
    r"\[\[\s*(APPLY_AVATAR|APPLY_NAME|APPLY_PROFILE)\s*(?:[:：]\s*([^\[\]]{1,40}))?\s*\]\]", re.I)
```
- 提示词只教「想要就写一行 `[[APPLY_AVATAR]]`」，不要求调用工具（聊天链路没有 `tools`）。
- 出口处 `send_private_human` / `send_group_human` 先 `_take_profile_marks(text)`，非空则
  `_apply_profile_marks(session_key, acts)`（图从 `_LAST_IMAGES[scope]` 取，`update_profile` 自带 24h 冷却）。
- 用户输入侧 `_strip_profile_marks` 兜底，防止「打字提权」。

### 27.7 合并路径与回复冷却解耦

- `_debounce_flush()` 命中 `can_reply()==False` → 计算剩余冷却 → `_debounce_requeue(key, st)` 放回缓冲
  → `_debounce_wait` 再等一个窗口 → 成功即处理。日志 `[防抖] 命中回复冷却（还剩 x 秒），这批先留着不丢`。
- 非合并路径同样补了 `[冷却] …` 日志，保证「没回」永远有据可查。

### 27.8 验收

| 项 | 结果 |
|---|---|
| `t_qa2.py` | 31/31 |


## V1.9.13 技术要点（2026-11-03）—— 心跳式防抖

### 28.1 旧实现的两个结构问题
`_debounce_push()` 只在**第一次**启动 `_debounce_wait()` 轮询线程，之后仅更新 `st["last"]`；
线程是「一次性的」，一旦因异常/竞态提前返回，缓冲里剩下的消息就永远等不到 flush。
### 28.2 新实现：可重置的定时器
```python
with _DEBOUNCE_LOCK:
    st = _DEBOUNCE.setdefault(key, {"items": [], "timer": None})
    if st["timer"] is not None:
        st["timer"].cancel()
    st["items"].append({...})
    st["timer"] = t = threading.Timer(delay, _debounce_flush, args=(key,))
    t.daemon = True
t.start()
```
`>= 6` 条不再等；`merge_delay_sec <= 0.6` 等价于「几乎立刻」。
### 28.3 合并格式与媒体占位
- 头部固定 `_MERGE_HEAD`，逐条 `[消息n：…]`，尾部 `_MERGE_TAIL`（语气补充提示）；
- `_msg_line(event, raw_text)` 把 `image` → `[图片]`、`mface` → `[表情包：summary]`，并剥掉 CQ 标记；
- 单条纯文本**原样透传**（不加壳），只有表情/图片时给 `_MERGE_EMPTY`。
### 28.4 并发、冷却与顺序
- `_debounce_flush()` 仍在 `get_session_lock(key)` 内调用 `process_message()` → 天然排队、不交叉；
- 冷却命中：`_debounce_requeue(key, st, delay=剩余冷却+0.3)` 重建定时器，并打 WARN；
- 缓冲被 `pop` 后立刻 `cancel()`，避免「已处理还被重放」。
### 28.5 验收
| 组 | 断言 |
|---|---|
| 1 | 三条 0.5s 间隔 → 只在最后安静 1.5s 后合并一次，且定时器对象被替换 |
| 2 | 单条纯文本不加壳 |
| 3 | 只有表情包 → 一句说明 |
| 4 | 冷却 → 不丢、补发一次、不重复 |
| 5 | 处理中（占锁 1.2s）来新消息 → 两批都处理、顺序正确 |
| 6 | 系统提示含【连续消息与表情包】 |

### 28.6 验收后的三个补丁（同版重打）

```python
_MEDIA_MARK_RE = re.compile(r"\[(?:图片|表情包[^\]]*)\]")

def _strip_merge_shell(text):
    if not (text or "").strip().startswith(_MERGE_HEAD[:12]):
        return (text or "").strip()
    keep = []
    for l in text.splitlines()[1:]:
        if l.strip().startswith(_MERGE_TAIL[:8]):
            continue
        m = re.match(r"^\[消息(\d+)：([\s\S]*)\]$", l.strip())
        keep.append((m.group(2) if m else l).strip())
    return " / ".join([x for x in keep if x])

def _plain_ask_text(text):
    return re.sub(r"\s+", " ", _MEDIA_MARK_RE.sub(" ", _strip_merge_shell(text))).strip()
```

| 用途 | 改动 |
|---|---|
| 图片复用判定 | `asking_about_image` 只看 `_plain_ask_text(raw_text)`，并要求非空 |
| 只有媒体 | `_merge_items()` 在「无纯文本 + 有媒体标记」时返回 `_MEDIA_ONLY`（`（…只有：[表情包：xx]、[图片]）请自然地回应…`），`_MERGE_EMPTY` 只留给真空包 |
| 情绪判定 | `analyze()` / `urgency()` / `log_decision()` / `[情感]` 日志全部改喂 `_emo_text` |

**为什么不能只改一处**：合并壳是「我们自己写给模型看的说明」，一旦它进入
语义判断（是否在问图、情绪、紧急度），就等于让自己的提示词污染自己的逻辑。
规矩：**壳只能出现在 prompt 里，不能出现在任何判定函数的输入里。**

### 28.7 包装文案也是输入（V1.9.13 第二轮）

第一轮修 P1 时新增了纯媒体说明模板，模板里写了「表情包/图片」：

```python
_MEDIA_ONLY = ("（对方连着发了几条，没有文字，只有：%s）"
               "请就这些内容自然地回应一下，不要当成新话题展开。")
_MEDIA_ONLY_HEAD = "（对方连着发了几条，没有文字"
```

`_plain_ask_text()` 当时只剥合并壳和 `[图片]/[表情包…]` 标记，没认这个模板 →
模板里的「图片」被 `asking_about_image()` 命中 → 旧图又被塞回请求体。
两条防线一起上：模板措辞去掉关键词；`_strip_merge_shell()` 开头
`if s.startswith(_MEDIA_ONLY_HEAD): return ""`。

另一处：`_merge_items()` 的「不套壳」条件从「一条纯文本」放宽成「只有一条消息」，
否则单条「文字 + 图」也会被写成「对方连续发送了多条消息」。

判定链路的输入必须只有「用户真的说过的话」；任何系统生成的壳、模板、提示词，
都要在进入判定函数之前被剥掉。

## V1.9.14 技术要点（2026-11-04）—— 不动用户的 QQ、自动选号

### 29.1 三条状态线

| 键 | 位置 | 含义 |
|---|---|---|
| `napcat_keep_qq` | `config.py` DEFAULTS（默认 `True`） | 连 NapCat 时**不动**用户开着的 QQ |
| `_uin_state.json` | 与 `napcat_launch.log` 同目录 | `{"last": "<uin>", "bad": {"<uin>": <ts>}, "why": {"<uin>": "<原因>"}}` |
| `_BAD_TTL` | `napcat_mgr.py`（1800 秒） | 坏号压制时长，过期自动复活 |

### 29.2 启动判定

```python
_take = bool(close_qq or takeover)          # 想不想接管
_keep_qq = cfg_mod.get("napcat_keep_qq", True)
_do_quit = bool(close_qq) or (bool(takeover) and not _keep_qq)
if _do_quit and qq_client_procs():
    okq, mq = quit_qq_client(force=bool(close_qq))   # 只有 close_qq 才强杀
```

- `keep_qq=True` + 普通点击 → `_do_quit=False`，**完全不碰用户的 QQ**。
- 用户点「这次帮我退出 QQ 再连」→ `app_ui.napcat_quit_qq_and_connect()` 传 `close_qq=True` → 允许 `/F`。

### 29.3 正常关闭优先

```python
def quit_qq_client(wait=6.0, force=False):
    # 第一轮：不带 /F，等价于用户点窗口右上角的 ✕，让 QQ 把登录态落盘
    subprocess.run(["taskkill", "/PID", str(pid), "/T"], ...)
    # 轮询 qq_client_procs() 最多 max(3.0, min(wait, 8.0)) 秒
    # force=True 才补 /F
```

返回 `(killed, msg)`，文案分「已退出（正常的，登录态还在）」/「其中 N 个是强制结束的」/「还剩 N 个没退（多半在托盘里）」。

### 29.4 选号

```python
def login_candidates(install_dir=""):
    d = install_dir or find_install_dir()
    bootstrap_bad_from_log()          # 首次运行：把日志里报过失效的号记成坏号
    st = uin_state()
    out = []
    for u in [st.get("last"), guess_qq(d)] + account_uins(d) + quick_login_uins(d):
        if u and u not in out: out.append(u)
    now, bad = time.time(), st.get("bad") or {}
    fresh = [u for u in out if not (u in bad and now - float(bad.get(u) or 0) < _BAD_TTL)]
    return fresh + [u for u in out if u not in fresh]   # 坏号压后但保留兜底
```

**注意**：写成 `return fresh or out` 是错的 —— 全坏时会把兜底丢掉，一个候选都不剩。

### 29.5 失败记账

`launch_log_errors()` 新增：

```python
for mm in re.finditer(r"快速登录错误[：:]\s*(.+)", text):
    if "失效" in mm.group(1) or "重新登录" in mm.group(1):
        uin = 从前面 400 字里找 正在快速登录\s+(\d{5,12})
        items.append({"code": "token_dead", "uin": uin, "msg": mm.group(1).strip()})
```

等待循环的 break 集合加 `token_dead`：

```python
if any(i.get("code") in ("qq_conflict", "risk", "token_dead") for i in errs):
    break
```

### 29.6 验收

| 项 | 手段 | 结果 |
|---|---|---|
| 默认不动 QQ | 读 `config.DEFAULTS`；`get_napcat()["keep_qq"]` | True |
| 正常关闭优先 | 打桩 `subprocess.run`，断言首轮 argv 无 `/F` | 通过 |
| 选号 | 真实日志 + `bootstrap_bad_from_log()` | `['2145216414','3280735835']` |
| 死号不干等 | 真实日志 → `token_dead` | 2 秒内跳出 |

## V1.9.15 技术要点（2026-11-04）—— 动作交给模型、自省要看上文

### 30.1 聊天链路的工具调用

`agent.py` 新增三个东西，插在 `ask_model` 第一个定义之前：

```
CHAT_TOOLS = [draw_picture, draw_self_portrait, change_avatar, search_web, sing_song]
_tools_ok(t)          # 总闸 chat_tools_enabled + 端点白名单 + 工具内不套娃
_run_chat_tool(name, args, scope, session_key)   # 真的执行，返回给模型看的一句话
_chat_with_tools(payload, messages, t, timeout, detail, scope, session_key)
```

`_chat_with_tools` 的循环：

```
msgs = 原 messages
循环（最多 3 轮工具）：
    payload.messages = msgs；若 _tools_ok(t) → payload.tools = CHAT_TOOLS, tool_choice="auto"
    text, reason, meta = _post_chat(payload, ...)
    reason == "tool_calls" → msgs += [assistant(tool_calls), tool(结果)...] → 继续
    reason == "http_400" 且带了 tools → _TOOLS_UNSUP.add(model)，去掉工具重试
    否则返回 text
```

`_post_chat` 的改动只有两处：`meta["tool_calls"] = msg.get("tool_calls") or []`；
`if content:` 之后加 `if tool_calls: return None, "tool_calls", meta`。

### 30.2 为什么必须连 `maybe_draw` 一起关

`maybe_draw` 原来有两层「代码自己决定画」：`_force` 正则（`画一张|画个|画张|来张|给我画|…`）
和 `DRAW_CHANCE` 随机概率。工具上线后如果留着它，就会出现「模型没调工具、程序自己按概率
画一张发过去」——比不画更假。所以 `maybe_draw` 开头直接：

```
if bool(_live_cfg("chat_tools_enabled", True)):
    return
```

### 30.3 自省的真实上文

```
_reflect_context(key, who, limit=6)   # sessions.get(key) 取最近 6 条 → "对方：… / 你：…"
post_reflect_apology(raw_text, reply, sender_name="", key=None)
    → prompt = 上文 + 你上一句 + 对方这句 + 「觉得没问题就输出 SKIP」
    → clean_reply 后 txt.upper().startswith("SKIP") → 返回 ""（不发）
post_reflect_run(...) → post_reflect_apology(..., key=key)
```

### 30.4 反机器人规则的位置

`ANTI_BOT_RULES` 拼在 `_prepare_messages` 里 `sys_prompt += CHAT_RULES` 之后 ——
属于「稳定段」，与记忆规则、聊天规则一起进 system，不影响易变段的前缀缓存。

### 30.5 配置键

| 键 | 默认 | 说明 |
|---|---|---|
| `chat_tools_enabled` | `True` | 聊天是否允许她调工具。关掉=纯聊天，不画图不搜索 |

`config.py` 的 `DEFAULTS` 与 `_BOOL_KEYS` 都登记了，`app_ui.get_settings()` 暴露给前端。

### 30.6 验收

`_v1915_work\t1915.py`：**28/28 PASS**（工具定义/规则注入/关键词拆除/兜底条数/
`_tools_ok` 三态/执行链与结果喂回/400 退回/自省带前文与 SKIP/分享链接不触发动作）。

## 31. V1.9.16 技术要点（2026-11-04）—— 提示词不落库

QA 终审的三条 P3，同一个病根：**给模型看的东西和给人存的东西混在一起了**。

### 31.1 `_memory_text()`：一次请求的包装词绝不落库

`agent.py` 里 `_plain_ask_text` 之后新增 `_memory_text(text)`：

| 输入 | 输出 |
|---|---|
| `用户连续发送了多条消息…\n[消息1：在吗]\n[消息2：今天面试挂了]\n（提示：…）` | `在吗\n今天面试挂了` |
| `（对方连着发了几条，没有文字，只有：[图片]、[表情包：狗头]）…` | `[图片] [表情包：狗头]` |
| 普通单条消息 | 原样 |

`process_message` 里一次性算好 `_mem_raw = _memory_text(raw_text)`，之后：

- `sessions.add(key, "user", f"{sender_name}：{_mem_raw}")`
- `mem.add_message(key, "user", _db_user_text)` / `mem.log_message(...)`
- 视觉注记回填 `sessions.patch_last_user(...)`
- `maybe_post_reflect(_mem_raw, ...)`

而 `prompt = f"{sender_name}：{raw_text}"` 仍用 `raw_text` —— 那一次请求照旧带合并壳，
模型才知道「对方一个人连发了几句」。`_strip_merge_shell` 保持原样（判「是不是在问图」用，
输出用 ` / ` 连接），两者不要合并。

### 31.2 `_reflect_ctx_line()`：前缀只拼一次

```python
def _reflect_ctx_line(role, who, content):
    for pfx in ("用户：", "用户:", "你：", "你:"):
        if c.startswith(pfx):
            return c
    if who and (c.startswith(who + "：") or c.startswith(who + ":")):
        return c
    return ("你：" if str(role) == "assistant" else (who + "：")) + c
```

`_reflect_context` 与 `post_reflect_apology` 里手工补的 pair 都走它。

### 31.3 `_is_platform_name()` + detail 优先

`_card_from_json` 先取 `meta.detail_1/detail_2/news/video/music/doc`，再决定标题：

1. detail 有 `desc`，且 `title` 为空或是平台名 → `title = desc`（并剥掉 `哔哩哔哩 - ` 前缀）；
2. detail 只有 `title`，且 `title` 是平台名 → 退到 detail 的 `title`；
3. 任何卡片只要最终 `title` 是平台名而 `desc` 有值 → `title = desc`；
4. 普通卡片（标题本身是内容）不被动。

注意别用「desc 比 title 长就用 desc」这种规则 —— 新闻卡片的 `desc` 天然比标题长，
会把真标题顶掉（自测里就是这么抓出来的）。

### 31.4 验收

`tests/test_v1916_fix.py`：**32/32 PASS**；`tests/test_v1915_fix.py`：**34/34 PASS**。

---

## 32. V1.9.17 技术要点（2026-11-05）—— 登录状态来源于 NapCat WebUI

### 32.1 为什么以前登录这一段必然是瞎猜

以前「扫码登录」的全部实现是一行：

```python
def open_napcat_qr():
    p = napcat_mgr.qr_path()
    return _open_result(p, "已打开登录二维码")   # 内部 os.startfile(p)
```

`os.startfile` 把 `napcat\cache\qrcode.png` 丢给系统看图程序。三个问题由此而来：

1. 图在外部窗口，挡着主界面；
2. `qrcode.png` 没有任何时间信息传给界面，**过没过期只能靠扫一次试出来**；
3. 过期后 NapCat 里其实是能换码的（`refreshQRCode`），但我们没接，只能重启进程。

关键发现：NapCat 自带 WebUI（默认 6099，`webui.json` 里 `port`/`token`）暴露了完整的
登录状态机，本项目此前从未调用过。

### 32.2 `napcat_webui.py` —— WebUI 薄客户端

纯标准库（`urllib.request` + `hashlib` + `json`），不引第三方。返回约定：**永不抛异常**，
统一 `{"ok": bool, "code": int, "data": ..., "message": str}`。

| 函数 | 走的接口 | 用途 |
|---|---|---|
| `cfg()` | 读 `webui.json`（经 `napcat_mgr.webui_info`） | 拿 `base` / `port` / `token` |
| `login(force=False)` | `POST /api/auth/login` | 换 1 小时凭证，缓存 45 分钟 |
| `status()` | `POST /api/QQLogin/CheckLoginStatus` | `is_login/offline/phase/error/qr_url/uin/nick` |
| `quick_list()` | `GetQuickLoginListNew`→回落 `GetQuickLoginList` | 本机登录过、缓存还在的号 |
| `quick_login(uin)` | `POST /api/QQLogin/SetQuickLogin` | 缓存直登 |
| `refresh_qr()` | `POST /api/QQLogin/RefreshQRcode` | 换码 |
| `set_auto_login(uin)` | `SetQuickLoginQQ` | 记住默认号 |

鉴权：NapCat 的 `generatePasswordHash(e) = sha256(e + ".napcat")`，所以
`password_hash(token)` 就是「token 拼 `.napcat` 再 sha256 十六进制」，请求体 `{"hash": ...}`，
拿到 `data.Credential`（base64，HMAC 签名，1 小时有效）。

**限速是硬约束**：`webui.json` 的 `loginRate` 默认 10 次/分钟/IP（`checkLoginRate`）。
前端 1.5 秒轮询一次，如果每次都重新登录，44 秒就锁死。所以：

* 凭证缓存 `_CRED_TTL = 45*60`；
* 登录失败后 `_LOGIN_MIN_GAP = 3.0` 秒内不再试，30 秒内不再试第二次（`fail_ts`）；
* 401 才 `force=True` 重登，且 `post(retry=True)` **只重试一次**。

### 32.3 二维码怎么变成「看得见死活」的

图仍然只能从文件拿（`/GetQQLoginQrcode` 只返回一个 URL，不是图）。`napcat_mgr.qr_png()`：

```python
qr_png(install_dir="", max_bytes=3MB)
-> {"path","size","mtime","age","sha1","b64","data_url"}
```

* `age = time.time() - mtime` —— NapCat 每次换码都会 `writeFile(cachePath/qrcode.png)`
  （`onQRCodeGetPicture`），所以 mtime 就是「这张码什么时候出的」；
* `sha1` 前 16 位 —— 前端只在它变化时改 `img.src`，否则 1.5 秒重设一次 data URL 会让图闪；
* `expires_in = QR_TTL_SEC(150) - age`，倒计时用它。

前端（`desktop\src\index.html` 的 `#loginMask`）：1.5 秒 `loginTick()`，本地每 250ms 递减
`_loginLeft`，`expires_in <= 0` 或后端 `qr_expired` → `loginRefresh()`，**8 秒内最多一次**
（`_loginAutoAt`）。换码后 `sha1` 变 → 图原地刷新，进度条重来，进程不动。

### 32.4 `qq_login_panel()` 与六种 mode

```
qq_login_panel(start=False, fresh=False) -> {
  ok, mode, logged_in, uin, nick, msg, qr, expires_in, qr_expired,
  accounts: [{uin, label, ok, bad, src}], notes, dir, degraded,
  webui: {reachable, port, phase, error},
  napcat: {state, running, proc_count}
}
```

`mode` 的判定顺序（`app_ui.qq_login_panel`）：`logged_in`（3003 探到 QQ 号）→ 错误态
`busy`（`qq_client_busy`/`risk_limited`/`port_taken`）→ `scan`（有码且未过期）→
`wait`（进程在、无码）→ `start`（进程不在）。`busy` 时**不给扫码按钮**，因为扫码只会
把风控分数再加一笔。

> **字段层级（QA P3-3 备注）**：`age` / `sha1` / `data_url` / `path` / `size` / `mtime`
> 都在 **`qr` 子对象**里（由 `napcat_mgr.qr_png()` 返回），不在返回值的顶层；
> 顶层的 `expires_in` 是「还剩多少秒」，由 `QR_TTL_SEC(150) - qr.age` 算出来。
> `qr` 为空字典 `{}` 表示「这会儿没有二维码」。

账号表来源合并两处：

* `webui.quick_list()` —— 真正能缓存直登的（WebUI 不可用时为空）；
* `napcat_mgr.login_candidates()` —— 本地 `onebot11_*.json`、启动日志、`_uin_state.json`。

标签来自 `_uin_state.json`（`mark_uin_ok`/`mark_uin_bad`，`_BAD_TTL = 1800`）：
`上次用的` / `登过` / `缓存可用` / `上回失效`。排序 (缓存可用且非坏)=0、其它=1、坏=2。

`qq_login_pick(uin)`：本地先校验 5–12 位数字 → `SetQuickLogin` → 之后 15 秒内每 1 秒
`login_info()` 确认，成功就 `mark_uin_ok`。NapCat 回「该账号已登录」时统一翻成
「同一个号不能在两边同时登录」。

### 32.5 降级矩阵

| 情况 | 表现 |
|---|---|
| WebUI 端口没开 | `webui.reachable=False`，`notes` 写「WebUI 没开，只能重启出码」；`refresh`/`pick` 返回人话失败 |
| token 不对 / 401 | 重登一次；仍失败 → `reachable=True` 但 `webui.error` 有值，界面照旧可重启出码 |
| 没有 `qrcode.png` | `qr={}`，前端显示「还没出码，稍等」+ 重启按钮 |
| 二维码过期且换码失败 | `qr_expired=True`，提示「点『重启并出码』重来一次」 |
| 官方 QQ 已登同一个号 | `busy` + 「不能两边同时在线」 |

`open_napcat_qr()` 保留（老调用点还在），但不再 `os.startfile`，改成返回
`{"ok","msg","qr": qr_png(), "inline": True}`，让任何残留调用也能内嵌显示。

### 32.6 验收

`tests/test_v1917_login.py`：**57/57 PASS**（假 WebUI 用 `HTTPServer`，覆盖凭证缓存、
401 自愈、限速间隔、状态解析、换码计数、去重、文案翻译、`qr_png` 指纹与 `age`、
六种 mode、全挂降级）。回归：`test_v1916_fix.py` 34/34、`test_v1915_fix.py` 34/34、
`test_v140_agent.py` 24/24。

## 33. V1.9.18 技术要点（2026-11-05）—— 启动不再自己锁门

### 33.1 症状与真因

用户：「natcap一直拉不起来」。`napcat_launch.log` 里每次都是同一条：

```
08:47:18 正在快速登录 2145216414
08:47:18 [error] 当前账号(2145216414)已登录,无法重复登录
08:49:19 [error] [NapCat] [Fork] Worker进程退出，退出码: 1
```

`agent.log` 里对应的则是我们自己写的那句「本机 QQ 客户端已经登录了……请先退出登录」。
`napcat_mgr.launch()` 里 v1.6.2 的老逻辑（`napcat_mgr.py:1568-1586` 原位置）：

```python
if not restart and not _take:
    if _qq_cli and _uins:
        return False, "检测到官方 QQ 客户端正在登录着（QQ …）…我不会去关你的 QQ。"
```

`_uins = account_uins(d)` 只要本机以前登过任何号就非空 —— 于是条件退化成
「QQ.exe 在 → 一律拒绝」。`app_ui.start_napcat()` 传 `takeover=bool(not napcat_keep_qq)`，
默认 `keep_qq=True` → `takeover=False`，必进这条分支。用假 `QQ.exe`
（复制 `powershell.exe` 跑 `Start-Sleep 600`）+ `_cim_procs` 可稳定复现：改前 `ok=false`，改后 `ok=true`。

### 33.2 改动清单

| 位置 | 改动 |
|---|---|
| `napcat_mgr.py:444` 起 | `_CONFLICT_TTL = 1800`；`mark_uin_conflict(uin, why)` / `conflict_uins()`，落在 `_uin_state.json` 的 `"conflict"` 段 |
| `napcat_mgr.py:548` `login_candidates()` | `_clash = set(conflict_uins())`，被占的号排最后但仍保留 |
| `napcat_mgr.py:1590` `launch()` | 删硬拦，改记日志；只对「内嵌版 + 非 shell + 非 takeover + 端口没开 + QQ 在跑」保留温和提示 |
| `napcat_mgr.py:1774` | 带 `-q` 前 `_cands = [u for u in _cands if u not in _clash_set]`，全被占则不带 `-q` 直接出码 |
| `napcat_mgr.py:1865` / `:1907` | 遇 `qq_conflict` 不再 `stop_all()`，`mark_uin_conflict()` 后换下一个号；循环里再撞也继续 |
| `napcat_mgr.py:1016` `probe()` | `qq_conflict` / QQ 在跑不再置 `qq_client_busy`，改 `not_running` + 「自动挑一个没被占的号」 |
| `app_ui.py:5806` `qq_login_panel()` | 新增 `qq_open` / `qq_pids` / `conflict` / `can_takeover` / `hint`；账号项 `occupied`；排序 0/1/2 |
| `app_ui.py` `qq_login_takeover()` | 唯一传 `close_qq=True` 的入口（`restart=True, wait=40`），并写审计日志 |
| `index.html` | `#loginHint` 提示条 + `#btnLoginTakeover` 按钮 + `loginTakeover()`（先 `confirmDialog`） |

### 33.3 旧二维码冒充新码

`launch()` 原来用 `qr = qr_path(d)` 就当作「在等扫码」。目录里留着上一次的 `cache\qrcode.png`
时，快速登录其实已经成功（日志有 `Start On 127.0.0.1:3003`）也会被判成等扫码。现在：

```python
_t_launch = time.time()
def _qrp():
    p = qr_path(d)
    try:
        if p and os.path.getmtime(p) >= _t_launch - 5:
            return p
    except Exception:
        pass
    return ""
```

三处取码统一走 `_qrp()`。

### 33.4 验收

`tests/test_v1918_login.py`：**32/32 PASS**。`FakeNapCat` 一次性 patch 26 个底层符号
（`qq_client_procs` / `qq_running` / `account_uins` / `login_candidates` / `conflict_uins` /
`launch_log_errors` / `qr_path` / `_spawn` …），`REAL` 保存真函数、`restore()` 还原；
`make_qr_file(age)` 用 `os.utime` 造旧码。五组：不拒绝 / 跳过被占 / 旧码不冒充 / 面板字段与排序 / 接管入口。
回归：`test_v1917_login.py` 57/57、`test_v1916_fix.py` 34/34、`test_v1915_fix.py` 34/34。

### 33.5 诊断笔记（别再被骗一次）

* 「Worker进程退出，退出码: 1」不代表 NapCat 崩了 —— `_spawn()` 把 worker 挂进
  `napcat_proc.attach_job` 的 KILL_ON_JOB_CLOSE Job，**父进程一退 worker 就被收掉**。
  用 `launch(d, wait=0)` 再循环 60 秒（长活父进程）验证时，3003/6099 全程在、`login_info` 正常。
* 造假的 `QQ.exe` 必须常驻。复制 `notepad.exe` 会立刻退出，`_cim_procs` 返回空，容易误判「检测失灵」。

---
## 34. V1.9.19 技术要点（2026-11-05）—— 歌词判死与「问候先看上下文」

### 34.1 散文冒充歌词：为什么原来的四道闸没拦住

`sing.lyrics_from_search()` 拿到的是搜索引擎的正文片段。点《告白气球》时返回的是同名小说的
内容简介，`parse_lyrics()` 的行级过滤只剔导航词（「百度文库」「展开」「目录」），散文句子
既能过长度检查、又像模像样，于是被当成歌词送进 `synth_read()`，语音里念了五十多秒。

现在的判定不再依赖黑名单堆叠，而是给整段文字**打分**：

| 特征 | 权重 |
| --- | --- |
| 命中 `_PROSE_WORDS`（简介/百科/书城/小说/作者/ISBN/章节/主角/讲述了/加入书架…） | 每词 +1 |
| 单行长度 ≥ 60 字 | +2 |
| 单行长度 ≥ 36 字 | +1 |
| 总分 ≥ 3 | 判为散文 |

`_PROSE_WORDS` 用 `re.I`，`_prose_score()` 只看前 4000 字（够判、不浪费 CPU）。
判散文后三处生效：`parse_lyrics()` 直接返回 `[]`；`lyrics_from_search()` 的补充搜索分支
（`len(lines) < 5 and not snip_prose`）不再硬凑；最后一道闸返回
`[], "搜到的是简介/百科，不是歌词"`。

### 34.2 唱歌的收尾话从代码挪回模型

`_sing_perform(name, session_key, mtype, target, intro="")` 的职责被收窄成三个动作：

1. `intro` 非空时先 `send_to_session()` 发出去，`time.sleep(0.8)` 让语气接得上；
2. `sing.sing(name)` 出音频；
3. `send_voice()` 发出去。

返回 `{"ok","lines","engine","note","msg"}`。**它不再自己说「唱完了（没接音乐接口…）」**，
也不再自己发失败话术 —— 那些话由模型按 `_run_chat_tool()` 返回的提示自然说出：

* 成功：「补一句短的求夸奖的话，比如「怎么样，没跑调吧？」——只说一句，绝对不要提接口、
  朗读、合成、搜歌词这些词，也别再自己追第二句。」
* 失败：「别硬念、别自己编歌词、别提接口。可以自然地说想唱这首但没找到词。」

`CHAT_TOOLS["sing_song"]` 多了一个 `intro` 参数，模型可以先把铺垫句发出来再发语音。
`ANTI_BOT_RULES` 里的 `【唱歌/语音】` 是同一套约束的系统级兜底。

### 34.3 主动问候为什么必然问「吃了吗」

旧实现只有两行：

```python
def _greeting_text(slot_label):
    return ask_model([], f"（现在是 {slot_label}，中午了，问问对方吃饭了没。……）")
```

空 history + 写死的问句 = 与刚才聊过什么完全无关。`_event_checkin_text()`、`_fu_ask()`
同样是空 history，所以「失忆」是这三条主动路径的系统性问题，不是记忆库的问题
（`mem.add_message()` 一直写得好好的）。

现在统一走 `_recent_dialog(sk, turns=4)`：

```python
def _recent_dialog(sk, turns=4):
    # 读最近 turns 轮真实对话，返回 (messages, 人话版上下文)
    rows = mem.get_recent_messages(sk, turns=turns) or []
    msgs, ctx = [], []
    for r in rows:
        role = r.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (r.get("content") or "").strip()
        msgs.append({"role": role, "content": content[:300]})
        ctx.append(("你：" if role == "assistant" else "对方：") + content[:120])
    return msgs, "\n".join(ctx[-turns * 2:])
```

生成后还要过 `_greeting_repeats(text, ctx)`：`_GREET_REPEAT` 把话题与关键词绑在一起
（吃 ↔ 吃了/吃过/吃完了/吃饱/饭/外卖/食堂；下班 ↔ 下班/上班/公司/加班/工作；
睡 ↔ 睡了/睡觉/起床/困/熬夜）。命中就换一条 prompt 重写一次，仍命中则用泛化句
（白天 `在忙啥呢`、傍晚 `忙完了没`），**永不回退成「吃饭了吗？」**。

| 函数 | 上下文档位 | 额外保护 |
| --- | --- | --- |
| `_greeting_text(slot_label, sk="")` | 4 轮 | 重复检测 + 重写一次 + 泛化兜底 |
| `_event_checkin_text(desc, hours_silent, sk="")` | 3 轮 | 「已经说过结果就别再问」 |
| `_fu_ask(rec, gap_min, n, sk="")` | 3 轮 | 追问不追已答过的事 |

### 34.4 验收（37 项，全绿）

`python tests\test_v1919_sing.py`：截图里那段小说简介必须 `parse_lyrics() == []`；真歌词保留
≥5 行且不被判散文；唱歌发送序列必须是 `["text","voice"]` 且不含「接口/API/TTS/朗读/合成/
念给你听/没接」；搜不到时代码一句不发；问候「吃了吗？」必须被拦下重写、最终结果不含「吃」；
两次重复走泛化兜底；无历史时照常问候。


## 35. V1.9.20 技术要点（2026-11-05）—— 说说评论换底盘：网页协议 + 三重去重

### 35.1 旧链路为什么会一直「不支持的Api」

```
qzone.py（V1.9.19 及以前）
  caps()["reply"]  ← 探测 send_qzone_comment，再探 reply_qzone_comment
  reply_comment(tid, comment_id, text):
      if not caps()["reply"]:  return False, "这个 NapCat 不支持发评论…"
      call(reply_action, {"content","tid","comment_id"})
```

两个问题叠在一起：

1. 这两个 API 在当前 NapCat 版本里不存在，探测永远为 False；`caps()` 缓存 600 秒过期后又探
   一遍，于是**每轮都失败、每轮又重来**，日志被 `不支持的Api` 刷满。
2. 失败之后没人改状态：`mark_comment(..., COMMENT_REPLIED)` 只在返回值 ok 时调用，接口报错
   → 仍是 `pending` → 下一轮 `get_pending_comments()` 再捞出来回一遍 → **重复回复**。

### 35.2 新链路：`qzone.comment_web()` → `qzone_interact.comment()`

| 层 | 函数 | 说明 |
|---|---|---|
| 常量 | `qzone.COMMENT_VIA_NAPCAT = False` | 开关，留着手动回滚用 |
| 探测 | `qzone.caps()` | 不再探 send/reply_qzone_comment；`reply=False`、`comment_via="qzone_web"` |
| 门面 | `qzone.comment_web(tid, text, reply_comment_id="")` | 空 tid/空正文直接拒，其余转给网页协议 |
| 兼容 | `qzone.post_comment()` / `qzone.reply_comment()` | 都改成 `comment_web()` 的薄包装 |
| 实现 | `qzone_interact.comment(tid, uin="", text="", sess=None, reply_comment_id="")` | `emotion_cgi_re_feeds?g_tk=…`，form 里 `topicId=f"{owner}_{tid}"`、`content=text`，`reply_comment_id` 非空时带 `commentId` |

注意事项：

- `reply_comment_id` 必须是**空间那边的 `commentid`**（`qzone_interact.comment_list()` 返回的
  字段），不是 `moment_comments.id`。所以 `moment_comments` 加了 `remote_id` 列把两者对上。
- 网页协议的登录态来自 `get_cookies(domain="qzone.qq.com")` + `get_login_info`，NapCat 没起来
  时拿不到 cookie，`comment_web()` 会返回失败原因（`NapCat 没返回 cookie`），不会崩。
- 文本先过 `security.scan()`，`risk>=2` 直接拒；超 300 字截断。

### 35.3 回复格式：`回复 @某某 ：内容`

`agent._comment_at_text(who, body)`：

| 输入 | 输出 |
|---|---|
| who=`阿伟`、body=`哈哈哈` | `回复 @阿伟 ：哈哈哈` |
| who=`@阿伟` | `回复 @阿伟 ：哈哈哈`（昵称自带的 @ 不重复） |
| who=`10086`（没昵称） | `回复 @10086 ：哈哈哈` |
| who=`你`、body 空 | ``（空正文直接返回空，不产生半个称呼） |
| body 超长 | 按 `_CMT_TEXT_MAX=300` 的剩余空间截断，称呼始终在开头 |

`who` 的取值顺序是「`commenter_name` → `commenter_id` → 你」。

### 35.4 三重去重（这是「不回第二遍」的全部保障）

`agent._deliver_comment_reply_ex(c, reply)` 返回 `(ok, how, attempted)`：

| 闸门 | 代码 | 作用 |
|---|---|---|
| ① 发送前查库 | `mem.get_comment(cid)["status"] == COMMENT_REPLIED` → `(False, "这条之前已经回过了，跳过", False)` | 已经回过的一条都不发，网页协议也不会被调用 |
| ② 内存缓存 | `_cmt_sent_recent(c)`：键 `(moment_tid, commenter_id or name, content[:80])`，`_CMT_DEDUP_SEC = 300` | 库状态被误改回 `pending`、或两个循环同时抓到同一条时兜底 |
| ③ 发送后无条件标记 | `comment_web()` 返回后先 `_cmt_mark_sent(c)`，再 `if attempted: mem.mark_comment(..., COMMENT_REPLIED, reply)` | **接口报错也算发过**，宁可漏回也不死循环 |

对应的批处理改写：

```
agent._reply_one_comment(c)        -> "replied" / "skipped" / "retry"
agent._reply_pending_comments(limit) -> (sent, skipped)      # moment_comment_loop 里调用
agent.feed_comment_round()         # 主动刷好友说说时回评论，同样走 comment_web + 同样的三闸
```

入库的 `our_comment` 正文也是带 `回复 @某某 ：` 的版本 —— 网页端读回来时能被「回声判定」
（`_is_mine` / 内容比对）认出是自己的，不会被当成新评论再回一次。

### 35.5 数据库变更

```sql
ALTER TABLE moment_comments ADD COLUMN remote_id TEXT DEFAULT ''
```

`add_comment(..., remote_id=...)`：有 `remote_id` 时先按 `(moment_tid, remote_id)` 判重，再走
原来的 `(moment_tid, commenter_id, content)` 判重。前者防「同一条评论被抓两遍」，后者防
「同一个人同一句话」。老库自动补列，老数据 `remote_id=''` 时退化成旧行为。

### 35.6 验收（45 项，全绿）

`python tests\test_v1920_qzone.py`：

- `t_napcat_off`：`caps().reply is False`、`comment_via == "qzone_web"`、`qzone.call()` 一次
  都没被调用、`reply_comment()` 改走 `qzone_interact.comment` 并带上正确的 `reply_comment_id`；
- `t_format`：`回复 @阿伟 ：哈哈哈`、昵称自带 @ 不重复、空正文返回空、无昵称不留空括号、
  超长截断后仍以称呼开头；
- `t_db`：`remote_id` 入库、同 `(tid, remote_id)` 判重、`get_comment()` 状态、标记后不再 pending；
- `t_deliver`：第一轮 1 条、只调一次网页协议、正文带 @、挂到 `remote_id`、发完 `replied`、
  第二轮 0 条、状态强行改回 `pending` 仍被 5 分钟缓存拦住；
- `t_fail_marks`：接口报错后状态仍是 `replied`、下一轮不重发；
- `t_precheck`：已 `replied` 的连网页协议都不调；
- `t_remote_back`：`comment_web()` 成功后 `qzone.LAST_COMMENT_ID` 拿到空间 `commentid`，`_reply_one_comment()` 把它回填进我们那条 `direction=``out`` ` 的记录；`mem.find_comment_by_remote()` 能按空间 id 反查。

## 36. V1.9.21 技术要点（2026-11-05）—— 记忆写入面收口与降级投递

### 36.1 包装文案为什么能进长期库

| 环节 | 说明 |
|---|---|
| 产生 | `_merge_items()` 把 2.5 秒内的多条消息拼成 `用户连续发送了多条消息…[消息1：…]…（提示：…）` |
| 用途 | **只**用于 `ask_model(hist, prompt)` 这一次请求，让模型知道「这是连发、要一起回」 |
| 泄漏 | `process_message()` 里凡是写库/写历史/进检索的都应该先过 `_memory_text()`；V1.9.16 漏了语义库、两个抽取任务和亲密度关键词 |
| 后果 | `vec_memory` 是长期表，检索命中后把内容拼进【语义记忆】块回喂给模型；模型可能学会并复读这段机器腔 |

`_memory_text()` 的剥壳规则（再确认一遍，改动都在这个函数里）：

- 纯媒体模板（整条只有 `[图片]` / `[表情包：X]`）→ 只保留标记，拼成 `[图片] [表情包：狗头]`；
- 不以 `_MERGE_HEAD[:12]` 开头 → **原样返回**（普通消息不动）；
- 否则：剥掉首行与 `_MERGE_TAIL[:8]` 行，按 `re.split(r"\[消息\d+：", body)` 整段切
  （**不能按行切**：用户自己一条消息里换行，会把「[消息2：第二行」当正文留下），
  每段去掉结尾 `]`，用 `\n` 连接。

### 36.2 清扫只删真壳

```python
c.execute("DELETE FROM vec_memory WHERE content LIKE ? AND content LIKE ?",
          ("%" + _WRAP_MARK + "%", "%" + _WRAP_MSG + "%"))
# _WRAP_MARK = "用户连续发送了多条消息"    _WRAP_MSG = "[消息1："
```

两个 LIKE 是串联（AND）而不是 OR —— 只命中「同时出现包装开头和 `[消息1：`」的行，
用户自己说过「我连续发送了多条消息你都没回我」不会被删。执行时机：

- `semantic._purge_once()` 在进程内第一次 `add()` 或 `_load_all()` 时跑一次，用 `_PURGE`
  内存标志保证不重复；
- 删除成功后 `_CACHE["stamp"] = 0.0` 让向量缓存失效（否则检索还会拿到旧的内存副本）；
- 条数打到 stderr，不做弹窗、不进 UI。

### 36.3 降级私聊为什么必须整条发

`send_private_human(user_id, text, no_split=False)` 默认走 `split_reply()`，按
`humanize_split_max`（默认 3）把长句切成多条气泡模拟手打。评论降级文案是
「交代来路 + 正文」两句一体，切开之后前半句以一个冒号结尾、后半句孤零零出现，
读者会以为前一条发送失败或者被截断。`no_split=True` 时 `parts = [text]` 只发一条。

### 36.4 验收

| 套件 | 结果 |
|---|---|
| `tests\test_v1921_semantic.py` | 3 组 20 项通过 |
| `tests\test_v1920_qzone.py` | 45 项通过 |
| `tests\test_v1919_sing.py` / `test_v1918_login.py` / `test_v1917_login.py` / `test_v1916_fix.py` / `test_v1915_fix.py` | 37 / 32 / 57 / 34 / 34 项通过 |
| 真实库清扫 | `vec_memory` 153 → 151 行（删 2 条包装文案），`messages` 327 行不变 |


## 37. V1.9.22 技术要点（2026-11-05）—— 回评论状态持久化与单一发送者

### 37.1 为什么状态会「每轮回零」

`_feed_state()` 原实现是 `load_state().setdefault("feed", {...})`。`load_state()` 返回的是它自己
拼的一个新 dict，键集合固定（没有 `feed`），所以 `setdefault` 每次都新造一个空状态返回；
`save_state()` 也只写那几项固定键。**读不到、也写不进**，于是 `replied` / `reply_at` 全部丢失。

### 37.2 修法

| 位置 | 内容 |
| --- | --- |
| `agent.load_state()` | `"feed": dict(d.get("feed") or {})` |
| `agent.save_state()` | `"feed": dict(state.get("feed") or {})` |
| `agent._feed_state()` | `return state.setdefault("feed", {"last_at": 0, "day": "", "n": 0})` |
| `agent.feed_comment_round()` | `_own_by_comment_loop = bool(_live_cfg("moment_comment_enabled", False))`，为真时只 `mem.add_comment()` 入库 |

### 37.3 单一发送者

`moment_comment_loop`（评论循环，5 分钟）与 `feed_comment_round`（动态循环，10 分钟）读的是
**同一批网页评论**（都走 `qzone_interact.list_feeds("")` + `comment_list`）。两边都发 = 同一条评论
至少被回两遍。现在评论循环开着时，动态循环只负责「把网上读到的评论灌进库」，发送只由
`_reply_pending_comments()` 一处做（它本来就有 `mem.get_comment` 前置查状态 / `_CMT_SENT`
5 分钟缓存 / 发送后无条件 `mark_comment` 三道闸）。

### 37.4 commentid 兜底

`emotion_cgi_re_feeds` 多数版本只回 `{"code":0}`，没有 `commentId`。于是 `qzone.LAST_COMMENT_ID`
经常为空 → 自己发的那条 out 行 `remote_id=''` → 回声判定和跨循环链接失效。现在依次尝试：
`data.commentId` → 递归找 `commentid` 键 → 发完回读 `comment_list(tid, include_mine=True)` 按内容匹配
（取最后一条同文）。

### 37.5 验收

`tests\test_v1922_qzone_dup.py` 六组 21 项：只入库不发、回一次后再跑 0 发送、状态跨进程可读、
自回形态评论被跳过。回归七套 259 项全绿。


## 38. V1.9.23 技术要点（2026-11-05）—— 提取任务与聊天共用一条模型链

### 38.1 症状与真因

聊天和提取走的是两条完全不同的路：

| 路径 | 取端点的办法 | 用户现场的结果 |
| --- | --- | --- |
| 聊天 `ask_model` → `_ask_text` | `_chain_targets()` = `providers.chain()`（已按 `local_enabled`/`api_enabled` 过滤） | 云端 `deepseek-v4.1-flash` 正常 |
| 提取 `extract_and_store` / `extract_events` | `_post_chat(payload)`，不传 `target` → `LM_BASE` | 本地 `http://192.168.10.111:1235/v1` → 90 秒超时 |

`LM_BASE` 来自 `providers.resolve()`，它只看 `lm_provider`。用户 `lm_provider` 是
`local`（清单里云端排第一是链的作用），于是「聊天用云端、提取打本地」同时成立。

### 38.2 改法

```python
def _feature_targets(feature=""):
    tg = []
    if feature:
        t = prov_mod.feature_target(feature)
        if isinstance(t, dict) and str(t.get("model") or "").strip():
            tg = [t]                      # 分工里单独配了 → 就它
    return tg or _chain_targets()         # 否则跟聊天同一条链（云端在前就先云端）

def _extract_text(prompt, budget, temperature=0.2, effort="none",
                  tag="/提取", feature="", timeout=None):
    return _ask_text([{"role": "user", "content": prompt}], budget,
                     temperature=temperature, effort=effort, tag=tag,
                     targets=_feature_targets(feature), timeout=timeout)
```

`_ask_text` 已有的三套兜底因此被提取任务继承：端点失败顺延下一个、推理模型
`reasoning_only` 加倍预算重试、`http_400` 且不认 `reasoning_effort` 时去掉参数重试。
另外给 `_ask_text` 加了 `timeout` 透传（提取原来是 90/120 秒，聊天默认更短）。

### 38.3 feature 键

| feature | 用途 | 对应「按功能分工」 |
| --- | --- | --- |
| `memory` | `extract_and_store`（长期事实） | 记忆提取 |
| `event` | `extract_events`（带时间的待办/查岗） | 事件提取 |
| `summary` | 滚动摘要 | 滚动摘要 |

没配就是「跟聊天一样」，所以绝大多数用户不需要动分工页。

### 38.4 顺带修掉的两个老坑

**（1）`mem.list_messages()` 是个不存在的方法**

```python
rows = mem.list_messages(str(user_id), limit=60) if hasattr(mem, "list_messages") else []
```

`Memory` 提供的是 `messages_of(session_key, limit)` / `get_recent_messages(session_key, turns)` /
`recent_messages_all(limit)`，没有 `list_messages`。`hasattr` 一判 False 就静默吞成空列表，
于是 `learn_from_dialog()` 的 `if len(chat) < 40` 永远成立 → 永远「聊天太少，先多聊几句」。
`summarize_day()` 那边是 `try/except` 包着，`AttributeError` 被吞得更彻底，连日志都没有。

现在统一走 `learn.recent_lines(mem, uid, limit)`：先试 `private_<uid>`，再试裸 `uid`，
最后退 `get_recent_messages()`；任何异常都返回空列表，不往上抛。

真实库实测（`release\小鲸鱼\memory\memory.db` + WAL）：`private_3280735835` 446 条消息，
取最近 60 条拼成 1507 字聊天记录（旧逻辑这里是 0 字）。

**（2）`DAILY_PROMPT.format()` 的 KeyError**

`DAILY_PROMPT` 里含 `{"type":"habit",...}` 这种 JSON 示例，`.format(chat=chat)` 会把
`{"type"...}` 当占位符 → `KeyError: '"type"'`。因为前面那条路一直断在「聊天太少」，
这行从来没被执行到，错误藏了十几版。已改成 `.replace("{chat}", chat)`，
并把 `group_*` 会话的消息也拼进去（`[群]` 前缀）。

### 38.5 防抖默认值 2.5 → 4.5 秒

| 位置 | 原值 | 新值 |
| --- | --- | --- |
| `agent.merge_delay_sec()` 兜底 | 2.5 | 4.5 |
| `config.DEFAULTS["merge_delay_sec"]` | 2.5 | 4.5 |
| `app_ui` state 兜底 | 2.5 | 4.5 |
| `index.html` `#mergeDelay` 兜底 | 2.5 | 4.5 |

范围仍是 `_NUM_RULES["merge_delay_sec"] = (float, 0.5, 10.0)`，`_MERGE_MAX_WAIT = 8.0` 不变。

### 38.6 验收

`tests\test_v1923_extract.py` 六组 22 项（提取走链）+ `tests\test_v1923_learn.py` 五组 21 项
（学习能读到聊天、每日总结带群聊、防抖默认 4.5）。回归十套共 323 项全绿。

---

## 39. V1.9.24 技术要点（2026-11-05）—— 记忆提取四修与钟点换算

用户原话：「记忆还是无法提取，马上修」「我说 4 电下课一直好像不知道几点，让它能随时知道时间」。
查下来是**四个独立原因叠在一起**，任何一个单独存在都足以让 `memories` 表永远是 0 行。

### 39.1 原因一：提取结果被 `clean_reply` 整句挖空（最隐蔽）

这是主因，也是最难想到的一个 —— 两段本来互不相关的逻辑撞在了一起。

`clean_reply(text)`（`agent.py:1712`）是给**聊天回复**用的：模型有时会把内部指令
`- 记住: …` 漏到回复里，所以它调 `_split_remember(text)`，用这条正则整行删掉：

```python
_REMEMBER_RE = re.compile(r"^[ \t]*[-*·•]?[ \t]*记住[ \t]*[:：][ \t]*(.+?)[ \t]*$", re.M)
```

而 `EXTRACT_PROMPT` 恰恰**要求模型就输出这个格式**（`- 记住: （人名）事实`）：

```python
>>> agent.clean_reply("- 记住: （安瑆.雨）明天早上九点要去面试")
''
>>> agent.clean_reply("1. 记住: （安瑆.雨）明天早上九点要去面试")
'1. 记住: （安瑆.雨）明天早上九点要去面试'      # 带序号就不匹配，所以侥幸没被吃掉
```

调用链：`extract_and_store` → `_extract_text` → `_ask_text` → 成功分支 `return clean_reply(text), reason`
→ 空串 → `if not text: log("  [记忆] 模型没给出结果（ok），本轮跳过", "DEBUG"); return`。

**修法**：`_ask_text(..., raw=False)` 新增参数，`raw=True` 时成功分支返回 `strip_think(text).strip()`，
不过 `clean_reply`；`_extract_text` 一律传 `raw=True`（docstring 写明原因）。

```python
# agent.py:2741
def _ask_text(messages, budget, temperature=None, effort="none", tag="/裸调用",
              allow_fallback=False, targets=None, timeout=None,
              respect_cooldown=True, raw=False):
```

滚动摘要那条也因此拿到了原始文本，自己加了一句 `re.sub(r"```[a-zA-Z]*", "", summary_text).strip()`
再 `mem.save_summary(key, _sm[:100])`，免得 ``` 围栏进库。

### 39.2 原因二：提示词把情绪 / 关系 / 约定全排除了

旧 `EXTRACT_PROMPT` 只有 **208** 字符，关键那句是「闲聊、玩笑、情绪宣泄都不算」；
而 `diary.PERM_PROMPT` 有 **1512** 字符（7 倍），里面有一条 ★ 强制条款：

```
★ 关于「情感与承诺」：只要对话里出现下面任何一类，**必须**至少记一条，不许因为「不够客观」就跳过
```

同一个模型、同一段对话的对照实验：

| 链路 | 提示词 | 结果 | 目标表 |
|---|---|---|---|
| 会话记忆 | `EXTRACT_PROMPT`（208 字符，排除情绪） | `completion_tokens = 1`，即「无」 | `memories` = **0 行** |
| 永久记忆 | `PERM_PROMPT`（1512 字符，★ 强制） | 抽出 3 条 `[work]` / `[preference]` | `perm_memory` = **29 行** |

用户和朋友聊的本来就是情绪、调侃、关系互动（互称「大肥鱼」「小瘦猪」、说想念到失眠），
在旧提示词下全被归成「闲聊」→ 模型规规矩矩答「无」。

**修法**：`EXTRACT_PROMPT` 扩到 500+ 字符，列六类可记内容 + 同样的 ★ 强制条款，条数上限 2 → 3。

### 39.3 原因三：端点挂了不记，每轮白等 90 秒

`_ask_text` 每次调用无条件重试全部端点。用户那条链上云端 `https://new.21r.cn/v1` 报 503、
本地 `http://192.168.10.111:1235/v1` 一律超时，于是每轮白等 90 秒（记忆）+ 120 秒（事件）。
实测 `agent.log`：`[模型/记忆提取] 请求超时（90s）` **19 次**、`[模型/事件提取] 请求超时（120s）` **4 次**。

**修法**：新增端点冷却。

```python
_EP_COOLDOWN = {}          # tkey -> (过期时间戳, 原因)
_EP_COOL_SEC = {"connection": 120.0, "timeout": 120.0, "quota": 600.0,
                "http_429": 120.0, "http_500": 180.0, "http_502": 180.0,
                "http_503": 180.0, "http_504": 180.0,
                "http_401": 900.0, "http_403": 900.0,
                "bad_json": 30.0, "bad_shape": 60.0}
_ENABLE_EP_COOLDOWN = True
```

- `_ep_cool_sec(reason)`：未知 reason 但 `startswith("http_5")` → 180.0；其余未知 → 0.0（不冷却）
- `_ep_cool(target, reason)`：记冷却，已有更长的冷却不会被缩短
- `_ep_cooling(target)` → `(bool, left_sec, reason)`，过期自动 pop
- `_ep_clear(target)`：成功即解除
- `ep_cooldown_state()`：给控制台的快照 `[{endpoint, left_sec, reason}]`

`_ask_text` 开头过滤：全冷却 → 日志 `[模型{tag}] 全部 N 个端点都在冷却中，本次跳过：…` + `return "", "cooldown"`；
部分冷却 → DEBUG 记一笔后只试活着的那些。

> **为什么聊天不受影响**：聊天主路径 `ask_model` 不走 `_ask_text`（它自己遍历 `_chain_targets()` 调
> `_chat_with_tools`），所以**用户正等着的那句回复永远会真的去试一次**。冷却只作用于后台提取类任务。

### 39.4 原因四：三条提取线程抢同一把槽位

`_after_reply_job`（`agent.py:7823` 区域）同时起三个线程，它们以前共用同一个 `key=scope` 写
`_EXTRACT_LAST`，`_EXTRACT_MIN_INTERVAL = 45` 秒 —— 谁先抢到谁跑，另外两个直接被丢掉：

| 链路 | 改前 | 改后 |
|---|---|---|
| 会话记忆 | `bg_slot_ok(scope)` | `bg_slot_ok(scope, ns="memory", slot="memory")` |
| 永久记忆 | `bg_slot_ok(scope)` | `bg_slot_ok(scope, ns="perm")` |
| 事件提取 | `bg_slot_ok(scope, 30, slot="event")` | `bg_slot_ok(scope, 30, slot="event", ns="event")` |

配套新增 `_extract_key(key, ns) = f"{ns}|{key}" if ns else key`（`ns=""` 保持老行为）
与 `_MEM_SLOT = threading.Lock()`，`_BG_SLOTS = {"bg": _BG_SLOT, "event": _EV_SLOT, "memory": _MEM_SLOT}`。

### 39.5 解析放宽：`_parse_memory_block`

`extract_events` 早就在用 `_extract_json_array`（剥 ``` 围栏 + `re.search(r"\[.*\]", s, re.S)`），
所以事件那条没事；`extract_and_store` 却只会 `for line in text.splitlines()`。新增三级兜底：

1. `_extract_json_array(raw)` 抠 JSON 数组 —— dict 元素取 `content/fact/text/memory` 作事实、
   `person/name/who` 作人名（缺省用 `default_person`），字符串元素直接当事实；
2. 逐行，跳过 `无 / None / - / * / [] / [ ] / [/] / { / } / ``` / ```json` 这些噪声；
3. 整段兜底（没有换行且 `4 <= len <= 120`）。

`_parse_memory_line` 的清洗链也补了序号与分类标签（注意：两条序号正则**必须分开写**，
合并成 `^[（(]?\s*\d{1,2}\s*[）)]?\s*[.、．:：)]` 会误吃正文里的「12:30」）：

```python
s = re.sub(r"^[（(]\s*\d{1,2}\s*[）)]\s*[.、．:：)]?\s*", "", s)   # （1）
s = re.sub(r"^\d{1,2}\s*[.、．]\s*", "", s)                        # 1.  1、
s = re.sub(r"^\d{1,2}\s*[)）]\s*", "", s)                          # 1)
s = re.sub(r"^\d{1,2}\s*[:：](?!\d)\s*", "", s)                    # 1:  （后面不能跟数字）
s = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩]\s*", "", s)                          # ①
# 去掉「记住:」前缀之后：
s = re.sub(r"^[\[【][^\]】]{1,12}[\]】]\s*", "", s)                 # [work] 【情绪】
```

冒号分支也改成 `^([^：:\d]{1,12})[：:]\s*(.+)$`，人名字段排除数字。

### 39.6 钟点换算：`time_point_context`

`time_context()` 只给「现在是 2026年11月5日 星期四 20:19」，模型得自己推「四电」是哪天哪个点 ——
这正是用户抱怨的「我说 4 电下课一直好像不知道几点」。新增 `time_point_context(user_text)`
（`agent.py:1416`），配 `_TP_RE`、`_HOUR_CN`、`_norm_hour`。

`_TP_RE` 故意把**「电」**也当成「点」：

```python
(上午|早上|早晨|凌晨|中午|下午|傍晚|晚上|夜里|今晚|今早|明早|明天|后天|昨天|今天|周[一二三四五六日天]|星期[一二三四五六日天])?
\s*(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*
(?:点|电|時|时|:|：)\s*
(\d{1,2}|[一二三四五六七八九十]{1,2})?\s*(分|半)?
```

**关键的歧义设计**：没讲上午/下午的钟点（`base <= 11`）两个候选都算 —— `[(base, m), (base+12, m)]`；
`base == 12` 则是 `[(12, m), (0, m)]`。今天时主选 = 「已经过去的那个，同类再比谁离现在近」：

```python
primary = min(cands, key=lambda c: (0 if c[0] * 60 + c[1] <= now_min else 1,
                                    abs(now_min - (c[0] * 60 + c[1]))))
```

实测输出（20:19）：

| 用户原话 | 注入内容 |
|---|---|
| 我四电下课 | `= 钟点 四 点，没讲上午还是下午。按现在（20:19）最可能是今天 16:00（已经过去 4 小时 19 分钟），也可能指今天 04:00（已经过去 16 小时 19 分钟）。用最可能的那个回答，别自己另推。` |
| 我下午四电下课 | `= 2026年11月5日 16:00，是**今天**、而且已经过去 4 小时 19 分钟了 —— 别说成以后的事，也别问「到时候怎样」。`（讲清了就只给一个） |
| 今晚8点面试 | `= 2026年11月5日 20:00，是**今天**、而且已经过去 19 分钟了` |
| 明天九点考试 | `= 明天 09:00（今天11月5日，星期四），没说上午下午，按一般作息是这个点；也可能指 明天 21:00。` |
| 在干嘛呀 | 不注入（没有钟点） |

**注入位置**：`_prepare_messages` 的 `_volatile_parts`（最后一条 user 消息里），紧跟 `time_context()` 之后，
**不进 system** —— 易变内容进 system 会把前缀缓存全废掉，这是 v1.1.3 定下的规矩。

### 39.7 验收

`tests\test_v1924_extract_time.py`（新建，六组 62 项）：

| 组 | 覆盖 |
|---|---|
| [1] | 端点冷却：冷却中/剩余秒/原因、不影响另一端点、跳过时只打 1 次、全冷却发 0 次请求且 `reason == "cooldown"`、快照 2 条、`_ep_clear` 解除、`_ep_cool_sec("empty") == 0`、`http_503 → 180`、失败自动进冷却且下一轮跳过 |
| [2] | 节流命名空间：`memory` 先拿、同 ns 被挡、换 ns 能拿、裸 key 老行为不变 |
| [3] | 槽位分离：`perm` 占着 bg 槽时 `memory` 仍能拿 |
| [4] | 解析：JSON dict、带 ```json 的字符串数组、编号列表、`[work]` 分类、圈号、老格式带人名、「无」/空串 → `[]`、正文 `12:30` 不被切、整段兜底 |
| [4b] | `clean_reply` 剥「记住:」复现 + `_extract_text` 拿到原始行 + 仍能解析（核心回归） |
| [5] | 钟点换算：`我四电下课` 两候选都给出、`我下午四电下课` 单一时点、`今晚8点` → 20:00、`明天9点`、`下午三点半` → 15:30、无钟点/空文本/关掉时间感知 → 空串、`_norm_hour` |
| [6] | 提示词放宽 + 端到端入库（JSON 结果入库、模型说「无」不入库） |

回归 **11 套 385 项全绿**：v1924 62、v1923_learn 21、v1923_extract 22、v1922_qzone_dup 21、
v1921_semantic 20、v1920_qzone 45、v1919_sing 37、v1918_login 32、v1917_login 57、v1916_fix 34、v1915_fix 34。

> `tests\test_v1923_extract.py` 有一处适配：它第 [3] 组故意让两个端点都失败，新冷却机制会把它们记下来
> 污染第 [5]/[6] 组，所以在 [5]/[6] 组开头加了 `agent._EP_COOLDOWN.clear()` + `_EXTRACT_LAST.clear()`
> + `_FG_TS["t"] = 0.0`。这是新功能的正确行为，不是回归。

---

## 40. V1.9.25 技术要点（2026-11-06）—— NapCat 启动误判修正与真唱合成

用户原话：「这个版本现在在其他账号登陆状态下启动不了 Natcap 了，马上修」「唱歌念出来有些生硬了，改成唱」。
第一件的真因在**报错判定**（不在登录），第二件是把 read 引擎从「念」换成了逐字变调 + 伴奏的合成唱腔。

### 40.1 NapCat 侧现场：登录成功、端口起来了，1.5 秒后被踢

`release\小鲸鱼\napcat_launch.log`（5904 行 / 69 个分段标记 / 51 个启动标记，最后一次启动在第 5873 行）逐段复盘，
报障时段（11-06 09:53～09:58）连续 5 次都是同一形态：

```
09:57:26 [info] [NapCat] 正在快速登录  2145216414
09:57:28 [info] [NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略
09:57:28 [info] [NapCat] 已通知主进程登录成功
09:57:29 [info] [OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3003
09:57:31 [error] [NapCat] [Fork] Worker进程退出，退出码: 1
09:57:31 [warn] [NapCat] [Fork] Worker进程意外退出 (1/3)，正在尝试重新拉起...
```

关键统计（用于区分「撞号」的两种形态）：

| 串 | 全日志出现次数 |
|---|---|
| `当前账号(...)已登录,无法重复登录` | **1** 次（行 5281，2026-11-05 08:47:18，远早于报障） |
| `Worker进程退出，退出码` | **13** 次（行 5722/5759/5796/5833/5870 就是报障那 5 次） |
| `Worker进程已登录成功` | **29** 次 |
| `快速登录错误` | 7 次 |

所以「启动不了」≠「已登录冲突」——**同号不能两边同时在线**这件事有两种表现：
显式的「已登录,无法重复登录」，以及这种更安静的「登进去就被顶下线（退出码 1）」。

### 40.2 三处判定缺陷

| 缺陷 | 位置 | 实测 |
|---|---|---|
| 只读「日志尾部 800 行」 | `napcat_mgr.py::launch_log_errors(lines=800)` | 复刻同一套正则：尾部 800 行命中 `['token_dead/3280735835', 'qq_conflict/2145216414', 'token_dead/3280735835']`，`hits.sort()` 后返回 `token_dead`；只扫「本次启动之后」命中 **0** 条。它把过期历史当成本次结论，还因为「只返回最后一条」把同窗口里真正有用的 `qq_conflict` 丢掉了 |
| 拿到结论就提前 `break` | `napcat_mgr.py::launch()` 等待循环 | 循环第一圈（约 1 秒）就跳出 → `wait` 秒数没走完 → 对外报「20/25 秒内 3003 端口没起来」+ 无关的「③ 你的官方 QQ 正开着…」 |
| 顺手误标坏号 | `napcat_mgr.py::launch()` 出口 | `_uc == "token_dead"` → `mark_uin_bad(_uin or _q, "登录态已失效")`，把 `3280735835` 记进 `bad`（这轮根本没试它），污染 `_uin_state.json`。另 `login_candidates()` 每次都调 `bootstrap_bad_from_log()`（仅 `bad` 为空时跑）写盘 —— **调试期间的探针脚本就是这样把 `bad` 从 `{}` 写脏的**，所以修复必须能自愈 |

### 40.3 改动

```python
def launch_log_errors(lines=800, after=None):
    ...
    if after is not None:
        text = raw[int(after):].decode("utf-8", "ignore")   # 只看本次启动
    else:
        text = "\n".join(text.splitlines()[-int(lines):])   # 老行为（尾 800 行）
    ...
    # 新增第四类码：登进去又被踢（必须「先成功、后退出」成对出现）
    for m in re.finditer(r"Worker进程退出[，,]?\s*退出码[：:]?\s*(\d+)", tail):
        seg = tail[:m.start()]
        i_login = seg.rfind("正在快速登录")
        after_login = seg[i_login:] if i_login >= 0 else seg
        if "Worker进程已登录成功" not in after_login:
            continue
        um = re.findall(r"正在快速登录\s+(\d{5,12})", after_login)
        hits.append((m.start(), {"code": "account_kicked",
            "uin": (um[-1] if um else ""),
            "msg": "这个号刚快速登录成功就被顶下线了（多半官方 QQ 正用它）"}))
```

`launch()` 侧四步：

1. 启动前 `_log_off = _log_size()`（`napcat_mgr.py:1942`）；
2. 等待循环里 `last_err = launch_log_errors(after=_log_off)`（`napcat_mgr.py:2039`）；
3. **只有 `risk` 才 break**（`napcat_mgr.py:2048-2049`）；`token_dead` → `mark_uin_bad` 后继续等；
   `account_kicked` → `_kicked.add(uin)` + `mark_uin_conflict(uin, "本机 QQ 客户端正用着这个号")`（`napcat_mgr.py:2055-2064`）；
4. 循环出口 `_uc in ("qq_conflict", "account_kicked")` 再记一次 conflict（`napcat_mgr.py:2084-2092`）；
   换号循环的避让集合 `_clash_now = set(conflict_uins()) | set(_kicked) | set(qq_client_uins())`（`napcat_mgr.py:2099`），
   每个换的号先记 `_off2 = _log_size()` 再 spawn，判定用 `after=_off2`（`napcat_mgr.py:2138`）。

### 40.4 新增 `qq_client_uins()`：官方 QQ 到底占着哪个号

不能靠进程命令行 —— 实测 `QQ.exe` 的 `CommandLine` **不含 QQ 号**。可用的判据是数据目录活跃度：
官方 QQ 在某号上跑着就会持续写 `文档\Tencent Files\<QQ>\nt_qq\nt_db\*.db-wal`。

文档目录必须**问注册表**（这台机器的「文档」被移到了 `D:\Documents`，猜 `~\Documents`/`~\OneDrive\Documents` 全部落空）：

```python
winreg.QueryValueEx(k, "Personal")                                   # 实测 → D:\Documents
winreg.QueryValueEx(k, "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
```

| 新函数 | 位置 | 说明 |
|---|---|---|
| `_documents_dirs()` | `napcat_mgr.py:1206` | 注册表 + 常见路径兜底，只返回存在的目录（实测 `['D:\\Documents', 'C:\\Users\\32807\\OneDrive\\文档']`） |
| `qq_client_uins(max_age=1800, use_cache=True)` | `napcat_mgr.py:1228` | `Tencent Files` 下 5~12 位数字目录，`_newest_mtime` 在 1800 秒内即认定「官方 QQ 正用着」；30 秒缓存 |
| `_newest_mtime(d, budget=400)` | `napcat_mgr.py:1273` | 只探 `d` 本身 + `nt_qq\nt_db` + `nt_db` + `nt_qq`，不全量 walk（QQ 号目录几万文件） |
| `_log_size()` | `napcat_mgr.py:1331` | `int(os.path.getsize(launch_log_path()))` |

`login_candidates()` 里 `_clash |= set(qq_client_uins())`，被占号排到队尾。

### 40.5 真唱：先划清 edge-tts 的能力边界

改前 `sing.py::synth_read` 是逐行 `edge_tts.Communicate(txt, voice, rate=rate, pitch=pitch)` 再**裸拼 mp3 字节**。
生硬的根因不是音高没变，而是**没有旋律和节奏**。

| 试过 | 结论 |
|---|---|
| `pitch` 参数 | 真生效：`+0Hz` / `+200Hz` / `-200Hz` 三个 sha1 分别是 `6ae79c18250b` / `c336a8746613` / `d46e66056ca3`。字节数相同只因 edge 输出是 **CBR mp3**（131072 bps 恒定）→ 靠换参数做不出旋律 |
| 自定义 SSML（逐字 `<prosody>` / 嵌套 prosody / `<s>` 包裹） | 全部 `NoAudioReceived: No audio was received. Please verify that your parameters are correct.` —— `edge_tts\communicate.py:263 mkssml()` 出来的是**单层** `<speak><voice><prosody …>`，只支持单层 → 不能一次请求喂整条旋律 |
| 裸 PCM（直连 websocket 试 8 种 `outputFormat`） | `raw-24khz-16bit-mono-pcm` / `raw-16khz` / `riff-24khz` / `ogg-24khz-16bit-mono-opus` / `audio-24khz-160kbitrate-mono-mp3` 全 **0 字节**（服务只回 `turn.start` + `response` 的 `streamId`，没有 audio 帧）。可用：`audio-24khz-48kbitrate-mono-mp3`（默认）、`audio-24khz-96kbitrate-mono-mp3`、`webm-24khz-16bit-mono-opus` → **必须解码 mp3** |
| `boundary="WordBoundary"` | ✅ `{"Metadata":[{"Type":"WordBoundary","Data":{"Offset":1055410,"Duration":4027910,"text":{"Text":"月亮",…}}}]}` —— 单位 **1e-7 秒**，**按词**给（「月亮」「代表」各一条，「我」「的」「心」各一条），字级要词内均分 |
| 能量包络切音节 | ❌ `hop=sr//100`、阈值 `max(-38, percentile(rms_db,92)-26)`，实测「月亮代表我的心」7 个字只切出 3 段 |
| 逐字单独合成 | ❌ 每字 1.42 秒（静音占 72%），12 行要 124 秒 |
| MP3 帧级掐静音 | ❌ 55 个帧载荷只有 37 份不同，众数法找不到静音指纹，砍 0 帧 |

### 40.6 解码器选型：miniaudio（1.6MB）而不是 av（128MB）

| 候选 | 体积 | 结论 |
|---|---|---|
| `av 18.1.0` | **128.04 MB**（`av.libs` 里 `avcodec-62-*.dll` 18.4MB、`libx265-*.dll` 12.2MB、`libSvtAv1Enc-*.dll` 7.3MB…） | 不用；且实测写 mp3 会**变成 stereo**（`layout="mono"` 无效，`AudioFrame.layout` 只读） |
| `imageio_ffmpeg` | 83.66 MB（单个 `ffmpeg-win-x86_64-v7.1.exe`） | 不用 |
| `miniaudio 1.71` | **约 1.6 MB**（`miniaudio.py` 0.086MB + `_miniaudio.pyd` 0.573MB + `cffi` 0.813MB + `_cffi_backend` 0.171MB） | **选它**。只能解不能编（`encode_file=False`/`mp3_write_file_s16=False`），2.42 秒音频解码只要 **0.016 秒** |

编码不靠库，直接用标准库 `wave` 写 **WAV**（mono / 24000Hz / 16bit），头是标准 `RIFF/WAVE/fmt`。
NapCat 侧也不需要我们转码：`agent.py:3999-4003 send_voice()` 的 record 段直接吃本地绝对路径，
`napcat\napcat.mjs` 里的 `convertToNTSilkTct` 自己会调 ffmpeg（`-c:a ntsilk_s16le -ar 24000`）转 silk。

### 40.7 真唱实现（`sing.py::synth_sing`）

```
edge_tts(WordBoundary) → miniaudio.decode → float32 mono 24000Hz
  → 按 marks 切字级区间（词内均分）
  → 逐字：掐静音(-40dB, ±10ms)
          → 测基频 _f0_hz → 算出离目标音差几个半音 → 变调（夹在 ±8 半音内）
          → WSOLA 把时长拉回原样（保住咬字节奏）
          → 颤音 → 淡入淡出(14ms)
  → 字间 30ms / 行间 260ms
  → vox*0.92 + _acc_track()  → 归一化 0.90
  → wave 写 sing_<ms>.wav
```

| 常量 / 函数 | 值 / 作用 |
|---|---|
| `_SR` | `24000` |
| `_PENTA` | `(0, 2, 4, 7, 9, 12)` 五声音阶半音 |
| `_MELODY` | `(0, 2, 4, 3, 2, 1, 0, 2, 4, 5, 4, 2)` 级数；**目标音** `target = _ROOT_HZ * 2 ** ((_PENTA[deg%6] - 2) / 12)` |
| `_PITCH_CAP` | `8.0` —— 单字最多挪 8 个半音，再大重采样就听得出变形 |
| `_CHORDS` | `((1.00,1.26,1.50),(0.84,1.00,1.26),(1.26,1.59,2.00),(1.12,1.41,1.68))`，每 2.4 秒换 |
| `_ROOT_HZ` | `196.0` |
| `_VIB_RATE` / `_VIB_DEPTH` | `5.4` Hz / `0.009` |
| `_f0_hz(x, np, lo=90, hi=400)` | 自相关（去均值、归一化、抛物线细化），测不准返回 `None` |
| `_pitch_shift(x, np, semis)` | `n = int(x.size / 2 ** (semis / 12))` + `np.interp(...)` —— 重采样，音高与时长同时变 |
| `_time_stretch(x, np, target_len)` | WSOLA：30ms Hann 窗、`hop_out = win/2`、`hop_in = hop_out * n_in/target`、±10ms 内按互相关挑最接得上的位置再叠 |
| `_vibrato(x, np)` | `x[np.clip(np.arange(n) + (depth*sin(2π·rate·t)*SR).astype(int64), 0, n-1)]` |
| `_acc_track(n, np)` | 根音 196Hz 铺 pad（`0.05*sin`）+ 每 0.6 秒一个 `0.045*sin(2π*98*t)*exp(-7d)` 低音脉冲 |
| 语速 | `rate = "%d%%" % max(-35, min(0, pct - 10))` —— 比用户设置再慢一档 |

保底设计：`synth_read()` 变成分发器 ——
依赖齐 → `synth_sing()`，抛异常 → `print("[唱歌] 真唱失败（…），退回朗读")`；
依赖缺失 → `print("[唱歌] 没装 numpy/miniaudio，退回朗读")`；
两种情况最后都落回 `_synth_read_plain()`（原实现整体改名）。`synth_svc` 也改调 `_synth_read_plain`（SVC 要干声）。
`sing_status()` 新增 `sing_ready` 与 `sing_engine_cn`（`"真唱（拆字变调 + 伴奏）"` / `"朗读（保底）"`）。

实测：4 句歌词 → **6.80 秒音频 / 处理 5.5s / 319KB WAV**（163226 帧、峰值 0.900、有效值 0.1655）；
3 句 → 6.22 秒音频 / 298780 字节；10 句 → 12.70 秒音频 / 合成+处理 12.05s（**每行约 1.21 秒 → 12 行约 14 秒**，必须放后台线程）。

**为什么是「归一化到目标音」而不是「相对平移几个半音」**（这一步是 v1.9.25 的关键修正）：
说话本身有 150~320Hz 的语调起伏，若只在每个字自身音高上乘一个比例，旋律会被语调淹没。
实测同一句「月亮代表我的心」：

| 做法 | 逐字实测基频 | 离目标音平均误差 | 音域跨度 |
|---|---|---|---|
| 相对平移（`semis = _PENTA[deg%6] - 2`） | `282 296 400 308 276 178 136` | **4.52 半音** | 18.7 半音 |
| 归一化到目标音（`_f0_hz` 测出后算差值） | `199 199 219 262 294 280 174` | **0.85~0.92 半音** | ~8 半音 |

**为什么必须补 `_time_stretch`**：重采样变调会把时长一起改（±8 半音 = 0.63~1.59 倍），
不拉回来咬字就糊。但**普通 OLA 不行** —— 相邻颗粒相位对不上会梳状滤波，
实测 220Hz 正弦拉长后谱峰跑到 204.7Hz，等于把刚调准的音准又搅糊（而唱的正是带明确音高的元音）。
换成 WSOLA（拼接前在 ±10ms 内按互相关找最接得上的偏移）后，160/220/294Hz 正弦在
0.7~1.4 倍伸缩下谱峰偏移都 ≤1.4Hz，每字时长比值稳定为 **1.000**（完全保住原来说话的节奏）。


打包必须同步（**改 `build_backend.py`，不是 `backend_server.spec`**）：

```python
# v1.9.25：唱歌要解 mp3 —— 用 miniaudio（只解不编，包体约 1.6MB）而不是 av。
# `_miniaudio` 是它的 C 扩展（.pyd，`miniaudio.py` 里 `from _miniaudio import ffi, lib`），
# 必须显式写进来，否则打包后只会退回朗读。
if _have_module("miniaudio"):
    hidden += ["miniaudio", "_miniaudio"]
```

> **踩过的坑**：一开始把这两行加在 `backend_server.spec` 里，结果打包日志第一句就是
> `INFO: wrote …\backend_server.spec` —— 那个 spec 是 PyInstaller 依命令行参数**重写出来的产物**，
> 不是输入。手改它下一次打包就被冲掉，包里的 PYZ 也就没有 miniaudio，表现为「本机测试会唱、
> 打出来的安装包只会念」。正确位置是 `build_backend.py` 的 `hidden` 列表（`HIDDEN` 之后）。

### 40.8 验收

`tests\test_v1925_napcat_sing.py`（新建，六组 **42 项**）：

| 组 | 覆盖 |
|---|---|
| [1] | `launch_log_errors` 新旧对比：旧写法翻出过期结论、`after=off` 拿到 `account_kicked`（uin 2145216414、文案含「顶下线」）、本次无 `token_dead` |
| [2] | `account_kicked` 成对判据（真机日志片段）+ 老 `qq_conflict` / `risk` 仍认 |
| [3] | `_documents_dirs()` 问到 `D:\Documents`、`Tencent Files` 存在、`qq_client_uins()` 数字列表、`max_age=1` 返回空、30 秒缓存 |
| [4] | `login_candidates()` 把被官方 QQ 占着的号排到最后 |
| [5] | 真唱：`_char_spans` 均分、`_pitch_shift` 升 12 半音时长减半、WAV 头 RIFF/WAVE/1ch/24000/16bit、>1024 字节、`synth_read` 走真唱、依赖缺失退回 mp3、`synth_sing` 抛异常也退回 mp3、POLY_RULES 50 条、`_looks_like_prose`、`polyphone_fix("行不行") == "形不形"` |
| [6] | `launch()` 端到端：旧 `token_dead` 躺在尾部也不提前 break 且最终等到 3003、成功时记的是本次真正用的号、不误标坏号；本次真 `token_dead` 仍等到 3003 但如实记坏号；`account_kicked` 记 conflict 而非坏号 |

> 写 [6] 组时踩到的坑（值得记）：等待循环每圈是**先 `port_open()` 后 `launch_log_errors()`**，
> 桩里若让 `port_open` 第 2 次就返回 True，`launch_log_errors` 一次都不会被调用 —— 断言会**假通过**。
> 桩必须保证「本次启动的日志已经落盘」之后再过一轮才放行 3003。

回归 **11 套 385 项全绿**：v1924 62、v1923_learn 21、v1923_extract 22、v1922_qzone_dup 21、
v1921_semantic 20、v1920_qzone 45、v1919_sing 37、v1918_login 32、v1917_login 57、v1916_fix 34、v1915_fix 34。

## 41. V2.0.0 架构（2026-11-08）—— core/ 九模块与活配置桥

### 41.1 一张图

```
                        ┌──────────────────────────────┐
   NapCat WebSocket ───▶│  agent.py  6515 行            │
                        │  · 初始化（配置/建库/连 NapCat）│
                        │  · WS 主循环 + 连发防抖        │
                        │  · handle_event 分发           │
                        │  · 兼容层：from core.xxx import │
                        └───────┬──────────────────────┘
                                │ process_message(...)
                                ▼
        ┌──────────────────────────────────────────────┐
        │ core/pipeline.py   消息处理流水线 553 行        │
        │  媒体归一化 → 取记忆/人设 → 决定回不回          │
        │  → 拼上下文 → ask_model → 清洗拆分 → 发送      │
        └──┬────────┬─────────┬──────────┬─────────────┘
           │        │         │          │
           ▼        ▼         ▼          ▼
   llm_client  prompt_builder sender   extract
   通信/链路   提示词/记忆块   发送层    事件与记忆提取
      │              │                    │
      └──────┬───────┴────────┬───────────┘
             ▼                ▼
      core/utils.py      core/tasks.py
      纯函数             14 个后台循环
             │                │
             └──────┬─────────┘
                    ▼
            core/globals.py  ← 活配置桥（唯一通道）
```

### 41.2 各模块职责与边界

| 模块 | 行数 | 管什么 | 明确不管什么 |
|---|---|---|---|
| `core/globals.py` | 119 | `live(name)` 取 agent 的当前值；`call(name, *a)` 调 agent 的函数；`cfg(key, d)` 转发 `_live_cfg`；`sync(names, g)` 批量灌入；`set_live(name, v)` 回填 | 不缓存、不持有状态 |
| `core/utils.py` | 949 | `log` / `clean_reply` / `strip_think` / `normalize_messages` / `estimate_tokens` / `time_context` / `time_point_context` / `_card_from_json` / `_card_from_xml` / 合并标记 `_merge_items` | 不碰数据库、不发网络请求 |
| `core/sender.py` | 752 | `send_private_human` / `send_group_human` / `send_voice` / `send_sticker` / `send_image_file` / `send_to_session` / `split_reply` / TTS 全套 / `remember_assistant` | 不决定「说什么」（那是 pipeline 的事） |
| `core/prompt_builder.py` | 397 | `build_messages` / `refresh_system_prompt` / `SYSTEM_PROMPT` / `ANTI_BOT_RULES` / 记忆块 / 永久记忆块 / 好感度块 / 浪漫块 | 不决定「发不发」（编排在 `_prepare_messages`，留在 agent） |
| `core/llm_client.py` | 1128 | payload 组装、`_post_chat` 与错误分类、`_ep_*` 端点冷却、`_chain_targets` 优先级链、`_ask_text` 多端点重试、`_extract_text`、用量记账、`ask_model` / `ask_model_raw` + `_pq_*` 优先级队列 | 不改 `LM_*` 配置全局（那是 `apply_provider`，留在 agent） |
| `core/tasks.py` | 996 | 14 个守护循环：`care_loop` / `care_plan_loop` / `proactive_loop` / `moment_loop` / `moment_comment_loop` / `followup_loop` / `feed_loop` / `diary_loop` / `private_catchup_loop` / `share_video_loop` / `kg_build_loop` / `memory_export_loop` / `_switch_supervisor_loop` / `_loop_log` | 不持有锁与状态字典（全走 sync 拿 agent 那一份） |
| `core/extract.py` | 1075 | `extract_events` / `_regex_event_fallback` / 事件重试队列 / `extract_and_store` / `_parse_memory_block` / `_parse_memory_line` / `_perm_extract_job` / `_memory_text` / 槽位与节流 | 不拼对话上下文（调用方给） |
| `core/qzone_flow.py` | 672 | `feed_fetch` / `feed_round` / `feed_comment_round` / `catch_up_unread` / `catch_up_private` | 不实现空间协议（`qzone.py` / `qzone_interact.py` 管） |
| `core/pipeline.py` | 553 | `process_message` | 不实现发送、不实现模型通信 |

### 41.3 活配置桥：为什么是 `_sync()` 而不是模块级 `__getattr__`

函数体里的裸名字走的是**本模块 `__dict__`**，PEP 562 的模块 `__getattr__` 只影响 `module.attr` 这种属性访问，管不到函数体内的全局查找。所以「模块级转发」对 `if LM_IS_CLOUD:` 这种写法无效。

最终形态：

```python
_SYNC_NAMES = ("LM_MODEL", "LM_BASE", "mem", "_live_cfg", ...)

def _sync():
    _g.sync(_SYNC_NAMES, globals())        # 逐个 setattr 到本模块

def _post_chat(payload, timeout=None, ...):
    _sync()          # ← 每个入口函数第一句
    ...              # 函数体一个字未改
```

`_g.sync` 的实现要点：只搬 `hasattr(agent, name)` 为真的名字（agent 尚未定义就跳过），agent 缺席时返回 0 而不是抛错 —— 这样 `import core.llm_client` 单独跑（比如自测）也不会炸。

**`core/tasks.py` 额外规则**：不仅函数入口，`while` 体第一句也 `_sync()`。因为 `MOMENT_HOURS`、`FEED_POLL_MIN`、`CARE_PLAN_HOURS` 这类配置由 agent 的 `_reload_constants()` 就地刷新，只在入口取一次的话，用户在设置里改完要重启才生效。

### 41.4 三处「写回」补丁（改这块代码时务必保留）

| 位置 | 代码 | 为什么 |
|---|---|---|
| `prompt_builder.refresh_system_prompt` 结尾 | `_g.set_live("SYSTEM_PROMPT", SYSTEM_PROMPT)` | agent 里几十处直接读 `SYSTEM_PROMPT`；不回填就是空串 |
| `llm_client.ask_model` 抬高 `LM_REASONING_MAX_TOKENS` 之后 | `_g.set_live("LM_REASONING_MAX_TOKENS", ...)` | 否则下一次 `_sync()` 用 agent 的老值盖掉「抬高」 |
| `utils.time_point_context` 的开头 | `_switch("ENABLE_TIME_AWARENESS", ...)` | 自测会 `agent.ENABLE_TIME_AWARENESS = False`，纯副本会让这条失效 |

### 41.5 搬家时的三个真实故障（写下来免得再犯）

| 症状 | 根因 | 教训 |
|---|---|---|
| 两套自测 `通过 0 失败 -1`（直接崩） | `core/extract.py` 漏 `import queue`，`_EV_RETRY = queue.Queue()` 未定义 | 搬块要连「这个块用了哪些标准库」一起搬，不能只看自定义名字 |
| `NameError: _MEDIA_ONLY_HEAD` 在 `_memory_text` 里 | 搬了函数没搬它依赖的 4 个正则常量 | 同上：**常量也是依赖** |
| `v1922_qzone_dup` 5 项失败，`name 'active_name' is not defined` | `active_name`（`from personas import`）和 `qzone_mod`（`import qzone as`）是 agent 顶部的**导入别名**，桥接报告统计不到 | 搬移前先列「agent 顶部的 import 别名」，它们也是模块级名字 |

### 41.6 AST 静态体检（建议固化进 CI）

思路：对每个 `core/*.py` 的函数体，收集 `Name(Load)`，减去
① 参数与函数内绑定名（含闭包内的 `Store`、`for` 目标、`with as`、`except as`、内嵌 `def` 名、推导式变量），
② 模块级 `def` / `class` / 赋值 / import 绑定名，
③ `_SYNC_NAMES` 里的名字（这些本来就该由 sync 提供），
④ `builtins`，
剩下的就是**真·未定义名字**。

它一次性扫出十几处「只在异常分支/特定平台才走到」的漏网，比靠回归撞效率高得多。注意排除闭包自由变量造成的假阳性（报出来的名字若在**外层函数**里有绑定，就是假阳性）。

### 41.7 兼容层与包装

- `agent.py` 顶部 `from core.xxx import (...)`：九个模块、一百多个名字。**不要删** —— `app_ui.py` / `web_server.py` / `tests/` 全是 `agent.xxx`。
- `process_message` 在 agent 里只剩包装，参数原样穿透；实测替换成假实现后 7 个参数顺序不变。
- 行尾统一 **CRLF**（agent.py 与 core/*.py）。

### 41.8 跨代升级的完整性防呆（V2.0.0）

跨大版本升级的失败模式不是「功能不对」，而是「启动不了」：`verify_release()` 会扫
程序目录往上 4 层内的 `manifest.sha256` / `resources/manifest.sha256`，**每一份都必须
通过**（v1.9.7 的反降级条款）。旧版清单记的是旧文件哈希，于是新版一启动就判篡改。

> 踩坑记录：第一次想到的办法是给清单加一行 `# build=<版本>` 当版本标记。查了一遍
> 解析器才发现 `signing._parse_manifest()` 遇到「不含两个空格的行」会**抛异常**
> （`raise SignError("清单格式不对")`），而 `Tools\verify_bundle.exe` 是预编译好的
> 二进制，改格式风险不可控。于是换成「用签名状态 + 内容漂移」判断，不动清单格式。

最终判据（`lib.rs`）：

| 清单状态 | 处理 |
|---|---|
| 签名有效 + 内容全对 + 含正在跑的 exe | 通过，交出后端路径 |
| 签名有效 + 内容对不上 | **旧版残骸**：删 `.sha256` + `.sig`，从候选里移除，`write_boot_error` 留痕 |
| 签名不对 / 缺 `.sig` | 不删，计为错误 → 拒绝启动（伪造要留证据） |
| 一份都不剩 | 拒绝启动，提示「可能升级没做完整，请用完整包覆盖或恢复工具」 |

增量包侧的拦截（`DELTA_PS1` 里的 `Get-InstalledVersion`）：先读目标目录
`resources\backend_server.exe` 的 `VersionInfo.ProductVersion`（绿色版布局退到根目录那份
`backend_server.exe`），主版本号与包内 `to_version` 不一致就红字 `Fail 7` 退出。函数里
对空目录、`Join-Path` 失败都做了短路 —— 脚本开头 `$ErrorActionPreference = "Stop"`，
一个空路径就会把「读不到版本」变成「脚本崩掉」。

安装器侧（`installer-hooks.nsh`）：`!macro NSIS_HOOK_PREINSTALL` 里删掉 `$INSTDIR` 根下的
两份清单文件。`RES_DIR` 那份由安装器覆盖，不用管。

### 41.9 开源包为什么漏了 core/（V2.0.1）

`step_source_zip()` 的产物由三部分拼出来：根目录白名单（按扩展名筛）、`FILE_PATTERNS`
（几个零散文件）、`DIRS`（目录白名单）。V2.0.0 新增的 `core/` 属于第三类，但
`DIRS = ["Docs", "tests", "icons", ...]` 里没它 —— 拆代码时只想着"能跑起来"，
没人回头看一眼打包白名单。

> 教训：**结构变了，打包白名单必须同步**。判断标准不是"包里有多少个文件"，
> 而是"拿到包的人第一步能不能跑"。

自检也一并升级：原来只 `len(namelist()) == entries` + `testzip()`，两者都通过
仍然可能缺文件（条目数守恒即可）。现在改成按「必须存在」清单验：

```python
need = [prefix + "agent.py"] + [prefix + "core/" + n for n in 全部 core/*.py]
missing = [p for p in need if p not in names]
if missing:
    raise SystemExit(...)
```

前缀 `小鲸鱼_V<版本>/` 也一并断言，顺带保证「agent.py 与 core/ 平级」——
如果哪天有人把 core 塞进子目录，这里同样会炸。

### 41.10 V2.0.2：点歌链路四个真因（2026-11-08）

| # | 现象 | 真因（证据） | 修法 |
|---|---|---|---|
| 1 | 点歌后收到无关 B 站视频 | `share_video_loop()` 每 600 秒醒一次，恰好落在等歌词的 8 秒里；目标选择只要求「24h 内说过话」 | `_share_video_target()` 增加安静闸门 `share_video_idle_min`（默认 30 分钟，`config.py` 新增键，设置页可调） |
| 2 | 「翻遍了都找不着词」 | 歌词只来自 `computer_use.tool_web_search`，实测返回 `ok=False / items=0`（Playwright 打开页面但解析不到标题） | `sing._lyrics_netease()` 走音乐库结构化接口作第一顺位，网页搜索降为兜底 |
| 3 | 「清唱一段」+ 立刻「唱不了」 | `_sing_perform()` 先无条件发 `intro`，8 秒后才发现没词 | 先 `sing.preview()` 拿到 ≥5 行才发 intro，同一份词直接传给 `sing(lines=)` |
| 4 | 搜索工具从未成功 | 传了不存在的参数 `budget_ms`，每次 `TypeError` 被 `except` 吞掉 | 按真实签名 `tool_web_search(q, 5, "chat")` 调用 |

### 41.10.1 为什么「清嗓子」必须放在确认之后

清嗓子是**前摇**，前摇一旦发出去，后面任何失败都会变成「铺垫完就翻车」。真人不会先清了嗓子再说「我不会唱」，所以顺序必须是：查词 → 有词 → 清嗓子 → 出声。代价是 `preview()` 多一次歌词检索，但它只查一次词，且拿到结果后直接复用，不会重复搜。

### 41.10.2 歌词层的设计：结构化优先 + 变体回退 + 信息行过滤

```python
_lyrics_netease(name, max_lines=12)
  ├─ 查询变体：原话 → 去书名号 → 只留歌名      （带歌手名常排到「无词版本」）
  ├─ 候选排序：_title_score() 贴合度 + 歌手名命中加权
  ├─ 逐个候选取词，取到 ≥5 行即返回；全空则换下一个变体
  └─ _lrc_lines()：去 [mm:ss.xx] 时间轴，滤掉 作词/作曲/编曲/OP :/SP :/未经… 等信息行
```

`_title_score()` 归一化掉书名号与标点，完全相同 1.0、包含 0.9，否则按字符交集比例；`< 0.6` 视为「对不上号」，宁可返回空也不拿同名的别的歌凑数。

### 41.10.3 搜索结果的代码级相关性过滤

```python
_rel_score(query, text)      # query 实词命中比例；中文按 2 字滑窗
_filter_relevant(query, items, min_score=0.34)
```

命中不足就丢弃并打 DEBUG 日志；**一条不剩时工具返回失败**，让模型如实说没查到。这条是「不硬编码、大模型自主决策」的落地：程序只负责别递假料，怎么说由模型定。

### 41.10.4 防复读的两层

- 提示词层：`ANTI_BOT_RULES` 增加【同一个理由不许说第二遍】——工具没帮上忙就换说法/换做法，第二次绝不复述。
- 工具层：`_TOOL_FAIL_SEEN` 记 `(session, tool, reason)` 时间戳，15 分钟内同因再失败，结果文案追加「别再用同一句话解释」。模型看到的失败不再是一模一样的字符串，不会顺着同一套话术再推一遍。

### 41.10.5 验收

`tests\test_v202_sing.py` 26 项 + 回归 13 套共 **459 项全绿**；真实链路（用户实例配置）`sing.preview("时过境迁 苏艺馨")` 取到 12 行歌词并完成真唱合成。

