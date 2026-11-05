# -*- coding: utf-8 -*-
"""
喝水提醒小助手 (Water Reminder)

功能:
  1. 定时提醒, 间隔 1-240 分钟可调 (默认 45 分钟), 右下角置顶弹窗
  2. 弹窗内 "已喝水" 重置计时 / "5 分钟后再提醒"
  3. 系统托盘常驻, 右键菜单 (立即提醒 / 免打扰 / 设置 / 开机自启开关 / 退出), 悬停显示剩余时间
  4. 免打扰: 可配置时段 (支持跨午夜) / 一键暂停 1 或 4 小时 / 全屏应用时延后
  5. 开机自启: 写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, 无需管理员权限

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
from datetime import datetime

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
DND_PAUSE_PRESETS = (60, 240)  # 托盘一键暂停的分钟数

CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_ID)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "error.log")

# 配置白名单: 只认这些键, 避免外部文件往 cfg 里塞任意字段
CONFIG_DEFAULTS = {
    "interval": DEFAULT_INTERVAL,
    "welcomed": False,
    "dnd_enabled": False,
    "dnd_start": "22:00",
    "dnd_end": "08:00",
    "dnd_fullscreen": True,
    "pause_until": 0.0,
}

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
# 日志: 启动期和运行期的异常统一落盘, 不再静默吞掉
# --------------------------------------------------------------------------
def _log_exception(exc_type=None, exc_value=None, exc_tb=None, tag="crash"):
    try:
        if exc_type is None:
            exc_type, exc_value, exc_tb = sys.exc_info()
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fp:
            fp.write("\n=== %s %s ===\n" % (tag, time.strftime("%Y-%m-%d %H:%M:%S")))
            traceback.print_exception(exc_type, exc_value, exc_tb, file=fp)
    except Exception:
        pass


def _log_crash():
    _log_exception(tag="startup")


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
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
        g.CreateRoundRectRgn.restype = ctypes.c_void_p
        g.CreateRoundRectRgn.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int,
        ]
        u.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
    except Exception:
        _log_exception(tag="setup-ctypes")


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


# 单实例句柄必须活在模块级: 局部引用一旦被回收, 内核对象可能被关闭
_MUTEX_HANDLE = None


def single_instance():
    """返回 mutex 句柄; 若已有实例在跑返回 None。

    用 use_last_error=True + get_last_error(), 避免 GetLastError() 被 Python 层
    其它 Win32 调用覆盖导致偶发放出第二个实例。
    """
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p,
        ]
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            # 创建失败时宁可放行, 也不要让用户双击后毫无反应的进程消失
            _log_exception(tag="single-instance-mutex-failed")
            return True
        ERROR_ALREADY_EXISTS = 183
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return None
        _MUTEX_HANDLE = handle
        return handle
    except Exception:
        _log_exception(tag="single-instance")
        return True


def is_foreground_fullscreen():
    """前台窗口是否铺满主屏 (粗略判定全屏应用 / 演示 / 游戏)。"""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        rect = RECT()
        if not u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return False
        screen_w = u.GetSystemMetrics(0)
        screen_h = u.GetSystemMetrics(1)
        if screen_w <= 0 or screen_h <= 0:
            return False
        if not (rect.left <= 0 and rect.top <= 0
                and rect.right >= screen_w and rect.bottom >= screen_h):
            return False
        # 桌面 / 任务栏等 shell 窗口本来就是全屏, 不算
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
        if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return False
        return True
    except Exception:
        return False


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
def normalize_hhmm(value, fallback):
    """把 '9:5' / '09:05' 规范成 'HH:MM'; 非法输入回退到 fallback。"""
    try:
        parts = str(value).strip().split(":")
        if len(parts) == 2:
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return "%02d:%02d" % (h, m)
    except (TypeError, ValueError):
        pass
    return fallback


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            for key in CONFIG_DEFAULTS:
                if key in data:
                    cfg[key] = data[key]
    except FileNotFoundError:
        pass
    except Exception:
        _log_exception(tag="load-config")

    try:
        interval = int(cfg["interval"])
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL
    cfg["interval"] = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))

    cfg["welcomed"] = bool(cfg["welcomed"])
    cfg["dnd_enabled"] = bool(cfg["dnd_enabled"])
    cfg["dnd_fullscreen"] = bool(cfg["dnd_fullscreen"])
    cfg["dnd_start"] = normalize_hhmm(cfg["dnd_start"], CONFIG_DEFAULTS["dnd_start"])
    cfg["dnd_end"] = normalize_hhmm(cfg["dnd_end"], CONFIG_DEFAULTS["dnd_end"])
    try:
        pause_until = float(cfg["pause_until"])
    except (TypeError, ValueError):
        pause_until = 0.0
    cfg["pause_until"] = pause_until if pause_until > 0 else 0.0
    return cfg


def save_config(cfg):
    """原子写: 先落 .tmp 再 replace, 避免写到一半留下半截 JSON。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        _log_exception(tag="save-config")
        return False


