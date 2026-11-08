# -*- coding: utf-8 -*-
"""公告栏 —— 开发者功能，界面上不显示按钮

===NOTICES = [{'id': '2026-11-03-x1912',
  'version': '1.9.12',
  'date': '2026-11-03',
  'level': 'important',
  'title': '小鲸鱼 1.9.9：使用说明、这一版修了什么',
  'body': '**小鲸鱼 1.9.9 · 使用说明与更新**\n'
          '\n'
          '**一、她能做什么**\n'
          '· 聊天：QQ 私聊、群里接话，语气和人设都能自己调\n'
          '· 记事：长期记忆、日记、关系网（谁和谁什么关系，她记得住）\n'
          '· 空间：看说说、回评论、自己发说说\n'
          '· 唱歌与语音：点歌真唱、语音条、自定义音色（要你自己配）\n'
          '· 电话：语音通话可用；视频通话正在内测，这一版先关掉\n'
          '· 联网：需要时自己上网查（设置里打开「网上冲浪」）\n'
          '· 主动：定时关心、到点提醒（你说「下午四点叫我」就会叫）\n'
          '\n'
          '**二、第一次用怎么弄**\n'
          '1) 设置 → 模型：填一个云端 Key，或本地 Ollama / LM Studio 地址\n'
          '2) 设置 → QQ：启动 NapCat，点「接管」直接用已登录的 QQ，不用扫码\n'
          '3) 聊天页选一个联系人就能说话；要她主动找你，就在「主动关心」里打开\n'
          '\n'
          '**三、这一版修了什么**\n'
          '· 说说评论不再自己回自己（以前会无限套娃）\n'
          '· 你说「几点叫我 / 提醒我」不会再被「静默时间」压住，到点就提醒\n'
          '· 程序文件的校验只查程序本体：改说明文档、换图片不会再被误判成「被篡改」\n'
          '\n'
          '**四、官方群：1046057152**\n'
          '有问题可以进来问，不强制加入。\n'
          '\n'
          '**五、小批量测试期说明**\n'
          '视频通话、自动接管鼠标键盘（RPA）暂时关闭或默认关，稳定后再放出来。\n'
          '\n'
          '**五、V1.9.9.1 补了什么**\n'
          '· 她不会再给自己点赞、自己评论自己了（以前会把自己的说说当成好友动态）\n'
          '· 回复过的评论不会被重复回；你自己发的评论也不会被当成新评论再回一遍\n'
          '· 你说「下午四点叫我」这类明确指令，就算你正在安静/睡觉也会按时叫你\n'
          '· 完整性校验只查程序本体：改 README、换图片都不会再被拦\n'
          '\n'
          '**六、V1.9.9.2 补了什么**\n'
          '· 她记得你发过的图了：你说「网上买的」，她不会再反问「买的啥呀」\n'
          '· 每张图都会留一句极短的注记（如「一碗米饭和一碗汤」），下一轮还能接着聊\n'
          '· 聊天规则里加了一条：刚评价过的图，不许装作没看见\n'
          '\n'
          '**七、V1.9.9.3 补了什么**\n'
          '· 读评论不会再报「不支持的Api」，这个 NapCat 版本没有读评论接口时会自动换一条路\n'
          '· 空间网页协议兜底：能读到自己说说下的评论，照常回复、照常去重\n'
          '· 接口暂时读不到时，日志里只留一行调试信息，不再刷红字\n'
          '\n'
          '七、V1.9.10 补了什么\n'
          '· 她记得你发过的图了：不只记「发过图」，还记图里是什么，下一轮能接着说。\n'
          '· 你甩一个视频链接过来，她会自己点开看标题和简介，还看得见封面。\n'
          '· 她也会主动分享：刷到有意思的视频，挑一个发给你（太晚不发，有冷却）。\n'
          '· 记忆改成「快照」：不再一天一篇，而是每隔几小时记一小段（关键事实 + 情绪走向）。就算聊天记录被截断，她也能从最近的碎片里接上话。\n'
          '· 日志和回复的「当前状态」挪到了消息末尾，云端缓存命中更稳，长对话更省钱。\n'
          '· 她可以「嘴硬心软」：嘴上拒绝换头像/改名，背地里偷偷换掉，然后继续演。\n'
          '\n'
          '八、V1.9.11 补了什么\n'
          '· 省钱：给云端的前缀现在稳定了 —— 以前每条消息的情感判定都会带一段「当前时间」，同一段内容每分钟都算新的，缓存永远命中不了；现在那部分拿掉了。\n'
          '· 靠谱：找「上一版」做增量包时能正确认 `1.9.8-Beta` 这种带后缀的版本号了，以后不会再因为认不出而断链。\n'
          '\n'
          '九、V1.9.11.1 修正\n'
          '· 写清楚了一件事：换头像/改名（包括「悄悄换」）只是她**本来就有的一个能力**，什么时候用、要不要用，完全看她当时的人设与三观 —— '
          '没有哪一种性格被规定必须偷偷换，也没有哪一种性格被规定不许换。\n'
          '\n'
          '十、V1.9.11.2 修了什么\n'
          '· **换人设真的会重启了**：以前界面说「正在重启」，其实只是又调了一次启动（人已经在跑 → '
          '直接返回），所以换完人设还是旧的在说话。现在会先停掉旧进程、等一秒、再起新进程，新进程读的就是你选的那个人设。\n'
          '· **动手模式也认人设了**：以前它写死「你是小鲸鱼」，你换成阿冷，它动手时还是小鲸鱼的口气；现在每次动手前都会重新读一遍当前人设。\n'
          '· **日记里不再出现 [CQ:image,file=…] 这种机器码**：图片记成「[图片]」、表情记成「[表情]」、语音记成「[语音]」，读起来像正常流水账。\n'
          '\n'
          '十一、V1.9.12 补了什么（活人感）\n'
          '· 连发几条消息会等她说完再一起回，不再一句一个气泡地追着答。\n'
          '· 你引用了她之前说的话，她现在能看到原话，接得上茬。\n'
          '· 她自己分享过的视频/链接，聊起来时记得是自己发的，不会装作不知道。\n'
          '· 你发图说「换上当头像」，她会认真考虑换，而不是只夸一句好不好看。\n'}]
一处）
============================================================
 往下翻到  NOTICES = [

    NOTICES = [
        {
            "id": "2026-10-22-001",          # 唯一标识，随便起，别重复
            "date": "2026-10-22",            # 显示用的日期
            "level": "info",                 # info / warn / important 三档
            "title": "关于记忆功能的说明",
            "body": "把用户需要知道的事写在这里；换行用 \\n 转义，别写真实换行。",
            "link": "",                      # 可选：一个 http(s) 链接
            "dismissible": True,             # 用户能不能点"不再提示"
            "expire": "",                    # 可选：'2026-12-31' 之后自动不再显示
        },
        ...
    ]

  · 改完保存即可，界面「控制台」Tab 顶部会自动出现公告卡片。
  · 不想让公告出现 → 界面「设置 → 开发者」里关掉，或把 config.json 的
    notice_enabled 改成 false。
  · 想彻底隐藏公告栏 → 把 index.html 里 id="noticeBar" 那个 div 删掉即可，
    Python 端不会报错（前端找不到元素就跳过）。

  v5 新增两个行为：
    · 弹窗提醒：新公告会在启动时弹一次窗（只弹一次，靠 notice_popup_shown 记）。
      config.json 的 notice_popup 可关。
    · 通知中心：主界面右上角的铃铛图标，点开能看到所有公告（含已读）。
      config.json 的 notice_center 可关。

============================================================
 设计说明
============================================================
  · 公告内容不落盘、不进数据库 —— 它就写在这个文件里，跟着代码一起发布，
    所以你改完重新打包就生效，用户端不需要任何迁移。
  · 用户点"不再提示"的记录存在 agent_state.json 的 notice_dismissed 里
    （见 load_dismissed / dismiss），不用单独建表。
  · 这个模块不 import agent.py，避免循环依赖；需要读状态文件时自己读。
============================================================
"""
import json
import os
import sys
import time

