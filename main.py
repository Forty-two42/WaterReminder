# -*- coding: utf-8 -*-
"""
喝水提醒小助手 (Water Reminder)

功能:
  1. 定时提醒, 间隔 1-240 分钟可调 (默认 45 分钟), 右下角置顶弹窗
  2. 弹窗内 "已喝水" 重置计时 / "5 分钟后再提醒"
  3. 系统托盘常驻, 右键菜单 (立即提醒 / 设置 / 开机自启开关 / 退出), 悬停显示剩余时间
  4. 开机自启: 写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, 无需管理员权限

技术栈: Python + tkinter + pystray + Pillow, PyInstaller 打包为单文件 exe
"""

import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import traceback
import winreg

import tkinter as tk
from tkinter import font as tkfont

try:
    import winsound
except ImportError:  # 非 Windows 平台
    winsound = None

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem as Item

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
APP_NAME = "喝水提醒小助手"
APP_ID = "WaterReminder"
MUTEX_NAME = "Local\\WaterReminder_SingleInstance"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_INTERVAL = 45
MIN_INTERVAL = 1
MAX_INTERVAL = 240
SNOOZE_MINUTES = 5

CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_ID)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# 浅色配色
C_WIN_BG = "#FFFFFF"
C_PANEL = "#F5F9FF"
C_TITLE = "#17324D"
C_SUB = "#6B87A3"
C_PRIMARY = "#2E8BF0"
C_PRIMARY_HOVER = "#1B76D8"
C_PRIMARY_ACTIVE = "#1668C4"
C_GHOST = "#EAF2FD"
C_GHOST_HOVER = "#DCEAFC"
C_LINE = "#DCE7F5"
C_ICON_LIGHT = (79, 195, 247)
C_ICON_DARK = (18, 92, 178)

FONT_FAMILY = "Microsoft YaHei UI"


# --------------------------------------------------------------------------
# Win32 辅助
# --------------------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _setup_ctypes():
    """64 位下句柄是 64 位, 必须修正 restype/argtypes, 否则句柄被截断。"""
    try:
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = [ctypes.c_void_p]
        g.CreateRoundRectRgn.restype = ctypes.c_void_p
        g.CreateRoundRectRgn.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int,
        ]
        u.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
    except Exception:
        pass


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_work_area():
    """主显示器工作区 (已排除任务栏)"""
    r = RECT()
    try:
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(r), 0
        ):
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def apply_round_corners(win, radius_px):
    """给无边框窗口切圆角 (SetWindowRgn)"""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            hwnd = win.winfo_id()
        w = win.winfo_width()
        h = win.winfo_height()
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, w + 1, h + 1, radius_px * 2, radius_px * 2
        )
        if rgn:
            ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, True)
    except Exception:
        pass


def single_instance():
    """返回 mutex 句柄; 若已有实例在跑返回 None"""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return None
        return handle
    except Exception:
        return True


# --------------------------------------------------------------------------
# 开机自启 (HKCU Run, 无需管理员)
# --------------------------------------------------------------------------
def autostart_command():
    """当前程序的自启命令行"""
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_ID)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled):
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, APP_ID, 0, winreg.REG_SZ, autostart_command()
                )
            else:
                try:
                    winreg.DeleteValue(key, APP_ID)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def load_config():
    default = {"interval": DEFAULT_INTERVAL}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            default.update(data)
    except Exception:
        pass
    interval = default.get("interval", DEFAULT_INTERVAL)
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL
    default["interval"] = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))
    return default


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


# --------------------------------------------------------------------------
# 图标绘制
# --------------------------------------------------------------------------
def _bezier(p0, p1, p2, p3, segments=40):
    pts = []
    for i in range(segments + 1):
        t = i / segments
        mt = 1.0 - t
        x = (mt ** 3) * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + (t ** 3) * p3[0]
        y = (mt ** 3) * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + (t ** 3) * p3[1]
        pts.append((x, y))
    return pts


