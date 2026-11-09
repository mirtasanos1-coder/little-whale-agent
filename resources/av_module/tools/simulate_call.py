"""模拟一个 QQ 风格的通话来电窗口 —— 没有真来电也能测 RPA（UIA + 图像识别两条路）。

这个窗口是 Tk 画的"自绘界面"：UIA 大概率看不到里面的按钮（跟 QQ NT 一个毛病），
正好用来验证「UIA 拿不到按钮 → 截图模板匹配 → pyautogui 点击」这条兜底链路。

用法：
    # 1) 开一个假来电窗口（保持这个终端别关）
    python tools\\simulate_call.py

    # 2) 另一个终端：看看 UIA 能不能看到按钮（多半 controls=0）
    python selftest.py --rpa-scan

    # 3) 全自动验证图像兜底（推荐，会自己开窗口/截模板/点接听）
    python tools\\test_rpa_image.py

窗口里点"接听"会在 runtime\\sim_call_state.json 里记一条 accepted（证明点击真的落上了）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from av_core import screen as screenmod  # noqa: E402  必须在 tk 之前设置 DPI 感知

ACCEPT_COLOR = "#27ae60"
HANGUP_COLOR = "#c0392b"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="模拟 QQ 通话窗口（RPA 测试用）")
    ap.add_argument("--title", default="QQ 语音通话 - 模拟来电")
    ap.add_argument("--geometry", default="540x380+320+200")
    ap.add_argument("--state", default=str(ROOT / "runtime" / "sim_call_state.json"))
    ap.add_argument("--seconds", type=float, default=0, help="自动关闭秒数（0=一直开着）")
    args = ap.parse_args(argv)

    import tkinter as tk

    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    root = tk.Tk()
    root.title(args.title)
    root.geometry(args.geometry)
    root.configure(bg="#1b1d23")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    payload: dict = {"title": args.title, "events": [], "buttons": {}, "started": time.time()}

    def dump() -> None:
        try:
            state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError:
            pass

    tk.Label(root, text="模拟来电（虚拟麦克风/摄像头已就绪）", bg="#1b1d23", fg="#9aa4b2",
             font=("Microsoft YaHei", 10)).pack(pady=(16, 4))
    tk.Label(root, text="对方：张三", bg="#1b1d23", fg="#e6e8ee",
             font=("Microsoft YaHei", 20, "bold")).pack(pady=(0, 10))

    status = tk.Label(root, text="● 来电中…", bg="#1b1d23", fg="#f39c12",
                      font=("Microsoft YaHei", 12))
    status.pack(pady=(0, 14))

    holder = tk.Frame(root, bg="#1b1d23", width=340, height=76)
    holder.pack(pady=6)
    holder.pack_propagate(False)      # 子控件是 place 的，别让它们把父亲压成 1x1

    accept_btn = tk.Button(holder, text="接听", font=("Microsoft YaHei", 16, "bold"),
                           bg=ACCEPT_COLOR, fg="white", activebackground="#2ecc71",
                           relief="flat", cursor="hand2")
    accept_btn.place(x=85, y=6, width=170, height=62)

    hangup_btn = tk.Button(holder, text="挂断", font=("Microsoft YaHei", 16, "bold"),
                           bg=HANGUP_COLOR, fg="white", activebackground="#e74c3c",
                           relief="flat", cursor="hand2")

    def on_accept() -> None:
        payload["events"].append({"event": "accepted", "ts": round(time.time(), 3)})
        status.config(text="● 通话中…（已接听）", fg="#2ecc71")
        accept_btn.place_forget()
        hangup_btn.place(x=85, y=6, width=170, height=62)
        dump()

    def on_hangup() -> None:
        payload["events"].append({"event": "hangup", "ts": round(time.time(), 3)})
        status.config(text="● 通话已结束", fg="#95a5a6")
        hangup_btn.place_forget()
        accept_btn.place(x=85, y=6, width=170, height=62)
        dump()

    accept_btn.config(command=on_accept)
    hangup_btn.config(command=on_hangup)

    def record_buttons() -> None:
        root.update_idletasks()
        try:
            payload["buttons"]["accept"] = [
                int(accept_btn.winfo_rootx()), int(accept_btn.winfo_rooty()),
                int(accept_btn.winfo_width()), int(accept_btn.winfo_height()),
            ]
            payload["window_rect"] = [
                int(root.winfo_rootx()), int(root.winfo_rooty()),
                int(root.winfo_width()), int(root.winfo_height()),
            ]
        except Exception:
            pass
        dump()

    root.after(500, record_buttons)
    dump()

    if args.seconds > 0:
        root.after(int(args.seconds * 1000), root.destroy)

    print(f"模拟来电窗口已打开：{args.title}")
    print(f"  状态文件: {state_path}")
    print("  验证图像兜底: python tools\\test_rpa_image.py")
    root.mainloop()
    payload["events"].append({"event": "closed", "ts": round(time.time(), 3)})
    dump()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
