# -*- coding: utf-8 -*-
"""运行时配置 —— 用 config.json 替代硬编码常量

为什么要这个：
  不同电脑上 LM Studio 里装的模型不一样（qwen / llama / glm / deepseek…），
  有的模型会输出 reasoning_content，有的不会；有的版本不认 reasoning_effort，
  硬编码在 agent.py 里换台机器就跑不起来。所以全部抽到 config.json。

配置优先级：环境变量 > config.json > 内置默认值
"""
import json
import os
import sys
import threading


def _data_dir():
    """数据目录：环境变量 > exe 同级（打包态）> 脚本目录"""
    env_dir = os.environ.get("QQ_AGENT_DATA_DIR")
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_lock = threading.RLock()

# ==================== 版本信息（v1.1.0 正式版） ====================
# 界面左上角副标题、关于模块、公告、更新检查、免责协议都用这几个常量，
# 以后发版只改这里，别在别的地方再写死版本号。
APP_NAME = "小鲸鱼 Agent 控制台"
# V1.3.2（BUG-02）：版本号不再写在这里，统一从 version.py 取。
# 以前这里有 APP_VERSION="1.1.6" 和 RELEASE_VERSION="1.0.0-beta" 两份，
# 加上 backend_server 里的第三份，界面上和安装包里能显示成两个不同的版本。
try:
    import version as _version_mod
    APP_VERSION = _version_mod.APP_VERSION
    APP_VERSION_TAG = _version_mod.APP_VERSION_TAG
    RELEASE_VERSION = _version_mod.RELEASE_VERSION
except Exception:                      # 极端情况下（文件被删）也别让程序起不来
    APP_VERSION = "2.0.2"
    APP_VERSION_TAG = "测试版"
    RELEASE_VERSION = "2.0.2"
APP_AUTHOR = "Ruao"
APP_SUBTITLE = f"{APP_VERSION_TAG} · v{APP_VERSION}"
APP_REPO_DEFAULT = ""                 # 热更新用的 GitHub 仓库，如 "Ruao/qq-agent"