def make_icon_image(size=512):
    """蓝色圆角渐变底 + 白色水滴"""
    S = 1024
    cx = 0.5 * S
    cy = 0.668 * S
    r = 0.243 * S
    tip = (cx, 0.108 * S)
    right = (cx + r, cy)
    left = (cx - r, cy)

    # 渐变底
    grad = Image.new("RGB", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line(
            [(0, y), (S, y)],
            fill=tuple(
                int(C_ICON_LIGHT[i] + (C_ICON_DARK[i] - C_ICON_LIGHT[i]) * t)
                for i in range(3)
            ),
        )

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(0.22 * S), fill=255
    )

    img = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    img.paste(grad, (0, 0), mask)

    # 水滴轮廓: 尖点 -> 右切点 -> 下半圆弧 -> 左切点 -> 回到尖点
    pts = [tip]
    pts += _bezier(tip, (cx + 0.042 * S, 0.330 * S), (cx + 0.232 * S, 0.505 * S), right, 40)
    for i in range(0, 61):
        a = math.radians(-180.0 * i / 60.0)
        pts.append((cx + r * math.cos(a), cy - r * math.sin(a)))
    pts += _bezier(left, (cx - 0.232 * S, 0.505 * S), (cx - 0.042 * S, 0.330 * S), tip, 40)

    ImageDraw.Draw(img).polygon(pts, fill=(255, 255, 255, 255))

    # 高光
    overlay = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    ImageDraw.Draw(overlay).ellipse(
        [cx - 0.16 * S, cy - 0.10 * S, cx - 0.035 * S, cy + 0.06 * S],
        fill=(255, 255, 255, 120),
    )
    img = Image.alpha_composite(img, overlay)
    img = img.resize((size, size), Image.LANCZOS)
    return img


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------
class WaterReminder:
    def __init__(self):
        self.cfg = load_config()
        self.interval = int(self.cfg["interval"])
        self.remaining = self.interval * 60
        self.cycle_start = time.time()

        self.popup = None
        self.settings = None
        self.icon = None
        self._queue = queue.Queue()
        self._last_title = ""

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)

        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except Exception:
            dpi = 96
        self.scale = dpi / 96.0
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        self.f_title = tkfont.Font(family=FONT_FAMILY, size=14, weight="bold")
        self.f_body = tkfont.Font(family=FONT_FAMILY, size=10)
        self.f_small = tkfont.Font(family=FONT_FAMILY, size=9)
        self.f_btn = tkfont.Font(family=FONT_FAMILY, size=10, weight="bold")
        self.f_big = tkfont.Font(family=FONT_FAMILY, size=22, weight="bold")

        self._build_tray()
        self.root.after(1000, self._tick)
        self.root.after(80, self._poll_queue)

        # 首次运行: 用托盘气泡告诉用户程序在哪
        if not self.cfg.get("welcomed"):
            self.cfg["welcomed"] = True
            save_config(self.cfg)
            self.root.after(2500, self._welcome_tip)

    # -------------------------------- 工具 --------------------------------
    def px(self, value):
        return int(round(value * self.scale))

    def post(self, fn, *args):
        """从托盘线程把任务丢回主线程执行"""
        self._queue.put(lambda: fn(*args))

    def _poll_queue(self):
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
        try:
            self.root.after(80, self._poll_queue)
        except tk.TclError:
            pass

    # -------------------------------- 托盘 --------------------------------
    def _build_tray(self):
        menu = Menu(
            Item(lambda item: "剩余 %s" % fmt_time(self.remaining), None, enabled=False),
            Menu.SEPARATOR,
            Item("立即提醒", lambda icon, item: self.post(self.trigger_now)),
            Menu.SEPARATOR,
            Item("设置…", lambda icon, item: self.post(self.open_settings)),
            Item(
                "开机自启",
                lambda icon, item: self.post(self.toggle_autostart),
                checked=lambda item: is_autostart_enabled(),
            ),
            Menu.SEPARATOR,
            Item("退出", lambda icon, item: self.post(self.quit_app)),
        )
        self.icon = pystray.Icon(APP_ID, make_icon_image(64), APP_NAME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _welcome_tip(self):
        try:
            if self.icon is not None:
                self.icon.notify(
                    "已在后台运行，%d 分钟后第一次提醒你喝水。\n右键托盘图标可设置间隔或退出。"
                    % self.interval,
                    APP_NAME,
                )
        except Exception:
            pass

    def _refresh_tray(self):
        if self.icon is None:
            return
        text = "%s · 剩余 %s" % (APP_NAME, fmt_time(self.remaining))
        if text != self._last_title:
            self._last_title = text
            try:
                self.icon.title = text
            except Exception:
                pass

    # ------------------------------- 计时 --------------------------------
    def _tick(self):
        if self.popup is None and self.remaining > 0:
            self.remaining -= 1
        if self.popup is None and self.remaining <= 0:
            self._show_popup()
        self._refresh_tray()
        self._refresh_settings_hint()
        try:
            self.root.after(1000, self._tick)
        except tk.TclError:
            pass

    # ------------------------------- 弹窗 --------------------------------
    def _show_popup(self):
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.deiconify()
            self.popup.lift()
            self.popup.attributes("-topmost", True)
            return

        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self.popup = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=C_WIN_BG)

        pad = self.px(20)
        wrap = tk.Frame(win, bg=C_WIN_BG)
        wrap.pack(fill="both", expand=True, padx=pad, pady=pad)

        # --- 标题行 ---
        head = tk.Frame(wrap, bg=C_WIN_BG)
        head.pack(fill="x")

        drop = tk.Canvas(head, width=self.px(30), height=self.px(34),
                         bg=C_WIN_BG, highlightthickness=0)
        drop.pack(side="left")
        self._draw_drop(drop, self.px(30), self.px(34))

        title_box = tk.Frame(head, bg=C_WIN_BG)
        title_box.pack(side="left", padx=(self.px(10), 0))

        tk.Label(title_box, text="该喝水啦！", font=self.f_title,
                 fg=C_TITLE, bg=C_WIN_BG).pack(anchor="w")

        close = tk.Label(head, text="✕", font=self.f_body, fg="#9FB3C8",
                         bg=C_WIN_BG, cursor="hand2")
        close.pack(side="right", anchor="n")
        close.bind("<Button-1>", lambda e: self._on_snooze())
        close.bind("<Enter>", lambda e: close.configure(fg="#5B7A99"))
        close.bind("<Leave>", lambda e: close.configure(fg="#9FB3C8"))

        # --- 副标题 (按真实经过时长, 而非设定间隔) ---
        elapsed_min = max(1, int(round((time.time() - self.cycle_start) / 60.0)))
        subtitle = "已经专注了 %d 分钟，起来接杯水吧" % elapsed_min
        tk.Label(wrap, text=subtitle, font=self.f_body, fg=C_SUB, bg=C_WIN_BG,
                 justify="left", wraplength=self.px(300)).pack(anchor="w",
                                                               pady=(self.px(10), 0))

        # --- 按钮行 (两个按钮等宽平分) ---
        row = tk.Frame(wrap, bg=C_WIN_BG)
        row.pack(fill="x", pady=(self.px(18), 0))

        btn_wrap = self._make_button(row, "已喝水", self._on_drank, primary=True)
        btn_wrap.pack(side="left", expand=True, fill="x")

        btn_snooze = self._make_button(row, "%d 分钟后再提醒" % SNOOZE_MINUTES,
                                       self._on_snooze, primary=False)
        btn_snooze.pack(side="left", expand=True, fill="x", padx=(self.px(10), 0))

        # --- 底部提示 ---
        tk.Label(wrap, text="点击 ✕ 将 %d 分钟后再提醒" % SNOOZE_MINUTES,
                 font=self.f_small, fg="#8CA6C0", bg=C_WIN_BG).pack(
            anchor="w", pady=(self.px(12), 0))

        # --- 拖动 ---
        for widget in (head, title_box, wrap):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)

        win.update_idletasks()
        w = max(win.winfo_reqwidth(), self.px(352))
        h = win.winfo_reqheight()
        left, top, right, bottom = get_work_area()
        margin = self.px(16)
        x = right - w - margin
        y = bottom - h - margin
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))

        apply_round_corners(win, self.px(12))

        # 淡入
        win.attributes("-alpha", 0.0)
        steps = 8
        for i in range(1, steps + 1):
            alpha = 0.97 * i / steps
            win.after(int(25 * i), lambda a=alpha: self._set_alpha(a))

        win.attributes("-topmost", True)
        win.lift()

    def _set_alpha(self, value):
        try:
            if self.popup is not None and self.popup.winfo_exists():
                self.popup.attributes("-alpha", value)
        except Exception:
            pass

    def _draw_drop(self, canvas, w, h):
        """在 Canvas 上画一个小水滴"""
        cx = w / 2.0
        cy = h * 0.63
        r = w * 0.30
        tip_y = h * 0.10
        pts = [cx, tip_y]
        for (bx, by) in _bezier((cx, tip_y), (cx + w * 0.08, h * 0.34),
                                (cx + r - w * 0.02, h * 0.50), (cx + r, cy), 20):
            pts += [bx, by]
        for i in range(0, 25):
            a = math.radians(-180.0 * i / 24.0)
            pts += [cx + r * math.cos(a), cy - r * math.sin(a)]
        for (bx, by) in _bezier((cx - r, cy), (cx - r + w * 0.02, h * 0.50),
                                (cx - w * 0.08, h * 0.34), (cx, tip_y), 20):
            pts += [bx, by]
        canvas.create_polygon(pts, fill=C_PRIMARY, outline="")

    def _make_button(self, parent, text, command, primary=True):
        if primary:
            bg, hover, active, fg = C_PRIMARY, C_PRIMARY_HOVER, C_PRIMARY_ACTIVE, "#FFFFFF"
        else:
            bg, hover, active, fg = C_GHOST, C_GHOST_HOVER, "#CFE2F8", C_PRIMARY

        holder = tk.Frame(parent, bg=bg, cursor="hand2")
        label = tk.Label(holder, text=text, font=self.f_btn, fg=fg, bg=bg,
                         padx=self.px(18), pady=self.px(9), cursor="hand2")
        label.pack()

        def set_bg(color):
            holder.configure(bg=color)
            label.configure(bg=color)

        label.bind("<Enter>", lambda e: set_bg(hover))
        label.bind("<Leave>", lambda e: set_bg(bg))
        label.bind("<Button-1>", lambda e: set_bg(active))
        label.bind("<ButtonRelease-1>", lambda e: (set_bg(hover), command()))
        return holder

    def _start_drag(self, event):
        if self.popup is None:
            return
        self._drag_x = event.x_root - self.popup.winfo_x()
        self._drag_y = event.y_root - self.popup.winfo_y()

    def _on_drag(self, event):
        if self.popup is None:
            return
        self.popup.geometry("+%d+%d" % (event.x_root - self._drag_x,
                                        event.y_root - self._drag_y))

    def _close_popup(self):
        win = self.popup
        self.popup = None
        if win is not None:
            try:
                if win.winfo_exists():
                    win.destroy()
            except Exception:
                pass

    def _on_drank(self):
        self.remaining = self.interval * 60
        self.cycle_start = time.time()
        self._close_popup()
        self._refresh_tray()
        self._flash_tray("好样的！下一次提醒 %d 分钟后" % self.interval)

    def _on_snooze(self):
        self.remaining = SNOOZE_MINUTES * 60
        self._close_popup()
        self._refresh_tray()

    def _flash_tray(self, text):
        try:
            if self.icon is not None:
                self.icon.title = text
                self._last_title = text
                self.root.after(4000, self._refresh_tray)
        except Exception:
            pass

    # ------------------------------- 动作 --------------------------------
    def trigger_now(self):
        self.remaining = 0
        self._show_popup()

    def toggle_autostart(self):
        set_autostart(not is_autostart_enabled())
        if self.settings is not None and self.settings.winfo_exists():
            self._sync_autostart_var()

    def quit_app(self):
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        try:
            self._close_popup()
            if self.settings is not None:
                self.settings.destroy()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------- 设置窗 ------------------------------
    def open_settings(self):
        if self.settings is not None and self.settings.winfo_exists():
            self.settings.deiconify()
            self.settings.lift()
            self.settings.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.settings = win
        win.title("设置")
        win.configure(bg=C_WIN_BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)

        pad = self.px(22)
        wrap = tk.Frame(win, bg=C_WIN_BG)
        wrap.pack(fill="both", expand=True, padx=pad, pady=pad)

        tk.Label(wrap, text="提醒设置", font=self.f_title, fg=C_TITLE,
                 bg=C_WIN_BG).pack(anchor="w")
        tk.Label(wrap, text="调整提醒间隔，修改后计时立即重新开始",
                 font=self.f_small, fg=C_SUB, bg=C_WIN_BG).pack(
            anchor="w", pady=(self.px(4), self.px(18)))

        # 间隔输入
        row = tk.Frame(wrap, bg=C_WIN_BG)
        row.pack(fill="x")
        tk.Label(row, text="提醒间隔", font=self.f_body, fg=C_TITLE,
                 bg=C_WIN_BG).pack(side="left")

        self.var_interval = tk.StringVar(value=str(self.interval))
        spin = tk.Spinbox(row, from_=MIN_INTERVAL, to=MAX_INTERVAL,
                          textvariable=self.var_interval, width=6,
                          font=self.f_body, justify="center",
                          relief="solid", bd=1, bg="#FFFFFF", fg=C_TITLE,
                          buttonbackground=C_GHOST, highlightthickness=0)
        spin.pack(side="right")
        tk.Label(row, text="分钟", font=self.f_body, fg=C_TITLE,
                 bg=C_WIN_BG).pack(side="right", padx=(0, self.px(8)))

        # 快捷值
        quick = tk.Frame(wrap, bg=C_WIN_BG)
        quick.pack(fill="x", pady=(self.px(12), 0))
        tk.Label(quick, text="快捷", font=self.f_small, fg=C_SUB,
                 bg=C_WIN_BG).pack(side="left", padx=(0, self.px(8)))
        for value in (15, 30, 45, 60, 90, 120):
            self._make_chip(quick, value)

        # 开机自启
        self.var_autostart = tk.BooleanVar(value=is_autostart_enabled())
        chk = tk.Checkbutton(
            wrap, text="开机自动启动", variable=self.var_autostart,
            font=self.f_body, fg=C_TITLE, bg=C_WIN_BG, activebackground=C_WIN_BG,
            selectcolor="#FFFFFF", anchor="w", cursor="hand2",
            command=self._on_autostart_check,
        )
        chk.pack(anchor="w", pady=(self.px(16), 0))

        # 下次提醒
        self.lbl_next = tk.Label(wrap, text="", font=self.f_small, fg=C_SUB,
                                 bg=C_WIN_BG)
        self.lbl_next.pack(anchor="w", pady=(self.px(6), 0))
        self._refresh_settings_hint()

        tk.Frame(wrap, bg=C_LINE, height=1).pack(fill="x", pady=(self.px(16), 0))

        # 按钮
        bar = tk.Frame(wrap, bg=C_WIN_BG)
        bar.pack(fill="x", pady=(self.px(16), 0))
        self._make_button(bar, "取消", self._close_settings, primary=False).pack(
            side="right", padx=(self.px(10), 0))
        self._make_button(bar, "保存", self._save_settings, primary=True).pack(
            side="right")

        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        win.geometry("%dx%d" % (w, h))
        win.attributes("-alpha", 1.0)
        self._center_on_screen(win)
        win.lift()
        win.focus_force()
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

    def _make_chip(self, parent, value):
        chip = tk.Label(parent, text=str(value), font=self.f_small, fg=C_PRIMARY,
                        bg=C_GHOST, padx=self.px(9), pady=self.px(4), cursor="hand2")
        chip.pack(side="left", padx=(0, self.px(6)))
        chip.bind("<Enter>", lambda e: chip.configure(bg=C_GHOST_HOVER))
        chip.bind("<Leave>", lambda e: chip.configure(bg=C_GHOST))
        chip.bind("<Button-1>", lambda e: self.var_interval.set(str(value)))

    def _center_on_screen(self, win):
        left, top, right, bottom = get_work_area()
        w = win.winfo_width()
        h = win.winfo_height()
        x = left + (right - left - w) // 2
        y = top + (bottom - top - h) // 2
        win.geometry("+%d+%d" % (x, y))

    def _sync_autostart_var(self):
        try:
            self.var_autostart.set(is_autostart_enabled())
        except Exception:
            pass

    def _on_autostart_check(self):
        set_autostart(bool(self.var_autostart.get()))
        self._sync_autostart_var()

    def _refresh_settings_hint(self):
        if self.settings is None or not self.settings.winfo_exists():
            return
        try:
            self.lbl_next.configure(
                text="下次提醒：%s 后" % fmt_time(self.remaining))
        except Exception:
            pass

    def _save_settings(self):
        raw = self.var_interval.get().strip()
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = DEFAULT_INTERVAL
        value = max(MIN_INTERVAL, min(MAX_INTERVAL, value))
        self.interval = value
        self.cfg["interval"] = value
        save_config(self.cfg)

        set_autostart(bool(self.var_autostart.get()))

        self._close_popup()
        self.remaining = value * 60
        self.cycle_start = time.time()
        self._refresh_tray()
        self._close_settings()

    def _close_settings(self):
        if self.settings is not None:
            try:
                self.settings.destroy()
            except Exception:
                pass
        self.settings = None

    # ------------------------------- 运行 --------------------------------
    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit_app()


def _log_crash():
    """把异常堆栈写到 %APPDATA%\\WaterReminder\\error.log, 便于排查"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(os.path.join(CONFIG_DIR, "error.log"), "a", encoding="utf-8") as fp:
            fp.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exc(file=fp)
    except Exception:
        pass


def main():
    try:
        enable_dpi_awareness()
        mutex = single_instance()
        if mutex is None:
            # 已有实例在运行, 静默退出
            return
        _setup_ctypes()
        app = WaterReminder()
        app.run()
    except Exception:
        _log_crash()
        raise


if __name__ == "__main__":
    main()
