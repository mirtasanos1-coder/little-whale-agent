# -*- coding: utf-8 -*-
"""v1.8.12：真唱模块。

为什么单独一个文件：
    唱歌这条链路跟「聊天回复」完全不是一回事 —— 要搜歌词、要修多音字、
    要按「朗读 / 云端音乐 API / 本地 SVC」三档降级。塞进 agent.py 只会让
    那个文件更难读。这里只暴露三个入口：
        is_sing_request(text) -> (bool, 歌名)
        sing(text, style="")  -> dict(ok, path, engine, note, lines)
        sing_status()         -> dict 给设置页看

设计上的三条硬规矩：
    1. 任何一个引擎挂了都要往后退，最后一定退到「朗读模式」，绝不静默失败；
    2. 每退一步都记在 note 里，交给调用方原话告诉用户（用户最恨"假功能"）；
    3. 发给 TTS 之前必须过一遍多音字修正 —— 不然「行不行」会被念成 hang。
"""
import json
import os
import re
import time
import urllib.request

# ==================== 一、多音字修正 ====================
# 做法：按「词」替换成同音字，让 TTS 念对。为什么不用拼音标注：
# 大多数 TTS（edge / sapi / 云端）不认 "xing2 bu4 xing2" 这种写法，会逐字母念出来，
# 反而更难听。同音字替换是唯一对各类引擎都有效的办法。
# 每条 = (正则, 替换, 说明)。**长的放前面**，避免「行」先被短规则吃掉。
POLY_RULES = [
	(r"行不行", "形不形", "行 xing2"),
	(r"行吗", "形吗", "行 xing2"),
	(r"行啊", "形啊", "行 xing2"),
	(r"不行", "不形", "行 xing2"),
	(r"银行", "银航", "行 hang2"),
	(r"行业", "航业", "行 hang2"),
	(r"行家", "航家", "行 hang2"),
	(r"内行", "内航", "行 hang2"),
	(r"外行", "外航", "行 hang2"),
	(r"同行", "同航", "行 hang2（同行=同业）"),
	(r"成长", "成掌", "长 zhang3"),
	(r"长大", "掌大", "长 zhang3"),
	(r"长辈", "掌辈", "长 zhang3"),
	(r"校长", "校掌", "长 zhang3"),
	(r"家长", "家掌", "长 zhang3"),
	(r"部长", "部掌", "长 zhang3"),
	(r"重复", "虫复", "重 chong2"),
	(r"重新", "虫新", "重 chong2"),
	(r"重庆", "崇庆", "重 chong2"),
	(r"重要", "众要", "重 zhong4"),
	(r"音乐", "音月", "乐 yue4"),
	(r"乐队", "月队", "乐 yue4"),
	(r"乐器", "月器", "乐 yue4"),
	(r"快乐", "快勒", "乐 le4"),
	(r"了解", "瞭结", "了 liao3"),
	(r"受不了", "受不瞭", "了 liao3"),
	(r"还是", "孩是", "还 hai2"),
	(r"归还", "归环", "还 huan2"),
	(r"都有", "兜有", "都 dou1"),
	(r"首都", "首督", "都 du1"),
	(r"出差", "出拆", "差 chai1"),
	(r"差不多", "岔不多", "差 cha4"),
	(r"处理", "楚理", "处 chu3"),
	(r"便宜", "便义", "便 pian2"),
	(r"大夫", "代夫", "大 dai4"),
	(r"弹琴", "谈琴", "弹 tan2"),
	(r"子弹", "子蛋", "弹 dan4"),
	(r"曲子", "取子", "曲 qu3"),
	(r"似的", "四的", "似 shi4"),
	(r"血液", "雪液", "血 xue4"),
	(r"为了", "位了", "为 wei4"),
	(r"干活", "赣活", "干 gan4"),
	(r"少年", "绍年", "少 shao4"),
	(r"说服", "税服", "说 shui4"),
	(r"游说", "游税", "说 shui4"),
	(r"几乎", "机乎", "几 ji1"),
	(r"胳膊", "哥膊", "胳 ge1"),
	(r"规矩", "归矩", "规 gui1（轻声）"),
	(r"答应", "耷应", "答 da1"),
	(r"招呼", "招乎", "呼 hu（轻声）"),
]


def polyphone_fix(text, mode=None):
	"""把容易念错的词换成同音字。mode:
	    homophone（默认）/ pinyin（给认拼音的云端引擎）/ off
	    返回 (修正后的文本, 命中的条目列表)
	"""
	raw = text or ""
	try:
		import config as _cfg
		if mode is None:
			mode = str(_cfg.get("sing_polyphone", "homophone") or "homophone").lower()
	except Exception:
		if mode is None:
			mode = "homophone"
	if mode == "off" or not raw:
		return raw, []
	hits = []
	out = raw
	for pat, rep, why in POLY_RULES:
		if pat not in out:
			continue
		n = out.count(pat)
		hits.append("%s→%s（%s，%d 处）" % (pat, rep if mode != "pinyin" else "(拼音)", why, n))
		if mode == "pinyin":
			# 拼音模式：把整个词换成带声调的拼音串，只对认这个的引擎有用
			py = _PINYIN.get(pat, "")
			out = out.replace(pat, (" " + py + " ") if py else pat)
		else:
			out = out.replace(pat, rep)
	return out, hits


# 只在 pinyin 模式下用得到的少量标注（够常用词即可）
_PINYIN = {
	"行不行": "xing2 bu4 xing2", "不行": "bu4 xing2", "行吗": "xing2 ma5",
	"银行": "yin2 hang2", "行业": "hang2 ye4", "成长": "cheng2 zhang3",
	"长大": "zhang3 da4", "重要": "zhong4 yao4", "重新": "chong2 xin1",
	"音乐": "yin1 yue4", "快乐": "kuai4 le4", "了解": "liao3 jie3",
	"还是": "hai2 shi4", "出差": "chu1 chai1", "处理": "chu3 li3",
	"便宜": "pian2 yi5", "曲子": "qu3 zi5", "似的": "shi4 de5",
}


# ==================== 二、点歌意图 ====================
_SING_VERB = r"(唱|来一?段|给我唱|唱一?首|唱歌|高歌|哼一段|哼两句)"
_SING_RE = re.compile(
	_SING_VERB + r"|点歌|想听你唱|来首歌|唱首歌|表演一?个", re.I)
