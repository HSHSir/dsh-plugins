"""生成 `shot_missing.png`：演示「两个 Agent 都没装」时的界面与提示。

只有在需要核对置灰样式与提示文案时才跑；跑完图片留着、脚本可删。
"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))  # 复用 test_model_probe 里的假 HTTP 服务


import ctypes
import os
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

from modelkeeper import agent_import as ai
from modelkeeper import gui_app as ga
from test_model_probe import H


def bring_to_front(win):
    win.update()
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
    u.ShowWindow(hwnd, 9)
    u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
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


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 8768), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="missing-"))
    # 指向不存在的路径：模拟这台机器上没装这两个 Agent
    ai.WORKBUDDY_MODELS = tmp / "nope" / "models.json"
    ai.DSH_PATCH = tmp / "nope" / "cordis.patch.yml"
    ai.DSH_CREDENTIALS = tmp / "nope" / ".credentials.yaml"
    ga.CONFIG_PATH = tmp / "config.json"
    os.environ["MODEL_PROBE_BACKUP_DIR"] = str(tmp / "backups")

    app = ga.App()
    app.update()
    app.tabbar.select(2)          # DeepSeekHarness 管理页最能看出置灰效果
    app.update()
    bring_to_front(app)

    from PIL import ImageGrab

    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    out = Path(__file__).resolve().parents[1] / "docs" / "images" / "shot_missing.png"
    ImageGrab.grab(bbox=(x - 8, y - 32, x + w + 8, y + h + 8)).save(out)
    print(f"saved {out}")
    print("hint:", app.var_dsh_hint.get())

    app.destroy()
    srv.shutdown()


if __name__ == "__main__":
    main()
