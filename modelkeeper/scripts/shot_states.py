"""生成 `shot_states.png`：把测速五种状态放进同一张图，供人工确认配色。

不进测试集——它只回答「五种状态一眼能不能分得开」。改过 `SPEED_STATES`
的颜色后跑一次即可：

    python scripts/shot_states.py
"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))  # 复用 test_model_probe 里的假 HTTP 服务


import ctypes
import json
import os
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

from modelkeeper import gui_app as ga
from modelkeeper import model_probe as mp
from test_model_probe import H


def bring_to_front(win, title):
    """把窗口强行提到前台。

    Windows 会拦截后台进程的 SetForegroundWindow（前台锁），单靠 topmost 只能
    改层级、抢不到焦点，于是 ImageGrab 抓到的是别的程序。AttachThreadInput 把
    本线程挂到当前前台线程上，才允许抢焦点。
    """
    win.update()
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
    u.ShowWindow(hwnd, 9)                       # SW_RESTORE
    u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)   # HWND_TOPMOST, NOSIZE|NOMOVE
    fg = u.GetForegroundWindow()
    tid_fg = u.GetWindowThreadProcessId(fg, None)
    tid_me = k.GetCurrentThreadId()
    u.AttachThreadInput(tid_me, tid_fg, True)
    u.SetForegroundWindow(hwnd)
    u.BringWindowToTop(hwnd)
    u.AttachThreadInput(tid_me, tid_fg, False)
    win.lift()
    win.focus_force()
    win.update()
    time.sleep(1.0)
    win.update()


TEST_PORT = int(os.environ.get("SHOT_PORT", "8767"))
HOST = f"127.0.0.1:{TEST_PORT}"


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", TEST_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="shots-"))
    ga.CONFIG_PATH = tmp / "config.json"
    os.environ["MODEL_PROBE_BACKUP_DIR"] = str(tmp / "backups")

    from modelkeeper import agent_import as ai
    ai.WORKBUDDY_MODELS = tmp / "wb.json"
    ai.DSH_PATCH = tmp / "dsh.yml"
    ai.DSH_CREDENTIALS = tmp / "cred.yml"

    app = ga.App()
    app.update()

    app.var_base.set("http://" + HOST + "/openai")
    app.var_key.set("sk-test")
    app.update()
    app._start()
    end = time.time() + 15
    while time.time() < end and (app._busy or not app.tree.get_children()):
        app.update()
        time.sleep(0.02)

    ids = app.tree.get_children()
    print("rows:", ids)

    # 五种状态各挑一个，摆在同一张图里
    plan = {
        ids[0]: ("pending", None),
        ids[1]: ("running", None),
        ids[2]: ("done", mp.SpeedResult(model=ids[2], ttft_ms=35, tps=263.9, rounds=3)),
        ids[3]: ("slow", mp.SpeedResult(model=ids[3], too_slow=True)),
        ids[4]: ("error", mp.SpeedResult(model=ids[4], error="401 凭据被拒")),
    }
    app._speed = {k: v for k, (s, v) in plan.items() if v is not None}
    app._speed_state = {k: s for k, (s, _v) in plan.items()}
    app._fill()
    app._update_speed_progress()
    app._status("测速中：演示五种状态配色", ga.ACCENT)
    app.update()

    bring_to_front(app, mp.APP_TITLE)

    from PIL import ImageGrab

    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    img = ImageGrab.grab(bbox=(x - 8, y - 32, x + w + 8, y + h + 8))
    out = Path(__file__).resolve().parents[1] / "docs" / "images" / "shot_states.png"
    img.save(out)
    print(f"saved {out}", img.size)
    print("config:", json.dumps({"protocol": app._protocol()}))

    app.destroy()
    srv.shutdown()


if __name__ == "__main__":
    main()