# 内置默认值（config.json 缺哪个字段就用这里的）
DEFAULTS = {
    "lm_base": "http://127.0.0.1:1234/v1",
    "lm_model": "",                       # v1.3.0：默认留空，别把开发者的本机模型名带给用户
    "lm_api_key": "",
    # 本地引擎：lmstudio / ollama。选 ollama 时默认地址换成 11434，按钮文案也跟着变
    "lm_engine": "lmstudio",
    "max_tokens": 1200,
    "reasoning_max_tokens": 4000,     # 检测到推理模型时用的上限
    "temperature": 0.75,
    "top_p": 0.9,
    # reasoning_effort 取值：
    #   "auto"（默认）→ 实际发送 "none"，即关闭思考。实测 0.7s 出话；
    #                    不关思考要 34s、先想 2000+ 字，还容易把 max_tokens 吃光。
    #   "none"/"minimal"/"low"/"medium"/"high"/"xhigh" → 强制发该值
    #   "omit" / "off"  → 完全不发这个字段（给不认该参数的旧版 LM Studio）
    # 注意：LM Studio 只认 none/minimal/low/medium/high/xhigh，
    #       直接把 "auto" 发过去会 400，所以 agent.py 里会先解析再发。
    "reasoning_effort": "auto",
    "request_timeout": 300,
    "max_context_tokens": 6000,
    "enable_vision": True,

    # ---------- v1.1.2：回复速度 ----------
    # request_timeout 给后台任务（关注/说说/提取）用，可以长；
    # reply_timeout 只管「聊天回复」这一路 —— 卡住就快速兜底，不能让用户干等 5 分钟。
    "reply_timeout": 60,
    # 超时后还愿意再等一次吗（0 = 一次都不重试，直接兜底；1 = 换更短的上下文再试一次）
    "reply_retry_on_timeout": 1,
    # 打字延迟（活人感）单独可调：这是"假装在打字"，跟模型快慢无关，
    # 本地模型本来就慢，这里再压 6 秒就真的像卡住了。
    "humanize_enabled": True,
    "humanize_delay_min": 0.3,
    "humanize_delay_max": 1.0,
    "humanize_typing_cps": 40,
    "humanize_max_delay": 2.0,
    # QA 终审 P2-1：拆条参数以前写死在 agent.py（HUMANIZE_SPLIT=True、
    # HUMANIZE_SPLIT_MIN=40、段数无上限），模型回 4 行就发 4 条。
    # 现在接进配置：默认开、短线不拆、最多拆 3 段（超出合并成一段）。
    "humanize_split": True,
    "humanize_split_min": 40,
    "humanize_split_max": 3,

    # ---------- v1.4.0：快路由「记录模式」 ----------
    # 同一句话（归一化后）出现 route_memo_min 次之后，下次直接拿缓存回复，
    # 不再问模型 —— 高频的寒暄/口头禅省 token 就靠它。
    "route_memo_min": 2,
    "route_memo_max_len": 30,

    # ---------- v1.4.0：Computer Use Agent（三个开关分开管） ----------
    # v1.5.0：Agent 总闸门（关着一律不接活；开启要走一次 10 秒警告）
    "agent_enabled": False,
    # v1.5.0：事后自省 —— 聊到对方情绪不对时，隔一会儿自己回来道歉/安慰
    "post_reflect_enabled": True,
    "post_reflect_delay_min": 12,
    "post_reflect_cooldown_min": 90,
    # 键鼠控制：pyautogui（点按、打字、截图）
    "agent_keyboard": False,
    # 代码执行：白名单沙箱里跑一小段 python
    "agent_code": False,
    # 网络冲浪：Playwright headless 打开网页/搜索
    "agent_web": False,
    # 总开关：聊天里模型可以自己决定用工具（关掉只能从界面手点）
    "agent_auto": False,
    # 最多走几步（每一步 = 一次工具调用），防死循环
    "max_agent_steps": 10,
    # 危险动作（点按/输入/执行代码）前要不要人点一下确认
    "human_approval": True,
    # ---------- v1.4.0：主动打电话（拟真音视频） ----------
    "agent_call_enabled": False,
    "agent_call_start_hour": 9,
    "agent_call_end_hour": 22,
    # 一小时内最多主动呼叫几次
    "agent_call_max_per_hour": 1,
    # 打之前先看看对方在不在线（拿不到在线状态时放行，只记一条日志）
    "agent_call_check_online": True,
    # v1.5.1：「消耗预估」的频率口径（改这里，预估就跟着变）
    "est_msgs_per_day": 200,
    "est_care_per_day": 6,
    "est_moments_per_day": 2,
    "est_introspect_per_day": 3,
    "est_reflect_per_day": 1,
    "est_images_per_day": 3,
    "est_agent_per_day": 2,
    # v1.8.2：图谱两行（提取跟着记忆提取走，注入跟着每条回复走）
    "est_kg_extract_per_day": 8,
    # v1.6.0：消耗预估默认关闭（关着时后端不 import token_est、前端不显示卡片）
    "token_est_enabled": False,
    # 对方没接的来电 → 自动回拨
    "agent_call_autocallback": False,

    # ---------- v1.1.2：待办（到点主动来问）由谁决定 ----------
    # model  = 模型自己从聊天里判断要不要记（默认）
    # manual = 只认你手动加进列表的
    "event_source": "model",
    # 额外提示词：模型提取待办时会照着这个来（例：只记和工作有关的）
    "event_prompt": "",
    # 总开关（关掉后聊天里不再自动提取待办）
    "event_extract_enabled": True,

    # ---------- 活人感：私聊补回 + 引用回复 ----------
    # 关掉程序期间用户发来的私聊，重新上线后逐条补回（不限时间，只受每次条数限制）
    "private_catchup_enabled": True,
    # v1.1.6 ⑧：群聊上线补回也做成开关（以前是硬编码 True + 写死群号）
    "group_catchup_enabled": True,
    "group_catchup_max_msgs": 3,        # 每个群每次最多补几条 @ 消息
    "private_catchup_max_msgs": 6,      # 单个好友每次最多补几条
    "private_catchup_first_hours": 24,  # 首次见到该好友时，只补最近 N 小时（避免一上线刷屏半年记录）
    # 私聊回复一律带"引用"，像真人点着你的消息回
    "private_quote_reply": True,
    # v1.1.1 引用策略：引用太多像机器人逐条念，太少又不知道在回哪句。
    # quote_enabled 总开关；quote_gap = 隔了几条才值得引用（0 表示每条都引用）。
    # 默认 2：紧挨着的两条不引用，隔了 2 条以上才引用——和真人手感一致。
    "quote_enabled": True,
    "quote_gap": 2,

    # ---------- 活人感：自动发说说 ----------
    # 每天在这个区间里随机取一个配额，具体发不发、发什么由模型自己判断。
    # 改成手动档（moment_decide="timer"）时，配额就是"每天发几条"的确切数字。
    # 0 = 今天一条都不发；上限 50（配置层硬拦，界面上超过 30 会弹确认框）。
    "moment_enabled": True,
    "moment_min_per_day": 1,
    "moment_max_per_day": 10,
    "moment_hours": [8, 23],            # 只在这个时段内发
    "moment_check_interval_min": 17,    # 每轮评估间隔（分钟）

    # ---------- 活人感：表情包 ----------
    "sticker_enabled": True,
    "sticker_dir": "stickers",          # 相对数据目录
    "sticker_chance": 0.45,             # 有合适表情包时，多大比例真的发

    # ==================== v4 新增 ====================
    # ---------- 界面：主题 ----------
    # 内置 dark / light / ocean / sakura；也可以在 themes/ 目录放 <名字>.json 自定义。
    # 自定义主题的发现规则见 theme.py：themes/*.json，文件名即主题名。
    "theme": "dark",
    "theme_dir": "themes",

    # ---------- 系统：开机自启动 ----------
    # True 时在 HKCU\...\Run 里写一条 "小鲸鱼Agent" 指向本程序（不需要管理员权限）。
    "autostart": False,
    # v1.3.0：程序启动后是否自动开始工作（收消息）。默认关 —— 打开软件不等于让它上岗。
    # --autostart（开机自启）不看这个开关，开机就是要它干活。
    "agent_auto_start": False,

    # ---------- 日记 / 永久记忆 ----------
    # 只有被"绑定"的 QQ 号，记忆才会被提炼成永久记忆写进数据库；
    # 没绑定的账号只走 messages 表临时上下文（滚动窗口，会随 token 预算被截断）。
    "diary_enabled": False,
    "diary_bound_accounts": [],         # 最多 MAX_BOUND_ACCOUNTS 个 QQ 号（字符串）
    "diary_auto_write": True,           # 每天自动生成一篇日记
    "diary_hour": 23,                   # 几点生成（0-23）
    "diary_min_msgs": 6,                # 当天至少聊几句才值得写日记
    # v1.9.10：日记改成「高频记忆快照」—— 按间隔生成，不再死等晚上 22 点。
    # 为什么要有 trigger_min_msgs：时间到了但只聊了两句，写出来的全是水话，
    # 反而把「记忆碎片」污染掉，所以「没聊够就不写」。
    "diary_interval_value": 4,          # 间隔数值（配下面的单位）
    "diary_interval_unit": "hour",      # hour / minute
    "diary_trigger_min_msgs": 10,       # 这一轮至少聊几条才写快照
    "diary_last_ts": 0.0,               # 上次写快照的时间戳（后台线程自己维护）

    # ---------- 视频（v1.9.10） ----------
    # 用户在聊天里发视频链接时，自动解析标题/UP主/简介，并把封面一起喂给视觉模型
    "video_parse_enabled": True,
    # 允许她主动分享「刚刷到的视频」（受主动发言冷却保护，深夜不发）
    "agent_share_video_enabled": True,
    "share_video_gap_min": 90,          # 主动分享的最小间隔（分钟）
    "share_video_idle_min": 30,        # v2.0.2：对方安静多少分钟后才允许主动分享（正在聊就不打扰）
    # v1.9.12：连发几条消息时，等这么久没新消息就合并成一条再回
    "merge_enabled": True,
    "merge_delay_sec": 4.5,

    # ---------- 学习模块（v1.1.1） ----------
    "learn_enabled": True,              # 总开关：关掉就不往提示词里塞经验
    "learn_auto": True,                 # 自动从聊天里提炼（模型自主决定）
    "learn_mode": "auto",               # auto=模型自主 / manual=只用条件框里的提示词
    "learn_condition": "",              # 列表上方那个「条件」文本框，永久保存
    "learn_hour": 22,                   # 每天几点自动学一次（0-23）
    "learn_daily": True,
    # v1.1.4：学习安全底线。默认开；关掉必须签本机免责协议（协议文件删了就自动恢复）
    "learn_bottom_line": True,

    # ---------- 记忆：群聊 ----------
    "save_group_memory": False,         # 是否把群聊消息也写进 messages（否则只有 @ 时才落库）
    "group_bind_mode": "all",           # all=全部群都回 / whitelist=只回白名单 / blacklist=屏蔽黑名单
    "group_whitelist": [],              # 最多 MAX_BOUND_GROUPS 个群号
    "group_blacklist": [],

    # ---------- v1.1.1：群聊免 @（叫名字就回） ----------
    # 群里的消息只要带下面这些称呼就当成在叫它，不用非要 @。
    # 名字用逗号分隔；它的人设名字会自动也算进去（providers/personas 那边取）。
    "group_name_reply": True,
    "bot_names": "小鲸鱼, 鲸鱼, 小鲸",
    # 被叫到之后的「静默到点回复」：和私聊共用 checkin_silence_* 那套设置
    "group_name_cooldown": 0,

    # V1.1.4 阶段三：唤醒守护。群里有人喊「醒醒/在吗」这类词，即使没 @ 也叫它一声。
    # cooldown 是同一时间段内的最小间隔，防止有人刷屏把你刷醒几十次。
    "guard_wake_enabled": False,
    "guard_wake_words": "醒醒,在吗,在不在,出来,小鲸鱼",
    "guard_wake_cooldown": 300,
    # v1.1.4 阶段三：守护态窗口。被 @ / 叫名字 / 喊醒之后，这个群里
    # 「群友正常聊天」也接话，时长就是这里配的秒数（每接一句会刷新）。
    "guard_window_enabled": True,
    "guard_window_sec": 10,

    # ---------- v1.1.3 任务三：群聊上下文环形缓冲 ----------
    # 每个群在内存里留最近 N 条（5~100）。@ 或叫名字时，取「@ 之前 N/2 条 + @ 之后 M 条」。
    "context_buffer_size": 20,
    # 前后两条消息隔得比这个秒数久，说明是另一段对话，截断（默认 300 秒 = 5 分钟）
    "context_gap_sec": 300,
    # 同一个人连着刷屏超过这么多条就从这里截断（默认 10 条）
    "context_spam_limit": 10,
    # 活人感：没绑定过的临时会话不进上下文缓冲（省内存、也避免别人的闲聊混进来）
    "ignore_temp_sessions": True,

    # ---------- v1.1.3 任务四：情感中枢 ----------
    # 打开后回复前先让模型判一次「情绪 + 权重 + 姿态建议」，判不出来就静默用本地词表。
    "emotion_engine_enabled": True,
    # 预设沿用上面「情感预设」那项：normal 细腻 / rational 理性 / tsundere 傲娇
    # 判一次最多等多久（秒）。本地模型常常超过，超了就用本地词表，不卡回复。
    "emotion_engine_timeout": 0.9,

    # ---------- v1.1.4 阶段二：先想再说 ----------
    # 开 = 要求模型先写 <thinking> 再给正文（程序会把思考剥掉，不发给对方）
    "thinking_enabled": True,
    # 思考过程是否入库存档（控制台能看到，也能在「记忆」里复核）
    "thinking_store": True,
    # 会话跨多少小时，就把老消息压成一句话占位（最近 N 条不压）
    "context_compress_hours": 6,

    # ---------- v1.1.3 任务九：伴侣模块 ----------
    # v1.1.4 3.2：默认开启。和好感度是两套：好感度讲「熟不熟」，伴侣讲「是不是这一层关系」。
    # 现在默认不写死阶段——私聊里由模型按对话自然拿捏，群聊里永远踩刹车。
    "romance_enabled": True,
    # 好感度到这个分才自动认为「伴侣」（也可以只靠 romance_prompt 自己写死）
    "romance_min_score": 600,
    # 伴侣模式的补充要求（写什么就按什么来，留空用默认的那套）
    "romance_prompt": "",
    # 群聊里是否强制注入「公开场合」警告（强烈建议保持开启）
    "romance_group_warn": True,

    # ---------- v1.1.3 任务十二：语音回复（实验性，默认关） ----------
    # 需要本机装了 edge-tts（pip install edge-tts）。没装时开关开着也只会打一条日志。
    "tts_enabled": False,
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    # 超过这个字数就不转语音了（语音太长反而烦）
    "tts_max_chars": 60,
    # 只对私聊/只对绑定者/都开 → private / bound / all
    "tts_scope": "private",

    # ---------- v1.1.1：上线主动打招呼 ----------
    # 启动后主动给最近私聊过的人（或下面写死的 QQ 号）发一句，
    # 说什么由「场景 + 时间 + 人设 + 记忆」决定，不是固定模板。
    "online_greet_enabled": True,
    "online_greet_hours": [7, 23],      # 只在这个时段打招呼（深夜不打扰）
    "online_greet_targets": [],         # 留空 = 取最近 3 天内私聊过的人
    "persona_order": [],                # v1.1.2 #34：人设卡拖动后的顺序（key 列表）
    "startup_order": [],                # v1.1.2 #32：启动检测的先后顺序（qq/model/talk/draw/vision/feed）
    "online_greet_prompt": "",          # 留空用内置提示词；也可以自己写死一句要求
    # 主动消息（定时问候 / 查岗追问 / 主动关心计划 / 群里冒泡）统一追加的额外要求
    "proactive_prompt": "",

    # ---------- v1.1.1：画图 ----------
    "draw_enabled": True,               # 画图功能总开关
    "draw_auto": False,                 # 聊天里自己决定画不画（默认关，开了才会主动发图）
    "draw_chance": 0.15,                # 自动画的概率（0-1）
    "draw_max_side": 1280,              # 最长边上限：1280 就是 720p
    "draw_learn": True,                 # 学会画一点就往「学习」里记一条

    # ---------- 界面：自定义图标 ----------
    # icons/ 目录里放图片，然后在「设置 → 外观 → 自定义图标」里选中即可替换左上角的"鲸"
    "icon_dir": "icons",
    "icon_file": "",                    # 选中的文件名（空=用默认的"鲸"字）

    # ---------- 开发者：公告栏 ----------
    # 公告内容写在 notice.py 的 NOTICES 列表里，界面上不放按钮，靠这里开关是否显示。
    "notice_enabled": True,

    # ---------- 在线更新（GitHub） ----------
    "update_enabled": False,
    "update_repo": "",                  # 形如 "yourname/qq-agent"
    "update_branch": "main",
    "update_api": "https://api.github.com",
    "update_timeout": 20,

    # ==================== v5 新增 ====================
    # ---------- 记忆策略 ----------
    # auto   = 让模型自己判断这条消息值不值得记（推荐）
    # always = 每轮对话结束都提炼一次（记得最全，但更费 token）
    # temp   = 完全不写永久记忆，只留临时上下文（等于"忽略所有聊天"）
    "memory_mode": "auto",
    # 提炼出来的新事实如果和旧的冲突（比如改了纪念日、换了工作），
    # 是否让新事实顶掉旧条目。False 则两条都留着（会互相矛盾）。
    "memory_replace_conflict": True,

    # ---------- 云端 API 模型接入 ----------
    # provider: "local" = LM Studio 本地；其余见 providers.py 的预设表
    #   可选 local / openai / deepseek / moonshot / zhipu / qwen / openrouter /
    #        siliconflow / custom
    "lm_provider": "local",
    "cloud_base": "",                   # custom 时手填，其它由预设决定
    "cloud_model": "",
    "cloud_api_key": "",                # 只存本地，不进开源包（build.py 已排除）
    # 用量保护：单日 token 上限，超过后拒绝调用云端，避免账单失控
    "usage_daily_limit": 200000,
    "usage_guard_enabled": True,        # 超限时真的拦截（False 只警告）
    "usage_warn_percent": 80,           # 用到多少百分比开始警告

    # ---------- NapCat（QQ 接入脚本）自动检测 ----------
    # install = 只在第一次启动时检测（默认）；always = 每次启动都检测；never = 不检测
    # v1.9.14：默认【不动】用户正在用的 QQ。硬杀 QQ 会让登录态来不及落盘，
    # 下次快速登录就被判「登录态已失效」→ 只能扫码 → 反复登录容易被风控。
    "napcat_keep_qq": True,
    # v1.9.15：聊天里的动作（画图/换头像/搜/唱）改由模型自己调工具决定，
    # 不再用关键词抢答。模型端不支持 tools 时会自动退回普通调用，这个键是总闸。
    "chat_tools_enabled": True,
    "napcat_check_mode": "install",
    "napcat_dir": "",                   # 用户指定的 NapCat 目录（空=自动找）
    "napcat_http": "http://127.0.0.1:3003",
    "napcat_ws": "ws://127.0.0.1:3004",
    # 允许本软件帮你下载安装 NapCat（要联网）
    "napcat_allow_install": True,
    # v1.3.2：启动时 NapCat 没跑就自动拉起来（没登录就拉起让用户扫码）
    "napcat_auto_launch": True,
    # v1.4.2：本机 QQ 客户端占着同一个号时，自动关掉它并用缓存快速登录
    # （不用用户手点、也不用再扫码；关掉 QQ 后手机上照样能收消息）
    "napcat_auto_takeover": False,          # v1.6.2：绝不自动关用户的 QQ
    # install 模式下用来记"已经问过一次了"，避免每次启动都烦人
    "napcat_checked_once": False,

    # v1.5.2：允许导入「旧版未加密」的胶囊（默认关）——
    # 明文 zip 谁都能改，默认一律拒绝，只有用户自己知道要迁旧数据时才打开
    "spark_allow_plain": False,

    # ---------- 公告：弹窗与通知中心 ----------
    "notice_popup": True,               # 新公告用弹窗提醒一次
    "notice_remote_enabled": False,     # v1.1.2 #23：从远端拉公告（热推送）
    "notice_remote_url": "",            # 远端公告 JSON 地址（http(s)）
    "notice_center": True,              # 主界面右上角显示通知图标

    # ==================== v7 新增 ====================
    # ---------- 模型：本地 / 云端两个独立开关 + 优先级链 ----------
    # 两个开关可以同时打开。真正用哪个，看下面的 model_chain（优先级列表）。
    "local_enabled": True,              # 本地部署模块（LM Studio / Ollama 等）开关
    "api_enabled": True,                # 云端 API 模块开关（v1.3.0：默认开，让用户自己关）
    # 优先级列表：界面里可以拖动排序，排在前面的先用，失败顺延下一个。
    # 每项结构：{"kind":"local"|"cloud","provider":"deepseek","base":"...","model":"...","api_key":"..."}
    # api_key 为空时，回落到 provider_keys[provider] 或 cloud_api_key。
    "model_chain": [],
    # v1.1.2：按功能分工的模型（回复/记忆/日记/动态/画图…各用一个）
    "feature_models": {},
    # v1.1.2：插件开关（内置 key / 用户插件名 → bool）
    "plugin_enabled": {},
    # v1.1.2 #25：控制台排版（卡片顺序 + 最多 10 套预设）
    "ui_card_order": {},                # {"tab-model": ["卡片标题", ...], ...}
    "ui_layout_presets": {},            # {"我的常用": {"tab-model": [...]}}，最多 10 套
    "ui_card_drag": False,              # 开着才能在页面上拖卡片
    "lock_layout": False,               # v1.8.9：锁定卡片布局，防误触（拖不动）
    "model_failover": True,             # 前一个失败时是否顺延下一个（关掉就只用第一个）
    "provider_keys": {},                # {"deepseek": "sk-...", ...} 各云端各存一把

    # ---------- 记忆：工作目录 ----------
    # 数据库、导出、备份统一放在 <数据目录>/memory/ 下，方便整个拷走
    "memory_dir": "memory",

    # ---------- 说说：浏览别人的动态 ----------
    # 有的 NapCat 版本读不到好友动态，读不到时会自动退回 feed_inbox/ 手动投喂目录
    "feed_enabled": False,
    # model = 模型逐条判断要不要点赞/评论；always = 全部点赞+评论；readonly = 只看不互动
    "feed_decide": "model",
    "feed_poll_min": 30,                # 多久看一次（分钟）
    "feed_max_per_round": 3,            # 每轮最多处理几条
    "feed_dir": "feed_inbox",           # 手动投喂目录（JSON 文件）
    # 别人回复了"模型在你动态下的评论"，是否继续回，直到没有新回复
    "feed_comment_reply": True,
    # v1.1.2：点赞/评论/回评论三个动作分开开关（以前只能一起开，NapCat 不支持就全哑了）
    "feed_like_enabled": True,
    "feed_comment_enabled": True,
    # 回别人在"我动态下"的评论（她发了动态、朋友评论了，机器人要回一句）
    "feed_reply_enabled": True,
    "feed_reply_check_min": 10,         # 追评论的检查间隔（分钟）

    # ---------- 记忆备份 ----------
    "memory_backup_dir": "",            # v1.3.2（QA BUG-10）：备份/导出目录，空 = memory_backup
                                        # 以前写死 memory_export 而列表读 memory_backup，列表永远空
    "memory_export_dir": "memory_backup",   # 老键，仅作兼容读取（新键优先）
    "memory_auto_export": False,        # 每天自动导出一次备份
    "memory_auto_export_days": 7,       # 只保留最近 N 份

    # ---------- 卸载行为 ----------
    # True = 卸载时默认保留 config.json / memory.db（推荐）
    "keep_data_on_uninstall": True,

    # ==================== v6 新增 ====================
    # ---------- 界面：背景图 ----------
    # backgrounds/ 目录里放图片，设置里选一张作为窗口背景。
    # bg_dim 是压暗程度（0-90），避免背景太花看不清字；bg_blur 是模糊像素。
    "bg_dir": "backgrounds",
    "bg_file": "",                      # 空 = 不设背景（纯主题色）
    "bg_dim": 35,
    "bg_blur": 0,
    "bg_fit": "cover",                  # cover / contain / repeat

    # ---------- 界面：多语言 ----------
    # langs/ 目录里放 <语言码>.json 就能新增一种语言（外部扩展，不用改代码）。
    "lang": "zh-CN",
    "lang_dir": "langs",

    # ---------- 活人感：场景感知 → 主动回复计划 ----------
    # 用户说"我一会儿要下课了""下午三点面试"这类话，模型判断需要关心时，
    # 会在 care_plans 表里排一条计划，到点主动开口。
    # source: model = 只信模型判断；manual = 只在界面里手动加；both = 两者都要
    "care_plan_enabled": True,
    "care_plan_source": "model",
    "care_plan_hours": [8, 23],         # 只在这个时段内触发
    "care_plan_max_per_day": 6,         # 每个会话每天最多主动几次（防骚扰）
    "care_plan_min_gap_min": 25,        # 两次主动之间至少隔多久
    "care_plan_respect_silence": True,  # 对方正在聊的时候不打断
    # v1.8.5：主动关心到点时喂给模型的提示词（留空 = 用内置那份）。
    # 支持 {name} {now} {reason} {topic} {history} 五个占位符。
    "care_plan_prompt": "",

    # ---------- 活人感：说说发布决策 ----------
    # model = 每次评估让模型自己决定发不发（更自然，推荐）
    # timer = 老逻辑：按每日随机配额 + 时间间隔
    "moment_decide": "model",
    # 说说配图：off 不发图 / 用户预置的 moment_images/ / 表情包 / 模型生图 / mix 混着来
    "moment_image_enabled": False,
    "moment_image_source": "mix",       # preset / sticker / generated / mix
    "moment_image_dir": "moment_images",
    "moment_image_max": 1,              # 一条说说最多几张图

    # ---------- 活人感：回复说说下面的评论 ----------
    # 注意：读说说评论依赖 NapCat 的 get_qzone_* 接口，部分版本没有。
    # 检测不到时会自动退回「qzone_inbox/ 收件箱」模式（见 TECH_DOC）。
    "moment_comment_enabled": False,
    "moment_comment_poll_min": 5,
    "moment_comment_deliver": "auto",   # auto=优先评论区、失败转私聊 / comment=只评论 / private=只私聊
    "moment_comment_inbox": "qzone_inbox",

    # ---------- 生图（可选，需要支持 OpenAI 兼容 /images/generations 的服务） ----------
    # 【v8 改了】默认直接用「模型设置」里当前应用的模型 —— 不再要求学生单独配一套 API。
    #   imagegen_use_current=True 时：按优先级列表挨个试，谁支持生图就用谁；
    #   也支持在优先级列表里专门放一个生图模型（如 gpt-image-1 / flux），它会优先被选中。
    # imagegen_base / imagegen_model / imagegen_api_key 留空 = 跟随当前模型设置；
    # 只有想强制指定别的服务时才填（填了 model 就只用它）。
    "imagegen_use_current": True,
    "imagegen_enabled": False,
    # v1.4.2：出图方式 —— auto(先接口再聊天再 SVG) / images(只走 /images) / chat(聊天出图) / svg(模型写 SVG 再本地渲染)
    "imagegen_via": "auto",
    "imagegen_base": "",
    "imagegen_model": "",
    "imagegen_api_key": "",
    "imagegen_size": "1024x1024",
    "imagegen_style": "",               # 追加到 prompt 后面的风格词，如 "anime style, flat colors"
    "imagegen_timeout": 180,
    "imagegen_dir": "generated",
    # 聊天里让模型"自己画一张"当表情包发（慢，默认关）
    "sticker_gen_enabled": False,
    "sticker_gen_chance": 0.12,

    # ---------- 记忆导入/导出格式 ----------
    # 见 formats.py：qq-agent / generic-json / csv / markdown / jsonl / text
    "memory_format": "qq-agent",

    # ==================== v8 新增 ====================
    # ---------- 主动回复：什么时候才该主动开口 ----------
    # mode = fixed：对方连续 N 小时没说话，才主动发（N = checkin_silence_hours，0 = 到点就发）
    # mode = model：到点先问模型"现在适合发吗"，由模型结合上下文和人设自己决定
    "checkin_silence_mode": "fixed",
    "checkin_silence_hours": 2.0,       # 0-72，支持小数（0.02 ≈ 1 分钟，方便测试）
    # v1.1.4：统一用「秒」存一份真值，界面可选 秒/分/小时/天。
    # 老配置只有 hours 的，在 load() 里等比换算一次（写 _units_migrated 标记防重复），
    # 避免老用户升级后 2.0 小时被当成 2 秒。
    "checkin_silence_sec": 7200,
    "checkin_silence_unit": "hour",
    "_units_migrated": "",
    "_napcat_port_migrated": "",
    # v1.8.2：无边框被写坏的一次性纠正标记。**必须登记在这里**，
    # 因为 save() 只保留 DEFAULTS 里有的键 ——不登记的话标记永远写不进盘，
    # 用户自己关掉无边框后每次启动又会被强行改回 True。
    "_frameless_migrated": "",
    # 主动回复线程多久扫一次（秒）。放小到 30 秒，1 分钟的测试条目也能准点发。
    "checkin_poll_sec": 30,
    # 关心计划（模型从聊天里推断出来的）也受同一个静默门槛约束
    "care_plan_use_silence": False,
    # ---------- v1.8.10：对方没回就追问 ----------
    # 机器人发完一句，对方 N 分钟没回就自然地追问一句；
    # 最多追问 M 次、最多等 W 分钟，之后算话题结束，不再纠缠。
    "followup_enabled": True,
    "followup_gap_min": 8,
    "followup_max": 3,
    "followup_window_min": 180,
    "followup_private_only": True,
    # v1.8.11：静默太久就主动开口。计划/事件都要靠模型从聊天里抽，
    #抽不到就永远安静 —— 用户看到的就是「主动关心从没触发过」。
    "care_idle_enabled": True,
    "care_idle_silence_hours": 2.0,
    # v1.8.12：静默多久算「该主动开口」——数量 + 单位分开存。
    # 真值用秒（care_idle_silence_sec），单位只负责显示（minute/hour/day），
    # 这样「15 分钟」和「2 小时」在界面上都能填，不会被小时卡住。
    "care_idle_silence_sec": 1800,
    "care_idle_silence_unit": "minute",
    # 情感动态触发：情绪判到对方很低落，可以不等时间到就开口
    "care_trigger_by_emotion": True,
    # 情感判定三档：local（仅本地）/ model（仅大模型）/ both（本地+大模型）
    "emotion_mode": "both",
    # 情感画像：把「这个人什么脾气、哪句话会炸」记下来并拼进回复
    "emotion_profile_enabled": True,
    # 一次性迁移标记（save() 只保留登记在 DEFAULTS 里的键，必须登记！）
    "_care_idle_unit_migrated": "",

    # ---------- 好感度系统（默认关闭） ----------
    # 开启后按聊天表现给每个好友累积好感度，越高关系越近（会写进 system prompt）。
    "affinity_enabled": False,
    # v1.3.0 ⑳：关系等级表（[[分数, 名字], ...]，空 = 内置 7 档）
    "affinity_levels": [],
    "affinity_gain_per_msg": 1,         # 每聊一句 +多少（0-10）
    "affinity_gain_positive": 3,        # 说了让人开心的话额外 +多少（0-20）
    "affinity_decay_per_day": 1,        # 每天不来往 -多少（0-20，0 = 不掉）
    "affinity_max": 1000,               # 上限
    "affinity_show_score": True,        # 是否把分数告诉模型（关掉只给"关系等级"）
    "intimate_enabled": True,           # v1.1.2 #30：够熟了允许更亲密的话题
    "intimate_min_score": 600,          # 多少分以上算"可以更亲近"
    "intimate_prompt": "",              # 用户对"亲近"的额外要求（留空=按默认）
    "emotion_forecast_enabled": True,   # v1.1.2 #15：回复前先判断对方情绪（关掉可省 token）
    # ---------- 提示词本地可改（V1.1.6 补丁 3 / m08875 ⑦）----------
    # 学习提炼的提示词：留空=用 learn.py 内置的那份；填了就以这里为准（不用改代码）。
    "learn_prompt": "",
    # 情感中枢三个预设的判断口径：{"normal": "...", "rational": "...", "tsundere": "..."}，
    # 只覆盖你填了的那几项，没填的仍用内置默认。
    "emotion_preset_prompts": {},

    # ---------- 上下文与推送（v1.1.2）----------
    # 一次给模型多少轮历史：1-30，默认 8 轮（= 16 条）。调小省 token，调大接得住长对话。
    "max_history_turns": 8,
    # v1.1.5.1：历史对话总开关。关掉 = 模型只看永久记忆/摘要/这句原话，
    # 完全不看上面的对话（省 token，也避免把不该带上的话带进去）。
    "history_enabled": True,
    # 公告热推送：后台每 notice_poll_sec 秒查一次新公告，有新的就顶到前面
    "notice_poll_enabled": True,
    "notice_poll_sec": 60,
    # 网页启动版：dsh web_server.py 起的那个页面（默认关，开了才有端口）
    "web_enabled": False,
    "web_port": 8765,

    # ---------- 语义记忆（v1.1.6 核心任务一）----------
    # 开关：关掉就只走关键词召回（不调 embeddings，省一次请求）。
    "semantic_enabled": True,
    # V1.0.0-Beta 任务二：热记忆缓存。开着时同一话题的连续对话直接复用最近召回的记忆，
    # 不再为每句话都查一次库/算一次向量；关掉就每句都查（更准、更慢）。
    "memory_cache_enabled": True,
    # 每次都召回几条最相关的旧记忆（1-10）。多了占 token，少了想不起来。
    "semantic_top_k": 4,
    # 相关度门槛（0~1）：低于这个分的不塞进 prompt，宁可不提。
    "semantic_min_score": 0.35,
    # 向量后端（v1.1.6 补丁 5）：auto = 有本地 ONNX 模型就用本地，没有就用模型服务，再没有退关键词。
    # 可选值：auto / onnx / server / keyword。任何异常都静默降级，不影响聊天。
    "embed_backend": "auto",
    # 本地 embed 模型目录（里面要有 model.onnx + tokenizer.json）。留空则用下面默认位置。
    "embed_model_dir": "",
    # 归一化后的最大分块字符数：太长的记忆切成几段再算向量。
    "embed_chunk_chars": 240,

    # ---------- 界面：卡片透明度 ----------
    # 20-100，100 = 完全不透明。只在设了背景图时看得出来。
    "ui_panel_alpha": 86,

    # ---------- 界面：字体与字号（v1.1.2 #27）----------
    # 显示缩放百分比：80-150，100 = 原样。整个界面按比例放大/缩小。
    "ui_font_scale": 100,
    # 字体：留空 = 系统默认；也可以填 "Microsoft YaHei"、"SimSun" 之类
    "ui_font_family": "",
    # 强调色：留空 = 跟随主题；填 #rrggbb 就覆盖按钮/高亮的主色
    "ui_accent": "",
    # v1.1.2：界面上的文字能不能被选中（False = 点哪儿都不会选中文字，像桌面软件）
    "ui_text_select": False,
    # v1.8.4：自定义鼠标指针（深海/赛博风）。纯 CSS cursor + PNG，不走 JS 跟随，
    # 所以关掉之后立刻退回系统默认，连重绘都不用。默认开。
    "custom_cursor_enabled": True,
    # v1.1.2：卡片右下角能不能拖动改大小
    "ui_card_resize": False,
    # v1.1.2 安全：临时会话（群临时/陌生临时）默认不回复；关掉这个开关就回复
    "ignore_temp_session": True,
    # v1.1.2：「正在输入」状态（NapCat 支持就抛，不支持自动忽略）
    "typing_status_enabled": True,
    # v1.1.2：后台「睡眠反思」——每天凌晨整理昨天聊的内容，写成 3 条自我认识
    # v1.3.0 ③：条数 / 分钟 / 衰减强度都做成可配（用户在「自省」页调）
    "subconscious_enabled": True,
    "subconscious_hour": 3,
    "subconscious_minute": 0,
    "subconscious_count": 3,
    # v1.1.2：记忆衰退——把 180 天以上、很少用的永久记忆压成模糊记忆（默认关）
    "memory_decay_enabled": False,
    "memory_decay_days": 180,
    "memory_decay_max": 10,
    "memory_decay_len": 15,
    # v1.1.2：极简规则拦截（纯图片/「嗯哦哈哈」直接本地回，不调模型）
    # v1.8.5：快路由改成「默认关闭」。原来那条「字数很短就直接抽本地预设」
    # 不看上下文，连着聊两句就会被一句「嗯嗯」敷衍，用户反馈很强烈。
    # 现在要用户自己在设置里打开，并且连续对话中一律不接管。
    "fast_route_enabled": False,
    # 老逻辑（≤12 字语气词直接回预设）。默认关；想省钱可以打开。
    "fast_route_filler": False,
    # 用户自定义快路由规则：每行 `触发词或正则 => 要回的话`，# 开头是注释
    "fast_route_rules": "",
    # 同一会话这么多分钟内又说话了 = 正在连着聊 → 快路由不接管（0 = 不判）
    "fast_route_gap_min": 10,
    # 命中快路由后，还让模型复核一眼（合适就原样发，敷衍就重写）
    "fast_route_review": True,

    # ---------- 公告：堆叠显示 ----------
    # True = 右上角一条一条叠起来、可拖动、已读就不再显示；False = 老的一排横幅
    "notice_stack": True,

    # ==================== v9 / v1.1.0 正式版 新增 ====================
    # ---------- 界面：动效引擎 ----------
    # 关掉后所有过渡/悬浮/发光效果立即消失（低配机器、晕动症友好）。
    "effects_enabled": True,
    # 动效速率倍数：0.3 = 慢悠悠，1 = 标准，3 = 飞快；改成 0 等价于关闭
    "effects_speed": 1.0,
    # 光效亮度 0-100：按钮悬浮时的辉光强度，0 = 不发光
    "effects_glow": 55,
    "effects_hover": True,              # 鼠标悬浮动效
    "effects_page": True,               # 切换页面时的入场动效
    "effects_ripple": True,             # 点击水波纹
    # 积雪厚度 0-100（雪夜/金秋这类带积雪的主题才看得到）：0 = 薄薄一层，100 = 厚雪压顶
    "snow_depth": 55,
    # v1.1.5：卡片积雪 / 飘雪可以分开关（有些人只要背景飘雪，不想卡片被雪盖住）
    "card_snow_enabled": True,
    # v1.3.0 ⑰：特效可自定义（空=跟随主题；见 Docs\主题特效开发指南.md）
    "bg_effect": "",
    "card_effect": "snow",
    "snow_flakes_enabled": True,
    # v1.3.0：性能模式（低配电脑减负）。开启后关掉所有动效/毛玻璃/粒子，
    # 界面该有的功能一个不少，只是不再「滑来滑去」。
    # 首次启动会问一次（启动引导第二个弹窗），之后随时能在 设置 → 通用 里改。
    "perf_mode": False,
    # 是否已经问过「要不要开性能模式」（只问一次，别每次开都烦人）
    "perf_mode_asked": False,
    # v1.8.15：性能监控的「自动刷新」开关要能记住（以前只活在这个页面的内存里）
    "perf_auto_refresh": True,

    # ---------- v1.1.1：窗口外观与托盘 ----------
    # 无边框窗口 = 连系统边框都不要，用 index.html 里自绘的标题栏
    # v1.1.6：改成默认 True（用户要求默认无边框，更干净）；Win32 保留 WS_THICKFRAME，
    # 所以四边仍然可以拖大小。第一次运行没这个键的用户会自动拿到 True。
    "window_frameless": True,           # v1.1.6：默认用「自绘标题栏」（无系统边框）；设 False 则用深色系统标题栏
    # 关窗口时缩到托盘继续跑（点鲸鱼图标回到主界面）；False = 关了就退出
    "tray_enabled": True,

    # ---------- 本地部署：引擎选择 ----------
    # lmstudio = LM Studio（默认，OpenAI 兼容 /v1）
    # ollama   = Ollama（同样 OpenAI 兼容，默认端口 11434）
    "local_engine": "lmstudio",
    "ollama_base": "http://127.0.0.1:11434/v1",
    "ollama_model": "qwen2.5:7b",

    # ---------- 首次运行：免责协议 ----------
    # 只在第一次安装时弹一次；同意后写 True。卸载时会被重置（见 uninstall.py）。
    "disclaimer_accepted": False,
    "disclaimer_version": "1.1.0",      # 协议改版后可让它再弹一次

    # ---------- 管理员暗号（远端调试，默认关闭） ----------
    # 开启后，只有聊天内容里出现 codeword_token 才会返回控制台数据；
    # 不开则这个功能完全不存在（连解析代码都不走）。
    "codeword_enabled": False,
    "codeword_token": "",
    "codeword_scope": "all",            # all / private / group

    # ---------- 管理员控制台（v1.1.1） ----------
    # 暗号命中时用的「最高权限提示词」；不是密钥，只是额外指令。
    "admin_prompt": "",
    # 打开后：暗号命中的那一条消息不走注入防护（用户明确要求"不受安全策略阻拦"）。
    # 默认关；即使打开，也只对自带暗号的消息生效。
    "admin_bypass_guard": False,
    # v1.0.0-Beta 防爆破（QA BUG-06）：失败计数与锁定时间落在这里，
    # 关掉程序重开也不会清零，避免"重启即重置"绕过冷却。
    "admin_fail_count": 0,
    "admin_fail_last": 0.0,
    "admin_lock_until": 0.0,

    # ---------- 安全：输入防护 ----------
    # 打开后对进站文本做注入检测（SQL / Prompt / 命令），命中就隔离并记日志。
    "guard_enabled": True,
    "guard_sql": True,                  # 检测 ' OR 1=1 -- 这类
    "guard_prompt": True,               # 检测"忽略之前所有指令"这类
    "guard_command": True,              # 检测 & del /f /s 这类
    "guard_block": True,                # True = 直接丢弃并回一句拒绝；False = 只记日志放行
    # v1.9.3：上面五个开关的 HMAC 章（对不上就强制全开，见 _guard_check）。
    # **必须登记在这里** —— save() 只保留 DEFAULTS 里的键，不登记等于没写
    #（V1.8.2 的 _frameless_migrated 就栽在这上面）。
    "guard_sig": "",
    "api_key_hidden": True,             # 配置保存后前端永久不再显示明文 key
    "api_key_encrypt": True,            # 用 Windows DPAPI 加密存 api_key（不可用则明文+警告）

    # ---------- 记忆：定时导出 / 导入 ----------
    # 单位可填 second/minute/hour/day/week/month/year，界面上会自动换算。
    "memory_export_enabled": False,   # 定时自动导出总开关（默认关，用户勾了才会真跑）
    "memory_export_every": 1,
    "memory_export_unit": "day",        # 见 EXPORT_UNITS
    "memory_import_dir": "memory_import",   # 丢进这个目录就自动导入

    # ---------- 日记：自动备份（和记忆备份同一套单位换算，默认关） ----------
    "diary_export_enabled": False,
    "diary_export_dir": "",             # 空 = data/diary_backup
    "diary_export_every": 1,
    "diary_export_unit": "day",

    # ---------- 用量：导入导出格式 ----------
    "usage_export_format": "csv",       # csv / txt / json

    # ---------- 人设：一人设一文件 ----------
    "persona_dir": "personas",
    "persona_active": "default",        # 当前生效的人设 id（personas/<id>.json）

    # ---------- 更新：镜像渠道 + 完整性校验 ----------
    # GitHub 走不通时按顺序试这些镜像（先实现通道，地址留空）
    "update_mirrors": [],
    "update_auto_check": True,          # 启动时静默查一次（不落盘、不上传任何本地数据）
    "update_require_sig": True,         # 没有 sha256 校验值的包拒绝安装
    "integrity_check": True,            # 启动时校验自身文件哈希（防篡改）
    "integrity_manifest": "manifest.sha256",

    # ---------- v1.1.3：启动顺序判定（可拖拽排序） ----------
    # 每项对应 app_ui.run_startup_checks 里的一个检测步骤
    "boot_check_order": ["qq", "model", "chat", "image_out", "image_in", "qzone"],

    # ---------- v1.1.3：群聊上下文环形缓冲 ----------
    "context_buffer_size": 20,      # 每个会话在内存里留最近多少条
    "context_before_at": 8,         # @ 到本机时，往前取几条
    "context_after_at": 3,          # 往后取几条（通常是 @ 之后马上跟的话）
    "context_gap_minutes": 5,       # 前文空档超过这么久就当断片，截掉
    "context_spam_repeat": 10,      # 连续这么多条无意义灌水就截断
    "ignore_temp_sessions": False,  # 未绑定的会话不写进临时上下文

    # ---------- v1.1.3：情感中枢（大模型判情绪 + 优先级队列） ----------
    "emotion_engine_enabled": True,
    "emotion_presets": "normal",    # normal 细腻 / rational 理性 / tsundere 傲娇
    "emotion_model_timeout": 3,     # 秒；超时就降级本地词表
    "priority_queue_enabled": True,

    # ---------- v1.1.3：本地快路（时间/硬件/天气，绝不调模型） ----------
    "fastpath_enabled": True,
    "weather_city": "",             # 留空则只回一句「没配城市」

    # ---------- v1.1.3：伴侣模块（防社死） ----------
    # v1.1.4 3.2：伴侣模式默认开启（阶段由模型按对话自然拿捏，不硬套分数）
    "romance_enabled": True,       # 独立于好感度，可联动
    "romance_prompt": "",           # 伴侣口吻补充（留空用内置）
    "romance_group_guard": True,    # 群聊强制加距离约束

    # ---------- v1.1.3：语音（Edge-TTS） ----------
    "tts_enabled": False,
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    "tts_max_chars": 80,            # 超过就不转（长了像念稿）
    # v1.2.1：本地语音模型/缓存放哪。留空 = 跟着数据目录走
    # （打包版是安装目录\runtime\models\tts，不会往 C 盘用户目录里塞）。
    # 以后接大体积本地模型（几百 MB～几 GB）时，用户可以改到 D:/E: 盘。
    "tts_model_dir": "",
    # v1.3.0：语音三档 —— auto=偶尔发一条 / always=每条都发 / command=我喊了才发
    "tts_mode": "auto",
    "tts_prob": 0.2,                 # auto 档的概率
    "tts_cmd_prompt": "",            # 「喊了才发」的自定义触发词，支持 | 分隔
    # v1.8.5：默认音色太"播音腔"，给一点语速/音调偏移，听着更像正常语聊。
    # 格式与 edge-tts 一致（rate 要带 %，pitch 要带 Hz），留空 = 不改。
    "tts_rate": "+5%",
    "tts_pitch": "-2Hz",

    # ---------- v1.8.12：真唱 ----------
    # 三档引擎：read=朗读模式（本地，永远能用）/ cloud=云端音乐 API（配地址+Key）
    # / svc=本地 SVC 转换（配本地服务地址）。降级顺序见 sing.py。
    "sing_enabled": True,
    "sing_engine": "read",
    "sing_cloud_url": "",
    "sing_cloud_key": "",
    "sing_cloud_format": "suno",
    "sing_svc_url": "",
    "sing_svc_key": "",
    "sing_style": "",              # 风格描述，如「流行，女声，木吉他」
    "sing_rate": "-14%",           # 朗读模式的语速（慢一点没那么像念课文）
    "sing_pitch": "+2Hz",
    "sing_max_lines": 12,          # 最多唱几行，太长就成念经了
    "sing_polyphone": "homophone",  # homophone=替换同音字 / pinyin=拼音标注 / off

    # ---------- v1.8.12：画自己 / 偷表情包 / 换头像 ----------
    # 自我形象描述。用户说「画你自己」时，生图就用这段（留空则用内置默认描述）。
    "self_appearance": "",
    # v1.9.15：外貌特征（新的正式字段）。留空则回落到 self_appearance，
    # 再回落到「用户上传形象图时写的描述」，最后才用内置默认。
    # 例：齐肩黑发带一点蓝色挑染，浅灰卫衣，十几岁少女
    "appearance_desc": "",
    "sticker_steal_enabled": True,   # 收到 mface 表情时顺手存进 stickers/
    "avatar_auto_change": False,     # 允许机器人自己换头像（默认关，需明确授权）
    # v1.2.1：NapCat 下载源。留空 = auto（官方优先，失败自动换镜像）
    "napcat_mirror": "",

    # ---------- v1.1.3：学习模式 ----------
    "learn_mode": "auto",           # auto 自主 / fixed 固定条件触发 / manual 只手工写
    "learn_fixed_trigger": "",      # fixed 模式下的触发词

    # ---------- v1.1.3：动态收件箱 ----------
    "inbox_like": True,             # 收件箱里的动态自动点赞
    "inbox_comment": True,          # 自动评论

    # ---------- v1.3.0：音视频通话模块（独立微服务，端口 5800）----------
    # 关着就完全不知道有这个模块：不拉进程、不连端口，前端只显示一句提示。
    "av_module_enabled": False,
    # v1.6.0：性能预警确认（音视频 / 自动说说 / 本地向量记忆各记一次，确认后才允许启动服务）
    "perf_ack_av": False,
    "perf_ack_moment": False,
    "perf_ack_embed": False,
    # V1.0.0-Beta：启动时**自动**把 av_server.py 拉起来（找到模块才拉，找不到就跳过）。
    # 用户可以在这里关掉，关了就回到「手动点启动」的老行为。
    # v1.6.1（QA P0-1）：和 av_module_enabled 语义冲突过一次 —— 用户明明关了
    # 音视频，后端还是按「自动启动」把它拉起来。现在默认关，且必须
    # av_module_enabled 也为真才会拉。
    "av_auto_launch": False,
    # 微服务在哪。留空 = 自动找（控制台同级目录 / 数据目录下的「音视频接入模块」）
    "av_module_dir": "",
    "av_module_port": 5800,
    # 打包版没有 python 解释器可用时在这里指定（例如 C:\Python311\python.exe）
    "av_python": "",
    # 拉起微服务后顺手打开 RPA 接听（默认关：它会在通话弹窗上真的点鼠标）
    "av_rpa_on_start": False,
    
    # ---------- v1.8.0：知识图谱关系网 ----------
    #把永久记忆再拆一层：谁—做了什么—谁。用来回答"老板的那只猫"这种跨好几层的问题。
    #关掉只是不再注入关系网，已有的图表原样留着，不会删数据。
    "kg_enabled": True,
    "kg_hops": 2,                   # 顺着关系走几层（1~3）
    #历史建图：老记忆没三元组，靠后台线程一天啃一批补上（闲时跑，别抢模型）
    "kg_build_enabled": True,
    "kg_build_interval_min": 1440,  # 多久补一次（分钟，默认一天）
    "kg_build_batch": 30,          # 每次啃几条永久记忆（20~50）
}