# ============================================================
#  ↓↓↓↓↓↓↓↓↓↓  在这里写你的公告  ↓↓↓↓↓↓↓↓↓↓
# ============================================================
NOTICES = [{'id': '2026-11-08-x201',
  'version': '2.0.1',
  'date': '2026-11-08',
  'level': 'important',
  'title': 'V2.0.1：修好开源包里缺 core/ 的问题',
  'body': '这一版只修一个打包问题，功能和 V2.0.0 完全一样。\n'
          '一、问题：V2.0.0 把原来那个大文件拆成了 core/ 目录下的九个模块，但打「开源包」时用的目录白名单没跟着更新 —— 开源包里只有 agent.py，没有 core/。谁拿源码包去跑，第一句 `import agent` 就会报「找不到 core」。\n'
          '二、普通用户完全不受影响：安装包和绿色版里跑的是编译好的后端程序，模块都打在里面了，不缺任何东西。只有拿源码包做二次开发的人才会碰到。\n'
          '三、现在白名单补上了 core/，并且打包脚本多了一道硬自检：开源包里必须同时有 agent.py 和 core/ 下的每一个模块，缺一个就直接中断打包（以前只核对条目总数，总数对但内容缺了照样放行，所以这次才漏出去）。\n'
          '四、顺手把根目录里那些「_打头」的临时脚本（自测探针、核对脚本）也从开源包里排除了，不再跟着源码包发出去。\n'
          '其余行为和 V2.0.0 完全一致。'},
  {'id': '2026-11-08-x200',
  'version': '2.0.0',
  'date': '2026-11-08',
  'level': 'important',
  'title': 'V2.0.0：底层引擎架构全面升级，运行更稳、拓展更强',
  'body': '这一版**没有加新功能**，动的全是里子：把原来那一个上万行的代码文件按职责拆开了。\n'
          '一、为什么要动它。那个文件以前什么都管 —— 连模型、拼提示词、发消息、跑定时任务、看空间、提记忆，全堆在一起，改一处要通读上下文，出问题也很难定位。现在按职责拆成九块，改哪件事就只看哪一块。\n'
          '二、拆成了什么。大模型通信（请求怎么发、端点挂了怎么顺延、用量怎么记）、提示词与记忆拼装、后台定时任务（关心/查岗/说说/评论/日记等十四个循环）、消息处理流水线、事件与记忆提取、空间互动、发送层、纯函数库、配置桥梁。每一块开头都写了「这里管什么、不在这里的东西在哪」。\n'
          '三、你最关心的：会不会变慢、会不会坏。不会。这一版是**纯搬家**，一行业务逻辑都没重写；对外调用方式一个都没改。每一块搬完都跑了全套自测（12 套、433 项），每一次都是全绿才继续。\n'
          '四、顺带修掉三个「搬家时才发现」的隐患：事件重试队列缺一个模块导入（会让后台重试整条失效）、记忆文本清洗用到的几个规则没跟着搬（异常路径会报错）、空间回评论缺一个人设名字的来源（回评论会静默失败）。这三处正常聊天碰不到，但都是真会炸的地方，现在都补上了。\n'
          '六、升级安全这件事我们专门做了三道防呆。跨大版本升级（V1.9.x → V2.0.0）最容易踩的坑是：你以前把绿色版解压进过安装目录，那里就留着一份旧版清单；新版本启动时看见「目录里的清单对不上现在的文件」，会判成「被篡改」而拒绝启动 —— 这就是所谓「变砖」。现在：① 启动器会先把「官方签过、但属于别的版本」的旧清单清掉，只认新版本的那份；② 增量包发现你当前版本跨了大版本，会红字直接拦住，让你去下完整包，绝不会给你升成半砖；③ 万一真的弹出「文件被改过」，提示里会多一句「如果您刚做了大版本升级，可能是旧文件残留，请重新下完整包覆盖，或用恢复工具」。\n'
          '五、对一个普通用户来说，这一版和 V1.9.25 长得一模一样、用起来一模一样。它的意义是：以后加功能、改毛病都会更快更稳。\n'
          '其余行为和 V1.9.25 完全一致。'},
  {'id': '2026-11-06-x1925',
  'version': '1.9.25',
  'date': '2026-11-06',
  'level': 'important',
  'title': 'V1.9.25：官方 QQ 开着也能连上 + 唱歌真的唱了',
  'body': '这一版修两件事。\n'
          '一、上一版「官方 QQ 开着的时候 NapCat 连不上」，真因不在登录，而在报错判断。日志里其实已经写得清清楚楚：快速登录成功了、3003 端口也起来了，可 1.5 秒后 NapCat 的工作进程就被顶下线了 —— 同一个号不能手机/电脑/机器人三边同时在线。而旧程序只会翻启动日志的最后 800 行找错误，那里躺着几天前的一条旧记录（「登录态已失效」），它就把这条旧记录当成了本次的结论：立刻放弃等待、对外报「20 秒内端口没起来」，还顺手把另一个本来能用的号记成了坏号。\n'
          '二、现在程序只看「这次启动之后」写出来的日志，并且多认一种情况：登录成功了、紧接着又被顶下线 —— 这就是被官方 QQ 占着号的样子（以前只认「已登录，无法重复登录」那一种）。认出以后会自动让开，去用没被占着的号，不会再拿一个已经登进去的号当成「端口没起来」来回折腾。\n'
          '三、唱歌以前只是把歌词用几个不同音高念出来，听着确实生硬。这一版真的做了唱腔：把歌词按字切开，每个字单独变调唱出来（走五声音阶的旋律），加上颤音和自然的淡入淡出，再叠一层软件合成的伴奏，拼成一段完整的音频发给你。你听到的不再是「念」了。\n'
          '四、这层真唱依赖两个小库（合计约 1.6MB）。万一某台机器上装不齐，她会自动退回原来的朗读方式，不会变成没声音。\n'
          '其余行为和 V1.9.24 完全一致。'},
  {'id': '2026-11-05-x1924',
  'version': '1.9.24',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.24：长期记忆终于真的存得下来了',
  'body': '这一版把「记忆一直提取不出来」从头到尾修干净了，一共四个原因，全在里面。\n'
          '一、最隐蔽的一个：提示词让模型按「- 记住: （人名）事实」这种格式回答，可程序里另有一段逻辑专门删聊天回复里的「记住:」控制行 —— 它把提取结果整句挖空了。于是模型明明答了，程序拿到的却是空字符串，日志只写「本轮没有值得长期记住的内容」。现在提取走的是不过滤的原文本。\n'
          '二、以前那条提示词写着「闲聊、玩笑、情绪宣泄都不算」。可你和朋友聊的本来就是情绪、调侃、约定这些东西 —— 模型照规矩回答「无」。现在能记的清单扩到六类：基本情况、情绪状态、关系变化、承诺约定、强偏好和雷区、说话习惯，并且明确要求「出现这些必须至少记一条，不许因为不够客观就跳过」。\n'
          '三、某个模型端点挂了以后程序不记得，下一轮又去连、又等满 90 秒。现在挂掉的端点会被临时拉黑（连不上 2 分钟、云端 503 是 3 分钟、额度问题 10 分钟），恢复后自动解除。你正等着的那句回复不受影响，永远会真的去试。\n'
          '四、三条后台提取任务（会话记忆、永久记忆、事件）以前共用一把锁，同时启动时只能跑一个，另外两个直接被丢掉。现在各用各的。\n'
          '五、时间她会算得更明白了。你说「我四电下课」，她不会再自己瞎猜是凌晨四点还是下午四点，而是按现在的时间挑最可能的那个（下午 16:00，已经过去几小时），并说清另一种可能；你说「下午四电」「今晚八点」「明天九点」，她都能直接换算成具体钟点。\n'
          '六、顺带把提取结果解析放宽了：模型给 JSON、给编号列表、带 [work] 这种分类标签，现在都能认出来。\n'
          '其余行为和 V1.9.23 完全一致。'},
  {'id': '2026-11-05-x1923',
  'version': '1.9.23',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.23：记忆提取不会再「连不上本地模型」了',
  'body': '这一版修的是「聊天在用云端，记忆却一条都存不下来」。\n'
          '一、原因：聊天走的是「优先级链」（云端配在前面就先云端），而记忆提取、事件提取这些后台任务走的是另一个地址——全局单模型设置。你把聊天切到云端之后，那套全局设置还停在本地 LM Studio 上，于是每轮都去连那台已经关掉的机器，等满 90 秒超时，什么都没提取出来。\n'
          '二、现在它们跟聊天用同一条链：云端在前就用云端，云端挂了才顺延到本地。也就是说，你不用为了记忆特意去开 LM Studio。\n'
          '三、想给提取单独指定模型（比如用便宜的小模型跑记忆），去「设置 → 模型设置 → 按功能分工」里配「记忆提取 / 事件提取 / 滚动摘要」，配了就用你配的那个。\n'
          '四、顺带修了两个「看着一直在跑、其实一次都没成」的老毛病：「学习」以前调的是一个不存在的读聊天方法，永远只回一句「聊天太少，先多聊几句」，现在真的会从你们最近的对话里总结了；每日总结的提示词一格式化就报错，也一并修好，群里聊的也算。\n'
          '五、连发合并的等待时间从 2.5 秒调到 4.5 秒 —— 你一句话分几条发的时候，她不会抢在半截就开口。\n'
          '其余行为和 V1.9.22 完全一致。'},
  {'id': '2026-11-05-x1922',
  'version': '1.9.22',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.22：同一条评论不会被回第二遍了',
  'body': '这一版专治「同一条评论每隔十分钟又被回一遍」。\n'
          '一、根本原因不在去重逻辑，而在状态没存下来。程序里有两套回评论的循环，它们都要记「这条已经回过了」「上一轮是什么时候」——可这份状态从来就没被写进状态文件，每次读出来都是空的。于是每一轮都以为「这些评论都没回过」，同一批评论被反复回复。现在这份状态会正常落盘，重启也还在。\n'
          '二、两套循环不再各回一遍。以前「动态回评论」和「评论回复」读的是同一批评论，一个先回、另一个过五分钟再回一次。现在评论回复统一由一处负责，另一处只把新评论记进库，不再自己发。\n'
          '三、她自己的回复不会再被当成新评论。接口把她刚发的「回复 @某某 ：…」也读了回来，拿不到自己账号时会认不出来，就成了自己回自己。现在按内容特征直接摘掉。\n'
          '四、顺带补了评论 id 的兜底：有些版本回复成功却不告诉你评论 id，导致「这条是我刚发的」认不出来。现在拿不到 id 会回读一次评论列表按内容找回来。\n'
          '其余行为和 V1.9.21 完全一致。'},
  {'id': '2026-11-05-x1921',
  'version': '1.9.21',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.21：她不会再把「包装台词」当成你说过的话',
  'body': '这一版是 V1.9.20 的补丁，只修两处验收时翻出来的毛病。\n'
          '一、语义记忆里不再存内部包装文案。你连发几条消息时，程序会把它们包成「用户连续发送了多条消息，请综合这些内容统一回复：[消息1：…]」再给模型看 —— 这段本来是临时包装，却被当原话存进了语义库。之后每次检索到它，模型上下文里都会带着这段机器腔，还有被复读出来的风险。现在写入只存你的原话（多句就是一行一句），并且升级后会自动把以前存错的那几条清掉，别的地方一条不动。\n'
          '二、评论降级成私聊时不再拆成两条。以前「你在我的说说下面评论了…我偷偷在这儿回你：」和正文会被拆成两条气泡，中间空一拍，看着像说漏嘴。现在整条一起发。\n'
          '其余行为和 V1.9.20 完全一致。'},
  {'id': '2026-11-05-x1920',
  'version': '1.9.20',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.20：说说评论修好了 —— 不再报错重试，也不再重复回复',
  'body': '这一版只重做一件事：她在你空间里回别人的评论。\n'
          '一、不再刷「不支持的Api」。以前后台一直调 NapCat 的评论接口，可这个接口在你现在的版本里根本不存在，于是每一轮都失败、每一轮又重来。现在这条链路彻底关掉了，评论统一走 QQ 空间的网页协议（用你登录 QQ 的凭证直接调空间自己的接口），不再依赖那个不存在的接口。\n'
          '二、回复会带称呼。以前回一句「哈哈」挂在你朋友下面，看着像个陌生人自言自语。现在格式固定成「回复 @某某 ：内容」，谁都能一眼看出是在接谁的话。\n'
          '三、不会再重复回复同一条。以前接口报错之后系统以为没回成功，下一轮又把同一条抓出来再回一遍。现在三道闸：发送前先查这条是不是已经回过、发完不管接口报不报错都立刻记成已回、外加五分钟内同一条绝不回第二次。就算状态被意外改回「待回复」，也发不出去。\n'
          '四、网页协议走不通时会自动降级成私聊回一句，不会静默吞掉；不管哪种方式，只要发过一次就不再重试。\n'
          '其余行为和 V1.9.19 完全一致。'},
  {'id': '2026-11-05-x1919',
  'version': '1.9.19',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.19：唱歌不再念百科，她也不会再「装傻」了',
  'body': '这一版两件事：唱歌翻车，和「刚说过的话她当没听见」。\n'
          '一、唱歌不再把百科当歌词念。以前点《告白气球》，搜回来的其实是一段小说内容简介，程序把它当歌词，用语音念了五十多秒，还带「加入书架」「开始阅读」这种字。现在只要搜到的是简介、百科、书城那一类文字，一律判为没搜到，不会再念。\n'
          '二、唱完不再说「没接音乐接口，先这么念给你听」。她本来就不知道什么是接口、TTS、朗读，说这些只会让人一秒出戏。现在唱完最多补一句求夸奖的话（「怎么样，没跑调吧？」），语音里也只有歌词，不夹解释。\n'
          '三、搜不到歌词会诚实说。想给你唱这首、但没找到词时，她会直接说，并让你先唱两句给她听，不会再硬念一段不属于这首歌的东西。\n'
          '四、主动问候会先看你们刚才聊了什么。以前到点问候是空的上下文加一句写死的「吃了吗」——你刚说完吃过饭，她还在问吃了没。现在问候、查岗、追问之前都会先读最近四轮真实对话；刚聊过吃饭、下班、睡觉这些，就不许再问同一件事，会换个话题或顺着刚才那件事往下说。\n'
          '五、被你说「不是说过了吗」时不再狡辩。以前她会回「我刚才问你的是吃了没，你还没答呢」，很生硬。现在像真人那样认下来（「哎呀你看我这脑子」），不会硬说自己没问过。\n'
          '其余行为和 V1.9.18 完全一致。'},
  {'id': '2026-11-05-x1918',
  'version': '1.9.18',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.18：QQ 开着也能把小鲸鱼拉起来了',
  'body': '这一版只修一件事：点了「启动 NapCat」却一直被顶回来。不是 NapCat 坏了，是我们自己拦的。\n'
          '一、本机 QQ 开着不再拒绝启动。以前只要检测到 QQ.exe，程序就整条判定拒绝，回一句「请先去官方 QQ 里退出登录」——你本来就想一边挂着 QQ 一边让她在线，于是点几次都是同一句话，怎么都拉不起来。现在照常启动。\n'
          '二、被你的 QQ 占着的号会自动跳过。同一个号不能两边同时在线，所以程序会先挑一个没被占着的本地号直接登录；都占着就直接出二维码，你扫另一个号就行。被占着的号会在列表里标清楚（「你的 QQ 正用着」），不会再让你对着它反复点。\n'
          '三、「退 QQ 并接管」变成一个按钮。真绕不开、你确实想让她用正在用的那个号时，弹窗里会有一个按钮，点一下才会去退你的 QQ，退完立刻用本地缓存登上，不用扫码。不会再自动替你退。\n'
          '四、旧二维码不再冒充新码。以前目录里留着上一次的二维码图片时，哪怕这次根本没起来、或者其实已经登录成功，界面也会显示成「在等扫码」。现在只认这次启动之后写出来的码。\n'
          '如果你只是手动点了「扫码登录」，行为与 V1.9.17 完全一致。'},
  {'id': '2026-11-05-x1917',
  'version': '1.9.17',
  'date': '2026-11-05',
  'level': 'important',
  'title': 'V1.9.17：登录修好了，二维码不再是一张看不清死活的图',
  'body': '这一版只修一件事：登录 QQ 这一段。以前最容易卡在这里，现在把能自动的都自动了。\n'
          '一、二维码画在应用里了。以前点「扫码登录」会拉一个系统看图程序打开一张 png——图在别的窗口里，根本看不出它过没过期，只能扫了才知道；过期了还得关掉、重启、再扫。反复扫码正是被腾讯风控的直接原因。现在二维码就在这个窗口里，下面写着「这张码还有 XX 秒」。\n'
          '二、过期会自己换。到期没扫，程序会直接替你向 QQ 要一张新的，二维码原地换掉，不用重启、不用退出、也不用重开窗口。\n'
          '三、登录过好几个 QQ 时可以自己挑。这台电脑上登录过的号会列出来（标着「上次用的」「缓存可用」「上回失效」），点哪个就用本地缓存直接登哪个——不扫码、不退官方 QQ、也不重启 NapCat。同一个号正开在官方 QQ 里时会明说「不能两边同时在线」，不会让你对着二维码一直扫。\n'
          '四、连接状态是看出来的，不是猜出来的。以前界面靠「进程在不在」猜登录状态，现在直接问 NapCat 自己要状态：在出码、等你扫码、正在初始化、已登录，都会如实显示。\n'
          '五、拿不到就如实说。NapCat 的 WebUI 没开、端口被占、接口不通，这些都只降级成一句人话 + 一个「重启并出码」按钮，不会让界面崩掉，也不会给你一张不知道死活的图。\n'
          '其余行为和 V1.9.16 完全一致。'},
  {'id': '2026-11-04-x1916',
  'version': '1.9.16',
  'date': '2026-11-04',
  'level': 'important',
  'title': 'V1.9.16：修掉三个「越聊越不像人」的小尾巴',
  'body': '这一版是 V1.9.15 的补丁，只修三条 QA 抓出来的问题，没有新功能。\n'
          '一、聊天用的包装词不再写进记忆里。以前你连着发几条，程序会打包成「[消息1：…][消息2：…]」再发给她；这个包装本来只该出现在那一次请求里，结果被原样存进了对话历史和长期记忆。于是她下一轮从自己的记忆里读到这些机器腔，越聊越像程序。现在只存你的原话：多句就按行拼（「在吗」换行「今天面试挂了」），图片和表情包记成「[图片]」「[表情包：xx]」。\n'
          '二、事后自省不再重复报名。她事后补话时看到的上文里，说话人名字以前会被拼两遍（「阿杰：阿杰：我今天面试挂了」），模型照着学，补出来的话也开始自报家门。现在历史里已经带了名字就不再拼。\n'
          '三、B 站和小程序卡片能读到真的标题了。以前只读到平台名「哔哩哔哩」，你说「这个视频笑死」，她压根不知道是哪个视频。现在优先读卡片里的描述当标题，只有平台名时会用后面的真实视频标题顶上，链接照旧保留。\n'
          '其余行为和 V1.9.15 完全一致。'},
  {'id': '2026-11-04-x1915',
  'version': '1.9.15',
  'date': '2026-11-04',
  'level': 'important',
  'title': 'V1.9.15：把她身上「像机器」的地方拆掉了',
  'body': '这一版只干一件事：把「像机器」的地方拆掉。\n'
          '一、连发不再被拆成一堆回复。你连着发三条，她现在会等 2.5 秒、把这三条当成一段话看，只回一次；中间间隔太久（超过 8 秒）就先回一批，不会让你干等。群里两个人同时说话，不会被并成一句，也不会顶着别人的名字回你。\n'
          '二、卡片和链接她真的看得见了。以前你发视频卡片、小程序卡片、网页分享，她只收到一句「[卡片消息]」，于是回「没看到图」。现在会把卡片标题和链接一起读进去，当成「你分享了一个东西」来聊，不会再装瞎。\n'
          '三、她不再靠关键词抢动作了。以前程序自己用字符串猜你的意思——你说一句「你看这个」，它可能直接画一张图；你说「换头像」，它回一句写死的台词。现在改成她自己在回复里决定要不要动手（画图、换头像、搜索、唱歌），程序只负责执行。你发链接、发图、发视频，只是分享，她不会突然开始作画。\n'
          '四、自画像必须是她本人。以前没填形象就直接凭空捏一个人，一眼就看出来不是同一个。现在按「设置里的外貌特征 → 上传形象图时写的描述 → 内置默认」的顺序取值，你上传过形象图还会垫图再画；两样都没有时她会老实说没画，而不是嘴硬说「图发你了」。\n'
          '五、事后自省不再瞎编。以前补话时只给她看「你上一句」和「对方这一句」，前面聊了什么都不给，所以她只能硬编，于是出现「是程序啦…中午刚吃过大白饭」这种八竿子打不着的句子。现在补话前会先把最近三轮真实对话喂给她，她自己判断该不该补；觉得没问题就一个字都不发。\n'
          '六、加了一段「像个人，别像个程序」的硬规则：不许把毫无关联的记忆碎片硬拼进回复；不许像客服一样「好的呢」「感谢您的分享」；对方发搞笑视频可以吐槽，对方说废话可以只回一个「？」；接不上就少说两句，绝不胡编。\n'
          '如果你觉得她还是啰嗦或者爱乱画，设置里有一项「聊天里允许她调用动作」（默认开），关掉就完全按纯聊天处理。'}]