# --------------------------------------------------------------------------
# 免打扰时段判定 (纯函数, 便于单测)
# --------------------------------------------------------------------------
def hhmm_to_minutes(text):
    parts = str(text).split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def in_dnd_window(now, start, end):
    """now 为 datetime; 时段支持跨午夜 (如 22:00-08:00)。起止相同视为未启用。"""
    s, e = hhmm_to_minutes(start), hhmm_to_minutes(end)
    if s is None or e is None or s == e:
        return False
    cur = now.hour * 60 + now.minute
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


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

        # 计时以「下次触发的绝对时刻」为准, remaining 只是派生显示值。
        # 用墙钟 time.time(): Windows 睡眠期间 RTC 继续走, 唤醒后能立刻算出已超时。
        self.cycle_start = time.time()
        self.next_fire_at = self.cycle_start + self.interval * 60
        self.remaining = self.interval * 60
        self._dnd_reason = ""
        self._dnd_active_prev = False

        self.popup = None
        self.settings = None
        self.icon = None
        self._queue = queue.Queue()
        self._last_title = ""
        self._flash_until = 0.0

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        # Tk 回调里的异常默认打到 stderr; --windowed 下 stderr 不可见, 必须接管
        self.root.report_callback_exception = self._on_tk_exception

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

    def _on_tk_exception(self, exc, val, tb):
        _log_exception(exc, val, tb, tag="tk-callback")

    def _poll_queue(self):
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                _log_exception(tag="tray-callback")
        try:
            self.root.after(80, self._poll_queue)
        except tk.TclError:
            pass

    def _arm(self, seconds):
        """把下次触发时刻设为「从现在起 seconds 秒后」。"""
        self.next_fire_at = time.time() + seconds
        self.remaining = max(0, int(round(seconds)))

    # -------------------------------- 托盘 --------------------------------
    def _build_tray(self):
        def pause_action(minutes):
            # pystray 会校验 action 签名必须恰好是 (icon, item), 不能用默认参数绑值
            return lambda icon, item: self.post(self.pause_reminders, minutes)

        pause_items = [
            Item("暂停 %d 小时" % (minutes // 60), pause_action(minutes))
            for minutes in DND_PAUSE_PRESETS
        ]
        dnd_menu = Menu(
            *pause_items,
            Item(
                "恢复提醒",
                lambda icon, item: self.post(self.resume_reminders),
                enabled=lambda item: float(self.cfg.get("pause_until") or 0) > time.time(),
            ),
            Menu.SEPARATOR,
            Item(
                "启用免打扰时段",
                lambda icon, item: self.post(self.toggle_dnd_window),
                checked=lambda item: bool(self.cfg.get("dnd_enabled")),
            ),
            Item(
                "全屏应用时不打扰",
                lambda icon, item: self.post(self.toggle_dnd_fullscreen),
                checked=lambda item: bool(self.cfg.get("dnd_fullscreen")),
            ),
        )

        menu = Menu(
            Item(
                lambda item: self._dnd_reason or ("剩余 %s" % fmt_time(self.remaining)),
                None,
                enabled=False,
            ),
            Menu.SEPARATOR,
            Item("立即提醒", lambda icon, item: self.post(self.trigger_now)),
            Item("免打扰", dnd_menu),
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
            _log_exception(tag="welcome-tip")

    def _refresh_tray(self):
        if self.icon is None or time.time() < self._flash_until:
            return
        if self._dnd_reason:
            text = "%s · %s" % (APP_NAME, self._dnd_reason)
        else:
            text = "%s · 剩余 %s" % (APP_NAME, fmt_time(self.remaining))
        if text != self._last_title:
            self._last_title = text
            try:
                self.icon.title = text
            except Exception:
                _log_exception(tag="refresh-tray")

    # ------------------------------- 免打扰 ------------------------------
    def _dnd_state(self, now=None):
        """返回 (是否处于免打扰, 给用户看的原因)。"""
        now = time.time() if now is None else now

        pause_until = float(self.cfg.get("pause_until") or 0)
        if pause_until > now:
            left = max(1, int(math.ceil((pause_until - now) / 60.0)))
            return True, "已暂停提醒（还剩 %d 分钟）" % left
        if pause_until:
            # 一次性清掉过期值, 免得每次读配置都带个旧时间戳
            self.cfg["pause_until"] = 0.0
            save_config(self.cfg)

        if self.cfg.get("dnd_enabled"):
            if in_dnd_window(
                datetime.fromtimestamp(now),
                self.cfg["dnd_start"],
                self.cfg["dnd_end"],
            ):
                return True, "免打扰时段 %s–%s" % (
                    self.cfg["dnd_start"], self.cfg["dnd_end"]
                )

        if self.cfg.get("dnd_fullscreen") and is_foreground_fullscreen():
            return True, "全屏应用中，暂不打扰"

        return False, ""

    def pause_reminders(self, minutes):
        self.cfg["pause_until"] = time.time() + minutes * 60
        save_config(self.cfg)
        self._close_popup()
        self._dnd_reason = "已暂停提醒"
        self._refresh_tray()
        self._flash_tray("已暂停提醒 %d 分钟，右键托盘可恢复" % minutes)

    def resume_reminders(self):
        self.cfg["pause_until"] = 0.0
        save_config(self.cfg)
        self._arm(self.interval * 60)
        self.cycle_start = time.time()
        self._dnd_active_prev = False
        self._dnd_reason = ""
        self._refresh_settings_hint()
        self._flash_tray("已恢复提醒，下一次 %d 分钟后" % self.interval)

    def toggle_dnd_window(self):
        self.cfg["dnd_enabled"] = not bool(self.cfg.get("dnd_enabled"))
        save_config(self.cfg)
        self._sync_dnd_vars()

    def toggle_dnd_fullscreen(self):
        self.cfg["dnd_fullscreen"] = not bool(self.cfg.get("dnd_fullscreen"))
        save_config(self.cfg)
        self._sync_dnd_vars()

    def _sync_dnd_vars(self):
        if self.settings is None:
            return
        try:
            if not self.settings.winfo_exists():
                return
        except Exception:
            return
        try:
            self.var_dnd_enabled.set(bool(self.cfg.get("dnd_enabled")))
            self.var_dnd_fullscreen.set(bool(self.cfg.get("dnd_fullscreen")))
        except Exception:
            pass

    # ------------------------------- 计时 --------------------------------
    def _tick(self):
        try:
            now = time.time()
            dnd, reason = self._dnd_state(now)
            self._dnd_reason = reason

            if dnd:
                # 免打扰期间计时暂停, 剩余时间冻结在当前值
                pass
            else:
                if self._dnd_active_prev:
                    # 刚从免打扰恢复: 给一个完整间隔, 避免一解除就连环轰炸
                    self._arm(self.interval * 60)
                    self.cycle_start = now
                self.remaining = max(0, int(math.ceil(self.next_fire_at - now)))
                if self.popup is None and now >= self.next_fire_at:
                    self._show_popup()

            self._dnd_active_prev = dnd
            self._refresh_tray()
            self._refresh_settings_hint()
        except Exception:
            _log_exception(tag="tick")
        finally:
            # 无论上面发生什么, 都必须把下一拍挂回去, 否则计时器会永久停摆
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

        # --- 副标题 (按距上次喝水的真实经过时长, 而非设定间隔) ---
        elapsed_min = max(1, int(round((time.time() - self.cycle_start) / 60.0)))
        subtitle = "已经 %d 分钟没喝水啦，起来接杯水吧" % elapsed_min
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
        self._arm(self.interval * 60)
        self.cycle_start = time.time()
        self._close_popup()
        self._refresh_tray()
        self._flash_tray("好样的！下一次提醒 %d 分钟后" % self.interval)

    def _on_snooze(self):
        self._arm(SNOOZE_MINUTES * 60)
        self._close_popup()
        self._refresh_tray()

    def _flash_tray(self, text, seconds=4):
        try:
            if self.icon is not None:
                # 期间压住 _refresh_tray, 否则下一拍 tick 就会把提示盖掉
                self._flash_until = time.time() + seconds
                self.icon.title = text
                self._last_title = text
                self.root.after(int(seconds * 1000), self._refresh_tray)
        except Exception:
            _log_exception(tag="flash-tray")

    # ------------------------------- 动作 --------------------------------
    def trigger_now(self):
        self._arm(0)
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

        # --- 免打扰 ---
        tk.Frame(wrap, bg=C_LINE, height=1).pack(fill="x", pady=(self.px(16), 0))
        tk.Label(wrap, text="免打扰", font=self.f_title, fg=C_TITLE,
                 bg=C_WIN_BG).pack(anchor="w", pady=(self.px(14), 0))
        tk.Label(wrap, text="免打扰期间计时暂停，恢复后重新给一个完整间隔",
                 font=self.f_small, fg=C_SUB, bg=C_WIN_BG).pack(
            anchor="w", pady=(self.px(4), self.px(10)))

        self.var_dnd_enabled = tk.BooleanVar(value=bool(self.cfg["dnd_enabled"]))
        tk.Checkbutton(
            wrap, text="启用免打扰时段（不弹出提醒）", variable=self.var_dnd_enabled,
            font=self.f_body, fg=C_TITLE, bg=C_WIN_BG, activebackground=C_WIN_BG,
            selectcolor="#FFFFFF", anchor="w", cursor="hand2",
        ).pack(anchor="w")

        time_row = tk.Frame(wrap, bg=C_WIN_BG)
        time_row.pack(fill="x", pady=(self.px(8), 0))
        tk.Label(time_row, text="时段", font=self.f_body, fg=C_TITLE,
                 bg=C_WIN_BG).pack(side="left")

        self.var_dnd_start = tk.StringVar(value=self.cfg["dnd_start"])
        self.var_dnd_end = tk.StringVar(value=self.cfg["dnd_end"])
        entry_kw = dict(font=self.f_body, width=6, justify="center",
                        relief="solid", bd=1, bg="#FFFFFF", fg=C_TITLE,
                        highlightthickness=0)
        tk.Entry(time_row, textvariable=self.var_dnd_start, **entry_kw).pack(
            side="left", padx=(self.px(12), 0))
        tk.Label(time_row, text="至", font=self.f_body, fg=C_TITLE,
                 bg=C_WIN_BG).pack(side="left", padx=(self.px(6), self.px(6)))
        tk.Entry(time_row, textvariable=self.var_dnd_end, **entry_kw).pack(side="left")
        tk.Label(time_row, text="HH:MM，可跨午夜", font=self.f_small, fg=C_SUB,
                 bg=C_WIN_BG).pack(side="left", padx=(self.px(10), 0))

        self.var_dnd_fullscreen = tk.BooleanVar(value=bool(self.cfg["dnd_fullscreen"]))
        tk.Checkbutton(
            wrap, text="全屏应用 / 演示时延后提醒", variable=self.var_dnd_fullscreen,
            font=self.f_body, fg=C_TITLE, bg=C_WIN_BG, activebackground=C_WIN_BG,
            selectcolor="#FFFFFF", anchor="w", cursor="hand2",
        ).pack(anchor="w", pady=(self.px(8), 0))

        self.lbl_pause = tk.Label(wrap, text="", font=self.f_small, fg=C_SUB,
                                  bg=C_WIN_BG)
        self.lbl_pause.pack(anchor="w", pady=(self.px(8), 0))

        # 按钮
        tk.Frame(wrap, bg=C_LINE, height=1).pack(fill="x", pady=(self.px(16), 0))
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
        self._refresh_settings_hint()

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
        if self.settings is None:
            return
        try:
            if not self.settings.winfo_exists():
                return
            if self._dnd_reason:
                self.lbl_next.configure(text="当前：%s（恢复后重新计时）" % self._dnd_reason)
            else:
                self.lbl_next.configure(
                    text="下次提醒：%s 后" % fmt_time(self.remaining))
            pause_until = float(self.cfg.get("pause_until") or 0)
            if pause_until > time.time():
                self.lbl_pause.configure(
                    text="手动暂停中，保存设置不会解除暂停；请用托盘菜单恢复")
            else:
                self.lbl_pause.configure(text="托盘菜单「免打扰」可一键暂停 1 / 4 小时")
        except Exception:
            _log_exception(tag="refresh-settings-hint")

    def _save_settings(self):
        raw = self.var_interval.get().strip()
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = DEFAULT_INTERVAL
        value = max(MIN_INTERVAL, min(MAX_INTERVAL, value))
        self.interval = value
        self.cfg["interval"] = value

        self.cfg["dnd_enabled"] = bool(self.var_dnd_enabled.get())
        self.cfg["dnd_fullscreen"] = bool(self.var_dnd_fullscreen.get())
        self.cfg["dnd_start"] = normalize_hhmm(
            self.var_dnd_start.get(), self.cfg["dnd_start"])
        self.cfg["dnd_end"] = normalize_hhmm(
            self.var_dnd_end.get(), self.cfg["dnd_end"])
        save_config(self.cfg)

        set_autostart(bool(self.var_autostart.get()))

        self._close_popup()
        self._arm(value * 60)
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