_CACHE = {"stamp": None, "data": None}


def _stamp():
    """(mtime_ns, size)：文件一改，缓存立刻失效。"""
    try:
        st = os.stat(CONFIG_PATH)
        return (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return None


def load():
    """读配置，缺失字段用默认值补齐（不写回，避免污染用户文件）

    v1.1.2：加了按文件时间戳的缓存。以前每个 IPC、每次 cfg.get 都真的读一遍盘，
    一次 get_settings 能读 60 多次；现在文件没变就直接返回上一次的结果（约 20 倍）。
    返回的是新 dict，调用方随便改不会污染缓存。
    """
    stamp = _stamp()
    cached = _CACHE.get("data")
    if stamp is not None and stamp == _CACHE.get("stamp") and isinstance(cached, dict):
        return dict(cached)
    cfg = dict(DEFAULTS)
    with _lock:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cfg.update({k: v for k, v in data.items() if v is not None})
        except Exception:
            pass
    # v1.1.4 数据迁移：静默时间从「小时」改成用「秒」存真值。
    # 老用户配置里只有 checkin_silence_hours=2.0 → 换算成 7200 秒，单位仍显示「小时」，
    # 这样升级后不可能变成「2 秒」。标记 _units_migrated 保证只做一次。
    if not str(cfg.get("_units_migrated") or ""):
        try:
            _h = float(cfg.get("checkin_silence_hours") or 0)
        except (TypeError, ValueError):
            _h = 0.0
        if _h > 0:
            cfg["checkin_silence_sec"] = int(round(_h * 3600))
            cfg["checkin_silence_unit"] = "hour"
        cfg["_units_migrated"] = "1.1.4"
        try:
            save(cfg)
        except Exception:
            pass

    # v1.8.12 数据迁移：静默开口原来只有「小时」一个单位（默认 2 小时）。
    # 老用户的 2 小时不能变成 2 分钟，所以这里按「有没有动过这个值」来定单位：
    # 动过 = 当成小时换算成秒；没动过（还是默认 2.0）= 用新的 30 分钟默认值。
    if not str(cfg.get("_care_idle_unit_migrated") or ""):
        try:
            _hv = float(cfg.get("care_idle_silence_hours") or 2.0)
        except (TypeError, ValueError):
            _hv = 2.0
        if abs(_hv - 2.0) > 1e-6 and _hv > 0:
            cfg["care_idle_silence_sec"] = int(max(60, min(_hv * 3600, 604800)))
            cfg["care_idle_silence_unit"] = "hour"
        else:
            cfg["care_idle_silence_sec"] = int(cfg.get("care_idle_silence_sec") or 1800)
            cfg["care_idle_silence_unit"] = str(
                cfg.get("care_idle_silence_unit") or "minute")
        cfg["_care_idle_unit_migrated"] = "1.8.12"
        try:
            save(cfg)
        except Exception:
            pass

    # v1.6.2 数据迁移：NapCat 的默认端口从 3000/3001 改成 3003/3004（避开官方 QQ
    # 和别的程序常占的端口）。老用户的 config.json 里还写着 3000/3001，这里换掉并
    # 写标记，只做一次。标记存在时用户自己改回 3000 也不会被覆盖。
    if not str(cfg.get("_napcat_port_migrated") or ""):
        _h = str(cfg.get("napcat_http") or "")
        _w = str(cfg.get("napcat_ws") or "")
        _ch = False
        if _h.rstrip("/").endswith(":3000"):
            cfg["napcat_http"] = _h.rstrip("/")[:-4] + "3003"
            _ch = True
        if _w.rstrip("/").endswith(":3001"):
            cfg["napcat_ws"] = _w.rstrip("/")[:-4] + "3004"
            _ch = True
        cfg["_napcat_port_migrated"] = "1.6.2"
        try:
            save(cfg)
        except Exception:
            pass
        if _ch:
            try:
                import napcat_mgr as _nm
                _nm.write_onebot_config(overwrite=True)
            except Exception:
                pass

    # v1.8.2数据迁移：无边框被写坏的自愈。
    #老版本启动时把 frameless=false当成真值去调 set_decorations(true)，
    #顺手把 config.json的 window_frameless存成了 False，于是每次启动都顶着
    #系统标题栏（截图里那条黑的就是它）。这里纠正一次并留标记，
    #之后用户自己再去设置里关掉，不会被反复覆盖。
    if not str(cfg.get("_frameless_migrated") or ""):
        if cfg.get("window_frameless") is not True:
            cfg["window_frameless"] = True
        cfg["_frameless_migrated"] = "1.8.2"
        try:
            save(cfg)
        except Exception:
            pass

    # v1.3.1 数据自愈：v1.3.0 把 bg_effect / card_effect 误当开关，存进去是 true/false。
    # 读到布尔值就地改回字符串并存盘，否则界面上这两个下拉永远是「第一个选项」。
    try:
        _dirty = False
        for _k, _fallback in (("bg_effect", ""), ("card_effect", "snow")):
            if isinstance(cfg.get(_k), bool):
                cfg[_k] = "snow" if (cfg.get(_k) and _k == "card_effect") else _fallback
                _dirty = True
        if _dirty:
            save(cfg)
    except Exception:
        pass

    # v1.9.3：防护开关的章对不上就强制打开，并立刻写回
    try:
        if _guard_check(cfg):
            try:
                save(cfg, guard_ok=True)
            except Exception:
                pass
    except Exception:
        pass

    _CACHE["stamp"] = stamp
    _CACHE["data"] = dict(cfg)
    return cfg


# ---------- v1.9.3 / v1.9.4：防护开关的签名 ----------
# 4.4（V1.9.3）：guard_enabled 只是明文布尔，改成 false 就能让 SQL / Prompt /
# 命令三道检测全部失效，而且改完还能正常启动。
# 4.5（V1.9.4）：上一版是「给传进来的任何值盖章」—— config.save() 不问
# 这个 false 是谁写的，于是普通的 save_settings 就能把四个开关关掉、
# 还顺手盖上合法的章。而且章的密钥就是 secret_salt()，那个盐报告 0.43 秒
# 就能从 .pyd 里提出来，离线也能伪造。现在三件事一起改：
#   ① 章的密钥换成首次运行生成的独立密钥，用 Windows DPAPI 存盘
#      （换机器读不出来；拿不到 DPAPI 时退回本地随机密钥，并记在文档里）；
#   ② 只有 guard_apply() 这一条路能改开关，它一定写审计日志；
#   ③ 其它路径想改 → 退回上一次盖章的取值，并告诉调用方「要关请走专门入口」。
_GUARD_KEYS = ("guard_enabled", "guard_sql", "guard_prompt",
               "guard_command", "guard_block")
_GUARD_SEAL = None      # 最近一次盖章通过的取值快照
_GUARD_SEAL_KEY = b""   # 章的密钥（DPAPI 解出来，只在内存里）
_GUARD_SEAL_TAMPERED = ""   # v1.9.5：seal 文件被人换过锁（"raw" / "unknown"）


def _log_guard(level, msg):
    """防护相关的告警统一出口（写 agent.log，便于事后取证）。"""
    try:
        import app_ui as _au
        _au._append_log("[安全] " + str(msg), level)
    except Exception:
        pass


def _guard_seal_path():
    return os.path.join(os.path.dirname(CONFIG_PATH), "guard.seal")


def _guard_machine():
    """本机标识（用于把 guard.seal 绑到这台机器上，拷到别的机器就失效）。"""
    try:
        import hashlib as _hl5
        import platform as _pf5
        import uuid as _uu5
        raw = "%s|%s|%s" % (_uu5.getnode(), _pf5.node(), _pf5.machine())
        return _hl5.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    except Exception:
        return ""


def _guard_fp(key):
    """v1.9.7（N-18）：seal 的内建指纹。

    DPAPI 的信任边界是「当前 Windows 用户」，不是「本程序」—— 任何以该用户
    身份跑的脚本都能自己加密一段密钥写回 guard.seal，从而自盖章关掉防护
    （复测报告 6.1）。指纹用构建期的盐 + 本机标识做 HMAC：不知道盐就没法伪造，
    拷别的机器的 seal 也对不上。拿不到盐时退回机器标识，仍然挡「换个 seal 文件」。
    """
    try:
        import hashlib as _hl6
        import hmac as _hm6
        msg = bytes(key) + b"|" + _guard_machine().encode("ascii", "ignore")
        salt = b""
        try:
            import secure_core as _sc6
            salt = bytes(_sc6.secret_salt() or b"")
        except Exception:
            salt = b""
        if not salt:
            salt = b"xjy-guard-seal-v197"
        return _hm6.new(salt, msg, _hl6.sha256).digest()[:16]
    except Exception:
        return b""


def _guard_key():
    """取（必要时生成）防护章的密钥。存盘形式：dpapi:<b64> 或 raw:<b64>。"""
    global _GUARD_SEAL_KEY
    if _GUARD_SEAL_KEY:
        return _GUARD_SEAL_KEY
    import base64 as _b64g
    p = _guard_seal_path()
    key = b""
    seal_bad = ""
    upgrade = False          # v1.9.7.1：老格式（32 字节）待补指纹
    legacy = False           # 这个 key 是从 1.9.5/1.9.6 的老 seal 里读出来的
    try:
        if os.path.isfile(p):
            raw = open(p, "rb").read().strip()
            if raw.startswith(b"dpapi:"):
                import security as _secg
                blob = _secg._dpapi(_b64g.b64decode(raw[6:]), protect=False) or b""
                # v1.9.7（N-18）：解开之后还要核对内建指纹。
                #   48 字节 = 32 密钥 + 16 指纹（本版格式）
                #   32 字节 = 1.9.5/1.9.6 的老格式（只有密钥，没有指纹）
                # v1.9.7.1（P1）：老格式**静默接受**。以前这里判 "legacy"，
                # 等于让每个从 1.9.6 升上来的老用户都挨一刀：五个防护开关被
                # 强制打开 + 一条「被改过/被换锁」的告警，跟他们干的事毫无关系
                # （复测报告 6.1）。章的格式串没变（"guard|1.9.4|..."），同一个
                # 密钥能验通老的 guard_sig，所以开关状态可以原样保留；读完顺手
                # 把指纹补进去，下次启动就是 48 字节的新格式。
                if len(blob) == 48 and blob[32:] == _guard_fp(blob[:32]):
                    key = blob[:32]
                elif len(blob) == 32:
                    key = blob
                    upgrade = True
                    legacy = True
                else:
                    seal_bad = "fp"
                    key = b""
            elif raw.startswith(b"raw:"):
                # v1.9.5（N-13）：Windows 上 DPAPI 必然可用，seal 里出现 raw:
                # 只有一种解释 —— 有人在换锁（改成 raw:<自选密钥> 就能自己盖章）。
                # 所以这里绝不采信文件里的密钥，直接判“被换过”，稍后强制全开。
                seal_bad = "raw"
                key = b""
            else:
                seal_bad = "unknown"
                key = b""
    except Exception:
        key = b""
    if seal_bad:
        global _GUARD_SEAL_TAMPERED
        _GUARD_SEAL_TAMPERED = seal_bad
        _log_guard("WARN", "防护章文件 guard.seal 被改过（前缀 %s）—— 不采信其中的密钥，"
                           "五个防护开关强制开启。" % seal_bad)
    if len(key) != 32:
        import secrets as _secretsg
        key = _secretsg.token_bytes(32)
        upgrade = True
    if upgrade:
        # v1.9.7.1：新生成的密钥要写盘；老格式（32 字节）也要顺手补上指纹，
        # 免得每次启动都走一遍这条分支。
        # v1.9.5 的规矩不变：只写 dpapi:。写 raw: 等于把密钥明文摆在配置目录里，
        # 而且会让“换锁”和“正常降级”没法区分。拿不到 DPAPI 就只在内存里用。
        try:
            import security as _secg2
            if _secg2._dpapi_available():
                blob = bytes(key) + _guard_fp(key)
                enc2 = _secg2._dpapi(blob, protect=True)
                if enc2:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "wb") as f:
                        f.write(b"dpapi:" + _b64g.b64encode(enc2))
                if legacy:
                    _log_guard("INFO", "防护章已升级为本版格式（32 字节密钥 + 16 字节指纹），"
                                       "防护开关的取值原样保留。")
        except Exception:
            pass
    _GUARD_SEAL_KEY = key
    return key


