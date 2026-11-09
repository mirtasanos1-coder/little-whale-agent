# -*- coding: utf-8 -*-
"""进程清道夫（V1.8.4）。

用户实机反馈：软件运行或退出后，后台会残留一堆 ``node.exe``（NapCat 的进程），
越积越多,最后几十个实例一起抢 3003/3004,表现就是「QQ 怎么都登不上」。

为什么不用 ``taskkill /F /IM node.exe``:
    用户机器上别的程序也在跑 node（IDE 插件、别的工具链）。
    按映像名一把梭等于把人家的工作一起杀掉 —— 这种事不能做。

所以这里的判据是「**只认我们自己的 NapCat**」,四条证据任意一条成立才算:
    1) 命令行里出现 napcat（``...\\runtime\\napcat\\napcat.mjs``、``...\\napcat\\index.js``）;
    2) 可执行文件本身就在 napcat 目录下;
    3) 命令行里的脚本落在我们记录的 NapCat 安装目录下;
    4) 兜底不成立:光有 ``index.js`` 不算 —— 别的 node 服务也叫 index.js
       （本机上就有一个 ``billion-context/dist/index.js``）,按文件名杀必然误杀。

另外提供两道「防孤儿」的保险,不靠事后清理:
    · ``attach_job()`` 把拉起的 node 挂进 ``KILL_ON_JOB_CLOSE`` 的 Job 对象,
      主程序就算崩了/被任务管理器结束,Windows 内核也会把 node 一起回收;
    · ``kill_all_napcat_nodes()`` 给「现在就已经残留的那些」兜底。

模块刻意不 import ``napcat_mgr``（避免循环引用）,安装目录一律由调用方传进来。
"""

import ctypes
import json
import os
import subprocess
import sys
import time

_IS_WIN = (os.name == "nt")

# 只认 node.exe,别的映像名一概不看
_NODE_NAME = "node.exe"

# Job 对象相关常量
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_EXTENDED_LIMIT_CLASS = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_INFORMATION = 0x0400

_JOB = None
_JOB_ERR = ""
_JOB_TRIED = False


def _low(x):
	return str(x or "").lower().replace("/", "\\")


def _dir_set(install_dir=""):
	"""收集「算我们自己的」那些目录（调用方传的 + 自动找的）。"""
	out = set()
	cand = [install_dir]
	try:
		import napcat_mgr
		cand.append(napcat_mgr.find_install_dir())
	except Exception:
		pass
	for d in cand:
		d = _low(d).rstrip("\\")
		if d:
			out.add(d)
	return out


def explain(name="", exe="", cmd="", dirs=None):
	"""这个进程是不是我们的 NapCat？返回 (是/否, 人话理由)。"""
	n = _low(name)
	e = _low(exe)
	c = _low(cmd)
	if n and n != _NODE_NAME:
		return False, "不是 node.exe"
	# 1) 命令行带 napcat（最常见）
	if "napcat" in c:
		return True, "命令行里有 napcat"
	# 2) 可执行文件在 napcat 目录里
	if "napcat" in e:
		return True, "node.exe 就在 napcat 目录下"
	# 3) 命令行里的路径落在我们记录的安装目录里
	for d in (dirs or ()):
		if d and d in c:
			return True, "命令行指向本机 NapCat 目录"
	# 4) 光有 index.js 不算 —— 见模块文档
	return False, ""