# ============================================================
#  ↑↑↑↑↑↑↑↑↑↑  公告写到这里为止  ↑↑↑↑↑↑↑↑↑↑
# ============================================================


LEVELS = ("info", "warn", "important")

_LEVEL_ALIAS = {
    "info": "info", "普通": "info", "通知": "info", "": "info",
    "warn": "warn", "warning": "warn", "警告": "warn", "提醒": "warn",
    "important": "important", "urgent": "important",
    "重要": "important", "紧急": "important",
}


def _data_dir():
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
STATE_FILE = os.path.join(DATA_DIR, "agent_state.json")


# ==================== 状态文件读写 ====================
def _read_state():
    try:
        if not os.path.exists(STATE_FILE):
            return {}
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_state_key(key, value):
    """只改一个键，别把 agent_state.json 里别的东西覆盖掉"""
    try:
        d = _read_state()
        d[key] = value
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        return True
    except Exception:
        return False


def load_dismissed():
    """用户点过"不再提示"的公告 id 集合"""
    d = _read_state().get("notice_dismissed") or []
    if not isinstance(d, list):
        return set()
    return {str(x) for x in d}


def dismiss(notice_id):
    """把一条公告标记为已读/不再提示"""
    nid = str(notice_id or "").strip()
    if not nid:
        return False, "公告 id 为空"
    cur = load_dismissed()
    if nid in cur:
        return True, "已经不再提示了"
    cur.add(nid)
    ok = _write_state_key("notice_dismissed", sorted(cur))
    return (True, "已不再提示") if ok else (False, "写入 agent_state.json 失败")