_SING_NEG = re.compile(r"别唱|不要唱|不许唱|唱的不好听|跑调了吧", re.I)
# 「你会唱歌吗」「能唱吗」是问本事，不是点歌 —— 否则机器人会莫名其妙开唱
_SING_ASK = re.compile(r"(会|能|可以|会不会|能不能|可不可以)唱(歌|曲)?(吗|么|不|嘛)", re.I)
# 歌名：常见写法「唱一首《晴天》」「来段 青花瓷」「给我唱周杰伦的稻香」
_TITLE_PATTERNS = [
	re.compile(r"[《「【]([^》」】\n]{1,24})[》」】]"),
	re.compile(r"唱(?:一?首|一?段|一下|个)?\s*[《「【]?([\u4e00-\u9fa5A-Za-z0-9·\- ]{2,24})[》」】]?"),
	re.compile(r"(?:来|听)(?:一?首|一?段|个)\s*[《「【]?([\u4e00-\u9fa5A-Za-z0-9·\- ]{2,24})[》」】]?"),
	re.compile(r"(?:点|放)(?:一?首|个)?\s*[《「【]?([\u4e00-\u9fa5A-Za-z0-9·\- ]{2,24})[》」】]?"),
]


def is_sing_request(text):
	"""-> (bool, 歌名)。歌名可能是空串（只说"唱首歌"没点名）。"""
	t = (text or "").strip()
	if not t or len(t) > 200:
		return False, ""
	if _SING_NEG.search(t):
		return False, ""
	if not _SING_RE.search(t):
		return False, ""
	name = ""
	for p in _TITLE_PATTERNS:
		m = p.search(t)
		if not m:
			continue
		cand = (m.group(1) or "").strip(" 　的了啊呀吧呢，,。.、")
		# 去掉尾巴上的疑问语气词：「能唱首稻香吗」→「稻香」
		cand = re.sub(r"[吗么嘛呢吧啊呀哦哈]+$", "", cand).strip()
		# 过滤掉「首歌」「一段」这类语气词；「唱歌吗」抓出来的「歌」也要丢掉
		if cand and cand[0] not in "歌曲" and \
				cand not in ("首", "一首", "一段", "几句", "两句", "一个", "一段吧") and len(cand) >= 2:
			name = cand
			break
	# 「你会唱歌吗」这种是问本事：没有明确歌名就当成普通聊天
	if _SING_ASK.search(t) and not name:
		return False, ""
	return True, name


# ==================== 三、歌词解析 ====================
# 抄来的歌词里常带这些东西，全要清掉，否则会被一字一句念出来
_LYRIC_NOISE = re.compile(
	r"(作词|作曲|编曲|制作人|混音|母带|发行|出品|监制|策划|统筹|录音|和声|"
	r"词[:：]|曲[:：]|演唱[:：]|原唱[:：]|翻唱|@|www\.|http|QQ音乐|网易云|酷狗|"
	r"Copyright|©|未经许可|禁止转载)", re.I)
_TIME_TAG = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
_CREDIT_LINE = re.compile(
	r"^(作词|作曲|编曲|制作人?|混音|母带|发行|出品|监制|策划|统筹|录音|和声|"
	r"词|曲|演唱|原唱|翻唱)\s*[:：]", re.I)
_BRACKET = re.compile(r"[（(\[【][^）)\]】]{0,20}[）)\]】]")
# v1.8.14：抓不到歌词区块时兜底整页去标签，会把「客户端 / 下载 / 登录」这类导航条
# 当成歌词念出来。这里再加一道行级黑名单：命中任何导航词，整行丢掉。
_NAV_LINE = re.compile(
	r"客户端|下载|开放平台|高品质|独家音效|视觉|皮肤|我的音乐|登录|注册|音月馆|首页|"
	r"扫码|二维码|关注公众号|意见反馈|隐私政策|用户协议|版权声明|京ICP|网络文化经营|"
	r"收藏本站|设为首页|更多精彩|相关推荐|热门歌曲|歌手大全", re.I)


# v1.8.15：标题与站点名一律不认 —— 搜索结果标题形如
# 「稻香 - 完整版歌词 - 百度文库」，被当成歌词念出来就是灾难。
_TITLE_LINE = re.compile(
    r"完整版|歌词|LRC|lrc|百度文库|百度知道|百度百科|文库|知道|"
    r"在线试听|免费下载|伴奏|铃声|简谱|吉他谱|钢琴谱|MP3|mp3|"
    r"无损|高清|专辑|作词|作曲|原唱|翻唱|试听|下载|版权所有|"
    r"百度|QQ音乐|网易云|酷狗|酷我|虾米|咪咕|搜狐|新浪|知乎|豆瓣|哔哩|"
    r"\d+\s*万?\s*次播放|收藏|分享|举报",
    re.I,
)


# v1.9.19：搜「告白气球 歌词」时，搜索结果里混进了书城/百科的**小说简介**，
# 一大段散文被当成歌词念了 55 秒（用户实机听到的就是阿Q书城那本小说的简介）。
# 真假歌词一眼能分：
#   真歌词 —— 短句、重复、没有叙事人称，十几行字；
#   伪歌词 —— 「内容简介 / 百科 / 书城 / 小说 / 作者 / 讲述了……」一整段落。
# 所以加两道闸：词级黑名单（整行丢掉）+ 整段散文打分（过线就判定「没搜到」）。
_PROSE_WORDS = re.compile(
	r"简介|内容简介|作品简介|百科|维基|书城|小说|网文|言情|文学|作者|出版社|"
	r"出版时间|定价|ISBN|字数|章节|连载|起点中文|晋江|番茄小说|掌阅|"
	r"梗概|故事背景|背景介绍|情节|导读|书评|点评|豆瓣评分|"
	r"男主角|女主角|主角|讲述了|讲述的是|这本书|该书|本书|"
	r"从此|后来他们|两人|一对|朋友|暗恋|告白成功|加入书架|开始阅读",
	re.I)
_MAX_LINE_CN = 26          # 单行超过这个长度，多半是散文不是歌词


def _prose_score(text):
	"""越大越像简介/散文。真歌词一般 0~2 分，小说简介轻松 4 分以上。"""
	t = text or ""
	if not t.strip():
		return 0
	score = len(_PROSE_WORDS.findall(t))
	for ln in t.split("\n"):
		s = ln.strip()
		if len(s) >= 60:
			score += 2
		elif len(s) >= _MAX_LINE_CN + 10:
			score += 1
	return score


def _looks_like_prose(text):
	return _prose_score(text) >= 3