def _guard_sig(cfg):
    """五个开关的 HMAC（密钥是本机独立生成的，不是构建期的盐）。"""
    try:
        import hashlib as _hl4
        import hmac as _hm4
        k = _guard_key()
        if not k:
            return ""
        body = "guard|1.9.4|" + ",".join(
            "%s=%d" % (kk, 1 if cfg.get(kk) else 0) for kk in _GUARD_KEYS)
        return _hm4.new(k, body.encode("utf-8"), _hl4.sha256).hexdigest()[:32]
    except Exception:
        return ""


def _guard_snapshot(cfg):
    return {k: bool(cfg.get(k)) for k in _GUARD_KEYS}


def _guard_check(cfg):
    """章不对 → 五个开关全部强制打开。返回 True 表示动过。"""
    global _GUARD_SEAL
    # v1.9.5：先解析一次 seal —— 换锁检测发生在 _guard_key() 里，
    # 不先调用它，下面那个 if 永远看到空标记（第一版就是这个顺序错）。
    try:
        _guard_key()
    except Exception:
        pass
    if _GUARD_SEAL_TAMPERED:
        changed = False
        for k in _GUARD_KEYS:
            if cfg.get(k) is not True:
                cfg[k] = True
                changed = True
        want = _guard_sig(cfg)
        if str(cfg.get("guard_sig") or "") != want:
            cfg["guard_sig"] = want
            changed = True
        _GUARD_SEAL = _guard_snapshot(cfg)
        _log_guard("WARN", "检测到防护章被换锁（%s），已把五个防护开关全部强制打开。"
                           % _GUARD_SEAL_TAMPERED)
        return changed
    try:
        import hmac as _hm4
        want = _guard_sig(cfg)
        got = str(cfg.get("guard_sig") or "")
        if want and got and _hm4.compare_digest(got, want):
            _GUARD_SEAL = _guard_snapshot(cfg)
            return False
    except Exception:
        pass
    changed = False
    for k in _GUARD_KEYS:
        if cfg.get(k) is not True:
            cfg[k] = True
            changed = True
    want = _guard_sig(cfg)
    if str(cfg.get("guard_sig") or "") != want:
        cfg["guard_sig"] = want
        changed = True
    _GUARD_SEAL = _guard_snapshot(cfg)
    return changed