def restore_all():
    """把公告状态彻底重置：不再提示 / 已读 / 已弹窗 三份记录全清。

    界面上用它做"恢复全部公告"，语义是"全部当作没看过"，
    所以 read 也要清 —— 只清 dismissed 的话未读数还是 0，
    用户会以为按钮没生效。
    """
    ok1 = _write_state_key("notice_dismissed", [])
    ok2 = _write_state_key("notice_read", [])
    ok3 = _write_state_key("notice_popup_shown", [])
    if ok1 and ok2 and ok3:
        return True, "已恢复全部公告（未读、不再提示均已重置）"
    return False, "写入失败"


# ==================== 公告计算 ====================
def _today():
    return time.strftime("%Y-%m-%d")


def _is_expired(expire):
    """expire 支持 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM'，空表示不过期"""
    s = str(expire or "").strip()
    if not s:
        return False
    return s[:10] < _today()


def _normalize(item, idx):
    """把一条公告补全成前端能直接用的结构"""
    if not isinstance(item, dict):
        return None
    nid = str(item.get("id") or f"auto-{idx}").strip()
    title = str(item.get("title") or "").strip()
    _raw_body = item.get("body")
    if _raw_body is None:
        _raw_body = item.get("content")
    # v1.8.7：body 写成 list（一行一段）也要能显示 —— 以前 str(list) 会把
    # 整个 Python repr 塞给前端，界面上就是一串带方括号和引号的字符。
    if isinstance(_raw_body, (list, tuple)):
        body = chr(10).join(str(x) for x in _raw_body).strip()
    else:
        body = str(_raw_body or "").strip()
    if not title and not body:
        return None
    level = _LEVEL_ALIAS.get(str(item.get("level") or "info").strip().lower(), "info")
    link = str(item.get("link") or "").strip()
    if link and not (link.startswith("http://") or link.startswith("https://")):
        link = ""      # 只允许 http(s)，防止 javascript: 之类
    return {
        "id": nid,
        # v1.8.1：公告写的版本号要留着，否则没法核对它跟安装包是不是一版
        "version": str(item.get("version") or "").strip(),
        "date": str(item.get("date") or _today())[:16],
        "level": level,
        "title": title or "公告",
        "body": body,
        "link": link,
        "dismissible": bool(item.get("dismissible", True)),
        "expire": str(item.get("expire") or "")[:16],
    }