def parse_lyrics(raw, max_lines=14):
	"""把搜来的一坨文本变成干净的一行行歌词。"""
	txt = (raw or "").replace("\r", "\n")
	# v1.9.19：整段就是简介/百科的，直接当没搜到 —— 一行都不许进 TTS
	if _looks_like_prose(txt):
		return []
	txt = _TIME_TAG.sub("\n", txt)
	lines = []
	for ln in txt.split("\n"):
		s = ln.strip()
		if not s:
			continue
		# 整行都是「作词：方文山」这类署名 → 整行丢掉（只删关键词会剩下人名）
		if _CREDIT_LINE.search(s):
			continue
		# v1.8.14：导航/页脚词整行丢掉 —— 宁可少一行，也不能把「客户端下载」念出来
		if _NAV_LINE.search(s):
			continue
		# v1.8.15：标题/站点名也整行丢掉 —— 「稻香 - 完整版歌词 - 百度文库」不是歌词
		if _TITLE_LINE.search(s):
			continue
		# v1.9.19：简介/百科/书城整行丢掉 —— 小说简介就是被这么念出去的
		if _PROSE_WORDS.search(s):
			continue
		if len(s) >= 60:                     # 单行 60 字以上基本是散文，不念
			continue
		s = _BRACKET.sub("", s)
		s = _LYRIC_NOISE.sub("", s)
		s = re.sub(r"^[\d\s.、\-—:：]+", "", s).strip()
		s = s.strip(" 　,，.。;；:：!！?？\"'“”‘’")
		if len(s) < 2:
			continue
		if not re.search(r"[\u4e00-\u9fa5]", s):
			continue
		if len(s) > 40:                      # 一整段说明文字的，砍成短句
			for part in re.split(r"[，,。.！!？?；;]", s):
				p = part.strip()
				if 2 <= len(p) <= 40:
					lines.append(p)
			continue
		lines.append(s)
		if len(lines) >= max_lines:
			break
	# 去掉重复行（搜索摘要常重复）
	seen, out = set(), []
	for s in lines:
		if s in seen:
			continue
		seen.add(s)
		out.append(s)
	return out


def _lyric_blocks_from_html(html):
    """从歌词页 HTML 里抠出歌词区块（class/id 含 lyric / songtext / content 等）。

    v1.8.13：这里必须用 HTMLParser 按标签深度收，不能用正则非贪婪 ——
    非贪婪会只吃到第一个 <p>，结果整页退回纯文本、把 <title> 也当成歌词。
    """
    import re as _re
    from html.parser import HTMLParser

    kw = _re.compile(r"lyric|Lyric|gciba|songtext|content|verse", _re.I)
    _NL_TAGS = ("p", "div", "li", "tr", "h1", "h2", "h3", "h4")

    class _P(HTMLParser):
        def __init__(self):
            try:
                HTMLParser.__init__(self, convert_charrefs=True)
            except TypeError:
                HTMLParser.__init__(self)
            self.stack = []
            self.buf = []
            self.out = []

        def _flush(self):
            t = "".join(self.buf)
            t = t.replace("\u00a0", " ")
            if t.strip():
                self.out.append(t.strip())
            self.buf = []

        def handle_starttag(self, tag, attrs):
            tag = (tag or "").lower()
            if not self.stack:
                d = {}
                for k, v in (attrs or []):
                    d[(k or "").lower()] = v or ""
                cls = (d.get("class") or "") + " " + (d.get("id") or "")
                if kw.search(cls):
                    self.stack.append(tag)
                    self.buf = []
                    return
                return
            if tag == self.stack[-1]:
                self.stack.append(tag)
                return
            if tag == "br" or tag in _NL_TAGS:
                self.buf.append("\n")

        def handle_startendtag(self, tag, attrs):
            if self.stack and (tag or "").lower() == "br":
                self.buf.append("\n")

        def handle_endtag(self, tag):
            tag = (tag or "").lower()
            if not self.stack:
                return
            if tag == self.stack[-1]:
                self.stack.pop()
                if not self.stack:
                    self._flush()
                else:
                    self.buf.append("\n")
            elif tag == "br" or tag in _NL_TAGS:
                self.buf.append("\n")

        def handle_data(self, data):
            if self.stack and data:
                self.buf.append(data)

    p = _P()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    return p.out