def guard_apply(values, note="", actor=""):
    """唯一允许改防护开关的入口（界面上的「我确认要关」走这里）。"""
    cfg = load()
    changed = {}
    for k, v in (values or {}).items():
        if k in _GUARD_KEYS:
            cfg[k] = bool(v)
            changed[k] = bool(v)
    _guard_audit(changed, note, actor)
    out = save(cfg, guard_ok=True)
    return {"ok": True, "changed": changed, "guard": _guard_snapshot(out)}


def _guard_audit(changed, note="", actor=""):
    """关掉防护必须留痕：谁、什么时候、关了什么、为什么。"""
    try:
        import time as _tg
        p = os.path.join(os.path.dirname(CONFIG_PATH), "guard_audit.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        line = "%s\t%s\t%s\t%s\n" % (
            _tg.strftime("%Y-%m-%d %H:%M:%S"), actor or "local",
            ",".join("%s=%d" % (k, 1 if v else 0) for k, v in sorted(changed.items())),
            str(note or "")[:120])
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def save(cfg, guard_ok=False):
    """写配置（只保留已知键，避免垃圾字段堆积）。

    v1.9.4：防护开关不能顺着任何一次写配置被改掉。除 guard_apply() 外，
    其它调用方想把开关关掉，这里会把它退回上一次盖章的取值 ——
    这样「关防护」就必须经过一个有审计记录的入口。
    """
    global _GUARD_SEAL
    clean = {k: cfg[k] for k in DEFAULTS if k in cfg}
    if not guard_ok:
        # v1.9.5（N-14）：还没 load 过（进程内第一次 save）时不能拿自己当基准 ——
        # 那就等于“谁先写谁说了算”。这里一律按“全开”做基线，关闭请求必须走 guard_apply。
        base = _GUARD_SEAL if _GUARD_SEAL is not None else {k: True for k in _GUARD_KEYS}
        for k in _GUARD_KEYS:
            if k in clean and bool(clean[k]) != bool(base.get(k)):
                clean[k] = bool(base.get(k))
    clean["guard_sig"] = _guard_sig(clean)
    _GUARD_SEAL = _guard_snapshot(clean)
    with _lock:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    # v1.1.2：写完让缓存失效，下次 load 重新读（保证默认值补齐逻辑一致）
    _CACHE["stamp"] = None
    _CACHE["data"] = None
    return clean