REMOTE_FILE = "notices_remote.json"   # v1.1.2 #23：远端热推送下来的公告缓存


def _remote_path():
    return os.path.join(_data_dir(), REMOTE_FILE)


def remote_notices():
    """读远端缓存的公告（没推送过就是空列表；坏文件当没有）。"""
    try:
        with open(_remote_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.get("notices") or [])
    except Exception:
        pass
    return []


def refresh_remote(url="", timeout=8):
    """v1.1.2 #23：从远端拉一份公告（JSON 数组，或 {"notices":[...]}）。

    - 只认 http(s)；配置里没填地址就直接返回「没配」
    - 拉下来先校验能解析、每条有 id 和 title，再落盘；失败不动旧缓存
    返回 {"ok","count","new","msg"}
    """
    import config as _c
    url = str(url or "").strip()
    if not url:
        try:
            url = str(_c.get("notice_remote_url") or "").strip()
        except Exception:
            url = ""
    if not url:
        return {"ok": False, "count": len(remote_notices()), "new": 0,
                "msg": "没配远端公告地址（设置 → 公告与通知 里可以填）"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "count": 0, "new": 0, "msg": "地址要以 http(s):// 开头"}
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return {"ok": False, "count": 0, "new": 0,
                    "msg": f"远端返回 HTTP {r.status_code}"}
        data = r.json()
    except Exception as e:
        return {"ok": False, "count": 0, "new": 0,
                "msg": f"拉取失败：{type(e).__name__}: {e}"}
    items = data if isinstance(data, list) else list((data or {}).get("notices") or [])
    clean = []
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        if not str(raw.get("id") or "").strip() or not str(raw.get("title") or "").strip():
            continue
        clean.append(raw)
    if not clean:
        return {"ok": False, "count": 0, "new": 0, "msg": "远端公告是空的（或格式不对）"}
    old_ids = {str(x.get("id")) for x in remote_notices()}
    new_ids = [str(x.get("id")) for x in clean if str(x.get("id")) not in old_ids]
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_remote_path(), "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "count": len(clean), "new": 0,
                "msg": f"已拉到 {len(clean)} 条，但写缓存失败：{e}"}
    msg = f"已同步 {len(clean)} 条远端公告"
    if new_ids:
        msg += f"，其中 {len(new_ids)} 条是新的"
    return {"ok": True, "count": len(clean), "new": len(new_ids), "msg": msg}