def _iter_nodes():
	"""列出本机所有 node.exe（拿不到信息的也列出来,标 denied）。"""
	rows = []
	try:
		import psutil
	except Exception:
		psutil = None
	if psutil is not None:
		for p in psutil.process_iter():
			try:
				nm = p.name()
			except Exception:
				continue
			if _low(nm) != _NODE_NAME:
				continue
			exe = ""
			cmd = ""
			try:
				exe = p.exe() or ""
			except Exception:
				exe = ""
			try:
				cmd = " ".join(p.cmdline() or [])
			except Exception:
				cmd = ""
			rows.append({
				"pid": int(getattr(p, "pid", 0) or 0),
				"name": nm or _NODE_NAME,
				"exe": exe,
				"cmd": cmd[:600],
				"denied": (not exe and not cmd),
			})
		return rows
	# 没 psutil 的退路：CIM 查（结果里没有 exe,所以判据会少两条,宁可少杀）
	try:
		ps = subprocess.run(
			["powershell", "-NoProfile", "-Command",
			 "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
			 "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
			capture_output=True, timeout=15,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
		raw = (ps.stdout or b"").decode("utf-8", "replace").strip() or "[]"
		data = json.loads(raw)
		if isinstance(data, dict):
			data = [data]
		for r in data:
			rows.append({
				"pid": int(r.get("ProcessId") or 0),
				"name": _NODE_NAME,
				"exe": "",
				"cmd": str(r.get("CommandLine") or "")[:600],
				"denied": not str(r.get("CommandLine") or ""),
			})
	except Exception:
		pass
	return rows


def scan_napcat_nodes(install_dir=""):
	"""扫一遍本机 node.exe,分成「我们的 NapCat」「别人的 node」「看不到的」。"""
	dirs = _dir_set(install_dir)
	out = {"ours": [], "kept": [], "denied": [], "total": 0, "psutil": True}
	try:
		import psutil  # noqa: F401
	except Exception:
		out["psutil"] = False
	for r in _iter_nodes():
		it = dict(r)
		yes, why = explain(r.get("name"), r.get("exe"), r.get("cmd"), dirs)
		it["ours"] = bool(yes)
		it["why"] = why
		if r.get("denied") and not yes:
			out["denied"].append(it)
		elif yes:
			out["ours"].append(it)
		else:
			out["kept"].append(it)
	out["total"] = len(out["ours"]) + len(out["kept"]) + len(out["denied"])
	return out


def _alive(pid):
	try:
		import psutil
		return psutil.pid_exists(int(pid))
	except Exception:
		return False


def _kill_one(pid):
	"""杀一个进程。返回 (成功?, 说明)。"""
	pid = int(pid or 0)
	if pid <= 0:
		return False, "进程号无效"
	try:
		import psutil
		pr = psutil.Process(pid)
	except Exception as e:
		# 进程本来就没了（或者根本看不到）
		if type(e).__name__ in ("NoSuchProcess",):
			return True, "已经不在了"
		return False, "%s: %s" % (type(e).__name__, e)
	try:
		pr.terminate()
	except Exception as e:
		# 权限不够：转 taskkill 再试一次（它给的错误信息更明确）
		msg = str(e)
		try:
			ps = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
								capture_output=True, timeout=8,
								creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
			txt = (ps.stdout or b"").decode("gbk", "replace") + (ps.stderr or b"").decode("gbk", "replace")
			if ps.returncode == 0:
				return True, "taskkill 成功"
			if ("拒绝访问" in txt) or ("Access is denied" in txt):
				return False, "需要管理员权限"
			return False, txt.strip()[:160] or msg
		except Exception as e2:
			return False, "%s / %s" % (msg, e2)
	try:
		pr.wait(timeout=1.5)
		return True, "已结束"
	except Exception:
		pass
	try:
		pr.kill()
		return True, "已强杀"
	except Exception as e:
		return False, "强杀失败：%s" % e


def kill_all_napcat_nodes(install_dir="", wait=2.5):
	"""把残留的 NapCat node 全清掉（别人的 node 一个都不碰）。

	返回 dict,前端直接拿去渲染:
	    ok / killed / failed / kept / denied / before / after / need_admin / msg
	"""
	t0 = time.time()
	before = scan_napcat_nodes(install_dir)
	killed, failed = [], []
	for it in before["ours"]:
		ok, why = _kill_one(it["pid"])
		row = {"pid": it["pid"], "why": it.get("why") or "", "cmd": it.get("cmd") or ""}
		if ok:
			row["how"] = why
			killed.append(row)
		else:
			row["err"] = why
			failed.append(row)
	# 等它们真的退出（NapCat 有时是两层：node + 内层 napcat 子进程）
	deadline = time.time() + max(0.5, float(wait or 0))
	pids = [x["pid"] for x in killed]
	while time.time() < deadline:
		if not [p for p in pids if _alive(p)]:
			break
		time.sleep(0.2)
	after = scan_napcat_nodes(install_dir)
	left = after["ours"]
	need_admin = bool(left) or bool(failed)
	if before["total"] == 0:
		msg = "没有发现任何 node.exe，很干净。"
	elif not before["ours"]:
		msg = "本机有 %d 个 node.exe，但都不是 NapCat，一个都没动。" % before["total"]
	elif not left:
		msg = "清掉了 %d 个残留的 NapCat 进程；另外 %d 个 node 不是 NapCat，没动它们。" % (
			len(killed), len(after["kept"]))
	else:
		msg = "清掉 %d 个，还剩 %d 个没退（多半要管理员权限）。" % (len(killed), len(left))
	return {
		"ok": not left,
		"killed": killed,
		"failed": failed,
		"kept": [{"pid": x["pid"], "cmd": x.get("cmd") or ""} for x in after["kept"]],
		"denied": [{"pid": x["pid"]} for x in after["denied"]],
		"before": before["total"],
		"after": after["total"],
		"left": [{"pid": x["pid"], "cmd": x.get("cmd") or ""} for x in left],
		"need_admin": need_admin,
		"msg": msg,
		"elapsed": round(time.time() - t0, 2),
	}


# ==================== 防孤儿：Windows Job 对象 ====================
class _IO_COUNTERS(ctypes.Structure):
	_fields_ = [
		("ReadOperationCount", ctypes.c_ulonglong),
		("WriteOperationCount", ctypes.c_ulonglong),
		("OtherOperationCount", ctypes.c_ulonglong),
		("ReadTransferCount", ctypes.c_ulonglong),
		("WriteTransferCount", ctypes.c_ulonglong),
		("OtherTransferCount", ctypes.c_ulonglong),
	]


class _JOB_BASIC_LIMIT(ctypes.Structure):
	_fields_ = [
		("PerProcessUserTimeLimit", ctypes.c_int64),
		("PerJobUserTimeLimit", ctypes.c_int64),
		("LimitFlags", ctypes.c_uint32),
		("MinimumWorkingSetSize", ctypes.c_size_t),
		("MaximumWorkingSetSize", ctypes.c_size_t),
		("ActiveProcessLimit", ctypes.c_uint32),
		("Affinity", ctypes.c_size_t),
		("PriorityClass", ctypes.c_uint32),
		("SchedulingClass", ctypes.c_uint32),
	]


class _JOB_EXTENDED_LIMIT(ctypes.Structure):
	_fields_ = [
		("BasicLimitInformation", _JOB_BASIC_LIMIT),
		("IoInfo", _IO_COUNTERS),
		("ProcessMemoryLimit", ctypes.c_size_t),
		("JobMemoryLimit", ctypes.c_size_t),
		("PeakProcessMemoryUsed", ctypes.c_size_t),
		("PeakJobMemoryUsed", ctypes.c_size_t),
	]


def job():
	"""拿到（或创建）我们那个 KILL_ON_JOB_CLOSE 的 Job 对象句柄。

	句柄一直挂在模块级变量上 —— 只要 backend_server.exe 活着,句柄就在;
	进程一死（正常退出、崩溃、被任务管理器结束都算）,句柄被系统关掉,
	Job 里的 node 会被内核立刻回收。
	"""
	global _JOB, _JOB_ERR, _JOB_TRIED
	if _JOB is not None:
		return _JOB
	if _JOB_TRIED:
		return None
	_JOB_TRIED = True
	if not _IS_WIN:
		_JOB_ERR = "不是 Windows,不用 Job 对象"
		return None
	try:
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		h = k32.CreateJobObjectW(None, None)
		if not h:
			_JOB_ERR = "CreateJobObjectW 失败(错误码 %d)" % ctypes.get_last_error()
			return None
		info = _JOB_EXTENDED_LIMIT()
		info.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_JOB_CLOSE
		ok = k32.SetInformationJobObject(h, _JOB_EXTENDED_LIMIT_CLASS,
										 ctypes.byref(info), ctypes.sizeof(info))
		if not ok:
			_JOB_ERR = "SetInformationJobObject 失败(错误码 %d)" % ctypes.get_last_error()
			try:
				k32.CloseHandle(h)
			except Exception:
				pass
			return None
		_JOB = h
		return _JOB
	except Exception as e:
		_JOB_ERR = "%s: %s" % (type(e).__name__, e)
		return None


def attach_job(proc_or_pid):
	"""把一个刚拉起的 node 挂进我们的 Job。成功返回 True。

	失败不算致命（比如 node 已经被别人挂进了不允许 breakaway 的 Job）:
	那种情况退回「退出时主动清理」那条路,lib.rs 里会先请后端杀 node。
	"""
	h = job()
	if not h:
		return False
	pid = getattr(proc_or_pid, "pid", proc_or_pid)
	try:
		pid = int(pid)
	except Exception:
		return False
	if pid <= 0:
		return False
	try:
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		hp = k32.OpenProcess(
			_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_INFORMATION, False, pid)
		if not hp:
			return False
		try:
			return bool(k32.AssignProcessToJobObject(h, hp))
		finally:
			try:
				k32.CloseHandle(hp)
			except Exception:
				pass
	except Exception:
		return False


def in_job(pid):
	"""这个进程在不在我们的 Job 里（自测/排错用）。"""
	h = job()
	if not h:
		return False
	try:
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		hp = k32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, int(pid))
		if not hp:
			return False
		flag = ctypes.c_int(0)
		try:
			ok = k32.IsProcessInJob(hp, h, ctypes.byref(flag))
			return bool(ok and flag.value)
		finally:
			try:
				k32.CloseHandle(hp)
			except Exception:
				pass
	except Exception:
		return False


def job_status():
	"""给前端/自测看的 Job 状态。"""
	h = job()
	return {
		"ok": bool(h),
		"handle": int(h or 0),
		"kill_on_close": _JOB_LIMIT_KILL_ON_JOB_CLOSE,
		"err": _JOB_ERR,
	}


# ==================== 提权清理 ====================
def cleanup_cmd():
	"""拼出「以管理员身份重新跑一次清理」的命令行。"""
	if getattr(sys, "frozen", False):
		return '"%s" --clean-napcat-nodes' % sys.executable
	here = os.path.dirname(os.path.abspath(__file__))
	bs = os.path.join(here, "backend_server.py")
	if os.path.isfile(bs):
		return '"%s" "%s" --clean-napcat-nodes' % (sys.executable, bs)
	return '"%s" --clean-napcat-nodes' % os.path.join(here, "napcat_mgr.py")


def elevate_cleanup(timeout=0):
	"""请求 UAC 提权,用管理员身份再清一遍。返回 (是否已拉起, 说明)。

	返回 True 只代表「权限对话框弹出来了并且用户点了是」——
	真正的结果要前端回头再调一次 ``kill_nodes_force`` 看还剩几个。
	"""
	if not _IS_WIN:
		return False, "这个功能只在 Windows 上有用"
	cmd = cleanup_cmd()
	try:
		rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd.split('"')[1],
												cmd.split('"')[2].strip() if cmd.count('"') >= 4 else None,
												None, 1)
	except Exception as e:
		return False, "提权失败：%s: %s" % (type(e).__name__, e)
	if int(rc) <= 32:
		if int(rc) == 5:
			return False, "你在权限对话框里点了「否」，没有清理。"
		return False, "没能拉起管理员进程（错误码 %d）" % int(rc)
	return True, "已经在管理员权限下重新清理了，请稍等几秒再看结果。"


def write_result(data, path=""):
	"""把清理结果落盘,方便提权进程和主进程之间对账/排错。"""
	p = path or os.path.join(os.environ.get("TEMP") or ".", "xiaojingyu_napcat_clean.json")
	try:
		with open(p, "w", encoding="utf-8") as f:
			json.dump(data, f, ensure_ascii=False, indent=2)
		return p
	except Exception:
		return ""


def main(argv=None):
	"""``--clean-napcat-nodes`` 入口：给提权进程用。"""
	args = list(argv if argv is not None else sys.argv[1:])
	install = ""
	for i, a in enumerate(args):
		if a == "--napcat-dir" and i + 1 < len(args):
			install = args[i + 1]
	res = kill_all_napcat_nodes(install)
	p = write_result(res)
	line = res.get("msg") or ""
	try:
		print("[napcat_proc] " + line, flush=True)
		if p:
			print("[napcat_proc] 结果已写入 " + p, flush=True)
	except Exception:
		pass
	return 0 if res.get("ok") else 1


if __name__ == "__main__":
	sys.exit(main())