def update(**kw):
    """批量改几个键并落盘，返回新配置。"""
    cfg = load()
    cfg.update(kw)
    return save(cfg)


def get(key, default=None):
    cfg = load()
    return cfg.get(key, DEFAULTS.get(key, default))


def set_value(key, value):
    cfg = load()
    cfg[key] = value
    save(cfg)
    return cfg


# ==================== 带范围约束的写入 ====================
# app_ui / theme / autostart_mgr 共用这一张表，保证"界面能改的"和"程序认的"一致。
# 格式：key -> (类型, 下限, 上限) ；bool/str/list 类型用下面的分支单独处理
_NUM_RULES = {
    # v1.9.10：记忆快照间隔 / 最少消息数 / 视频分享冷却
    "diary_interval_value": (float, 1.0, 720.0),
    "diary_trigger_min_msgs": (int, 1, 200),
    "share_video_gap_min": (int, 10, 1440),
    "share_video_idle_min": (int, 0, 1440),
    "merge_delay_sec": (float, 0.5, 10.0),
    "max_tokens": (int, 64, 32000),
    "reasoning_max_tokens": (int, 512, 32000),
    "temperature": (float, 0.0, 2.0),
    "top_p": (float, 0.0, 1.0),
    "max_context_tokens": (int, 1000, 200000),
    "request_timeout": (int, 10, 3600),
    "private_catchup_max_msgs": (int, 1, 50),
    "group_catchup_max_msgs": (int, 1, 20),
    "private_catchup_first_hours": (int, 1, 720),
    "moment_min_per_day": (int, 0, 50),
    "moment_max_per_day": (int, 0, 50),
    "moment_check_interval_min": (int, 1, 720),
    # v1.8.0：知识图谱
    "kg_hops": (int, 1, 3),
    "kg_build_batch": (int, 20, 50),
    "kg_build_interval_min": (int, 30, 10080),
    "sticker_chance": (float, 0.0, 1.0),
    "diary_hour": (int, 0, 23),
    "diary_min_msgs": (int, 1, 100),
    "learn_hour": (int, 0, 23),
    "draw_chance": (float, 0.0, 1.0),
    "draw_max_side": (int, 256, 1280),
    "update_timeout": (int, 5, 120),
    # v5
    "usage_daily_limit": (int, 0, 100000000),
    "usage_warn_percent": (int, 1, 100),
    "memory_auto_export_days": (int, 1, 365),
    # v6
    "bg_dim": (int, 0, 90),
    "bg_blur": (int, 0, 40),
    "care_plan_max_per_day": (int, 0, 50),
    "care_plan_min_gap_min": (int, 0, 720),
    "moment_image_max": (int, 0, 9),
    "moment_comment_poll_min": (int, 1, 240),
    "imagegen_timeout": (int, 10, 600),
    "sticker_gen_chance": (float, 0.0, 1.0),
    # v7
    "feed_poll_min": (int, 1, 1440),
    "feed_max_per_round": (int, 1, 20),
    "feed_reply_check_min": (int, 1, 240),
    # v8
    "checkin_silence_hours": (float, 0.0, 720.0),
    # v1.1.4：静默时间的「秒」真值，上限 30 天
    "checkin_silence_sec": (float, 0.0, 2592000.0),
    "checkin_poll_sec": (int, 10, 3600),
    "followup_gap_min": (int, 1, 240),
    "followup_max": (int, 1, 6),
    "followup_window_min": (int, 10, 1440),
    "care_idle_silence_hours": (float, 0.01, 720.0),
    "care_idle_silence_sec": (int, 60, 604800),
    "sing_max_lines": (int, 4, 40),
    "affinity_gain_per_msg": (int, 0, 10),
    "affinity_gain_positive": (int, 0, 20),
    "affinity_decay_per_day": (int, 0, 20),
    "affinity_max": (int, 10, 100000),
    "intimate_min_score": (int, 0, 100000),
    "ui_panel_alpha": (int, 20, 100),
    "ui_font_scale": (int, 80, 150),
    "subconscious_hour": (int, 0, 23),
    "subconscious_minute": (int, 0, 59),
    "subconscious_count": (int, 1, 8),
    "memory_decay_days": (int, 30, 3650),
    "memory_decay_max": (int, 1, 200),
    "memory_decay_len": (int, 6, 60),
    # v1.1.2：上下文轮数 / 公告轮询 / 网页端口
    "max_history_turns": (int, 1, 30),
    "notice_poll_sec": (int, 15, 3600),
    "web_port": (int, 1024, 65535),
    # v1.1.6：语义记忆
    "semantic_top_k": (int, 1, 10),
    "semantic_min_score": (float, 0.0, 1.0),
    # v9：动效 + 定时导出
    "effects_speed": (float, 0.0, 3.0),
    "effects_glow": (int, 0, 100),
    "snow_depth": (int, 0, 100),
    # v1.1.1：引用策略 / 主动关心间隔 / 好感度阈值
    "quote_gap": (int, 0, 50),
    "group_name_cooldown": (int, 0, 3600),
    # V1.1.4 阶段三：唤醒守护的最小间隔（秒）
    "guard_wake_cooldown": (int, 30, 86400),
    # v1.1.4 阶段三：守护态窗口时长（秒）
    "guard_window_sec": (int, 3, 120),
    # v1.1.3 任务三：群聊上下文缓冲
    "context_buffer_size": (int, 5, 100),
    "context_gap_sec": (int, 60, 3600),
    "context_spam_limit": (int, 3, 50),
    # v1.1.3 任务四：情感中枢
    "emotion_engine_timeout": (float, 0.2, 10.0),
    "memory_export_every": (int, 1, 9999),
    "diary_export_every": (int, 1, 9999),
    # v1.1.2：回复速度
    "reply_timeout": (int, 10, 300),
    "reply_retry_on_timeout": (int, 0, 2),
    "humanize_delay_min": (float, 0.0, 10.0),
    "humanize_delay_max": (float, 0.0, 20.0),
    "humanize_typing_cps": (int, 5, 200),
    "humanize_max_delay": (float, 0.0, 30.0),
    # QA 终审 P2-1：拆条上限（1~10，默认 3）
    "humanize_split_min": (int, 10, 400),
    "humanize_split_max": (int, 1, 10),
    # v1.4.0：记录模式阈值（同一句话几次后开始走缓存）
    "route_memo_min": (int, 2, 10),
    "route_memo_max_len": (int, 5, 200),
    # v1.4.0：Agent 步数与打电话的护栏
    "max_agent_steps": (int, 1, 50),
    "agent_call_start_hour": (int, 0, 23),
    "agent_call_end_hour": (int, 0, 23),
    "agent_call_max_per_hour": (int, 1, 10),
    "est_msgs_per_day": (int, 0, 5000),
    "est_care_per_day": (int, 0, 200),
    "est_moments_per_day": (int, 0, 50),
    "est_introspect_per_day": (int, 0, 100),
    "est_reflect_per_day": (int, 0, 50),
    "est_images_per_day": (int, 0, 100),
    "est_agent_per_day": (int, 0, 100),
    "est_kg_extract_per_day": (int, 0, 500),
    "post_reflect_delay_min": (int, 0, 240),
    "post_reflect_cooldown_min": (int, 5, 1440),
    # v1.1.3：上下文缓冲 / 情感中枢 / 语音
    "context_buffer_size": (int, 5, 200),
    "context_before_at": (int, 0, 100),
    "context_after_at": (int, 0, 50),
    "context_gap_minutes": (int, 1, 240),
    "context_spam_repeat": (int, 3, 100),
    "emotion_model_timeout": (int, 1, 20),
    "tts_max_chars": (int, 10, 500),
    "tts_prob": (float, 0.0, 1.0),
    # v1.8.5：快路由间隔（分钟，0 = 不判断「是不是在连着聊」）
    "fast_route_gap_min": (int, 0, 720),
    # v1.1.4 阶段二：上下文压缩周期（小时）
    "context_compress_hours": (float, 0.5, 168.0),
    # v1.3.0：音视频微服务端口（1024 以下要管理员权限，用不着）
    "av_module_port": (int, 1024, 65535),
}