def all_notices():
    """全部公告（已补全、已过滤过期），按日期倒序（新的在上）。

    v1.1.2 #23：远端热推送来的公告也并进来（id 相同的以本地为准）。
    """
    out = []
    local_ids = set()
    for i, raw in enumerate(NOTICES):
        n = _normalize(raw, i)
        if n is None:
            continue
        if _is_expired(n["expire"]):
            continue
        local_ids.add(str(n.get("id") or ""))
        out.append(n)
    for i, raw in enumerate(remote_notices()):
        if str(raw.get("id") or "") in local_ids:
            continue
        n = _normalize(raw, 9000 + i)
        if n is None or _is_expired(n["expire"]):
            continue
        n["remote"] = True
        out.append(n)
    # 日期倒序；同一天按 id 倒序（id 里通常带 v4/v5 这类序号，保证新的在上）
    out.sort(key=lambda x: (x["date"], str(x.get("id") or "")), reverse=True)
    return out


def active_notices(include_dismissed=False):
    """当前应该显示的公告。

    返回 {"ok","enabled","items","total","dismissed_count","msg"}
    """
    try:
        import config as _c
        enabled = bool(_c.get("notice_enabled", True))
    except Exception:
        enabled = True

    items = all_notices()
    dismissed = load_dismissed()
    if include_dismissed:
        for it in items:
            it["dismissed"] = it["id"] in dismissed
    else:
        items = [it for it in items if it["id"] not in dismissed]

    return {
        "ok": True,
        "enabled": enabled,
        "items": items if enabled else [],
        "total": len(all_notices()),
        "dismissed_count": len(dismissed),
        "msg": "" if enabled else "公告栏已在设置里关闭",
    }


