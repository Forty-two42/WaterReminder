# -*- coding: utf-8 -*-
"""为 README 生成干净的界面截图 (精确裁剪到窗口区域)"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PIL import ImageGrab

import main as M

OUT = os.path.join(HERE, "docs")
os.makedirs(OUT, exist_ok=True)


def log(msg):
    print("[shot]", msg, flush=True)


def grab(widget, name):
    widget.update_idletasks()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    path = os.path.join(OUT, name)
    img.save(path)
    log("%s  %dx%d" % (name, w, h))


M.enable_dpi_awareness()
M._setup_ctypes()

# 图标
M.make_icon_image(512).save(os.path.join(OUT, "icon.png"))
log("icon.png")

app = M.WaterReminder()
app.interval = 45
app.cycle_start = time.time() - 45 * 60   # 让弹窗显示"已经专注了 45 分钟"
app.remaining = 0


def show_popup():
    app._show_popup()
    app.popup.attributes("-alpha", 1.0)


def shot_popup():
    grab(app.popup, "screenshot-popup.png")
    app._on_snooze()


def open_settings():
    app.open_settings()
    app.settings.attributes("-topmost", False)


def shot_settings():
    grab(app.settings, "screenshot-settings.png")
    app._close_settings()


def done():
    app.quit_app()
    log("done")


app.root.after(700, show_popup)
app.root.after(1600, shot_popup)
app.root.after(2100, open_settings)
app.root.after(3000, shot_settings)
app.root.after(3500, done)

app.run()