# v1.3.1：背景特效 / 卡片视觉效果的合法取值（"空" 表示背景特效跟随主题）
FX_BG_ALLOWED = ("", "snow", "leaf", "sakura", "rain")
FX_CARD_ALLOWED = ("snow", "glow", "none")

_BOOL_KEYS = {
    "enable_vision", "private_catchup_enabled", "private_quote_reply",
    "group_catchup_enabled",
    "moment_enabled", "sticker_enabled", "autostart", "agent_auto_start", "diary_enabled",
    "diary_auto_write", "save_group_memory", "notice_enabled", "update_enabled",
    # v5
    "memory_replace_conflict", "usage_guard_enabled", "napcat_allow_install",
    "napcat_auto_launch",
    "napcat_auto_takeover",
    "napcat_keep_qq",
    "chat_tools_enabled",
    "spark_allow_plain",
    "notice_popup", "notice_center", "memory_auto_export", "keep_data_on_uninstall",
    "notice_remote_enabled",
    "napcat_checked_once",
    # v6
    "care_plan_enabled", "care_plan_respect_silence", "moment_image_enabled",
    "moment_comment_enabled", "imagegen_enabled", "sticker_gen_enabled",
    # v7
    "local_enabled", "api_enabled", "model_failover", "feed_enabled",
    "feed_comment_reply", "feed_like_enabled", "feed_comment_enabled",
    "feed_reply_enabled",
    # v8
    "imagegen_use_current", "care_plan_use_silence", "affinity_enabled",
    "affinity_show_score", "notice_stack", "intimate_enabled",
    "emotion_forecast_enabled", "ui_card_drag", "lock_layout",
    "ui_text_select", "ui_card_resize", "custom_cursor_enabled",
    "ignore_temp_session", "typing_status_enabled",
    "subconscious_enabled", "memory_decay_enabled",
    "fast_route_enabled",
    # v1.8.5：快路由的新开关也得登记，否则 save() 不认它们
    "fast_route_filler", "fast_route_review",
    "followup_enabled", "followup_private_only", "care_idle_enabled",
    "notice_poll_enabled", "web_enabled",
    "sing_enabled", "sticker_steal_enabled", "avatar_auto_change",
    "perf_auto_refresh",
    "video_parse_enabled", "agent_share_video_enabled", "merge_enabled",
    "care_trigger_by_emotion", "emotion_profile_enabled",
    "ignore_temp_sessions", "emotion_engine_enabled",
    # v9
    "effects_enabled", "effects_hover", "effects_page", "effects_ripple",
    "window_frameless", "tray_enabled",
    # v1.1.5：积雪/飘雪分开关
    "card_snow_enabled", "snow_flakes_enabled", "perf_mode", "perf_mode_asked",
    # 【v1.3.1 修】bg_effect / card_effect 是「取值的字符串」，绝不能放这里：
    # v1.3.0 误放进 _BOOL_KEYS，用户选「樱花」存进去变成 true，界面上再也选不回来、
    # 特效也不生效。它们现在走下面带白名单的分支。
    "group_name_reply",
    "guard_wake_enabled",
    "guard_window_enabled",
    "online_greet_enabled",
    "draw_enabled", "draw_auto", "draw_learn",
    "disclaimer_accepted", "codeword_enabled", "guard_enabled", "guard_sql",
    "admin_bypass_guard",
    "guard_prompt", "guard_command", "guard_block", "api_key_hidden",
    "api_key_encrypt", "update_auto_check", "update_require_sig",
    "integrity_check", "memory_export_enabled",
    "quote_enabled",
    "diary_export_enabled",
    "learn_enabled",
    "learn_auto",
    "learn_daily",
    "learn_bottom_line",
    # v1.1.2
    "humanize_enabled",
    "humanize_split",
    "event_extract_enabled",
    # v1.1.3
    "ignore_temp_sessions", "emotion_engine_enabled", "priority_queue_enabled",
    "fastpath_enabled", "romance_enabled", "romance_group_guard",
    "tts_enabled", "inbox_like", "inbox_comment",
    # v1.1.5.1
    "history_enabled",
    "semantic_enabled",
    "memory_cache_enabled",
    # v1.3.0：音视频模块
    "av_module_enabled", "av_auto_launch", "av_rpa_on_start",
    "perf_ack_av", "perf_ack_moment", "perf_ack_embed",
    # v1.4.0：Computer Use Agent + 主动呼叫 / v1.5.0：总闸门
    "agent_enabled",
    "token_est_enabled", "post_reflect_enabled", "agent_keyboard", "agent_code", "agent_web", "agent_auto",
    "human_approval", "agent_call_enabled", "agent_call_check_online",
    "agent_call_autocallback",
    # v1.8.0：知识图谱
    "kg_enabled", "kg_build_enabled",
}

_EFFORT_ALLOWED = ("auto", "omit", "off", "none", "minimal", "low", "medium", "high", "xhigh")

# v5：记忆策略三档
MEMORY_MODES = ("auto", "always", "temp")
# v5：NapCat 检测策略
NAPCAT_CHECK_MODES = ("install", "always", "never")
# v6：主动回复计划来源
CARE_PLAN_SOURCES = ("model", "manual", "both")
# v6：说说发布决策方式
MOMENT_DECIDES = ("model", "timer")
# v6：说说配图来源
MOMENT_IMAGE_SOURCES = ("preset", "sticker", "generated", "mix")
# v6：评论回复投递方式
MOMENT_COMMENT_DELIVERS = ("auto", "comment", "private")
# v6：背景图填充方式
BG_FITS = ("cover", "contain", "repeat")
# v7：浏览别人动态时的互动方式
FEED_DECIDES = ("model", "always", "readonly")
# v7：优先级列表最多几条
MAX_MODEL_CHAIN = 10
# v8：主动回复的静默判定方式
#   fixed = 对方连续 checkin_silence_hours 小时没说话才发
#   model = 到点问模型"现在适合发吗"，由模型结合上下文和人设决定
CHECKIN_SILENCE_MODES = ("fixed", "model")
# v1.1.4：时间单位（统一换算成秒存库/存配置）
TIME_UNITS = ("second", "minute", "hour", "day")
TIME_UNIT_SEC = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
TIME_UNIT_CN = {"second": "秒", "minute": "分钟", "hour": "小时", "day": "天"}
# v8：说说条数的手动上限（界面上超过 30 会弹确认，超过 50 直接拒绝）
MOMENT_MAX_HARD = 50
MOMENT_MAX_WARN = 30

# v9：本地部署引擎（都走 OpenAI 兼容 /v1，只是默认地址不同）
LOCAL_ENGINES = ("lmstudio", "ollama")
LOCAL_ENGINE_CN = {"lmstudio": "LM Studio", "ollama": "Ollama"}
LOCAL_ENGINE_DEFAULT_BASE = {
    "lmstudio": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}

# v9：定时导出的时间单位 —— 只认这些，界面下拉和换算都用这张表。
# 换算成秒时按「近似值」处理（月=30天、年=365天），这样界面上填 12 月 = 1 年。
EXPORT_UNITS = ("second", "minute", "hour", "day", "week", "month", "year")
EXPORT_UNIT_CN = {
    "second": "秒", "minute": "分钟", "hour": "小时", "day": "天",
    "week": "周", "month": "个月", "year": "年",
}
EXPORT_UNIT_SECONDS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 604800, "month": 2592000, "year": 31536000,
}
# 定时导出的最大间隔：365 天（用户要求"最大限制在 365 天"）
EXPORT_MAX_DAYS = 365

# v9：用量导出的文件格式
USAGE_EXPORT_FORMATS = ("csv", "txt", "json")
# v9：暗号作用范围
CODEWORD_SCOPES = ("all", "private", "group")

# ---------- v1.1.3 ----------
# 启动顺序判定：key -> 展示名（顺序按用户配置的 boot_check_order 走）
BOOT_STEPS = {
    "qq": "QQ 接入服务",
    "model": "模型服务",
    "chat": "出话",
    "image_out": "出图",
    "image_in": "读图",
    "qzone": "动态读写",
}
BOOT_STEP_KEYS = tuple(BOOT_STEPS)

# 学习模式：自主提炼 / 固定条件触发 / 只手工写
LEARN_MODES = ("auto", "fixed", "manual")
LEARN_MODE_CN = {"auto": "自主学习", "fixed": "固定条件触发", "manual": "只手工写"}

# 情感预设：常规细腻 / 直男理性 / 傲娇
EMOTION_PRESETS = ("normal", "rational", "tsundere")
EMOTION_PRESET_CN = {"normal": "常规细腻型", "rational": "直男理性型", "tsundere": "傲娇型"}


def export_interval_seconds(every, unit):
    """把「每 N 个单位」换算成秒。

    坑（用户明确提过）：选了「年」不要存成 365 年 —— 所以这里统一按秒换算，
    并且把总时长卡在 EXPORT_MAX_DAYS 天以内。返回 (秒, 说明文字)。
    """
    try:
        u = str(unit or "day").strip().lower()
        if u not in EXPORT_UNIT_SECONDS:
            u = "day"
        n = float(every or 1)
        if n <= 0:
            n = 1
        secs = n * EXPORT_UNIT_SECONDS[u]
        cap = EXPORT_MAX_DAYS * 86400
        capped = False
        if secs > cap:
            secs = cap
            capped = True
        txt = f"每 {int(n) if n == int(n) else n} {EXPORT_UNIT_CN[u]}"
        if capped:
            txt += "（已按上限 365 天计）"
        return secs, txt
    except Exception:
        return 86400, "每 1 天"


def human_duration(seconds):
    """把秒数说成人话：91 秒 → 1分31秒；93784 → 1天2小时。"""
    try:
        s = int(max(0, float(seconds)))
    except Exception:
        return "-"
    if s < 60:
        return f"{s} 秒"
    for unit, name in ((31536000, "年"), (2592000, "个月"), (604800, "周"),
                       (86400, "天"), (3600, "小时"), (60, "分")):
        if s >= unit:
            n = s // unit
            rest = s % unit
            return f"{n} {name}" + (f" {human_duration(rest)}" if rest else "")
    return f"{s} 秒"


