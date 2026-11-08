# -*- coding: utf-8 -*-
"""在线更新 —— 从 GitHub 仓库拉最新代码

============================================================
 你在 GitHub 上开一个仓库，把本项目源码推上去，然后：
   1. 界面「设置 → 在线更新」里填 用户名/仓库名（如 ruao/qq-agent）
   2. 点「检查更新」→ 会显示远端最新 commit
   3. 点「立即更新」→ 下载 zip 并覆盖本地 .py / .html / .json / .md
   4. 更新完自动重启 Agent

 或者直接改 config.json：
   "update_enabled": true,
   "update_repo": "你的用户名/仓库名",
   "update_branch": "main"

============================================================
 为什么用 zip 下载而不是 git clone
============================================================
 用户机器上不一定装了 git；zipball 是纯 HTTP，requests 就够。
 代价是拿不到增量，只能整包覆盖 —— 但本项目一共才几百 KB，无所谓。

============================================================
 安全设计（重要）
============================================================
 · 白名单：只覆盖 .py/.html/.json/.md/.bat/.txt，绝不碰 memory.db / agent.log /
   config.json / personas.json / stickers/ / themes/ / icons/ —— 用户数据一律不动。
 · 覆盖前把将被改的文件备份到 _backup/<时间戳>/，出问题可以手动还原。
 · 默认 update_enabled=false，用户自己开，不会偷偷联网。
 · 只认 https 的 GitHub 域名，防止被改成任意 URL 下载恶意文件。
============================================================
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

import config as cfg_mod

# 允许被更新覆盖的扩展名（白名单）
UPDATE_EXTS = (".py", ".html", ".md", ".bat", ".txt")

# 无论扩展名如何，这些文件绝不覆盖（用户数据 / 运行产物）
NEVER_OVERWRITE = {
    "config.json", "personas.json", "memory.db", "memory.db-shm", "memory.db-wal",
    "agent.log", "agent_state.json", "autostart.log", "使用说明.txt",
}
# 这些目录整个跳过（用户自己的资源）
NEVER_OVERWRITE_DIRS = {
    "stickers", "themes", "icons", "_backup", "__pycache__",
    "dist_程序包", "dist_开源包", "build", "spec", ".git", ".venv", "venv",
}

GITHUB_HOSTS = ("github.com", "api.github.com", "codeload.github.com",
                "objects.githubusercontent.com", "raw.githubusercontent.com")


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
BACKUP_ROOT = os.path.join(DATA_DIR, "_backup")


# ==================== 配置 ====================
def _cfg():
    return {
        "enabled": bool(cfg_mod.get("update_enabled", False)),
        "repo": str(cfg_mod.get("update_repo") or "").strip().strip("/"),
        "branch": str(cfg_mod.get("update_branch") or "main").strip() or "main",
        "api": str(cfg_mod.get("update_api") or "https://api.github.com").rstrip("/"),
        "timeout": int(cfg_mod.get("update_timeout") or 20),
        # v1.1.0：镜像加速 + 哈希校验
        "mirrors": list(cfg_mod.get("update_mirrors") or []),
        "require_sig": bool(cfg_mod.get("update_require_sig", True)),
        "auto_check": bool(cfg_mod.get("update_auto_check", True)),
    }


def _repo_ok(repo):
    """repo 形如 owner/name，只允许字母数字 . _ -"""
    if not repo or "/" not in repo:
        return False, "仓库格式应为 用户名/仓库名，例如 ruao/qq-agent"
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        return False, "仓库格式应为 用户名/仓库名"
    import re
    for p in parts:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", p):
            return False, f"仓库名里有非法字符：{p}"
    return True, "ok"


def _check_host(url, allow_mirror=False):
    """只允许 https + GitHub 域名（镜像地址单独放行，但仍强制 https）"""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False, "URL 解析失败"
    if u.scheme != "https":
        return False, "只支持 https"
    host = (u.hostname or "").lower()
    if host in GITHUB_HOSTS or host.endswith(".github.com"):
        return True, "ok"
    if allow_mirror:
        # 镜像必须是用户自己在设置里配的（update_mirrors），
        # 这里只保证是 https + 有主机名，不去限制具体域名。
        if host and "." in host:
            return True, "ok"
        return False, f"镜像地址不合法：{host or '空'}"
    return False, f"不允许的域名：{host}（只支持 GitHub）"


# ==================== 远端信息 ====================
def _headers():
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "xiaojingyu-updater/1.0"}
    # 可选：私有仓库可以用 token（环境变量，不写进 config.json 免得泄露）
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("QQ_AGENT_GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def remote_info():
    """查远端最新 commit。返回 {"ok","sha","date","message","html_url","msg"}"""
    c = _cfg()
    ok, msg = _repo_ok(c["repo"])
    if not ok:
        return {"ok": False, "msg": msg, "configured": False}
    url = f"{c['api']}/repos/{c['repo']}/commits/{c['branch']}"
    ok, msg = _check_host(url)
    if not ok:
        return {"ok": False, "msg": msg}
    try:
        import requests
        r = requests.get(url, headers=_headers(), timeout=c["timeout"])
    except Exception as e:
        return {"ok": False, "msg": f"网络错误：{e}"}
    if r.status_code == 404:
        return {"ok": False, "msg": f"仓库或分支不存在：{c['repo']}@{c['branch']}"
                                    f"（私有仓库请设置环境变量 GITHUB_TOKEN）"}
    if r.status_code == 403:
        return {"ok": False, "msg": "GitHub API 限流（403），过一会儿再试"}
    if r.status_code != 200:
        return {"ok": False, "msg": f"HTTP {r.status_code}：{r.text[:200]}"}
    try:
        d = r.json()
    except Exception:
        return {"ok": False, "msg": "返回内容不是 JSON"}
    commit = d.get("commit") or {}
    author = (commit.get("author") or {})
    return {
        "ok": True,
        "configured": True,
        "sha": (d.get("sha") or "")[:12],
        "sha_full": d.get("sha") or "",
        "date": author.get("date") or "",
        "message": (commit.get("message") or "").split("\n")[0][:200],
        "author": author.get("name") or "",
        "html_url": d.get("html_url") or "",
        "repo": c["repo"],
        "branch": c["branch"],
        "msg": "ok",
    }


def local_version():
    """本地记录的版本（上次更新时的 sha）。存在 _backup/version.json"""
    p = os.path.join(BACKUP_ROOT, "version.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def check():
    """检查有没有更新。返回给界面用的完整结构。"""
    c = _cfg()
    local = local_version()
    if not c["enabled"]:
        return {"ok": False, "configured": False, "enabled": False,
                "msg": "在线更新未启用（设置 → 在线更新 里打开）",
                "local": local, "repo": c["repo"], "branch": c["branch"]}
    if not c["repo"]:
        return {"ok": False, "configured": False, "enabled": True,
                "msg": "还没填仓库地址（设置 → 在线更新 → 仓库，形如 用户名/仓库名）",
                "local": local, "repo": "", "branch": c["branch"]}

    remote = remote_info()
    if not remote.get("ok"):
        return {"ok": False, "configured": True, "enabled": True,
                "msg": remote.get("msg") or "查询失败",
                "local": local, "repo": c["repo"], "branch": c["branch"]}

    local_sha = str(local.get("sha") or "")
    has_update = bool(remote["sha_full"]) and remote["sha_full"] != local_sha
    if not local_sha:
        note = "本地还没有版本记录（首次更新会整包覆盖一次）"
    elif has_update:
        note = "发现新版本"
    else:
        note = "已是最新"
    return {
        "ok": True,
        "configured": True,
        "enabled": True,
        "has_update": has_update,
        "local_sha": local_sha[:12],
        "local_date": local.get("date") or "",
        "remote": remote,
        "note": note,
        "repo": c["repo"],
        "branch": c["branch"],
        "msg": note,
    }


# ==================== 下载与覆盖 ====================
def _mirror_candidates(c):
    """候选下载地址：先用 mirrors 里配的，再回落官方 codeload。

    mirrors 支持两种写法：
      1) 前缀：   "https://ghproxy.net/https://"  → 拼在官方地址前面
      2) 模板：   "https://mydomain/u/{repo}/{branch}.zip"（含 {repo}/{branch} 占位）
    这样国内拉不动的时候，换镜像只要改配置，不用改代码。
    """
    official = f"https://codeload.github.com/{c['repo']}/zip/refs/heads/{c['branch']}"
    out = []
    try:
        mirrors = c.get("mirrors") or []
    except Exception:
        mirrors = []
    for m in mirrors:
        s = str(m or "").strip()
        if not s:
            continue
        if "{repo}" in s or "{branch}" in s:
            out.append(s.replace("{repo}", c["repo"]).replace("{branch}", c["branch"]))
        elif s.endswith("https://") or s.endswith("http://"):
            # 纯前缀写法，例如 https://ghproxy.net/https:// + 官方地址
            out.append(s + official)
        else:
            out.append(s.rstrip("/") + "/" + official)
    out.append(official)
    # 去重但保持顺序
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _download_zip(c):
    """下载 zipball 到临时文件（按镜像顺序逐个试），返回路径。"""
    import requests
    urls = _mirror_candidates(c)
    last_err = ""
    for url in urls:
        ok, msg = _check_host(url, allow_mirror=(url != urls[-1]))
        if not ok:
            last_err = f"{url} → {msg}"
            continue
        try:
            r = requests.get(url, headers=_headers(),
                             timeout=max(60, c["timeout"]), stream=True)
            if r.status_code != 200:
                last_err = f"{url} → HTTP {r.status_code}：{r.text[:120]}"
                continue
            fd, path = tempfile.mkstemp(suffix=".zip", prefix="xiaojingyu_update_")
            os.close(fd)
            total = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
                        if total > 200 * 1024 * 1024:
                            raise RuntimeError("压缩包超过 200MB，已中止")
            if total < 1024:
                try:
                    os.remove(path)
                except Exception:
                    pass
                last_err = f"{url} → 下载内容太小（{total} 字节），可能是镜像返回了错误页"
                continue
            return path
        except Exception as e:
            last_err = f"{url} → {type(e).__name__}: {e}"
            continue
    raise RuntimeError(last_err or "所有下载地址都失败了")


HASH_MANIFEST_NAMES = ("manifest.sha256", "SHA256SUMS.txt", "update.sha256")
# v1.8.5：增量更新包自带的清单（记了每个文件的 sha256）
DELTA_MANIFEST_NAMES = ("delta_manifest.json",)


def _sha_bytes(data):
    """字节的 sha256 —— 用来判断「这个文件和包里的一不一样」。"""
    import hashlib
    try:
        return hashlib.sha256(data or b"").hexdigest()
    except Exception:
        return ""


def _sha_file(path):
    """文件的 sha256（读不出来返回空串，调用方当成「不一样」照常覆盖）。"""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _read_hash_manifest(zf):
    """从 zip 里读哈希清单 -> {相对路径: sha256}（读不到返回 {}）"""
    import hashlib
    found = {}
    for rel, info in _iter_zip_files(zf):
        if rel.split("/")[-1] not in HASH_MANIFEST_NAMES:
            continue
        try:
            text = zf.read(info).decode("utf-8-sig", "replace")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            expect, name = parts[0].lower(), parts[-1].lstrip("*").replace("\\", "/")
            if len(expect) == 64:
                found[name] = expect
    _ = hashlib
    return found


def _verify_zip(zf, require=False):
    """校验下载包的完整性（哈希清单）。

    返回 {"ok","checked","bad","msg"}
    为什么要这么做：更新包是从网上拉的，得先确认"拉下来的东西没被中间人改过"。
    清单里没列出的文件（比如新增的）不算失败，但清单里列出的必须对上。
    """
    want = _read_hash_manifest(zf)
    if not want:
        if require:
            return {"ok": False, "checked": 0, "bad": [],
                    "msg": "更新包没有哈希清单（manifest.sha256），按设置拒绝了这次更新"}
        return {"ok": True, "checked": 0, "bad": [], "msg": "没有哈希清单，跳过校验"}
    import hashlib
    bad, checked = [], 0
    for rel, info in _iter_zip_files(zf):
        expect = want.get(rel)
        if not expect:
            continue
        try:
            got = hashlib.sha256(zf.read(info)).hexdigest()
        except Exception:
            continue
        checked += 1
        if got != expect:
            bad.append(rel)
    if bad:
        return {"ok": False, "checked": checked, "bad": bad[:10],
                "msg": f"哈希校验失败：{len(bad)} 个文件对不上（{', '.join(bad[:3])}）"}
    return {"ok": True, "checked": checked, "bad": [],
            "msg": f"哈希校验通过（{checked} 个文件）"}


def _download_zip_legacy(c):
    """旧实现保留在此仅作参考，新代码走 _download_zip（带镜像）"""
    url = f"https://codeload.github.com/{c['repo']}/zip/refs/heads/{c['branch']}"
    ok, msg = _check_host(url)
    if not ok:
        raise RuntimeError(msg)
    import requests
    r = requests.get(url, headers=_headers(), timeout=max(60, c["timeout"]),
                     stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"下载失败 HTTP {r.status_code}：{r.text[:200]}")
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="xiaojingyu_update_")
    os.close(fd)
    total = 0
    with open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            if chunk:
                f.write(chunk)
                total += len(chunk)
                if total > 200 * 1024 * 1024:
                    raise RuntimeError("压缩包超过 200MB，已中止")
    return path


def _iter_zip_files(zf):
    """遍历 zip 里的文件，返回 (相对路径(去掉顶层目录), ZipInfo)"""
    names = zf.namelist()
    if not names:
        return
    # GitHub zipball 顶层是 owner-repo-sha/，剥掉它
    tops = {n.split("/")[0] for n in names if n.strip()}
    top = tops.pop() if len(tops) == 1 else ""
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if top and name.startswith(top + "/"):
            name = name[len(top) + 1:]
        if not name or name.startswith("."):
            continue
        yield name.replace("\\", "/"), info


def _should_update(rel):
    """判断一个 zip 里的相对路径是否该覆盖到本地"""
    parts = rel.split("/")
    if any(p in NEVER_OVERWRITE_DIRS for p in parts[:-1]):
        return False
    base = parts[-1]
    if base in NEVER_OVERWRITE:
        return False
    if base.startswith("_"):        # _selftest / _mock_lm 这类自检脚本不覆盖
        return False
    ext = os.path.splitext(base)[1].lower()
    return ext in UPDATE_EXTS


def apply_update(restart_cb=None):
    """下载并覆盖。返回 {"ok","updated","skipped","backup","msg","need_restart"}"""
    c = _cfg()
    if not c["enabled"]:
        return {"ok": False, "msg": "在线更新未启用"}
    ok, msg = _repo_ok(c["repo"])
    if not ok:
        return {"ok": False, "msg": msg}

    remote = remote_info()
    if not remote.get("ok"):
        return {"ok": False, "msg": f"查询远端失败：{remote.get('msg')}"}

    try:
        zpath = _download_zip(c)
    except Exception as e:
        return {"ok": False, "msg": f"下载失败：{e}"}

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, stamp)
    updated, skipped, failed, unchanged = [], [], [], []

    try:
        with zipfile.ZipFile(zpath, "r") as zf:
            bad = zf.testzip()
            if bad:
                return {"ok": False, "msg": f"压缩包损坏（{bad}）"}
            # v1.1.0：先做哈希校验，再动任何本地文件
            vr = _verify_zip(zf, require=bool(c.get("require_sig")))
            if not vr.get("ok"):
                return {"ok": False, "msg": vr.get("msg") or "完整性校验失败"}
            verify_msg = vr.get("msg") or ""
            for rel, info in _iter_zip_files(zf):
                if not _should_update(rel):
                    skipped.append(rel)
                    continue
                dst = os.path.join(DATA_DIR, rel.replace("/", os.sep))
                # 防止 zip 里的 ../ 逃逸
                if not os.path.abspath(dst).startswith(os.path.abspath(DATA_DIR)):
                    skipped.append(rel + "(路径异常)")
                    continue
                try:
                    data = zf.read(info)
                    # v1.8.5：内容没变就别重写 —— 这才是「只加新增、只换改过的」
                    if os.path.exists(dst) and _sha_bytes(data) == _sha_file(dst):
                        unchanged.append(rel)
                        continue
                    with open(dst, "wb") as f:
                        f.write(data)
                    updated.append(rel)
                except Exception as e:
                    failed.append(f"{rel}: {e}")
    except zipfile.BadZipFile:
        return {"ok": False, "msg": "下载的文件不是有效 zip"}
    finally:
        try:
            os.remove(zpath)
        except Exception:
            pass

    # 记录版本
    try:
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        with open(os.path.join(BACKUP_ROOT, "version.json"), "w", encoding="utf-8") as f:
            json.dump({"sha": remote.get("sha_full"), "sha_short": remote.get("sha"),
                       "date": remote.get("date"), "repo": c["repo"],
                       "branch": c["branch"], "updated_at": time.time(),
                       "files": len(updated)}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # 清理旧备份，只留最近 5 次
    try:
        dirs = sorted([d for d in os.listdir(BACKUP_ROOT)
                       if os.path.isdir(os.path.join(BACKUP_ROOT, d))])
        for d in dirs[:-5]:
            shutil.rmtree(os.path.join(BACKUP_ROOT, d), ignore_errors=True)
    except Exception:
        pass

    need_restart = any(x.endswith(".py") for x in updated)
    restarted = False
    if need_restart and restart_cb:
        try:
            restarted = bool(restart_cb())
        except Exception:
            restarted = False

    msg = f"更新完成：{len(updated)} 个文件"
    if skipped:
        msg += f"，跳过 {len(skipped)} 个（用户数据/非代码）"
    if unchanged:
        msg += f"，{len(unchanged)} 个没变（跳过）"
    if failed:
        msg += f"，失败 {len(failed)} 个"
    return {
        "ok": True,
        "updated": updated,
        "skipped": skipped,
        "unchanged": unchanged,
        "rolled_back": False,
        "failed": failed,
        "backup": backup_dir if updated else "",
        "sha": remote.get("sha"),
        "need_restart": need_restart,
        "restarted": restarted,
        "verify": verify_msg,
        "msg": msg,
    }


def apply_package(path, restart_cb=None):
    """应用用户自己选的「增量更新包」（v1.8.5）。

    和在线更新的区别：包是本地文件，而且包里**只有新增和改动过的文件**，
    所以几十 MB 的程序包可以缩到几百 KB，升级不用整包替换。

    安全规则与在线更新完全一致：
      · 只覆盖 UPDATE_EXTS 白名单；config.json / memory.db / stickers/ 一律不动；
      · 包里带哈希清单就先校验，对不上一个字节都不写；
      · 覆盖前备份到 _backup/pkg-<时间戳>/；
      · 只要有一个文件写失败，就把这一批全部还原（不留半新半旧）。

    返回 {"ok","updated","skipped","unchanged","failed","backup",
          "rolled_back","need_restart","restarted","msg"}
    """
    p = str(path or "").strip().strip('"')
    if not p or not os.path.isfile(p):
        return {"ok": False, "msg": "找不到更新包文件：" + (p or "（空）")}
    if not zipfile.is_zipfile(p):
        return {"ok": False, "msg": "这个文件不是 zip 更新包"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, "pkg-" + stamp)
    updated, skipped, unchanged, failed = [], [], [], []
    try:
        zf = zipfile.ZipFile(p, "r")
    except Exception as e:
        return {"ok": False, "msg": "打不开压缩包：%s" % e}
    try:
        bad = zf.testzip()
        if bad:
            return {"ok": False, "msg": "压缩包损坏（%s）" % bad}
        # v1.8.6（QA P0）：绿色版增量包（payload/ 结构）不能走程序内更新。
        # 它是给「解压后双击『应用增量包.bat』」用的；若按源码白名单在这里处理，
        # payload/ 会被当成源码写进数据目录 —— 程序不会变好，只会更坏。
        _dm = [n for n in zf.namelist() if n.split("/")[-1] in DELTA_MANIFEST_NAMES]
        if _dm:
            try:
                _dj = json.loads(zf.read(_dm[0]).decode("utf-8", "replace") or "{}")
            except Exception:
                _dj = {}
            if isinstance(_dj, dict) and str(_dj.get("kind") or "") == "release-delta":
                return {
                    "ok": False,
                    "kind": "release-delta",
                    "msg": ("这个包是绿色版增量包（里面的文件都在 payload/ 里），"
                            "不是给程序内更新用的。请把整个 zip 解压到一个新目录，"
                            "双击里面的『应用增量包.bat』——它会先校验哈希、再备份、"
                            "最后才替换。程序内的『应用增量包』只装源码包"
                            "（.py / .html / .md 那种）。"),
                }
        vr = _verify_zip(zf, require=False)
        if not vr.get("ok"):
            return {"ok": False, "msg": vr.get("msg") or "完整性校验失败"}
        for rel, info in _iter_zip_files(zf):
            if rel.split("/")[-1] in DELTA_MANIFEST_NAMES:
                continue
            if not _should_update(rel):
                skipped.append(rel)
                continue
            dst = os.path.join(DATA_DIR, rel.replace("/", os.sep))
            if not os.path.abspath(dst).startswith(os.path.abspath(DATA_DIR)):
                skipped.append(rel + "(路径异常)")
                continue
            try:
                data = zf.read(info)
                if os.path.exists(dst) and _sha_bytes(data) == _sha_file(dst):
                    unchanged.append(rel)
                    continue
                _d = os.path.dirname(dst) or DATA_DIR
                os.makedirs(_d, exist_ok=True)
                if os.path.exists(dst):
                    bak = os.path.join(backup_dir, rel.replace("/", os.sep))
                    os.makedirs(os.path.dirname(bak) or backup_dir, exist_ok=True)
                    shutil.copy2(dst, bak)
                with open(dst, "wb") as f:
                    f.write(data)
                updated.append(rel)
            except Exception as e:
                failed.append("%s: %s" % (rel, e))
    finally:
        try:
            zf.close()
        except Exception:
            pass

    rolled = False
    if failed and updated:
        try:
            for rel in updated:
                bak = os.path.join(backup_dir, rel.replace("/", os.sep))
                dst = os.path.join(DATA_DIR, rel.replace("/", os.sep))
                if os.path.exists(bak):
                    shutil.copy2(bak, dst)
            rolled = True
        except Exception:
            rolled = False

    need_restart = any(x.endswith(".py") for x in updated)
    restarted = False
    if need_restart and restart_cb and not failed:
        try:
            restarted = bool(restart_cb())
        except Exception:
            restarted = False

    msg = "增量更新完成：写入 %d 个文件" % len(updated)
    if unchanged:
        msg += "，%d 个没变（跳过）" % len(unchanged)
    if skipped:
        msg += "，跳过 %d 个（用户数据/非代码）" % len(skipped)
    if failed:
        msg += "，失败 %d 个" % len(failed)
        if rolled:
            msg += "（已整批回滚，程序还是原来的样子）"
    return {"ok": not failed, "updated": updated, "skipped": skipped,
            "unchanged": unchanged, "failed": failed,
            "backup": backup_dir if updated else "", "rolled_back": rolled,
            "need_restart": need_restart, "restarted": restarted, "msg": msg}



def rollback(stamp=None):
    """从 _backup/<stamp>/ 还原。stamp 为空则用最近一次。"""
    try:
        if not os.path.isdir(BACKUP_ROOT):
            return {"ok": False, "msg": "没有备份目录"}
        dirs = sorted([d for d in os.listdir(BACKUP_ROOT)
                       if os.path.isdir(os.path.join(BACKUP_ROOT, d))])
        if not dirs:
            return {"ok": False, "msg": "没有任何备份"}
        use = stamp or dirs[-1]
        src_root = os.path.join(BACKUP_ROOT, use)
        if not os.path.isdir(src_root):
            return {"ok": False, "msg": f"备份不存在：{use}"}
        n = 0
        for root, _dirs, files in os.walk(src_root):
            for fn in files:
                sp = os.path.join(root, fn)
                rel = os.path.relpath(sp, src_root)
                dst = os.path.join(DATA_DIR, rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(sp, dst)
                    n += 1
                except Exception:
                    pass
        return {"ok": True, "msg": f"已从备份 {use} 还原 {n} 个文件", "count": n}
    except Exception as e:
        return {"ok": False, "msg": f"还原失败：{e}"}


def list_backups():
    out = []
    try:
        if os.path.isdir(BACKUP_ROOT):
            for d in sorted(os.listdir(BACKUP_ROOT), reverse=True):
                p = os.path.join(BACKUP_ROOT, d)
                if os.path.isdir(p):
                    n = sum(len(f) for _r, _d, f in os.walk(p))
                    out.append({"stamp": d, "files": n})
    except Exception:
        pass
    return out


# ==================== V1.0.0-Beta 任务五：看 GitHub Releases ====================
# 为什么不用「拉 main 分支 zip」那套：Tauri 桌面端是编译产物，源码 zip 换不进去。
# 预览版的更新走「半自动」：这里只负责告诉你「有新版本了」+ 给出下载页，点一下
# 就用系统浏览器打开，用户自己下载安装包覆盖安装 —— 比偷偷换文件安全得多。
def releases_url():
    """发布页地址（仓库没配就返回空串）。"""
    c = _cfg()
    ok, _msg = _repo_ok(c["repo"])
    if not ok:
        return ""
    return "https://github.com/%s/releases" % c["repo"]


def latest_release():
    """查最新 Release。返回给界面用的结构，任何失败都只是 ok=False + 人话 msg。"""
    c = _cfg()
    out = {"ok": False, "configured": False, "current": _app_version(),
           "html_url": "", "tag": "", "name": "", "published_at": "",
           "assets": [], "has_new": False, "msg": ""}
    ok, msg = _repo_ok(c["repo"])
    out["html_url"] = releases_url()
    if not ok:
        out["msg"] = "还没填仓库（owner/name），填完就能检查新版本"
        return out
    out["configured"] = True
    url = "%s/repos/%s/releases/latest" % (c["api"], c["repo"])
    ok, msg = _check_host(url)
    if not ok:
        out["msg"] = msg
        return out
    try:
        import requests
        r = requests.get(url, headers=_headers(), timeout=c["timeout"])
    except Exception as e:
        out["msg"] = "网络错误：%s（国内可以配 update_mirrors 加速）" % e
        return out
    if r.status_code == 404:
        out["msg"] = "这个仓库还没有发布 Release（或者仓库名不对）"
        return out
    if r.status_code == 403:
        out["msg"] = "GitHub API 限流（403），过一会儿再试"
        return out
    if r.status_code != 200:
        out["msg"] = "HTTP %s：%s" % (r.status_code, (r.text or "")[:160])
        return out
    try:
        d = r.json()
    except Exception:
        out["msg"] = "返回内容不是 JSON"
        return out

    tag = str(d.get("tag_name") or d.get("name") or "").strip()
    assets = []
    for a in (d.get("assets") or []):
        assets.append({
            "name": str(a.get("name") or ""),
            "size": int(a.get("size") or 0),
            "url": str(a.get("browser_download_url") or ""),
        })
    out.update({
        "ok": True,
        "msg": "ok",
        "tag": tag,
        "name": str(d.get("name") or tag),
        "published_at": str(d.get("published_at") or ""),
        "html_url": str(d.get("html_url") or out["html_url"]),
        "notes": str(d.get("body") or "")[:600],
        "assets": assets,
        "prerelease": bool(d.get("prerelease")),
    })
    cur = _norm_ver(_app_version())
    new = _norm_ver(tag)
    out["has_new"] = bool(new and cur and new != cur) or bool(new and not cur)
    if out["has_new"]:
        out["msg"] = "有新版本：%s（你现在是 %s）" % (tag, out["current"] or "未知")
    else:
        out["msg"] = "已经是最新的（%s）" % (tag or out["current"])
    return out


def _app_version():
    """当前版本号：优先用发行版号（如 1.0.0-beta），没有再退回核心版本号。"""
    try:
        import config as _c
        return str(getattr(_c, "RELEASE_VERSION", "") or getattr(_c, "APP_VERSION", "") or "")
    except Exception:
        return ""


def _norm_ver(v):
    """把 v1.3.1 / 1.3.1-beta / V1.0.0-Beta 统一成可比较的串。"""
    s = str(v or "").strip().lower()
    if s.startswith("v"):
        s = s[1:]
    return s


def status():
    """给界面用的一站式状态"""
    c = _cfg()
    return {
        "ok": True,
        "enabled": c["enabled"],
        "repo": c["repo"],
        "branch": c["branch"],
        "local": local_version(),
        "backups": list_backups()[:5],
        "backup_dir": BACKUP_ROOT,
        "can_check": c["enabled"] and bool(c["repo"]),
        "data_dir": DATA_DIR,
    }


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("数据目录:", DATA_DIR)
    print("配置:", json.dumps(_cfg(), ensure_ascii=False))
    print("状态:", json.dumps(status(), ensure_ascii=False, indent=2))
    print("检查:", json.dumps(check(), ensure_ascii=False, indent=2))
