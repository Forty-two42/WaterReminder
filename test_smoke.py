# -*- coding: utf-8 -*-
"""冒烟测试: 构造应用 -> 弹窗 -> 截图 -> 稍后 -> 设置窗 -> 截图 -> 注册表开关 -> 计时触发"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PIL import ImageGrab

import main as M

OUT = os.path.join(os.environ.get("TEMP", "."), "wr_shots")
os.makedirs(OUT, exist_ok=True)


def note(msg):
    print("[test]", msg, flush=True)


M.enable_dpi_awareness()
M._setup_ctypes()

app = M.WaterReminder()
note("constructed | tray visible=%s | title=%r"
     % (app.icon is not None and app.icon.visible,
        app.icon.title if app.icon else "n/a"))


def shot(widget, name):
    try:
        widget.update_idletasks()
        x, y = widget.winfo_rootx(), widget.winfo_rooty()
        w, h = widget.winfo_width(), widget.winfo_height()
        img = ImageGrab.grab(bbox=(x - 12, y - 12, x + w + 12, y + h + 12))
        path = os.path.join(OUT, name)
        img.save(path)
        note("shot %s  win=%dx%d at (%d,%d)" % (name, w, h, x, y))
    except Exception as exc:
        note("shot FAILED %s: %r" % (name, exc))


def test_registry():
    before = M.is_autostart_enabled()
    M.set_autostart(True)
    on = M.is_autostart_enabled()
    M.set_autostart(False)
    off = M.is_autostart_enabled()
    note("registry autostart: before=%s after_enable=%s after_disable=%s"
         % (before, on, off))


def finish():
    note("=== DONE ===")
    app.quit_app()


# --- 时间线 ---
app.root.after(600, app._show_popup)
app.root.after(1500, lambda: shot(app.popup, "popup.png"))
app.root.after(1900, app._on_snooze)
app.root.after(2100, lambda: note("after snooze -> remaining=%ds (expect 300)"
                                  % app.remaining))
app.root.after(2400, app.open_settings)
app.root.after(3400, lambda: shot(app.settings, "settings.png"))
app.root.after(3900, test_registry)
app.root.after(4100, lambda: note("tray visible@4.1s = %s"
                                  % (app.icon.visible if app.icon else None)))


def start_timer_test():
    app._close_settings()
    app.remaining = 3
    note("timer forced to 3s")


app.root.after(4300, start_timer_test)
app.root.after(8800, lambda: note("after 4.5s -> popup_open=%s (expect True)"
                                  % (app.popup is not None)))
app.root.after(9200, lambda: shot(app.popup, "popup_auto.png") if app.popup else None)
app.root.after(9800, finish)

app.run()