def _coerce_bool(value):
    """把各种"像布尔"的东西转成 bool。

    坑：内置 bool("0") == True、bool("false") == True。
    前端/手改 config.json 时经常传字符串，必须显式认这几种写法，
    否则"关掉开关"会变成"打开开关"。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    s = str(value).strip().lower()
    if s in ("", "0", "false", "no", "off", "none", "null", "n", "f", "否", "关", "关闭"):
        return False
    if s in ("1", "true", "yes", "on", "y", "t", "是", "开", "开启"):
        return True
    return bool(s)


def set_checked(key, value):
    """按规则写入一个键，返回 (ok, msg, coerced_value)。

    规则：
      * _NUM_RULES 里的键 → 转成目标类型并夹到区间内
      * _BOOL_KEYS 里的键 → 强转 bool
      * reasoning_effort   → 白名单
      * 其余标量（字符串/列表/字典）→ 直接写
    未知键直接拒绝，防止前端塞垃圾。
    """
    if key not in DEFAULTS:
        return False, f"未知配置项：{key}", None

    if key in _NUM_RULES:
        cast, lo, hi = _NUM_RULES[key]
        try:
            v = cast(value)
        except (TypeError, ValueError):
            return False, f"{key} 需要数字，收到 {value!r}", None
        v = max(lo, min(hi, v))
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"{key} = {v}", v

    if key in _BOOL_KEYS:
        v = _coerce_bool(value)
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"{key} = {v}", v

    if key == "diary_interval_unit":
        v = str(value or "hour").strip().lower()
        if v not in ("hour", "minute"):
            return False, "diary_interval_unit 只能是 hour / minute", None
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, "%s = %s" % (key, v), v

    if key == "reasoning_effort":
        v = str(value or "auto").strip().lower()
        if v not in _EFFORT_ALLOWED:
            return False, f"reasoning_effort 只能取 {_EFFORT_ALLOWED}", None
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"reasoning_effort = {v}", v

    if key == "memory_mode":
        v = str(value or "auto").strip().lower()
        if v not in MEMORY_MODES:
            return False, f"memory_mode 只能取 {MEMORY_MODES}", None
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"memory_mode = {v}", v

    if key == "imagegen_via":
        v = str(value or "auto").strip().lower()
        if v == "vector":
            v = "svg"
        if v not in ("auto", "images", "chat", "svg"):
            return False, "imagegen_via 只能取 auto / images / chat / svg", None
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"出图方式 = {v}", v

    if key == "napcat_check_mode":
        v = str(value or "install").strip().lower()
        if v not in NAPCAT_CHECK_MODES:
            return False, f"napcat_check_mode 只能取 {NAPCAT_CHECK_MODES}", None
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"napcat_check_mode = {v}", v

    # ---------- v6：枚举型 ----------
    for _key, _allowed in (
        ("care_plan_source", CARE_PLAN_SOURCES),
        ("moment_decide", MOMENT_DECIDES),
        ("moment_image_source", MOMENT_IMAGE_SOURCES),
        ("moment_comment_deliver", MOMENT_COMMENT_DELIVERS),
        ("bg_fit", BG_FITS),
        # v7
        ("feed_decide", FEED_DECIDES),
        # v8
        ("checkin_silence_mode", CHECKIN_SILENCE_MODES),
        # v1.1.4：静默时间的单位
        ("checkin_silence_unit", TIME_UNITS),
        # v1.1.0：本地引擎（LM Studio / Ollama）
        ("lm_engine", LOCAL_ENGINES),
        # v1.1.1：备份间隔单位（秒/分/时/天/周/月/年）
        ("memory_export_unit", EXPORT_UNITS),
        ("diary_export_unit", EXPORT_UNITS),
        # v1.1.1：学习方式（模型自主 / 手动条件）
        ("learn_mode", LEARN_MODES),
        # v1.1.3：情感预设
        ("emotion_presets", EMOTION_PRESETS),
    ):
        if key == _key:
            v = str(value or _allowed[0]).strip().lower()
            # v1.1.5.1：前端有些地方传的是短单位（h/m/s/d），
            # 之前直接判不合法 → 整个保存补丁失败，用户看到「一串错误」。
            v = {"h": "hour", "m": "minute", "s": "second", "d": "day",
                 "hr": "hour", "min": "minute", "sec": "second"}.get(v, v)
            if v not in _allowed:
                return False, f"{_key} 只能取 {_allowed}", None
            cfg = load()
            cfg[key] = v
            save(cfg)
            return True, f"{_key} = {v}", v

    if key in ("bg_effect", "card_effect"):
        # v1.3.1：这两个是枚举字符串（不是开关）。顺手兼容历史布尔值：
        # card_effect 的 true → snow、false → none；bg_effect 的 true/false 一律视作「跟随主题」。
        v = str(value or "").strip().lower()
        if isinstance(value, bool):
            v = ("snow" if value else "none") if key == "card_effect" else ""
        allowed = FX_BG_ALLOWED if key == "bg_effect" else FX_CARD_ALLOWED
        if v not in allowed:
            return False, f"{key} 只能取 {allowed}", None
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"{key} = {v!r}", v

    if key in ("theme", "theme_dir", "icon_dir", "icon_file", "sticker_dir",
               "group_bind_mode", "update_repo", "update_branch", "update_api",
               # v5
               "lm_provider", "cloud_base", "cloud_model", "cloud_api_key",
               "napcat_dir", "napcat_http", "napcat_ws", "memory_export_dir", "memory_backup_dir",
               "diary_export_dir",
               # v6
               "bg_dir", "bg_file", "lang", "lang_dir", "moment_image_dir",
               "moment_comment_inbox", "imagegen_base", "imagegen_model",
               "imagegen_api_key", "imagegen_size", "imagegen_style", "imagegen_via",
               "imagegen_dir", "memory_format",
               # v7
               "memory_dir", "feed_dir",
               # v1.1.3
               "weather_city", "tts_voice", "learn_fixed_trigger", "romance_prompt",
                   "tts_mode", "tts_cmd_prompt",
                   # v1.8.5
                   "tts_rate", "tts_pitch", "fast_route_rules",
                   "care_plan_prompt", "moment_comment_deliver",
                   # v1.8.12
                   "sing_engine", "sing_cloud_url", "sing_cloud_key",
                   "sing_cloud_format", "sing_svc_url", "sing_svc_key",
                   "sing_style", "sing_rate", "sing_pitch", "sing_polyphone",
                   "self_appearance", "appearance_desc",
                   # v1.8.12（任务一/三）
                   "care_idle_silence_unit", "emotion_mode"):
        v = str(value or "").strip()
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"{key} = {v!r}", v

    if key in ("diary_bound_accounts", "group_whitelist", "group_blacklist",
               "online_greet_targets", "persona_order", "startup_order",
               # v6
               "care_plan_hours", "moment_hours",
               # v1.1.3
               "boot_check_order"):
        if not isinstance(value, (list, tuple)):
            return False, f"{key} 需要列表", None
        if key == "boot_check_order":
            # 只认认识的步骤；去重；缺的按默认顺序补在后面（保证永远 6 步都在）
            seen, v = set(), []
            for x in value:
                s = str(x or "").strip().lower()
                if s in BOOT_STEP_KEYS and s not in seen:
                    seen.add(s)
                    v.append(s)
            for s in DEFAULTS["boot_check_order"]:
                if s not in seen:
                    v.append(s)
            cfg = load()
            cfg[key] = v
            save(cfg)
            return True, f"{key} = {' → '.join(v)}", v
        if key in ("care_plan_hours", "moment_hours"):
            try:
                v = [int(x) for x in value][:2]
            except (TypeError, ValueError):
                return False, f"{key} 需要两个整数（起止小时）", None
            if len(v) != 2 or not (0 <= v[0] <= 23 and 0 <= v[1] <= 24):
                return False, f"{key} 需要形如 [8, 23]", None
            cfg = load()
            cfg[key] = v
            save(cfg)
            return True, f"{key} = {v}", v
        v = [str(x).strip() for x in value if str(x).strip()]
        cfg = load()
        cfg[key] = v
        save(cfg)
        return True, f"{key} 共 {len(v)} 项", v

    # ---------- v7：优先级列表 ----------
    if key == "model_chain":
        if not isinstance(value, (list, tuple)):
            return False, "model_chain 需要列表", None
        cleaned = []
        for item in value:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in ("local", "cloud"):
                continue
            entry = {
                "kind": kind,
                "provider": str(item.get("provider") or ("local" if kind == "local" else "custom")).strip(),
                "base": str(item.get("base") or "").strip(),
                "model": str(item.get("model") or "").strip(),
                "api_key": str(item.get("api_key") or "").strip(),
            }
            if not entry["model"]:
                continue
            cleaned.append(entry)
            if len(cleaned) >= MAX_MODEL_CHAIN:
                break
        cfg = load()
        cfg[key] = cleaned
        save(cfg)
        return True, f"优先级列表共 {len(cleaned)} 项（上限 {MAX_MODEL_CHAIN}）", cleaned

    # ---------- v1.1.2：按功能分工的模型 ----------
    if key == "feature_models":
        if not isinstance(value, dict):
            return False, "feature_models 需要字典", None
        cleaned = {}
        for fk, fv in value.items():
            fk = str(fk or "").strip()
            if not fk or not isinstance(fv, dict):
                continue
            model = str(fv.get("model") or "").strip()
            if not model:
                continue
            cleaned[fk] = {
                "provider": str(fv.get("provider") or "custom").strip(),
                "base": str(fv.get("base") or "").strip(),
                "model": model,
                "api_key": str(fv.get("api_key") or "").strip(),
            }
        cfg = load()
        cfg[key] = cleaned
        save(cfg)
        return True, f"已保存 {len(cleaned)} 个功能的模型分工", cleaned

    if key == "plugin_enabled":
        if not isinstance(value, dict):
            return False, "plugin_enabled 需要字典", None
        cleaned = {str(k).strip(): bool(v) for k, v in value.items() if str(k).strip()}
        cfg = load()
        cfg[key] = cleaned
        save(cfg)
        return True, f"已记录 {len(cleaned)} 个插件开关", cleaned

    if key in ("ui_card_order", "ui_layout_presets"):
        if not isinstance(value, dict):
            return False, f"{key} 需要字典", None
        if key == "ui_layout_presets" and len(value) > 10:
            return False, "最多存 10 套排版预设", None
        cleaned = {}
        for k, v in value.items():
            name = str(k or "").strip()
            if not name:
                continue
            # v1.8.5：ui_card_order 的值是「每栏一组卡片键」的**列表**，以前这里只留
            # dict、其它一律变成 {} —— 存进去的顺序是空的，之后每次 loadSettings
            # 又按空顺序套一遍，预设就「存了跟没存一样」。列表必须原样留着。
            if isinstance(v, dict):
                cleaned[name] = v
            elif isinstance(v, list):
                cleaned[name] = [str(x) for x in v if str(x or "").strip()]
            else:
                cleaned[name] = {}
        cfg = load()
        cfg[key] = cleaned
        save(cfg)
        return True, f"已保存 {len(cleaned)} 项", cleaned

    if key == "emotion_preset_prompts":
        # V1.1.6 补丁 3（m08875 ⑦）：三个情感预设的判断口径可以本地改，只覆盖填了的项。
        if not isinstance(value, dict):
            return False, "emotion_preset_prompts 需要字典", None
        cleaned = {}
        for pk, pv in value.items():
            pk = str(pk or "").strip()
            if pk not in EMOTION_PRESETS:
                return False, f"emotion_preset_prompts 里有未知预设：{pk}", None
            text = str(pv or "").strip()
            if not text:
                continue
            if len(text) > 2000:
                return False, f"预设 {pk} 的口径太长（最多 2000 字）", None
            cleaned[pk] = text
        cfg = load()
        cfg[key] = cleaned
        save(cfg)
        return True, f"已保存 {len(cleaned)} 个预设口径", cleaned

    if key == "learn_prompt":
        text = str(value or "").strip()
        if len(text) > 4000:
            return False, "学习提示词太长（最多 4000 字）", None
        cfg = load()
        cfg[key] = text
        save(cfg)
        return True, ("学习提示词已更新（留空=用内置）" if text else "学习提示词已恢复内置"), text

    if key == "provider_keys":
        if not isinstance(value, dict):
            return False, "provider_keys 需要字典", None
        cleaned = {str(k).strip(): str(v or "").strip()
                   for k, v in value.items() if str(k).strip()}
        cfg = load()
        cfg[key] = cleaned
        save(cfg)
        return True, f"已保存 {len(cleaned)} 个提供方的 Key", cleaned

    cfg = load()
    cfg[key] = value
    save(cfg)
    return True, f"{key} 已更新", value


def ensure_file():
    """首次运行时生成一份 config.json，方便用户手改"""
    if not os.path.exists(CONFIG_PATH):
        try:
            save(DEFAULTS)
        except Exception:
            pass
    return CONFIG_PATH


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("config path:", CONFIG_PATH)
    print(json.dumps(load(), ensure_ascii=False, indent=2))