def count_unread():
    """未读公告条数（给右上角小红点用）"""
    return len(active_notices()["items"])


# ==================== v5：弹窗提醒一次 + 通知中心 ====================
# 三个独立的状态集合，各管一件事：
#   notice_dismissed   用户点了"不再提示" → 再也不出现（连通知中心也不显示）
#   notice_read        用户点开看过了 → 通知中心里变灰，但还留着可回看
#   notice_popup_shown 已经弹过窗了 → 不会再弹第二次（"弹窗只提醒一次"）
def _load_set(key):
    d = _read_state().get(key) or []
    if not isinstance(d, list):
        return set()
    return {str(x) for x in d}


def load_read():
    return _load_set("notice_read")


def mark_read(notice_id):
    """标记某条公告为已读（用户在通知中心点开时调用）"""
    nid = str(notice_id or "").strip()
    if not nid:
        return False, "公告 id 为空"
    cur = load_read()
    if nid in cur:
        return True, "已经是已读"
    cur.add(nid)
    ok = _write_state_key("notice_read", sorted(cur))
    return (True, "已标为已读") if ok else (False, "写入 agent_state.json 失败")


def mark_all_read():
    """一键全部已读（通知中心右上角）"""
    ids = {it["id"] for it in active_notices()["items"]}
    if not ids:
        return True, "没有未读公告"
    cur = load_read() | ids
    ok = _write_state_key("notice_read", sorted(cur))
    return (True, f"已把 {len(ids)} 条标为已读") if ok else (False, "写入失败")