def _fetch_lyric_page(url, timeout=8):
    """抓一个歌词页，返回纯文本（尽量是歌词区块）。失败返回空串。"""
    import re as _re
    u = (url or "").strip()
    if not u.lower().startswith("http"):
        return ""
    try:
        import urllib.request as _ur
        req = _ur.Request(u, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with _ur.urlopen(req, timeout=timeout) as r:
            raw = r.read(600 * 1024)
    except Exception:
        return ""
    txt = ""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            txt = raw.decode(enc)
            break
        except Exception:
            continue
    if not txt:
        return ""
    blocks = _lyric_blocks_from_html(txt)
    if blocks:
        return "\n".join(blocks)
    # v1.8.14：抠不到歌词区块就**返回空**。以前这里整页去标签兜底，
    # 结果把「客户端 / 下载 / 登录 / 首页」这些导航条当歌词喂给 TTS。
    # 宁可少一行、让用户看到「没搜到歌词」，也不能念导航。
    return ""


def _heuristic_split(blob, want=8):
    """搜索摘要常常是「一大段」——按标点切成短句，至少凑出 want 行。"""
    import re as _re
    s = _re.sub(r"\s+", " ", (blob or "")).strip()
    if not s:
        return []
    parts = [p.strip() for p in _re.split(r"[，,。.！!？?；;、/|·…\n]+", s)]
    out = []
    for p in parts:
        p = p.strip(" 　\"'“”‘’()（）[]【】-—:：")
        if len(p) < 2 or len(p) > 30:
            continue
        if not _re.search(r"[\u4e00-\u9fa5]", p):
            continue
        # v1.8.14：切出来的短句也要过黑名单 —— 否则「客户端 下载 登录」
        # 这种搜索摘要片段会被当成歌词。标题类（带「歌词/LRC」的）也一起丢。
        if _NAV_LINE.search(p):
            continue
        if _PROSE_WORDS.search(p):          # v1.9.19：简介/百科片段不切
            continue
        if _re.search(r"歌词|LRC|lrc|铃声|简谱|吉他谱|MP3|mp3", p):
            continue
        out.append(p)
        if len(out) >= want * 2:
            break
    return out


def lyrics_from_search(name, max_lines=12):
    """搜歌词。返回 (lines, note)。

    v1.8.13：以前只拼 title+snippet，摘要往往只有一句，于是「只抽到 1 行」。
    现在三路并进：① 搜索词直接要「完整版歌词」② 拿结果里的 url 抓歌词页、
    优先抠 .lyric-content / #lyric 这类区块 ③ 实在只有一大段，就按标点切成短句。
    """
    name = (name or "").strip()
    if not name:
        return [], "没给歌名"
    try:
        import computer_use as cu
    except Exception as e:
        return [], "搜索模块不可用：%s" % e

    queries = [name + " 歌词 完整版", name + " 歌词 全文", name + " 歌词"]
    r = None
    for q in queries:
        for call in (lambda q=q: cu.tool_web_search(q, session="sing"),
                     lambda q=q: cu.tool_web_search(q, 10),
                     lambda q=q: cu.tool_web_search(q)):
            try:
                r = call()
            except TypeError:
                continue
            except Exception:
                r = None
                continue
            # v1.8.15：搜不到就立刻当失败 —— 绝不把 {"ok": false, "msg": ...}
            # 这种 JSON 当歌词传给 TTS（用户真的听到过她念报错）
            if isinstance(r, dict):
                _items0 = r.get("items") or r.get("results") or r.get("list") or []
                if r.get("ok") is False or not _items0:
                    r = None
                    continue
            if r:
                break
        if r:
            break
    if not r:
        return [], "搜索失败"

    items = []
    blob = ""
    if isinstance(r, dict):
        items = r.get("items") or r.get("results") or r.get("list") or []
        # v1.8.15：**彻底不看 title**。标题是「稻香 - 完整版歌词 - 百度文库」
        # 这种东西，混进来就会被当成歌词唱出来。只用摘要/正文。
        snips = []
        for x in items:
            if isinstance(x, dict):
                _sn = str(x.get("snippet", "") or x.get("desc", "") or x.get("content", ""))
                if _sn.strip():
                    snips.append(_sn)
            else:
                if str(x).strip():
                    snips.append(str(x))
        blob = "\n".join(snips)
        snip_blob = blob
    elif isinstance(r, str):
        blob = r
        snip_blob = r

    # v1.9.19：摘要整段就是小说简介/百科的，直接作废 —— 不能让它当歌词
    snip_prose = _looks_like_prose(blob)

    # ① 先看摘要里能不能抽出足够行
    lines = [] if snip_prose else parse_lyrics(blob, max_lines=max_lines)

    # ② 摘要不够 5 行就去抓页面
    urls = []
    for x in items:
        if isinstance(x, dict):
            for k in ("url", "link", "href"):
                v = x.get(k)
                if isinstance(v, str) and v.lower().startswith("http"):
                    urls.append(v)
                    break
    if len(lines) < 5 and urls:
        seen_u = set()
        for u in urls[:4]:
            if u in seen_u:
                continue
            seen_u.add(u)
            page = _fetch_lyric_page(u)
            if not page:
                continue
            got = parse_lyrics(page, max_lines=max_lines)
            if len(got) > len(lines):
                lines = got
            if len(lines) >= 5:
                break

    # ③ 还不够就把整段按标点切碎（但散文/简介绝不切碎凑行）
    if len(lines) < 5 and not snip_prose:
        extra = _heuristic_split(snip_blob or blob, want=max(5, max_lines))
        seen = set(lines)
        for s in extra:
            if s in seen:
                continue
            seen.add(s)
            lines.append(s)
            if len(lines) >= max_lines:
                break

    # v1.8.15：最后一道闸 —— 不足 5 行一律当没搜到。
    # 宁可唱不出来，也不能拿半句标题凑数（QA 听到过她念「稻香 - 完整版歌词 - 百度文库」）。
    if len(lines) < 5:
        if snip_prose:
            return [], "搜到的是简介/百科，不是歌词"
        return [], "只搜到 %d 行，不够唱" % len(lines)
    # v1.9.19：拼起来还是一片散文的，照样不唱
    if _looks_like_prose("\n".join(lines)):
        return [], "搜到的是简介/百科，不是歌词"
    return lines, "从网上搜到 %d 行" % len(lines)


# ==================== 四、三种引擎 ====================
def _out_dir():
	try:
		import config as _cfg
		d = os.path.join(_cfg.DATA_DIR, "tts")
	except Exception:
		d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts")
	try:
		os.makedirs(d, exist_ok=True)
	except Exception:
		pass
	return d


def _voice_cfg():
	default = {"engine": "read", "voice": "", "cloud_url": "", "cloud_key": "",
			   "cloud_format": "suno", "style": "", "svc_url": "", "svc_key": "",
			   "rate": "-14%", "pitch": "+2Hz", "max_lines": 12}
	try:
		import config as _cfg
		g = _cfg.get if hasattr(_cfg, "get") else (lambda k, d=None: d)
		default["engine"] = str(g("sing_engine", "read") or "read")
		default["voice"] = str(g("tts_voice", "") or "")
		default["cloud_url"] = str(g("sing_cloud_url", "") or "")
		default["cloud_key"] = str(g("sing_cloud_key", "") or "")
		default["cloud_format"] = str(g("sing_cloud_format", "suno") or "suno")
		default["style"] = str(g("sing_style", "") or "")
		default["svc_url"] = str(g("sing_svc_url", "") or "")
		default["svc_key"] = str(g("sing_svc_key", "") or "")
		default["rate"] = str(g("sing_rate", "-14%") or "-14%")
		default["pitch"] = str(g("sing_pitch", "+2Hz") or "+2Hz")
		try:
			default["max_lines"] = int(g("sing_max_lines", 12) or 12)
		except Exception:
			default["max_lines"] = 12
	except Exception:
		pass
	return default


# --- 引擎一：真唱模式（v1.9.25：把「念」改成「唱」）---
# 用户实机反馈：「唱歌念出来有些生硬了，改成唱」。
# 为什么不能只调 edge-tts 的参数：它的 SSML 只认**单层** prosody（逐字/嵌套 prosody
# 一律 NoAudioReceived），`pitch` 只能整体升降调，做不出旋律；`boundary="WordBoundary"`
# 能给出每个字的起止时间，但也只是「念得更准」，不是唱。
# 所以走「拆字 → 逐字变调上旋律 → 加颤音 → 叠伴奏」这条路：
#   1) edge-tts 逐行合成 mp3，同时收 WordBoundary 拿到每个字的 offset/duration；
#   2) miniaudio 解码成 float32 单声道 24kHz（只解不编，包体只有 ~1.6MB）；
#   3) 每个字按五声音阶的旋律音高重采样变调（同时改时长，字可长可短）；
#   4) 加 5.4Hz 轻颤音 + 14ms 淡入淡出，接缝不咔哒；
#   5) 纯 numpy 正弦铺一层和弦 pad + 低音脉冲当伴奏；
#   6) 结果用标准库 wave 写 WAV —— 不引入 ffmpeg（`av` 会多带 128MB，`imageio_ffmpeg` 84MB）。
# NapCat 的 record 段本来就吃本地路径，silk 转换它自己用自带的 ffmpeg addon 做，
# 所以我们这边产出 WAV 就行。
_SR = 24000                      # 采样率
_PENTA = (0, 2, 4, 7, 9, 12)     # 五声音阶（半音），唱中文歌不容易「跑调难听」
_MELODY = (0, 2, 4, 3, 2, 1, 0, 2, 4, 5, 4, 2)   # 旋律级数
_CHORDS = ((1.00, 1.26, 1.50), (0.84, 1.00, 1.26),
           (1.26, 1.59, 2.00), (1.12, 1.41, 1.68))
_ROOT_HZ = 196.0                 # 伴奏根音
_VIB_RATE = 5.4                  # 颤音频率（Hz）
_VIB_DEPTH = 0.009               # 颤音深度（秒）
_SING_MAX = 12                   # 最多唱几句，再多就太长了
_PITCH_CAP = 8.0                 # 单字最多挪几个半音：再大重采样就听得出变形了


def _np():
	try:
		import numpy
		return numpy
	except Exception:
		return None


def _has_sing_deps():
	"""numpy + miniaudio 都在才谈得上「唱」，缺一个就退回朗读。"""
	if _np() is None:
		return False
	try:
		import miniaudio  # noqa: F401
		return True
	except Exception:
		return False


def _tts_line(text, voice, rate):
	"""合成一行，返回 (mp3 字节, [(词, 起秒, 止秒), …])。

	boundary="WordBoundary" 是关键：Edge 会按词回吐 offset/duration
	（单位 1e-7 秒），有了它才能把每个字切出来单独变调。
	"""
	import asyncio
	import edge_tts

	mp3 = bytearray()
	marks = []

	async def _run():
		com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
		async for chunk in com.stream():
			kind = chunk.get("type")
			if kind == "audio":
				mp3.extend(chunk.get("data") or b"")
			elif kind == "WordBoundary":
				try:
					off = float(chunk.get("offset") or 0) / 1e7
					dur = float(chunk.get("duration") or 0) / 1e7
					marks.append((str(chunk.get("text") or ""), off, off + dur))
				except Exception:
					continue

	loop = asyncio.new_event_loop()
	try:
		loop.run_until_complete(_run())
	finally:
		try:
			loop.close()
		except Exception:
			pass
	return bytes(mp3), marks


def _char_spans(marks, nchars=0):
	"""把「按词」的时间标记拆成「按字」—— 词内均分。"""
	out = []
	for txt, a, b in marks or []:
		t = (txt or "").strip()
		if not t or b <= a:
			continue
		n = len(t)
		step = (b - a) / float(n)
		for k, ch in enumerate(t):
			if not ch.strip():
				continue
			if nchars and len(out) >= nchars:
				return out
			out.append((ch, a + step * k, a + step * (k + 1)))
	return out


def _trim_silence(x, np, thr_db=-40.0, pad_sec=0.010):
	"""掐掉字音前后的静音 —— 逐字合成出来的静音能占七八成，不掐就成念经。"""
	if x.size == 0:
		return x
	peak = float(np.max(np.abs(x)))
	if peak <= 1e-6:
		return x
	thr = peak * (10.0 ** (thr_db / 20.0))
	idx = np.nonzero(np.abs(x) > thr)[0]
	if idx.size == 0:
		return x
	pad = int(pad_sec * _SR)
	i0 = max(0, int(idx[0]) - pad)
	i1 = min(x.size, int(idx[-1]) + 1 + pad)
	return x[i0:i1]


def _pitch_shift(x, np, semis):
	"""变调（顺带改时长）：重采样 —— 音高了，也短了。

	v1.9.25：真唱时紧接着会用 `_time_stretch` 把时长拉回原样，
	这样音准走旋律、咬字快慢还是原来说话的节奏。
	"""
	if not semis:
		return x
	if x.size < 8:
		return x
	n = int(x.size / (2.0 ** (float(semis) / 12.0)))
	if n < 8:
		n = 8
	return np.interp(np.linspace(0.0, x.size - 1.0, n),
					 np.arange(x.size, dtype=np.float64),
					 x.astype(np.float64)).astype(np.float32)


def _f0_hz(x, np, lo=90.0, hi=400.0):
	"""估一段人声的基频（自相关 + 抛物线细化）。测不准就返回 None。"""
	n = int(x.size)
	if n < int(_SR * 0.03):
		return None
	s = x - float(np.mean(x))
	if float(np.sqrt(np.mean(s * s))) < 1e-3:
		return None
	ac = np.correlate(s, s, mode="full")[n - 1:]
	if float(ac[0]) <= 0.0:
		return None
	ac = ac / float(ac[0])
	k0 = max(1, int(_SR / float(hi)))
	k1 = min(int(_SR / float(lo)), n - 1)
	if k1 <= k0 + 1:
		return None
	k = k0 + int(np.argmax(ac[k0:k1]))
	if k <= 0:
		return None
	if 0 < k < ac.size - 1:
		a, b, c = float(ac[k - 1]), float(ac[k]), float(ac[k + 1])
		den = a - 2.0 * b + c
		if abs(den) > 1e-9:
			k = k + 0.5 * (a - c) / den
	if k <= 0:
		return None
	hz = _SR / float(k)
	if hz < lo or hz > hi:
		return None
	return hz


def _time_stretch(x, np, target_len):
	"""把一段音频「只改时长、不改音高」拉到 target_len（WSOLA，纯 numpy）。

	v1.9.25：这是「唱」和「念」之间的关键一步。变调是重采样，音高上去的同时
	时长也被压缩了；不拉回来，每个字就会被压成 0.6 倍，咬字糊成一片。

	为什么不是普通的叠加相加（OLA）：直接按不同步长重叠相加，相邻颗粒的相位对不上，
	纯音会被梳状滤波带跑调 —— 实测 220Hz 的正弦拉长后谱峰跑到 204.7Hz，
	而唱的正是这种带明确音高的元音，等于把刚调准的音准又搅糊了。
	WSOLA 在拼接前先在 ±10ms 内找「波形最接得上」的位置再叠，音高就守住了。
	"""
	n_in = int(x.size)
	target_len = int(target_len)
	if n_in < 64 or target_len < 64 or abs(target_len - n_in) <= 2:
		return x
	win = int(_SR * 0.030)
	win = max(64, min(win, n_in // 2))
	hop_out = max(1, win // 2)
	ov = win - hop_out
	tol = max(2, int(_SR * 0.010))
	ratio = n_in / float(target_len)
	hop_in = max(1, int(round(hop_out * ratio)))
	pad = win + tol
	src = np.concatenate([
		np.zeros(pad, dtype=np.float32), x.astype(np.float32),
		np.zeros(pad + win + tol, dtype=np.float32)])
	w = np.hanning(win).astype(np.float32)
	out = np.zeros(target_len + win, dtype=np.float32)
	wsum = np.zeros(target_len + win, dtype=np.float32)
	pos_out = 0
	prev = None
	k = 0
	while pos_out < target_len:
		nominal = pad + int(round(k * hop_in))
		pos = nominal
		if prev is not None:
			# 拿「上一颗粒自然延续下去的那一段」当模板，在附近找出波形最像的位置
			tpl = src[prev + hop_out: prev + hop_out + ov]
			best, best_d = -1e18, 0
			for d in range(-tol, tol + 1):
				p = nominal + d
				if p < 0 or p + ov >= src.size:
					continue
				c = float(np.dot(src[p:p + ov], tpl))
				if c > best:
					best, best_d = c, d
			pos = nominal + best_d
		seg = src[pos:pos + win]
		if seg.size < win:
			break
		out[pos_out:pos_out + win] += seg * w
		wsum[pos_out:pos_out + win] += w
		prev = pos
		pos_out += hop_out
		k += 1
	wsum[wsum < 1e-6] = 1.0
	return (out[:target_len] / wsum[:target_len]).astype(np.float32)


def _vibrato(x, np):
	"""轻颤音 —— 一个音拖长时，这点抖动就是「唱」和「念」最大的差别。"""
	n = int(x.size)
	if n < int(_SR * 0.06):
		return x
	idx = np.arange(n, dtype=np.float64)
	sh = (_VIB_DEPTH * _SR) * np.sin(2.0 * np.pi * _VIB_RATE * idx / float(_SR))
	pos = np.clip(idx + sh, 0.0, n - 1.0).astype(np.int64)
	return x[pos]


def _fade(x, np, ms=14.0):
	n = int(x.size)
	k = min(int(_SR * ms / 1000.0), n // 2)
	if k <= 1:
		return x
	r = np.linspace(0.0, 1.0, k, dtype=np.float32)
	y = x.copy()
	y[:k] *= r
	y[-k:] *= r[::-1]
	return y


def _acc_track(n, np):
	"""伴奏：纯 numpy 正弦铺的和弦 pad + 每 0.6 秒一个低音脉冲。

	不引外部音源 —— 一是不想让包再大几十兆，二是这段 pad 只需要「有个调」，
	别抢人声就行。
	"""
	if n <= 0:
		return np.zeros(0, dtype=np.float32)
	t = np.arange(n, dtype=np.float64) / float(_SR)
	out = np.zeros(n, dtype=np.float64)
	seg = 2.4
	for i in range(int(n / float(_SR) / seg) + 2):
		t0 = i * seg
		up = np.clip((t - t0) / 0.45, 0.0, 1.0)
		dn = np.clip((t0 + seg - t) / 0.55, 0.0, 1.0)
		env = np.clip(up * dn, 0.0, 1.0)
		for mul in _CHORDS[i % len(_CHORDS)]:
			out += 0.05 * np.sin(2.0 * np.pi * _ROOT_HZ * mul * t) * env
	beat = 0.6
	for i in range(int(n / float(_SR) / beat) + 2):
		t0 = i * beat
		d = t - t0
		ok = d >= 0.0
		if not ok.any():
			continue
		out[ok] += 0.045 * np.sin(2.0 * np.pi * (_ROOT_HZ / 2.0) * d[ok]) * np.exp(-7.0 * d[ok])
	return out.astype(np.float32)


def synth_sing(lines, cfg=None):
	"""真唱：拆字 + 变调上旋律 + 颤音 + 伴奏 + 写 WAV。"""
	np = _np()
	if np is None:
		raise RuntimeError("没装 numpy")
	import miniaudio
	import wave

	cfg = cfg or _voice_cfg()
	voice = cfg.get("voice") or "zh-CN-XiaoxiaoNeural"
	lines = [x.strip() for x in (lines or []) if x and x.strip()]
	if not lines:
		raise RuntimeError("没有歌词可唱")
	try:
		cap = int(cfg.get("max_lines") or _SING_MAX)
	except Exception:
		cap = _SING_MAX
	lines = lines[:max(1, min(cap, _SING_MAX))]
	rate = cfg.get("rate") or "-14%"
	try:
		pct = int(str(rate).strip().rstrip("%") or "-14")
	except Exception:
		pct = -14
	# 唱比念慢一点更像唱：在用户设置的语速上再放慢一档，但别慢到拖
	rate = "%d%%" % max(-35, min(0, pct - 10))

	t0 = time.time()
	chunks = []
	deg = 0
	for ln in lines:
		txt = ln + ("。" if not ln.endswith(("。", "，", "！", "？", "…", "、")) else "")
		mp3, marks = _tts_line(txt, voice, rate)
		if not mp3:
			continue
		try:
			dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16,
								   nchannels=1, sample_rate=_SR)
			sig = np.asarray(dec.samples, dtype=np.float32) / 32768.0
		except Exception:
			continue
		if sig.size < int(_SR * 0.05):
			continue
		spans = _char_spans(marks)
		if not spans:
			# 拿不到字级时间就整行当一句唱（保底，不至于又变念）
			spans = [(txt[:1], 0.0, sig.size / float(_SR))]
		piece = []
		for _ch, a, b in spans:
			i0 = max(0, int(a * _SR))
			i1 = min(sig.size, int(b * _SR))
			if i1 - i0 < int(_SR * 0.02):
				continue
			seg = sig[i0:i1]
			seg = _trim_silence(seg, np)
			if seg.size < int(_SR * 0.02):
				continue
			semis = _PENTA[deg % len(_PENTA)] - 2
			target = _ROOT_HZ * 2.0 ** (float(semis) / 12.0)
			deg += 1
			# v1.9.25：把每个字**挪到目标音**上，而不是「在原音高上平移几个半音」。
			# 后者听着还是说话 —— 因为原句自己的语调（150~320Hz 的起伏）会整个叠上来，
			# 实测离目标音平均差 4.5 个半音、音域散到 18.7 个半音；归一化后只差 0.67。
			f0 = _f0_hz(seg, np)
			if f0:
				need = 12.0 * float(np.log2(target / f0))
				need = float(np.clip(need, -_PITCH_CAP, _PITCH_CAP))
			else:
				need = float(semis)          # 测不准就退回相对变调，总比不唱强
			keep = int(seg.size)
			seg = _pitch_shift(seg, np, need)
			seg = _time_stretch(seg, np, keep)
			seg = _vibrato(seg, np)
			seg = _fade(seg, np)
			piece.append(seg)
			# 字与字之间留一点点气口，不咬在一起
			piece.append(np.zeros(int(_SR * 0.030), dtype=np.float32))
		if piece:
			row = np.concatenate(piece)
			chunks.append(row)
			chunks.append(np.zeros(int(_SR * 0.26), dtype=np.float32))   # 行间换气
	if not chunks:
		raise RuntimeError("真唱一句都没合成出来")
	vox = np.concatenate(chunks).astype(np.float32)
	if vox.size < int(_SR * 0.3):
		raise RuntimeError("真唱音频太短了")
	acc = _acc_track(vox.size, np)
	mix = vox * 0.92 + acc
	peak = float(np.max(np.abs(mix)))
	if peak > 1e-6:
		mix = mix * (0.90 / peak)
	pcm = np.clip(mix * 32767.0, -32768.0, 32767.0).astype("<i2")
	final = os.path.join(_out_dir(), "sing_%d.wav" % int(time.time() * 1000))
	with wave.open(final, "wb") as w:
		w.setnchannels(1)
		w.setsampwidth(2)
		w.setframerate(_SR)
		w.writeframes(pcm.tobytes())
	try:
		print("[唱歌] 真唱 %d 句 / %.1f 秒音频 / 处理 %.1fs -> %s"
			  % (len(lines), vox.size / float(_SR), time.time() - t0, final))
	except Exception:
		pass
	return final, voice


# --- 引擎一（保底）：朗读模式（把歌词念得别太机械）---
def _synth_read_plain(lines, cfg=None):
	"""逐行合成 mp3 再拼起来：每行之间自然停顿，语气略微起伏。

	为什么逐行：edge-tts 不吃 SSML 的 <break/>（会被转义成文字），
	一次念整段又快又平，像念课文。逐行合成能给每行不同的语速/音高，
	拼起来的停顿就是「呼吸」。

	v1.9.25：这条降级成**保底路径** —— 真唱链任何一步挂了（没装 numpy/miniaudio、
	合成拉不到字级时间、解码失败…）才走它，保证「至少还能唱出来」。
	"""
	cfg = cfg or _voice_cfg()
	try:
		import asyncio
		import edge_tts
	except Exception as e:
		raise RuntimeError("没装 edge-tts（%s）" % e)
	voice = cfg.get("voice") or "zh-CN-XiaoxiaoNeural"
	lines = [x for x in (lines or []) if x]
	if not lines:
		raise RuntimeError("没有歌词可念")
	base_rate = cfg.get("rate") or "-14%"
	# 每行给一点点音高/语速差异，避免整段一个调
	variants = [("+2Hz", "-16%"), ("+1Hz", "-12%"), ("+3Hz", "-18%"),
				("+0Hz", "-13%"), ("+2Hz", "-15%")]
	out = bytearray()
	tmp = []
	loop = asyncio.new_event_loop()
	try:
		for i, ln in enumerate(lines):
			pitch, rate = variants[i % len(variants)]
			if pitch == "+2Hz":
				pitch = cfg.get("pitch") or "+2Hz"
			if rate == "-16%":
				rate = base_rate
			txt = ln + ("。" if not ln.endswith(("。", "，", "！", "？", "…")) else "")
			path = os.path.join(_out_dir(), "sing_line_%d_%d.mp3" % (int(time.time() * 1000), i))
			try:
				com = edge_tts.Communicate(txt, voice, rate=rate, pitch=pitch)
				loop.run_until_complete(com.save(path))
				with open(path, "rb") as f:
					out += f.read()
				tmp.append(path)
			except Exception:
				continue
	finally:
		try:
			loop.close()
		except Exception:
			pass
	if not out:
		raise RuntimeError("朗读模式一行都没合成出来")
	final = os.path.join(_out_dir(), "sing_%d.mp3" % int(time.time() * 1000))
	with open(final, "wb") as f:
		f.write(bytes(out))
	for p in tmp:
		try:
			os.remove(p)
		except Exception:
			pass
	return final, voice


# --- 引擎一 / 总入口：优先真唱，挂了才退回朗读 ---
def synth_read(lines, cfg=None):
	"""v1.9.25：默认走**真唱**；真唱链任何一步挂了才退回逐行朗读。

	为什么留这层：用户明确说「改成唱」，但唱歌依赖 numpy + miniaudio 与
	Edge 的字级时间回调 —— 任何一环在用户机器上出问题，也绝不能变成「没声音」。
	所以先试真唱，记下原因，再退回朗读；两层都失败才抛错让上层降级。
	"""
	cfg = cfg or _voice_cfg()
	if _has_sing_deps():
		try:
			return synth_sing(lines, cfg)
		except Exception as e:
			try:
				print("[唱歌] 真唱失败（%s: %s），退回朗读" % (type(e).__name__, e))
			except Exception:
				pass
	else:
		try:
			print("[唱歌] 没装 numpy/miniaudio，退回朗读")
		except Exception:
			pass
	return _synth_read_plain(lines, cfg)


# --- 引擎二 / 三：云端音乐 API、本地 SVC ---
def _post_audio(url, payload, key="", timeout=90):
	body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
	req = urllib.request.Request(url, data=body, method="POST")
	req.add_header("Content-Type", "application/json; charset=utf-8")
	if key:
		req.add_header("Authorization", "Bearer " + key.strip())
	with urllib.request.urlopen(req, timeout=timeout) as r:
		raw = r.read()
		ctype = (r.headers.get("Content-Type") or "").lower()
	if not raw:
		raise RuntimeError("接口返回空")
	head = raw[:4]
	if head[:2] == b"PK" or head[:4] == b"RIFF" or head[:3] == b"ID3" or (len(raw) > 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0):
		return raw, ctype
	# 返回 JSON：{audio|data|audio_base64|url}
	try:
		obj = json.loads(raw.decode("utf-8", "replace"))
	except Exception:
		raise RuntimeError("不认识这个接口的返回")
	if isinstance(obj, dict):
		for k in ("audio", "data", "audio_base64", "wav", "mp3", "result"):
			v = obj.get(k)
			if isinstance(v, str) and v:
				try:
					import base64
					if k in ("wav", "mp3") and not re.match(r"^[A-Za-z0-9+/=\s]+$", v[:40]):
						continue
					return base64.b64decode(v), ctype
				except Exception:
					continue
		u = obj.get("url") or obj.get("audio_url")
		if isinstance(u, str) and u.startswith("http"):
			with urllib.request.urlopen(u, timeout=timeout) as r2:
				return r2.read(), (r2.headers.get("Content-Type") or "")
		msg = obj.get("msg") or obj.get("message") or obj.get("error")
		if msg:
			raise RuntimeError(str(msg)[:120])
	raise RuntimeError("接口没返回音频")


def synth_cloud(lines, cfg=None):
	cfg = cfg or _voice_cfg()
	url = (cfg.get("cloud_url") or "").strip()
	if not url:
		raise RuntimeError("还没填云端音乐 API 地址")
	lyrics = "\n".join(lines or [])
	if not lyrics:
		raise RuntimeError("没有歌词")
	style = (cfg.get("style") or "").strip() or "流行，女声，干净的木吉他"
	payload = {"lyrics": lyrics, "style": style,
			   "title": (lines[0][:20] if lines else "小鲸鱼唱的"),
			   "format": cfg.get("cloud_format") or "suno",
			   "make_instrumental": False}
	raw, ctype = _post_audio(url, payload, cfg.get("cloud_key") or "")
	ext = ".wav" if raw[:4] == b"RIFF" else ".mp3"
	final = os.path.join(_out_dir(), "sing_cloud_%d%s" % (int(time.time() * 1000), ext))
	with open(final, "wb") as f:
		f.write(raw)
	return final, style


def synth_svc(lines, cfg=None):
	"""本地 SVC：把 TTS 念出来的干声喂给本地歌曲转换服务。

	没有服务就直接报错，让上层降级 —— 不要假装能唱。
	"""
	cfg = cfg or _voice_cfg()
	url = (cfg.get("svc_url") or "").strip()
	if not url:
		raise RuntimeError("还没填本地 SVC 服务地址")
	# v1.9.25：SVC 要的是**干声**，给真唱出来的带伴奏音频反而更难转，
	# 所以这里明确用逐行朗读那版，不要走 synth_read 的真唱分支。
	pcm_path, _v = _synth_read_plain(lines, cfg)     # 先拿干声
	try:
		with open(pcm_path, "rb") as f:
			import base64
			audio_b64 = base64.b64encode(f.read()).decode("ascii")
	finally:
		pass
	payload = {"vocal_base64": audio_b64, "format": "mp3",
			   "style": (cfg.get("style") or "").strip() or "流行"}
	raw, ctype = _post_audio(url, payload, cfg.get("svc_key") or "", timeout=180)
	ext = ".wav" if raw[:4] == b"RIFF" else ".mp3"
	final = os.path.join(_out_dir(), "sing_svc_%d%s" % (int(time.time() * 1000), ext))
	with open(final, "wb") as f:
		f.write(raw)
	return final, "本地 SVC"


# ==================== 五、总入口 ====================
_ENGINE_CN = {"read": "朗读模式", "cloud": "云端音乐 API", "svc": "本地 SVC 转换"}


def sing(text_or_name, style="", lines=None, user_wants_song=True):
	"""唱一段。返回 dict(ok, path, engine, note, lines, fix)

	降级顺序按用户设置来，但一定收在朗读模式：
	    cloud -> svc -> read ；svc -> read ；read -> read（就它自己）
	"""
	cfg = _voice_cfg()
	if style:
		cfg["style"] = style
	engine = (cfg.get("engine") or "read").lower()
	if engine not in _ENGINE_CN:
		engine = "read"
	notes = []
	fixes = []

	# 1) 歌词：给了就用给的，没给就搜
	if not lines:
		name = (text_or_name or "").strip()
		got, note = lyrics_from_search(name, max_lines=cfg["max_lines"])
		if got:
			lines = got
			notes.append(note)
		else:
			# v1.8.15：搜不到就老实说，绝不拿标题/报错去合成
			_msg = "没搜到这首歌的完整歌词，换一首试试吧"
			notes.append("%s（%s）" % (_msg, note))
			return {"ok": False, "path": "", "engine": engine, "msg": _msg,
					"note": "；".join(notes), "lines": [], "fix": []}

	# 2) 多音字
	fixed = []
	for i, ln in enumerate(lines):
		s, hits = polyphone_fix(ln)
		lines[i] = s
		if hits:
			fixed.extend(hits)
	if fixed:
		notes.append("顺手纠了 %d 处多音字" % len(fixed))

	# 3) 按设置跑引擎，逐级降级
	order = {"cloud": ["cloud", "svc", "read"], "svc": ["svc", "read"], "read": ["read"]}[engine]
	for eng in order:
		try:
			if eng == "cloud":
				path, extra = synth_cloud(lines, cfg)
			elif eng == "svc":
				path, extra = synth_svc(lines, cfg)
			else:
				path, extra = synth_read(lines, cfg)
			if path and os.path.getsize(path) > 1024:
				if eng != engine:
					notes.append("%s 没成功，已经退回 %s（声音会朴素一些）"
								 % (_ENGINE_CN.get(engine, engine), _ENGINE_CN[eng]))
				return {"ok": True, "path": path, "engine": eng,
						"note": "；".join(notes), "lines": lines, "fix": fixed}
			notes.append("%s 出来的文件是空的，继续降级" % _ENGINE_CN[eng])
		except Exception as e:
			notes.append("%s 失败：%s" % (_ENGINE_CN[eng], str(e)[:80]))
	return {"ok": False, "path": "", "engine": engine,
			"note": "；".join(notes) or "所有引擎都没出声", "lines": lines, "fix": fixed}


def sing_status():
	cfg = _voice_cfg()
	_eng_cn = dict(_ENGINE_CN)
	_sing_ok = _has_sing_deps()
	if _sing_ok:
		_eng_cn["read"] = "真唱模式"
	out = {"ok": True, "engine": cfg["engine"],
		   "engine_cn": _eng_cn.get(cfg["engine"], "朗读模式"),
		   "engines": [{"key": k, "name": v} for k, v in _eng_cn.items()],
		   "voice": cfg["voice"] or "默认音色",
		   "cloud_url": cfg["cloud_url"], "cloud_format": cfg["cloud_format"],
		   "cloud_ready": bool((cfg["cloud_url"] or "").strip()),
		   "svc_url": cfg["svc_url"], "svc_ready": bool((cfg["svc_url"] or "").strip()),
		   "style": cfg["style"], "max_lines": cfg["max_lines"],
		   "polyphone_rules": len(POLY_RULES),
		   "sing_ready": bool(_sing_ok),                       # v1.9.25：真唱链齐了没
		   "sing_engine_cn": "真唱（拆字变调 + 伴奏）" if _sing_ok else "朗读（保底）"}
	try:
		import edge_tts  # noqa: F401
		out["read_ready"] = True
	except Exception:
		out["read_ready"] = False
	return out