def popup_pending():
    """需要弹窗提醒的公告（从没弹过的那些）。

    用户要求「公告改弹窗提醒一次」—— 所以这里只返回
    id 不在 notice_popup_shown 里的公告；调用方弹完要立刻 mark_popup_shown。
    已 dismiss 的、过期的、notice_enabled=False 的都不弹。
    """
    try:
        import config as _c
        if not bool(_c.get("notice_popup", True)):
            return {"ok": True, "items": [], "msg": "弹窗提醒已关闭"}
        if not bool(_c.get("notice_enabled", True)):
            return {"ok": True, "items": [], "msg": "公告栏已关闭"}
    except Exception:
        pass
    shown = _load_set("notice_popup_shown")
    dismissed = load_dismissed()
    read = load_read()
    items = [it for it in all_notices()
             if it["id"] not in shown and it["id"] not in dismissed]
    for it in items:
        it["read"] = it["id"] in read
    return {"ok": True, "items": items, "msg": ""}


def mark_popup_shown(notice_ids):
    """记下"这些公告已经弹过窗了"，下次不再弹"""
    if isinstance(notice_ids, str):
        notice_ids = [notice_ids]
    cur = _load_set("notice_popup_shown")
    add = {str(x).strip() for x in (notice_ids or []) if str(x).strip()}
    if not add - cur:
        return True, "无需更新"
    cur |= add
    ok = _write_state_key("notice_popup_shown", sorted(cur))
    return (True, "已记录弹窗") if ok else (False, "写入失败")


def center():
    """通知中心的数据（右上角图标点开就是它）。

    与 active_notices 的区别：这里**包含已读**（只是打上 read 标记），
    也包含"不再提示"的（打上 dismissed 标记），方便用户回看历史公告。

    返回 {"ok","enabled","items","unread","total","msg"}
    """
    try:
        import config as _c
        enabled = bool(_c.get("notice_enabled", True))
        center_on = bool(_c.get("notice_center", True))
    except Exception:
        enabled, center_on = True, True

    if not enabled or not center_on:
        return {"ok": True, "enabled": False, "items": [], "unread": 0,
                "total": len(all_notices()),
                "msg": "通知中心已在设置里关闭"}

    dismissed = load_dismissed()
    read = load_read()
    items = []
    for it in all_notices():
        it = dict(it)
        it["read"] = it["id"] in read
        it["dismissed"] = it["id"] in dismissed
        items.append(it)
    unread = sum(1 for it in items if not it["read"] and not it["dismissed"])
    return {"ok": True, "enabled": True, "items": items, "unread": unread,
            "total": len(items), "msg": ""}


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("状态文件:", STATE_FILE)
    r = active_notices()
    print("启用:", r["enabled"], "共", r["total"], "条，待看", len(r["items"]), "条")
    for it in r["items"]:
        print(f"  [{it['level']:<9}] {it['date']} {it['title']}")
        print(f"      {it['body'][:60]}…")
    print("未读:", count_unread())
